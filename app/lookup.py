"""Smart Lookup: from one sentence to a few trusted, ranked, deliverable options.

Pipeline (each step is shown to the user):
 1. understand  - Claude turns the request into a structured need (offline parser as fallback)
 2. shortlist   - search 17,546 products offline: names (EN/FI/SV), category coherence, claims,
                  allergens, unit; then Claude judges which candidates are really the requested product
 3. live check  - ONE availability call + ONE price call for the shortlist only (Valio's rule)
 4. rank        - transparent rules: can it arrive in time and in full, then price per unit,
                  delivery reliability and CO2, with the buyer's priorities as weights
 5. plan        - the best way to fulfil the need (single product, or a split when stock is short)
Every option carries its evidence: where each fact came from and how fresh it is.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from . import understand as und
from .catalog import ALLERGENS, CLAIM_NAMES, LABEL_NAMES, get_catalog
from .valio_api import ValioApiError, client as default_valio

SHORTLIST_SEARCH = 40      # offline candidates considered
SHORTLIST_JUDGE = 14       # sent to Claude for the relevance check
LIVE_EXACT = 8             # exact matches checked live
LIVE_CLOSE = 3             # close substitutes checked live
TERMINAL_UNKNOWN = 999999  # Valio's marker for "supplier stock, quantity unknown"
BASE_WEIGHTS = {"price": 0.5, "reliability": 0.3, "co2": 0.2}


def _pack_fit(p: dict, quantity: float) -> float:
    """How sensible the pack size is for the quantity: 1,000 L favours 10 L packs, 24 L favours 1 L."""
    size = p["content_per_sales_unit"] or 0
    if not size:
        return 0.0
    units = quantity / size
    return -abs(math.log10(max(units, 1) / 20))   # ~20-200 sales units is the sweet spot


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
        "flags": flags, "evidence": evidence, "tier": tier,
    }


def rank(options: list[dict], priorities: list[str], preferred_labels: list[str]) -> list[dict]:
    """Rules, not AI: tier first (can it arrive in full and on time), then a weighted score."""
    w = _weights(priorities)
    priced = [o for o in options if o["comparison_price_eur"] or o["unit_price_eur"]]
    def metric(o):
        return o["comparison_price_eur"] or (o["total_eur"] / max(o["units_needed"] or 1, 1) if o["total_eur"] else None)
    prices = [metric(o) for o in priced if metric(o)]
    co2s = [o["co2_kg"] for o in options if o["co2_kg"] is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0, 0)
    cmin, cmax = (min(co2s), max(co2s)) if co2s else (0, 0)
    wanted = {LABEL_NAMES.get(l, l) for l in preferred_labels}
    for o in options:
        m = metric(o)
        price_s = 1 - (m - pmin) / (pmax - pmin) if (m is not None and pmax > pmin) else (1.0 if m is not None else 0.0)
        rel_s = o["reliability"] if o["reliability"] is not None else 0.85      # unknown = slightly below average
        co2_s = 1 - (o["co2_kg"] - cmin) / (cmax - cmin) if (o["co2_kg"] is not None and cmax > cmin) else 0.5
        label_bonus = 0.05 * len(wanted & set(o["labels"]))
        match_penalty = 0 if o["match"] == "exact" else 0.15
        o["score"] = round(w["price"] * price_s + w["reliability"] * rel_s + w["co2"] * co2_s + label_bonus - match_penalty, 3)
        o["score_parts"] = {"price": round(price_s, 2), "reliability": round(rel_s, 2), "co2": round(co2_s, 2),
                            "weights": w}
    options.sort(key=lambda o: (o["tier"], o["match"] != "exact", -o["score"]))
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


def plan(options: list[dict], item: dict) -> dict:
    """How to fulfil the need. Confirmed single product first; otherwise split or flag for a human."""
    exact = [o for o in options if o["match"] == "exact" and o["units_needed"]]
    for o in exact:
        if o["tier"] == 1:
            return {"type": "single", "confidence": "confirmed", "lines": [_line(o, o["units_needed"])],
                    "why": f"{o['name']} covers the full {item['quantity']:g} {item['unit']} from Valio's own stock, on time."}
    # Split across in-stock products (cheapest per unit first) when no single one has enough.
    stock = sorted([o for o in exact if o["status"] in ("partial", "in_stock") and o["on_time"] and not o["cutoff_passed"]],
                   key=lambda o: o["comparison_price_eur"] or 1e9)
    remaining, lines = float(item["quantity"]), []
    for o in stock:
        if remaining <= 0:
            break
        size = o["content_per_sales_unit"] or 1
        take = min(o["available_amount"] or 0, math.ceil(round(remaining / size, 6)))
        if take > 0:
            lines.append(_line(o, take))
            remaining -= take * size
    if lines and remaining <= 0:
        return {"type": "split", "confidence": "confirmed", "lines": lines,
                "why": "No single product has enough stock, so the order is split across in-stock products, cheapest first."}
    for o in exact:
        if o["tier"] == 2:
            return {"type": "single", "confidence": "not confirmed", "lines": [_line(o, o["units_needed"])],
                    "why": (f"{o['name']} can arrive on time, but " + "; ".join(o["flags"]) +
                            ". A human should confirm, or accept the split/substitute below.")}
    return {"type": "none", "confidence": "none", "lines": lines,
            "why": "Nothing can cover the full quantity on time. A human should decide on a substitute or a later date."}


def _line(o: dict, units: float) -> dict:
    return {"product_id": o["product_id"], "name": o["name"], "units": units, "sales_unit": o["sales_unit"],
            "content": round(units * (o["content_per_sales_unit"] or 0), 2), "unit_price_eur": o["unit_price_eur"],
            "total_eur": round(units * o["unit_price_eur"], 2) if o["unit_price_eur"] else None}


def lookup(request: str, *, today: date | None = None, catalog=None, valio=None) -> dict:
    today = today or date.today()
    catalog = catalog or get_catalog()
    valio = valio or default_valio
    calls_before = dict(valio.stats)
    need = und.understand(request, today)
    deliver_by = need.get("deliver_by") or (today + timedelta(days=2)).isoformat()
    usage = [need["usage"]] if need.get("usage") else []

    shortlists = []
    for item in need["items"]:
        found = catalog.search(item["product_terms"], must_claims=item["must_claims"],
                               exclude_allergens=item["exclude_allergens"], strict_allergens=item["strict_allergens"],
                               content_unit=item["unit"] if item["unit"] in ("L", "KG") else None, limit=SHORTLIST_SEARCH)
        cands = sorted(found["candidates"], key=lambda p: _pack_fit(p, float(item["quantity"])),
                       reverse=True)[:SHORTLIST_JUDGE]
        relevance, rel_usage = und.judge_relevance(item, cands)
        if rel_usage:
            usage.append(rel_usage)
        exact = [c for c in cands if relevance.get(c["id"], {}).get("match") == "exact"][:LIVE_EXACT]
        close = [c for c in cands if relevance.get(c["id"], {}).get("match") == "close"][:LIVE_CLOSE]
        shortlists.append({"item": item, "found": found, "relevance": relevance, "live": exact + close,
                           "rejected": [{"product_id": c["id"], "name": c["name"], **relevance[c["id"]]}
                                        for c in cands if relevance.get(c["id"], {}).get("match") == "different"]})

    ids = [p["id"] for s in shortlists for p in s["live"]]
    live_error = None
    try:
        avail = valio.availability(ids, deliver_by) if ids else {}
        prices = valio.price(ids, deliver_by) if ids else {}
    except ValioApiError as exc:
        avail, prices, live_error = {}, {}, str(exc)

    results = []
    for s in shortlists:
        item = s["item"]
        options = [build_option(p, item, deliver_by, avail.get(p["id"]), prices.get(p["id"]), s["relevance"].get(p["id"], {}))
                   for p in s["live"]]
        rank(options, need.get("priorities") or [], item["preferred_labels"])
        results.append({
            "item": item, "options": options, "badges": badges(options), "plan": plan(options, item),
            "funnel": {"catalog": s["found"]["catalog_size"], "steps": s["found"]["steps"],
                       "judged": len(s["relevance"]), "checked_live": len(s["live"])},
            "rejected": s["rejected"],
        })
    calls = valio.stats["calls"] - calls_before.get("calls", 0)
    return {
        "request": request, "need": {k: v for k, v in need.items() if k not in ("usage",)}, "deliver_by": deliver_by,
        "results": results, "live_error": live_error,
        "api": {"calls": calls, "products_checked": len(ids), "catalog_size": len(catalog)},
        "ai_usage": {"calls": len(usage), "usd": round(sum(u["usd"] for u in usage), 4),
                     "models": sorted({u["model"] for u in usage})},
    }
