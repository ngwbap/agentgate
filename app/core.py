"""AgentGate core: Valio Aimo's front door for external purchasing agents.

Everything an external agent may do goes through this module (HTTP in main.py, MCP in
mcp_server.py). Static product data comes from Valio's product file; price and availability
come live from Valio's mock APIs - only for products already narrowed down.

The door - not the agent - enforces:
* the buyer's MANDATE: spending limits, food only, lead time, validity, instant revoke
* the UNUSUAL-ORDER GUARD: far more than the buyer's normal weekly quantity -> human approval
* DELIVERY RISK: an order whose cutoff has passed is not committed without a human
* NEED-TO-KNOW data: no other customers, no internal fields; searches are throttled
* a TAMPER-EVIDENT audit log (hash chain) and a verifiable receipt per order
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config
from .catalog import get_catalog, public_view
from .valio_api import ValioApiError, client as default_valio

QUOTE_VALID_MINUTES = 30
SEARCH_LIMIT_PER_MINUTE = 30
NON_FOOD_TAX_RATE = 20.0   # Finnish VAT: food 13.5 %, non-food and alcohol 25.5 %
TERMINAL_UNKNOWN = 999999

_lock = threading.Lock()


class WholesalerError(Exception):
    """Invalid agent request; the message is safe to show to the agent."""


class MandateError(WholesalerError):
    """The agent has no valid mandate (revoked or expired)."""


def today() -> date:
    fixed = os.environ.get("DEMO_TODAY")
    return date.fromisoformat(fixed) if fixed else date.today()


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class Store:
    def __init__(self) -> None:
        self.catalog = None     # injected in tests; otherwise loaded lazily from data/valio
        self.valio = default_valio
        self.reset()

    def reset(self) -> None:
        raw = json.loads((config.ROOT / "data" / "buyers.json").read_text(encoding="utf-8"))
        self.buyers = {b["id"]: json.loads(json.dumps(b)) for b in raw["buyers"]}
        self.quotes: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.search_times: dict[str, list[float]] = {}
        self._ids = itertools.count(1)

    def get_catalog(self):
        return self.catalog or get_catalog()

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"


store = Store()


# ---------------------------------------------------------------- audit (hash chain)

def audit(actor: str, action: str, summary: str, *, reason: str = "", data: dict | None = None) -> None:
    entry = {
        "seq": len(store.audit) + 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "actor": actor, "action": action, "summary": summary, "reason": reason, "data": data or {},
        "prev_hash": store.audit[-1]["hash"] if store.audit else "0" * 64,
    }
    entry["hash"] = _sha(entry)
    store.audit.append(entry)


def verify_audit() -> dict:
    prev = "0" * 64
    for e in store.audit:
        body = {k: v for k, v in e.items() if k != "hash"}
        if e["prev_hash"] != prev or _sha(body) != e["hash"]:
            return {"ok": False, "broken_at": e["seq"], "entries": len(store.audit)}
        prev = e["hash"]
    return {"ok": True, "entries": len(store.audit), "head": prev}


# ---------------------------------------------------------------- buyers & mandates

def buyer_by_key(agent_key: str) -> dict:
    for b in store.buyers.values():
        if b["agent_key"] == agent_key:
            return b
    raise WholesalerError("Unknown agent key.")


def check_mandate(buyer: dict) -> dict:
    m = buyer["mandate"]
    if m["revoked"]:
        raise MandateError(f"Mandate {m['id']} was revoked by {m['issued_by']}. This agent may not act for {buyer['name']}.")
    if date.fromisoformat(m["valid_until"]) < today():
        raise MandateError(f"Mandate {m['id']} expired on {m['valid_until']}.")
    return m


def public_mandate(buyer: dict) -> dict:
    return {"buyer": buyer["name"], "approver": buyer["approver"], "usual_weekly": buyer.get("usual_weekly", {}),
            **buyer["mandate"]}


def set_mandate_revoked(buyer_id: str, revoked: bool, by: str) -> dict:
    buyer = store.buyers[buyer_id]
    buyer["mandate"]["revoked"] = revoked
    audit(f"human:{by}", "revoke_mandate" if revoked else "reinstate_mandate",
          f"Mandate {buyer['mandate']['id']} {'REVOKED' if revoked else 'reinstated'} for {buyer['name']}")
    return public_mandate(buyer)


# ---------------------------------------------------------------- helpers

def _product(product_id: str) -> dict:
    p = store.get_catalog().products.get(product_id)
    if not p:
        raise WholesalerError(f"No product with id {product_id}.")
    return p


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WholesalerError("Dates must be ISO format YYYY-MM-DD.") from exc


def _throttle(buyer_id: str) -> None:
    now = time.monotonic()
    recent = [t for t in store.search_times.get(buyer_id, []) if now - t < 60]
    if len(recent) >= SEARCH_LIMIT_PER_MINUTE:
        raise WholesalerError("Too many searches in one minute (anti-scraping limit). Slow down.")
    recent.append(now)
    store.search_times[buyer_id] = recent


def _live(product_ids: list[str], requested_date: str) -> tuple[dict, dict]:
    try:
        return store.valio.availability(product_ids, requested_date), store.valio.price(product_ids, requested_date)
    except ValioApiError as exc:
        raise WholesalerError(f"Live price/availability is unavailable right now ({exc}).") from exc


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+0200", "+02:00").replace("+0300", "+03:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- agent API

def search_products(buyer_id: str, query: str = "", max_results: int = 10) -> list[dict]:
    """Offline catalog search (no prices, no stock): the agent narrows down before asking live."""
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    _throttle(buyer_id)
    terms = [t for t in query.split(",") if t.strip()] or [query]
    found = store.get_catalog().search([t.strip() for t in terms], limit=max_results)
    results = [public_view(p) for p in found["candidates"]]
    audit(f"agent:{buyer_id}", "search_products", f"Searched '{query}' -> {len(results)} results (offline catalog)",
          data={"query": query})
    return results


def smart_lookup(buyer_id: str, request: str) -> dict:
    """One sentence in, ranked deliverable options out (see lookup.py)."""
    from .lookup import lookup
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    result = lookup(request, today=today(), catalog=store.get_catalog(), valio=store.valio)
    first = result["results"][0] if result["results"] else None
    plan = first["plan"] if first else {}
    audit(f"agent:{buyer_id}", "smart_lookup",
          f"Lookup '{request[:60]}': {result['api']['products_checked']} of {result['api']['catalog_size']} products "
          f"checked live in {result['api']['calls']} API calls; {len(first['plans']) if first else 0} plan(s), "
          f"best: {plan.get('kind', 'none')} ({plan.get('confidence', '-')})",
          reason=plan.get("why", ""), data={"api": result["api"], "ai_usage": result["ai_usage"]})
    return result


def check_availability(buyer_id: str, product_id: str, quantity: float, deliver_by: str | None = None) -> dict:
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    p = _product(product_id)
    requested = (_parse_date(deliver_by) or today() + timedelta(days=2)).isoformat()
    avail, prices = _live([product_id], requested)
    a, pr = avail.get(product_id, {}), prices.get(product_id, {})
    line = (a.get("scheduleLines") or [None])[0] or {}
    amount = line.get("availableAmount")
    result = {
        **public_view(p),
        "requested_units": quantity, "requested_date": requested,
        "sales_blocked": bool(a.get("salesBlock") or line.get("salesBlock")),
        "available_units": None if amount == TERMINAL_UNKNOWN else amount,
        "stock_source": "supplier (quantity not confirmed)" if a.get("logisticModel") == "Terminal" else "Valio stock",
        "available_date": line.get("availableDate"), "order_cutoff": a.get("closingTime"),
        "unit_price_eur": (pr.get("catalogPrice") or {}).get("value"),
        "price_per_unit": (pr.get("comparisonPrice") or {}).get("value"), "price_unit": pr.get("comparisonUnit"),
        "tax_rate": (pr.get("tax") or {}).get("taxRate"),
    }
    audit(f"agent:{buyer_id}", "check_availability",
          f"{p['name']} ({product_id}): {result['stock_source']}, earliest {result['available_date']}",
          data={"product_id": product_id, "quantity": quantity})
    return result


def request_quote(buyer_id: str, items: list[dict], deliver_by: str | None = None) -> dict:
    """Binding fixed-price quote from live Valio prices. items = [{product_id, quantity (sales units)}]."""
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    if not items:
        raise WholesalerError("A quote needs at least one item.")
    requested = (_parse_date(deliver_by) or today() + timedelta(days=2)).isoformat()
    products = [_product(i["product_id"]) for i in items]
    avail, prices = _live([p["id"] for p in products], requested)
    lines, problems = [], []
    for item, p in zip(items, products):
        qty = float(item["quantity"])
        if qty <= 0:
            raise WholesalerError("Quantities must be positive.")
        a, pr = avail.get(p["id"], {}), prices.get(p["id"], {})
        line = (a.get("scheduleLines") or [None])[0]
        cp = pr.get("catalogPrice") or {}
        if pr.get("error") or not cp.get("value"):
            problems.append(f"{p['id']}: no price available.")
            continue
        if a.get("salesBlock") or not line or line.get("salesBlock"):
            problems.append(f"{p['id']}: cannot be ordered right now (sales block or no delivery date).")
            continue
        supplier = a.get("logisticModel") == "Terminal" or line.get("availableAmount") == TERMINAL_UNKNOWN
        if not supplier and line.get("availableAmount", 0) < qty:
            problems.append(f"{p['id']}: only {line.get('availableAmount', 0):g} {p['sales_unit']} available.")
            continue
        if line.get("availableDate") and line["availableDate"] > requested:
            problems.append(f"{p['id']}: earliest delivery {line['availableDate']} is after {requested}.")
            continue
        closing, now = _ts(a.get("closingTime")), _ts((a.get("_meta") or {}).get("timeNow"))
        content = p["content_per_sales_unit"]
        cmp = pr.get("comparisonPrice") or {}
        if cmp.get("value"):
            content = round(cp["value"] / cmp["value"], 3)
        lines.append({
            "product_id": p["id"], "name": p["name"], "category": p["category"],
            "quantity": qty, "unit": p["sales_unit"], "unit_price_eur": cp["value"],
            "line_total_eur": round(qty * cp["value"], 2), "tax_rate": (pr.get("tax") or {}).get("taxRate"),
            "content": round(qty * content, 2) if content else None, "content_unit": pr.get("comparisonUnit") or p["content_unit"],
            "available_date": line.get("availableDate"),
            "stock_source": "supplier" if supplier else "valio_stock",
            "cutoff_passed": bool(closing and now and closing < now), "order_cutoff": a.get("closingTime"),
        })
    if problems:
        audit(f"agent:{buyer_id}", "request_quote", "Quote refused: " + " ".join(problems), data={"items": items})
        raise WholesalerError("Cannot quote: " + " ".join(problems))
    total = round(sum(l["line_total_eur"] for l in lines), 2)
    quote = {
        "quote_id": store.next_id("Q"), "buyer_id": buyer_id, "lines": lines, "total_eur": total,
        "currency": "EUR (excl. VAT)", "deliver_by": requested,
        "valid_until": (datetime.now() + timedelta(minutes=QUOTE_VALID_MINUTES)).isoformat(timespec="seconds"),
        "pricing": "fixed Valio price for this customer and date, no negotiation",
    }
    quote["policy_preview"] = evaluate(buyer, quote, is_substitute=False)
    store.quotes[quote["quote_id"]] = quote
    audit(f"agent:{buyer_id}", "request_quote",
          f"Quote {quote['quote_id']} for {total} EUR; door says: {quote['policy_preview']['decision']}",
          data={"quote_id": quote["quote_id"]})
    return quote


def _clean_risks(declared_risks) -> list[str]:
    """Risks the agent itself declares (free text). Bounded so an agent cannot flood the approver."""
    return [str(r).strip()[:300] for r in (declared_risks or []) if str(r).strip()][:10]


def evaluate(buyer: dict, quote: dict, *, is_substitute: bool, declared_risks: list[str] | None = None) -> dict:
    """The door's decision: agent may order alone, needs a human, or is blocked. Plain rules, plain reasons."""
    m = buyer["mandate"]
    block, approve = [], []
    for line in quote["lines"]:
        if m.get("food_only") and (line.get("tax_rate") or 0) >= NON_FOOD_TAX_RATE:
            block.append(f"{line['name']} is not food (VAT {line['tax_rate']} %): outside the mandate.")
        if line.get("available_date"):
            lead = (date.fromisoformat(line["available_date"]) - today()).days
            if lead > m["max_lead_time_days"]:
                block.append(f"{line['product_id']} arrives in {lead} days; the mandate allows {m['max_lead_time_days']}.")
        if line.get("cutoff_passed"):
            approve.append(f"{line['name']}: the supplier's order cutoff has passed, so delivery by "
                           f"{quote['deliver_by']} is not guaranteed.")
    per_category = defaultdict(float)
    for line in quote["lines"]:
        if line.get("content"):
            per_category[line["category"]] += line["content"]
    for cat, amount in per_category.items():
        usual = buyer.get("usual_weekly", {}).get(cat)
        if usual and amount > m["unusual_order_factor"] * usual["quantity"]:
            approve.append(f"Unusual order: {amount:g} {usual['unit']} of {usual['name']} is "
                           f"{amount / usual['quantity']:.1f}x the usual weekly {usual['quantity']:g} {usual['unit']}.")
    if quote["total_eur"] > m["hard_limit_eur"]:
        block.append(f"Total {quote['total_eur']} EUR exceeds the mandate's hard limit {m['hard_limit_eur']} EUR.")
    if quote["total_eur"] > m["auto_approve_limit_eur"]:
        approve.append(f"Total {quote['total_eur']} EUR is above the agent's own limit {m['auto_approve_limit_eur']} EUR.")
    if is_substitute and m["substitutes_need_approval"]:
        approve.append("Contains a substitute product; substitutes need human sign-off.")
    # An agent can only ADD caution: anything it declares sends the order to a human, never past a block.
    approve += [f"Agent declared: {r}" for r in _clean_risks(declared_risks)]
    if block:
        return {"decision": "blocked", "reasons": block}
    if approve:
        return {"decision": "needs_human_approval", "approver": buyer["approver"], "reasons": approve}
    return {"decision": "agent_may_order", "reasons": ["Within the mandate: under the spending limit, food only, normal quantities, delivery confirmed."]}


def _approve_by(quote: dict) -> str | None:
    """Latest time a human can approve and still keep the delivery date (earliest relevant cutoff)."""
    cutoffs = [l["order_cutoff"] for l in quote["lines"] if l.get("order_cutoff") and not l.get("cutoff_passed")]
    return min(cutoffs)[:16] if cutoffs else None


def place_order(buyer_id: str, quote_id: str, reason: str, is_substitute: bool = False,
                declared_risks: list[str] | None = None) -> dict:
    declared_risks = _clean_risks(declared_risks)
    with _lock:
        buyer = store.buyers[buyer_id]
        check_mandate(buyer)
        quote = store.quotes.get(quote_id)
        if not quote or quote["buyer_id"] != buyer_id:
            raise WholesalerError(f"No quote {quote_id} for this buyer.")
        if datetime.fromisoformat(quote["valid_until"]) < datetime.now():
            raise WholesalerError(f"Quote {quote_id} has expired; request a new one.")
        if quote.get("used"):
            raise WholesalerError(f"Quote {quote_id} was already used.")
        if not reason.strip():
            raise WholesalerError("A reason is required so humans can see why the agent ordered.")
        decision = evaluate(buyer, quote, is_substitute=is_substitute, declared_risks=declared_risks)
        status ={"agent_may_order": "confirmed", "needs_human_approval": "pending_approval",
                  "blocked": "rejected"}[decision["decision"]]
        order = {
            "order_id": store.next_id("O"), "buyer_id": buyer_id, "buyer_name": buyer["name"],
            "mandate_id": buyer["mandate"]["id"], "quote_id": quote_id, "lines": quote["lines"],
            "total_eur": quote["total_eur"], "deliver_by": quote["deliver_by"], "agent_reason": reason,
            "is_substitute": is_substitute, "declared_risks": declared_risks, "policy": decision, "status": status,
            "approve_by": _approve_by(quote) if status == "pending_approval" else None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "decided_by": "door (automatic)" if status != "pending_approval" else None,
        }
        quote["used"] = True
        if status == "confirmed":
            order["receipt"] = _receipt(order)
        store.orders[order["order_id"]] = order
    audit(f"agent:{buyer_id}", "place_order", f"Order {order['order_id']} {order['total_eur']} EUR -> {status}",
          reason=reason, data={"order_id": order["order_id"], "decision": decision})
    return order


def get_order(buyer_id: str, order_id: str) -> dict:
    order = store.orders.get(order_id)
    if not order or order["buyer_id"] != buyer_id:
        raise WholesalerError(f"No order {order_id} for this buyer.")
    return order


def _receipt(order: dict) -> str:
    return _sha({k: order[k] for k in ("order_id", "buyer_id", "mandate_id", "lines", "total_eur", "deliver_by", "decided_by")})


# ---------------------------------------------------------------- human side

def pending_orders() -> list[dict]:
    return [o for o in store.orders.values() if o["status"] == "pending_approval"]


def decide(order_id: str, approve: bool, approver: str, note: str = "") -> dict:
    with _lock:
        order = store.orders.get(order_id)
        if not order:
            raise WholesalerError(f"No order {order_id}.")
        if order["status"] != "pending_approval":
            raise WholesalerError(f"Order {order_id} is already {order['status']}.")
        if approve and order["approve_by"] and datetime.now() > datetime.fromisoformat(order["approve_by"]):
            order["warning"] = f"Approved after {order['approve_by']}; delivery by {order['deliver_by']} is no longer guaranteed."
        order["status"] = "confirmed" if approve else "rejected"
        order["decided_by"] = approver
        order["decision_note"] = note
        if approve:
            order["receipt"] = _receipt(order)
    audit(f"human:{approver}", "approve" if approve else "reject",
          f"Order {order_id} {order['status']} by {approver}", reason=note, data={"order_id": order_id})
    return order
