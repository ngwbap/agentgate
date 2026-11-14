"""Smart Lookup: from one sentence to a few trusted, ranked, deliverable options.

Pipeline (each step is shown to the user):
 1. understand  - Claude turns the request into a structured need (offline parser as fallback)
 2. shortlist   - search 17,546 products offline: names (EN/FI/SV), category coherence, claims,
                  allergens, unit; then Claude judges which candidates are really the requested product
                  (and, in the same call, which substitute ideas would serve the same purpose)
 3. live check  - ONE availability call + ONE price call for the shortlist only (Valio's rule).
                  Substitutes are checked live only when no confirmed exact plan exists.
 4. rank        - transparent rules: can it arrive in time and in full, then price per unit,
                  delivery reliability and CO2, with the buyer's priorities as weights
 5. plans       - good-better-best: up to three ways to fulfil the need, scored
                  0.55 x delivery certainty + 0.45 x match fidelity, price as tie-breaker
Every option carries its evidence: where each fact came from and how fresh it is.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from . import understand as und
from .catalog import ALLERGENS, CLAIM_NAMES, LABEL_NAMES, allergen_evidence, claim_proof, get_catalog
from .valio_api import ValioApiError, client as default_valio

SHORTLIST_SEARCH = 40      # offline candidates considered
SHORTLIST_JUDGE = 16       # sent to Claude for the relevance check
SUBSTITUTE_PER_IDEA = 4    # candidates per substitute idea sent to the same relevance check
LIVE_EXACT = 10            # exact matches checked live
LIVE_CLOSE = 3             # close variants checked live
LIVE_SUBSTITUTE = 4        # substitutes checked live, and only when needed
VALIO_SUBS_MAX = 6         # extra products added because Valio itself lists them as substitutes
VALIO_PICK_BONUS = 0.1     # a variant or substitute Valio recommends is a better match than one the AI guessed
TERMINAL_UNKNOWN = 999999  # Valio's marker for "supplier stock, quantity unknown"
BASE_WEIGHTS = {"price": 0.5, "reliability": 0.3, "co2": 0.2}

# Good-better-best plan scoring (shown in the UI).
W_CERTAINTY, W_FIDELITY = 0.55, 0.45
CERT_VALIO_STOCK = 1.0     # Valio's own stock, on time, cutoff still open
CERT_SUPPLIER = 0.6        # supplier ("Terminal") stock, on time, cutoff still open
CERT_CUTOFF_PASSED = 0.25  # order cutoff already passed: the date is at risk
FIDELITY = {"exact": 1.0, "close": 0.8, "substitute": 0.5}
MATCH_ORDER = {"exact": 0, "close": 1, "substitute": 2}
PLAN_LABELS = ["Best", "Better", "Good"]


def _pack_fit(p: dict, quantity: float) -> float:
    """How sensible the pack size is for the quantity: 1,000 L favours 10 L packs, 24 L favours 1 L."""
    size = p["content_per_sales_unit"] or 0
    if not size:
        return 0.0
    units = quantity / size
    return -abs(math.log10(max(units, 1) / 20))   # ~20-200 sales units is the sweet spot


def _shortlist(candidates: list[dict], quantity: float, n: int) -> list[dict]:
    """Pick n candidates that cover every pack size, best-fitting size first.

    Big supplier packs fit a big order best, but single cartons are what Valio keeps in its own
    stock - and stock is unknown until the live check. Taking turns across pack sizes keeps both
    in the shortlist without querying the API for everything.
    """
    buckets: dict[tuple, list[dict]] = {}
    for p in candidates:                                    # keeps the search's own order inside a bucket
        buckets.setdefault((p["sales_unit"], round(p["content_per_sales_unit"] or 0, 2)), []).append(p)
    order = sorted(buckets.values(), key=lambda b: _pack_fit(b[0], quantity), reverse=True)
    picked = []
    while len(picked) < n and any(order):
        for b in order:
            if b and len(picked) < n:
                picked.append(b.pop(0))
    return picked


def _weights(priorities: list[str]) -> dict:
    w = dict(BASE_WEIGHTS)
    if priorities and priorities[0] == "reliability":
        w = {"price": 0.35, "reliability": 0.45, "co2": 0.2}
    elif priorities and priorities[0] == "low_co2":
        w = {"price": 0.35, "reliability": 0.25, "co2": 0.4}
    return w


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+0200", "+02:00").replace("+0300", "+03:00"))
    except ValueError:
        return None


def build_option(p: dict, item: dict, deliver_by: str | None, avail: dict | None, price: dict | None,
                 relevance: dict) -> dict:
    """Combine static data + live answers into one decision-ready option with evidence."""
    flags, evidence = [], {}
    qty = float(item["quantity"])
    content = p["content_per_sales_unit"]
    content_src = p["content_source"]
    unit_price = comparison = None
    if price and not price.get("error") and price.get("catalogPrice"):
        unit_price = price["catalogPrice"]["value"]
        cp = price.get("comparisonPrice") or {}
        if cp.get("value") and price.get("comparisonUnit") == item["unit"]:
            comparison = cp["value"]
            derived = unit_price / cp["value"]
            # Trust the price API's own size when the static data disagrees by >5%.
            if not content or abs(derived - content) / content > 0.05:
                content, content_src = round(derived, 3), "derived from price API (price / price per unit)"
        evidence["price"] = (f"Valio Price API for {price['_meta'].get('pricingDate', '')[:10]}, valid "
                             f"{price['catalogPrice'].get('validFrom', '')[:10]} to {price['catalogPrice'].get('validTo', '')[:10]}"
                             f"{', customer net price' if price.get('netPriceFound') else ', catalog price'}")
    elif price and price.get("error"):
        flags.append("no price available")
    if item["unit"] == "PCS":
        units_needed = math.ceil(qty)
    elif content:
        units_needed = math.ceil(round(qty / content, 6))
    else:
        units_needed = None
        flags.append("pack size unknown")
    lot = p["lot_size"] or 1
    if units_needed is not None and lot >= 1:
        units_needed = math.ceil(units_needed / lot) * lot

    status, available_amount, available_date, closing = "unknown", None, None, None
    if avail:
        line = (avail.get("scheduleLines") or [None])[0]
        closing = avail.get("closingTime")
        evidence["availability"] = f"Valio Availability API at {avail['_meta'].get('timeNow', '')[:16].replace('T', ' ')}"
        if avail.get("salesBlock") or (line and line.get("salesBlock")):
            status = "blocked"
            flags.append("sales block: cannot be ordered now")
        elif not line:
            status = "unavailable"
            flags.append("cannot currently be delivered on any date")
        else:
            available_amount, available_date = line.get("availableAmount"), line.get("availableDate")
            if avail.get("logisticModel") == "Terminal" or available_amount == TERMINAL_UNKNOWN:
                status = "supplier"
                available_amount = None
                flags.append("delivered from the supplier's warehouse: quantity not confirmed")
            elif units_needed is not None and available_amount >= units_needed:
                status = "in_stock"
            elif available_amount and available_amount > 0:
                status = "partial"
                flags.append(f"only {available_amount:g} {p['sales_unit']} in stock")
            else:
                status = "unavailable"
                flags.append("out of stock")
    on_time = bool(available_date and (not deliver_by or available_date <= deliver_by))
    if available_date and deliver_by and available_date > deliver_by:
        flags.append(f"earliest delivery {available_date}, after {deliver_by}")
    closing_dt, now_dt = _parse_ts(closing), _parse_ts(avail["_meta"].get("timeNow")) if avail else None
    cutoff_passed = bool(closing_dt and now_dt and closing_dt < now_dt)
    if cutoff_passed:
        flags.append(f"order cutoff {closing[:16].replace('T', ' ')} has passed: delivery date at risk")

    total = round(units_needed * unit_price, 2) if (units_needed and unit_price) else None
    kg = qty if item["unit"] in ("KG", "L") else None          # 1 L of a food liquid ~ 1 kg (stated in the UI)
    co2_total = round(kg * p["co2_per_kg"], 1) if (kg and p["co2_per_kg"]) else None
    evidence["product"] = f"Product data (GTIN {p['gtin'] or 'n/a'}), updated {str(p['data_updated'] or 'n/a')[:10]}"
    evidence["pack_size"] = content_src
    if p["reliability"] is not None:
        evidence["reliability"] = "Valio delivery reliability file (share of orders delivered as ordered)"
    if p["co2_per_kg"] is not None:
        evidence["co2"] = "Valio carbon footprint file (product-group average)"
    if status == "in_stock" and (available_amount or 0) >= TERMINAL_UNKNOWN:
        evidence["availability"] += "; Valio's own stock, no stock limit reported"
    if item.get("must_claims"):
        evidence["requirements"] = "; ".join(f"{CLAIM_NAMES.get(c, c)}: {claim_proof(p, c) or 'NOT PROVEN'}"
                                             for c in item["must_claims"])
    if item.get("exclude_allergens"):
        evidence["allergens"] ="; ".join(allergen_evidence(p, item["exclude_allergens"]))

    if status == "in_stock" and on_time and not cutoff_passed:
        tier = 1   # confirmed: full quantity, on time
    elif status in ("supplier", "in_stock") and on_time:
        tier = 2   # on time but not fully confirmed
    elif status == "partial" and on_time:
        tier = 3
    else:
        tier = 4
    return {
        "product_id": p["id"], "name": p["name"], "name_fi": p["name_fi"], "brand": p["brand"], "origin": p["origin"],
        "match": relevance.get("match", "exact"), "match_reason": relevance.get("reason", ""),
        "sales_unit": p["sales_unit"], "content_per_sales_unit": content, "content_unit": item["unit"],
        "units_needed": units_needed, "unit_price_eur": unit_price, "comparison_price_eur": comparison,
        "total_eur": total, "status": status, "available_amount": available_amount, "available_date": available_date,
        "on_time": on_time, "order_cutoff": closing, "cutoff_passed": cutoff_passed,
        "reliability": p["reliability"], "co2_kg": co2_total,
        "claims": [CLAIM_NAMES.get(c, c) for c in p["claims"]],
        "labels": [LABEL_NAMES.get(l, l) for l in p["labels"]],
        "contains_allergens": [ALLERGENS.get(a, a) for a in p["contains"]],
        "may_contain_allergens": [ALLERGENS.get(a, a) for a in p["may_contain"]],
        "free_from_allergens": [ALLERGENS.get(a, a) for a in p["free_from"]],
        "flags": flags, "evidence": evidence, "tier": tier,
        "tax_rate": ((price or {}).get("tax") or {}).get("taxRate"),
        "available_units_text": _stock_text(status, available_amount, p["sales_unit"]),
    }


def _stock_text(status: str, amount, sales_unit: str) -> str:
    """Plain words for the product card."""
    if status == "in_stock":
        return "In Valio stock" if not amount or amount >= TERMINAL_UNKNOWN else f"{amount:g} {sales_unit} in Valio stock"
    if status == "partial":
        return f"Only {amount:g} {sales_unit} in Valio stock"
    if status == "supplier":
        return "From the supplier, quantity not guaranteed"
    return {"blocked": "Can't be ordered now", "unavailable": "Out of stock"}.get(status, "Not checked")


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def rank(options: list[dict], priorities: list[str], preferred_labels: list[str], quantity: float | None = None) -> list[dict]:
    """Rules, not AI: tier first (can it arrive in full and on time), then a weighted score.

    Scores are ratios to the best option, so one outlier cannot flatten the rest:
    price = cheapest / this price; CO2 = lowest / this value;
    reliability = (r - 85 %) / 15 %, because real values sit between 85 % and 100 %.
    """
    w = _weights(priorities)
    def metric(o):
        return o["comparison_price_eur"] or (o["total_eur"] / max(o["units_needed"] or 1, 1) if o["total_eur"] else None)
    prices = [metric(o) for o in options if metric(o)]
    co2s = [o["co2_kg"] for o in options if o["co2_kg"]]
    pmin = min(prices) if prices else None
    cmin = min(co2s) if co2s else None
    wanted = {LABEL_NAMES.get(l, l) for l in preferred_labels}
    for o in options:
        m = metric(o)
        price_s = pmin / m if m else 0.0
        rel_s = _clip((o["reliability"] - 0.85) / 0.15) if o["reliability"] is not None else 0.6   # unknown = average
        co2_s = cmin / o["co2_kg"] if (o["co2_kg"] and cmin) else 0.5
        label_bonus = 0.05 * len(wanted & set(o["labels"]))
        match_penalty = {"exact": 0.0, "close": 0.15}.get(o["match"], 0.3)
        if o.get("valio_pick") and o["match"] != "exact":
            match_penalty -= 0.05
        # Small penalty for buying more than needed because of whole cases (10 L asked, 12 L case).
        over = 0.0
        if quantity and o["units_needed"] and o["content_per_sales_unit"] and o["content_unit"] != "PCS":
            over = max(0.0, o["units_needed"] * o["content_per_sales_unit"] - quantity) / quantity
        over_penalty = min(0.1, 0.5 * over)
        o["score"] = round(w["price"] * price_s + w["reliability"] * rel_s + w["co2"] * co2_s + label_bonus
                           - match_penalty - over_penalty, 3)
        o["score_parts"] = {"price": round(price_s, 2), "reliability": round(rel_s, 2), "co2": round(co2_s, 2),
                            "over_order_penalty": round(over_penalty, 3), "weights": w}
    options.sort(key=lambda o: (o["tier"], MATCH_ORDER.get(o["match"], 3), -o["score"]))
    return options


def badges(options: list[dict]) -> dict:
    usable = [o for o in options if o["tier"] <= 2 and o["match"] == "exact" and o["total_eur"]]
    if not usable:
        return {}
    out = {"best": usable[0]["product_id"],
           "cheapest": min(usable, key=lambda o: o["total_eur"])["product_id"]}
    rel = [o for o in usable if o["reliability"] is not None]
    if rel:
        out["most_reliable"] = max(rel, key=lambda o: (o["reliability"], -o["total_eur"]))["product_id"]
    co2 = [o for o in usable if o["co2_kg"] is not None]
    if co2:
        out["lowest_co2"] = min(co2, key=lambda o: (o["co2_kg"], o["total_eur"]))["product_id"]
    return out


# ---------------------------------------------------------------- good-better-best plans

def _size(o: dict, item: dict) -> float:
    """How much of the need one sales unit covers (in the need's unit)."""
    return 1.0 if item["unit"] == "PCS" else float(o["content_per_sales_unit"] or 0)


def _usable(o: dict, item: dict) -> bool:
    return bool(o["unit_price_eur"] and _size(o, item) > 0 and o["on_time"]
                and o["status"] in ("in_stock", "partial", "supplier"))


def _confirmable(o: dict) -> bool:
    """Valio's own stock with the cutoff still open: the only thing we call confirmed."""
    return o["status"] in ("in_stock", "partial") and not o["cutoff_passed"] and (o["available_amount"] or 0) >= 1


def _line_certainty(o: dict) -> float:
    if not o["on_time"]:
        return 0.0
    if o["cutoff_passed"]:
        return CERT_CUTOFF_PASSED
    return CERT_VALIO_STOCK if o["status"] in ("in_stock", "partial") else CERT_SUPPLIER


def _line(o: dict, units: float, item: dict) -> dict:
    return {"product_id": o["product_id"], "name": o["name"], "units": units, "sales_unit": o["sales_unit"],
            "content": round(units * _size(o, item), 2), "unit_price_eur": o["unit_price_eur"],
            "total_eur": round(units * o["unit_price_eur"], 2) if o["unit_price_eur"] else None,
            "match": o["match"], "match_reason": o["match_reason"], "valio_pick": bool(o.get("valio_pick")),
            "source": "supplier" if o["status"] == "supplier" else "Valio stock",
            "cutoff_passed": o["cutoff_passed"], "certainty": _line_certainty(o)}


def _fill(sources: list[dict], item: dict) -> tuple[list[dict], float]:
    """Take from the sources in order until the quantity is covered. Valio stock is limited to what is there."""
    remaining, lines = float(item["quantity"]), []
    for o in sources:
        if remaining <= 1e-9:
            break
        size = _size(o, item)
        want = math.ceil(round(remaining / size, 6))
        take = want if o["status"] == "supplier" else min(math.floor(o["available_amount"] or 0), want)
        if take > 0:
            lines.append(_line(o, take, item))
            remaining -= take * size
    return lines, max(remaining, 0.0)


def _line_fidelity(l: dict) -> float:
    base = FIDELITY.get(l["match"], 0.5)
    return min(1.0, base + VALIO_PICK_BONUS) if l.get("valio_pick") and l["match"] != "exact" else base


def _make_plan(kind: str, lines: list[dict], item: dict) -> dict:
    qty, unit = float(item["quantity"]), item["unit"]
    covered = round(sum(l["content"] for l in lines), 2)
    # Weight each line by the part of the NEED it covers, so over-ordering cannot inflate a score.
    left, cert_sum, fid_sum, counted_sum = qty, 0.0, 0.0, 0.0
    for l in lines:
        counted = min(l["content"], left)
        left -= counted
        cert_sum += counted * l["certainty"]
        fid_sum += counted * _line_fidelity(l)
        counted_sum += counted
    certainty = min(1.0, cert_sum / qty) if qty else 0.0          # uncovered quantity counts as 0
    fidelity = fid_sum / counted_sum if counted_sum else 0.0
    full = covered >= qty - 1e-6
    if full and all(l["certainty"] >= CERT_VALIO_STOCK for l in lines):
        confidence = "confirmed"
    elif any(l["certainty"] >= CERT_VALIO_STOCK for l in lines):
        confidence = "partly confirmed"
    else:
        confidence = "not confirmed"
    total = round(sum(l["total_eur"] or 0 for l in lines), 2)

    trade_offs, approval = [], []
    for l in lines:
        share = f"{l['content']:g} {unit}"
        if l["match"] != "exact" and l.get("valio_pick"):
            trade_offs.append(f"{l['name']} is Valio's own recommended replacement")
        if l["match"] == "close":
            trade_offs.append(f"{l['name']} is a close variant, not exactly what was asked ({l['match_reason'] or 'variant'})")
            approval.append(f"{l['name']} is a close variant of {item['label']}: {l['match_reason'] or 'not exactly what was asked'}")
        elif l["match"] != "exact":
            trade_offs.append(f"{l['name']} is a substitute ({l['match_reason'] or 'different product type'})")
            approval.append(f"{l['name']} is a substitute for {item['label']}: {l['match_reason'] or 'different product type'}")
        if l["source"] == "supplier" and l["cutoff_passed"]:
            trade_offs.append(f"{share} from the supplier's warehouse: quantity not confirmed and the order cutoff has passed")
            approval.append(f"{l['name']}: supplier stock (quantity not confirmed) and the order cutoff has passed, "
                            f"so the delivery date is at risk")
        elif l["source"] == "supplier":
            trade_offs.append(f"{share} from the supplier's warehouse: quantity not confirmed")
            approval.append(f"{l['name']}: supplier stock, quantity not confirmed by Valio")
        elif l["cutoff_passed"]:
            trade_offs.append(f"order cutoff has passed for {l['name']}: delivery date at risk")
            approval.append(f"{l['name']}: the order cutoff has passed, so the delivery date is at risk")
    if len(lines) > 1:
        trade_offs.append(f"split across {len(lines)} products")
    if not full:
        trade_offs.append(f"covers only {covered:g} of {qty:g} {unit}")
        approval.append(f"Only {covered:g} of {qty:g} {unit} can be covered on time; a human decides about the rest")
    elif unit != "PCS" and covered - qty > 0.02 * qty:
        trade_offs.append(f"{covered - qty:g} {unit} more than needed (whole sales units)")

    first = lines[0]
    if kind == "single":
        why = f"{first['name']} covers the full {qty:g} {unit} from Valio's own stock, on time."
    elif kind == "split":
        why = (f"No single product has enough stock, so the order is split across {len(lines)} in-stock products, "
               f"cheapest first. Everything is in Valio's own stock.")
    elif kind == "close variant":
        why = "Fully confirmed from Valio's own stock by accepting a close variant of what was asked."
    elif kind == "substitute":
        why = "Fully confirmed from Valio's own stock by using a substitute product; all dietary requirements still hold."
    elif kind == "stock + supplier":
        sure = sum(l["content"] for l in lines if l["certainty"] >= CERT_VALIO_STOCK)
        why = (f"{sure:g} {unit} is confirmed from Valio's own stock; the remaining {covered - sure:g} {unit} comes from "
               f"the supplier and should be confirmed by a human.")
    elif kind == "supplier":
        why = (f"{first['name']} can arrive on time in one delivery, but it is not confirmed: " +
               ("the order cutoff has passed" if first["cutoff_passed"] else "the supplier's quantity is unknown") +
               ". A human should confirm.")
    else:
        why = (f"Nothing can cover the full {qty:g} {unit} on time. {covered:g} {unit} is available from Valio's own "
               f"stock now; a human should decide on the rest (later date or another product).")
    return {
        "label": None, "kind": kind, "confidence": confidence, "lines": lines,
        "covered": covered, "content_unit": unit, "total_eur": total,
        "price_per_unit": round(total / covered, 3) if covered else None,
        "certainty": round(certainty, 3), "fidelity": round(fidelity, 3),
        "score": round(W_CERTAINTY * certainty + W_FIDELITY * fidelity, 3),
        "trade_offs": trade_offs, "approval_needed": approval,
        "is_substitute": any(l["match"] != "exact" for l in lines), "why": why,
    }


NO_PLAN = {"label": None, "kind": "none", "confidence": "none", "lines": [], "covered": 0, "total_eur": 0,
           "price_per_unit": None, "certainty": 0.0, "fidelity": 0.0, "score": 0.0, "trade_offs": [],
           "approval_needed": [], "is_substitute": False,
           "why": "Nothing can cover the quantity on time. A human should decide on a substitute or a later date."}


def build_plans(options: list[dict], item: dict) -> list[dict]:
    """Good-better-best: up to three distinct ways to fulfil the need, best first.

    Hard requirements (claims, allergens) were already applied to every option, substitutes included,
    so no plan ever trades them away. Plans only trade certainty against fidelity, then price.
    """
    usable = [o for o in options if _usable(o, item)]
    per_unit = lambda o: o["unit_price_eur"] / _size(o, item)

    def stock(match):          # confirmable Valio stock, cheapest per unit first
        return sorted([o for o in usable if o["match"] == match and _confirmable(o)], key=per_unit)

    def unconfirmed(match):    # supplier stock or cutoff passed; cutoff still open first, then cheapest
        return sorted([o for o in usable if o["match"] == match and not _confirmable(o)],
                      key=lambda o: (o["cutoff_passed"], per_unit(o)))

    ex, cl, sub = stock("exact"), stock("close"), stock("substitute")
    candidates = []

    def add(kind, sources, *, must_have=None, min_lines=1):
        lines, remaining = _fill(sources, item)
        if remaining > 1e-6 or len(lines) < min_lines:
            return
        if must_have and not any(must_have(l) for l in lines):
            return
        candidates.append(_make_plan(kind, lines, item))

    for o in ex:                                            # (A) single exact, confirmed
        add("single", [o])
    add("split", ex, min_lines=2)                           # (B) exact, split across Valio stock
    if cl:                                                  # (C) confirmed with close variants
        add("close variant", ex + cl, must_have=lambda l: l["match"] == "close")
        for o in cl:
            add("close variant", [o])
        add("close variant", cl, min_lines=2)
    ex_unconfirmed = unconfirmed("exact")
    if ex and ex_unconfirmed:                               # (D) Valio stock first, rest from the best supplier option
        add("stock + supplier", ex + ex_unconfirmed[:1], must_have=lambda l: l["certainty"] < CERT_VALIO_STOCK)
    for o in ex_unconfirmed[:2]:                            # (E) single exact supplier option
        add("supplier", [o])
    for o in unconfirmed("close")[:1]:
        add("supplier", [o])
    if sub:                                                 # (F) functional substitute, confirmed
        add("substitute", ex + cl + sub, must_have=lambda l: l["match"] == "substitute")
        for o in sub:
            add("substitute", [o])
        add("substitute", sub, min_lines=2)
    if not candidates:                                      # nothing covers it all: say what CAN be delivered
        lines, _ = _fill(ex + cl, item)
        if lines:
            candidates.append(_make_plan("partial", lines, item))

    seen, distinct = set(), []
    for p in sorted(candidates, key=lambda p: (-p["score"], p["total_eur"])):
        key = tuple(sorted((l["product_id"], l["units"]) for l in p["lines"]))
        if key not in seen:
            seen.add(key)
            distinct.append(p)
    # Prefer three different KINDS of plan (a real choice), then fill up with the next best.
    chosen, kinds = [], set()
    for p in distinct:
        if p["kind"] not in kinds and len(chosen) < 3:
            chosen.append(p)
            kinds.add(p["kind"])
    for p in distinct:
        if len(chosen) < 3 and p not in chosen:
            chosen.append(p)
    chosen.sort(key=lambda p: (-p["score"], p["total_eur"]))
    for label, p in zip(PLAN_LABELS, chosen):
        p["label"] = label
    return chosen


def apply_budget(plans: list[dict], budget: float | None) -> list[dict]:
    """The buyer's own spending limit for this request: plans within it come first, the rest say so."""
    for p in plans:
        p["over_budget"] = bool(budget and p["total_eur"] > budget)
        if p["over_budget"]:
            p["trade_offs"].append(f"{p['total_eur']:,.2f} € is over your {budget:,.0f} € budget")
    plans.sort(key=lambda p: (p["over_budget"], -p["score"], p["total_eur"]))
    for label, p in zip(PLAN_LABELS, plans):
        p["label"] = label
    return plans


def normalise_need(need: dict, today: date) -> dict:
    """A need edited by the buyer in the UI: fill gaps and keep only known values."""
    out = {"items": [], "deliver_by": need.get("deliver_by") or None, "budget_eur": need.get("budget_eur") or None,
           "priorities": [p for p in need.get("priorities") or [] if p in und.PRIORITIES] or ["price"],
           "language": need.get("language") or "en", "engine": "edited by the buyer", "usage": None}
    if out["deliver_by"]:
        date.fromisoformat(out["deliver_by"])                     # raises ValueError on a bad date
    for it in need.get("items") or []:
        label = str(it.get("label") or "").strip()
        terms = [t.strip() for t in it.get("product_terms") or [] if str(t).strip()] or ([label] if label else [])
        if not terms or float(it.get("quantity") or 0) <= 0:
            continue
        out["items"].append({
            "label": label or terms[0], "product_terms": terms, "quantity": float(it["quantity"]),
            "unit": it.get("unit") if it.get("unit") in und.UNITS else "PCS",
            "must_claims": [c for c in it.get("must_claims") or [] if c in CLAIM_NAMES],
            "exclude_allergens": [a for a in it.get("exclude_allergens") or [] if a in ALLERGENS],
            "strict_allergens": bool(it.get("strict_allergens")),
            "preferred_labels": [l for l in it.get("preferred_labels") or [] if l in LABEL_NAMES],
            "substitutes": [s for s in it.get("substitutes") or [] if s.get("product_terms")][:2],
        })
    if not out["items"]:
        raise ValueError("Say what you need and how much.")
    return out


def has_confirmed_exact(plans: list[dict]) -> bool:
    return any(p["confidence"] == "confirmed" and p["fidelity"] >= 0.999 for p in plans)


# ---------------------------------------------------------------- pipeline

def _mark_valio(options: list[dict], valio_for: dict[str, list[str]]) -> None:
    """Flag products that Valio itself lists as a replacement for one of the products found."""
    for o in options:
        o["valio_substitute_for"] = valio_for.get(o["product_id"], [])
        o["valio_pick"] = bool(o["valio_substitute_for"])


def _live(valio, ids: list[str], deliver_by: str) -> tuple[dict, dict, str | None]:
    if not ids:
        return {}, {}, None
    try:
        return valio.availability(ids, deliver_by), valio.price(ids, deliver_by), None
    except ValioApiError as exc:
        return {}, {}, str(exc)


def _search(catalog, terms: list[str], item: dict, limit: int) -> dict:
    """Same hard rules for the requested product and for every substitute idea."""
    return catalog.search(terms, must_claims=item["must_claims"], exclude_allergens=item["exclude_allergens"],
                          strict_allergens=item["strict_allergens"],
                          content_unit=item["unit"] if item["unit"] in ("L", "KG") else None, limit=limit)


def lookup(request: str, *, today: date | None = None, catalog=None, valio=None, need: dict | None = None) -> dict:
    """need: a structured need the buyer edited; when given, the AI understanding step is skipped."""
    today = today or date.today()
    catalog = catalog or get_catalog()
    valio = valio or default_valio
    calls_before = dict(valio.stats)
    need = normalise_need(need, today) if need else und.understand(request, today)
    deliver_by = need.get("deliver_by") or (today + timedelta(days=2)).isoformat()
    budget = need.get("budget_eur") or None
    assumptions = []
    if not need.get("deliver_by"):
        assumptions.append({"field": "deliver_by",
                            "text": f"No date given, so I assumed {date.fromisoformat(deliver_by).strftime('%a %d %b')}"})
    if not budget:
        assumptions.append({"field": "budget_eur", "text": "No budget set"})
    usage = [need["usage"]] if need.get("usage") else []
    priorities = need.get("priorities") or []

    shortlists = []
    for item in need["items"]:
        qty = float(item["quantity"])
        found = _search(catalog, item["product_terms"], item, SHORTLIST_SEARCH)
        cands = _shortlist(found["candidates"], qty, SHORTLIST_JUDGE)
        # Valio's own substitution rules for the products found: add the replacements Valio lists,
        # but only those that meet the same hard rules (claims, allergens, unit). Judged in the same AI call.
        valio_for: dict[str, list[str]] = {}
        for c in found["candidates"]:
            for sid in c.get("valio_substitutes") or []:
                valio_for.setdefault(sid, []).append(c["id"])
        in_list, valio_added, valio_dropped = {c["id"] for c in cands}, [], 0
        for sid in valio_for:
            p = catalog.products.get(sid)
            if not p or sid in in_list:
                continue
            if catalog.meets(p, must_claims=item["must_claims"], exclude_allergens=item["exclude_allergens"],
                             strict_allergens=item["strict_allergens"],
                             content_unit=item["unit"] if item["unit"] in ("L", "KG") else None):
                valio_added.append(p)
            else:
                valio_dropped += 1
        cands += valio_added[:VALIO_SUBS_MAX]
        taken = {c["id"] for c in cands}
        sub_cands, ideas = [], []
        for idea in (item.get("substitutes") or [])[:2]:
            sub_found = _search(catalog, idea["product_terms"], item, SHORTLIST_SEARCH)
            picked = _shortlist([p for p in sub_found["candidates"] if p["id"] not in taken], qty, SUBSTITUTE_PER_IDEA)
            taken |= {p["id"] for p in picked}
            sub_cands += [{"product": p, "label": idea["label"], "trade_off": idea["trade_off"]} for p in picked]
            ideas.append({"label": idea["label"], "trade_off": idea["trade_off"], "candidates": len(picked)})
        relevance, rel_usage = und.judge_relevance(item, cands, sub_cands)   # ONE call for both groups
        if rel_usage:
            usage.append(rel_usage)
        everything = cands + [s["product"] for s in sub_cands]
        match_of = lambda c: relevance.get(c["id"], {}).get("match")
        shortlists.append({
            "item": item, "found": found, "relevance": relevance, "ideas": ideas, "valio_for": valio_for,
            "valio_added": len(valio_added[:VALIO_SUBS_MAX]), "valio_dropped": valio_dropped,
            "live": [c for c in everything if match_of(c) == "exact"][:LIVE_EXACT]
                    + [c for c in everything if match_of(c) == "close"][:LIVE_CLOSE],
            "substitutes": [c for c in everything if match_of(c) == "substitute"][:LIVE_SUBSTITUTE],
            "rejected": [{"product_id": c["id"], "name": c["name"], **relevance[c["id"]]}
                         for c in everything if match_of(c) == "different"],
        })

    # Live check 1: the shortlist only (one availability call + one price call).
    ids = [p["id"] for s in shortlists for p in s["live"]]
    avail, prices, live_error = _live(valio, ids, deliver_by)
    for s in shortlists:
        item = s["item"]
        s["options"] = [build_option(p, item, deliver_by, avail.get(p["id"]), prices.get(p["id"]),
                                     s["relevance"].get(p["id"], {})) for p in s["live"]]
        _mark_valio(s["options"], s["valio_for"])
        rank(s["options"], priorities, item["preferred_labels"], float(item["quantity"]))
        s["plans"] = build_plans(s["options"], item)
        s["sub_note"], s["sub_checked"] = None, 0
        if not s["substitutes"]:
            continue
        if has_confirmed_exact(s["plans"]):
            s["sub_note"] = (f"{len(s['substitutes'])} substitute(s) identified but not checked live: "
                             f"the requested product is confirmed, so no extra API calls were needed.")

    # Live check 2 (lazy): substitutes, only for items that have no confirmed exact plan.
    lazy = [s for s in shortlists if s["substitutes"] and not has_confirmed_exact(s["plans"])]
    sub_ids = [p["id"] for s in lazy for p in s["substitutes"]]
    if sub_ids and not live_error:
        avail2, prices2, sub_error = _live(valio, sub_ids, deliver_by)
        for s in lazy:
            item = s["item"]
            if sub_error:
                s["sub_note"] = f"Substitutes could not be checked live ({sub_error})."
                continue
            s["options"] += [build_option(p, item, deliver_by, avail2.get(p["id"]), prices2.get(p["id"]),
                                          s["relevance"].get(p["id"], {})) for p in s["substitutes"]]
            _mark_valio(s["options"], s["valio_for"])
            rank(s["options"], priorities, item["preferred_labels"], float(item["quantity"]))
            s["plans"] = build_plans(s["options"], item)
            s["sub_checked"] = len(s["substitutes"])
            s["sub_note"] = (f"No confirmed plan for the requested product, so {len(s['substitutes'])} substitute(s) "
                             f"were checked live as well (2 more API calls).")
        ids += sub_ids

    results = []
    for s in shortlists:
        plans = apply_budget(s["plans"], budget)
        results.append({
            "item": s["item"], "options": s["options"], "badges": badges(s["options"]),
            "plans": plans, "plan": plans[0] if plans else dict(NO_PLAN),
            "funnel": {"catalog": s["found"]["catalog_size"], "steps": s["found"]["steps"],
                       "judged": len(s["relevance"]), "checked_live": len(s["live"]) + s["sub_checked"],
                       "valio_substitutes_added": s["valio_added"], "valio_substitutes_dropped": s["valio_dropped"],
                       "substitute_ideas": s["ideas"], "substitutes_found": len(s["substitutes"]),
                       "substitutes_checked_live": s["sub_checked"], "substitutes_note": s["sub_note"]},
            "rejected": s["rejected"],
        })
    calls = valio.stats["calls"] - calls_before.get("calls", 0)
    return {
        "request": request, "need": {k: v for k, v in need.items() if k not in ("usage",)}, "deliver_by": deliver_by,
        "budget_eur": budget, "assumptions": assumptions,
        "results": results, "live_error": live_error,
        "plan_scoring": {"certainty_weight": W_CERTAINTY, "fidelity_weight": W_FIDELITY,
                         "certainty": {"Valio stock": CERT_VALIO_STOCK, "supplier stock": CERT_SUPPLIER,
                                       "cutoff passed": CERT_CUTOFF_PASSED, "not covered": 0.0},
                         "fidelity": FIDELITY, "tie_breaker": "total price"},
        "api": {"calls": calls, "products_checked": len(ids), "catalog_size": len(catalog)},
        "ai_usage": {"calls": len(usage), "usd": round(sum(u["usd"] for u in usage), 4),
                     "models": sorted({u["model"] for u in usage})},
    }
