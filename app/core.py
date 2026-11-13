"""AgentGate core: Valio Aimo's front door for external purchasing agents.

Everything an external agent may do goes through this module (exposed over HTTP
in main.py and over MCP in mcp_server.py). The door - not the agent - enforces:

* the buyer's MANDATE: spending limits, allowed categories, validity, revoke switch
* the UNUSUAL-ORDER GUARD: orders far above the buyer's normal pattern go to a human
* NEED-TO-KNOW data: each buyer sees only its own contract prices; no exact stock,
  no other customers, no internal data; bulk scraping is throttled
* a TAMPER-EVIDENT audit log (hash chain) and a verifiable receipt per order
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QUOTE_VALID_MINUTES = 30
ORDER_CUTOFF_HOUR = 14          # orders must be confirmed by 14:00 to ship that day
SEARCH_LIMIT_PER_MINUTE = 30    # anti-scraping throttle per buyer

# Finnish -> catalog terms, so agents can ask in Finnish ("laktoositonta maitoa").
SYNONYMS = {
    "maito": "milk", "maitoa": "milk", "maidon": "milk",
    "laktoositon": "lactose-free", "laktoositonta": "lactose-free",
    "kaura": "oat", "kaurajuoma": "oat", "kaurajuomaa": "oat",
    "kerma": "cream", "kermaa": "cream", "voi": "butter", "voita": "butter",
    "juusto": "cheese", "juustoa": "cheese", "jogurtti": "yoghurt", "jogurttia": "yoghurt",
    "kananmuna": "eggs", "kananmunia": "eggs", "munat": "eggs",
    "peruna": "potatoes", "perunaa": "potatoes", "tomaatti": "tomatoes", "tomaatteja": "tomatoes",
    "kahvi": "coffee", "kahvia": "coffee", "luomu": "organic",
}

_lock = threading.Lock()


class WholesalerError(Exception):
    """Invalid agent request; the message is safe to show to the agent."""


class MandateError(WholesalerError):
    """The agent has no valid mandate (revoked or expired)."""


def today() -> date:
    """DEMO_TODAY=YYYY-MM-DD fixes the date so demos are repeatable."""
    fixed = os.environ.get("DEMO_TODAY")
    return date.fromisoformat(fixed) if fixed else date.today()


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        catalog = _load("catalog.json")
        self.suppliers = {s["id"]: s for s in catalog["suppliers"]}
        self.products = {p["id"]: dict(p) for p in catalog["products"]}
        self.buyers = {b["id"]: json.loads(json.dumps(b)) for b in _load("buyers.json")["buyers"]}
        self.quotes: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.search_times: dict[str, list[float]] = {}
        self._ids = itertools.count(1)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"


store = Store()


# ---------------------------------------------------------------- audit (hash chain)

def audit(actor: str, action: str, summary: str, *, reason: str = "", data: dict | None = None) -> None:
    entry = {
        "seq": len(store.audit) + 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "summary": summary,
        "reason": reason,
        "data": data or {},
        "prev_hash": store.audit[-1]["hash"] if store.audit else "0" * 64,
    }
    entry["hash"] = _sha(entry)
    store.audit.append(entry)


def verify_audit() -> dict:
    """Recompute the chain; any edited or deleted entry breaks it."""
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
    """What the agent (and the buyer) can read about the agent's own authority."""
    return {"buyer": buyer["name"], "approver": buyer["approver"], **buyer["mandate"]}


def set_mandate_revoked(buyer_id: str, revoked: bool, by: str) -> dict:
    buyer = store.buyers[buyer_id]
    buyer["mandate"]["revoked"] = revoked
    audit(f"human:{by}", "revoke_mandate" if revoked else "reinstate_mandate",
          f"Mandate {buyer['mandate']['id']} {'REVOKED' if revoked else 'reinstated'} for {buyer['name']}")
    return public_mandate(buyer)


def price_for(buyer: dict, p: dict) -> float:
    return buyer.get("contract_prices", {}).get(p["id"], p["unit_price_eur"])


# ---------------------------------------------------------------- helpers

def _public(p: dict, buyer: dict) -> dict:
    """Need-to-know view of a product for one buyer. No exact stock, no other prices."""
    contract = p["id"] in buyer.get("contract_prices", {})
    return {
        "product_id": p["id"],
        "name": p["name"],
        "category": p["category"],
        "unit": p["unit"],
        "unit_price_eur": price_for(buyer, p),
        "price_basis": "your contract price" if contract else "list price",
        "min_order_qty": p["min_order_qty"],
        "in_stock": p["stock"] > 0,
        "lead_time_days": p["lead_time_days"],
        "supplier": store.suppliers[p["supplier_id"]]["name"],
        "labels": p["labels"],
        "co2_kg_per_unit": p["co2_kg_per_unit"],
    }


def _product(product_id: str) -> dict:
    p = store.products.get(product_id)
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


def normalize_terms(text: str) -> list[str]:
    return [SYNONYMS.get(w, w) for w in text.lower().split() if w]


def _throttle(buyer_id: str) -> None:
    now = time.monotonic()
    recent = [t for t in store.search_times.get(buyer_id, []) if now - t < 60]
    if len(recent) >= SEARCH_LIMIT_PER_MINUTE:
        raise WholesalerError("Too many searches in one minute (anti-scraping limit). Slow down.")
    recent.append(now)
    store.search_times[buyer_id] = recent


# ---------------------------------------------------------------- agent API

def search_products(buyer_id: str, query: str = "", category: str | None = None,
                    label: str | None = None, max_results: int = 10) -> list[dict]:
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    _throttle(buyer_id)
    words = normalize_terms(query)
    hits = []
    for p in store.products.values():
        haystack = " ".join([p["name"], p["category"], *p["labels"]]).lower()
        if category and p["category"] != category:
            continue
        if label and label.lower() not in [l.lower() for l in p["labels"]]:
            continue
        score = sum(w in haystack for w in words) if words else 1
        if score:
            hits.append((score, p))
    hits.sort(key=lambda t: (-t[0], price_for(buyer, t[1])))
    results = [_public(p, buyer) for _, p in hits[:max_results]]
    audit(f"agent:{buyer_id}", "search_products", f"Searched '{query}' -> {len(results)} results",
          data={"query": query, "category": category, "label": label})
    return results


def check_availability(buyer_id: str, product_id: str, quantity: float,
                       deliver_by: str | None = None) -> dict:
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    p = _product(product_id)
    deadline = _parse_date(deliver_by)
    earliest = today() + timedelta(days=p["lead_time_days"])
    available_qty = min(quantity, p["stock"])   # never reveals more stock than asked for
    price = price_for(buyer, p)
    result = {
        **_public(p, buyer),
        "requested_qty": quantity,
        "available_qty": available_qty,
        "fully_available": p["stock"] >= quantity,
        "meets_min_order": quantity >= p["min_order_qty"],
        "earliest_delivery": earliest.isoformat(),
        "meets_deadline": deadline is None or earliest <= deadline,
        "line_total_eur": round(available_qty * price, 2),
    }
    audit(f"agent:{buyer_id}", "check_availability",
          f"{p['name']} ({p['id']}): {available_qty:g}/{quantity:g} {p['unit']} available, earliest {earliest}",
          data={"product_id": product_id, "quantity": quantity})
    return result


def request_quote(buyer_id: str, items: list[dict], deliver_by: str | None = None) -> dict:
    buyer = store.buyers[buyer_id]
    check_mandate(buyer)
    deadline = _parse_date(deliver_by)
    if not items:
        raise WholesalerError("A quote needs at least one item.")
    lines, problems = [], []
    for item in items:
        p = _product(item["product_id"])
        qty = float(item["quantity"])
        if qty <= 0:
            raise WholesalerError("Quantities must be positive.")
        earliest = today() + timedelta(days=p["lead_time_days"])
        if qty < p["min_order_qty"]:
            problems.append(f"{p['id']}: below minimum order quantity {p['min_order_qty']} {p['unit']}.")
        if qty > p["stock"]:
            problems.append(f"{p['id']}: not enough stock for {qty:g} {p['unit']}.")
        if deadline and earliest > deadline:
            problems.append(f"{p['id']}: earliest delivery {earliest} is after {deadline}.")
        price = price_for(buyer, p)
        lines.append({
            "product_id": p["id"], "name": p["name"], "category": p["category"],
            "quantity": qty, "unit": p["unit"], "unit_price_eur": price,
            "line_total_eur": round(qty * price, 2),
            "lead_time_days": p["lead_time_days"],
            "earliest_delivery": earliest.isoformat(),
            "supplier": store.suppliers[p["supplier_id"]]["name"],
        })
    if problems:
        audit(f"agent:{buyer_id}", "request_quote", "Quote refused: " + " ".join(problems), data={"items": items})
        raise WholesalerError("Cannot quote: " + " ".join(problems))
    total = round(sum(l["line_total_eur"] for l in lines), 2)
    quote = {
        "quote_id": store.next_id("Q"),
        "buyer_id": buyer_id,
        "lines": lines,
        "total_eur": total,
        "deliver_by": deadline.isoformat() if deadline else None,
        "valid_until": (datetime.now() + timedelta(minutes=QUOTE_VALID_MINUTES)).isoformat(timespec="seconds"),
        "pricing": "fixed price, no negotiation",
    }
    quote["policy_preview"] = evaluate(buyer, quote, is_substitute=False)
    store.quotes[quote["quote_id"]] = quote
    audit(f"agent:{buyer_id}", "request_quote",
          f"Quote {quote['quote_id']} for {total} EUR; door says: {quote['policy_preview']['decision']}",
          data={"quote_id": quote["quote_id"]})
    return quote


def evaluate(buyer: dict, quote: dict, *, is_substitute: bool) -> dict:
    """The door's decision: may the agent order alone, does a human need to approve, or is it blocked?"""
    m = buyer["mandate"]
    block, approve = [], []
    for line in quote["lines"]:
        if line["category"] not in m["allowed_categories"]:
            block.append(f"Category '{line['category']}' is outside the mandate.")
        if line["lead_time_days"] > m["max_lead_time_days"]:
            block.append(f"{line['product_id']} lead time {line['lead_time_days']} days exceeds the mandate's {m['max_lead_time_days']}.")
        usual = buyer.get("usual_weekly_qty", {}).get(line["product_id"])
        if usual and line["quantity"] > m["unusual_order_factor"] * usual:
            approve.append(
                f"Unusual order: {line['quantity']:g} {line['unit']} of {line['product_id']} is "
                f"{line['quantity'] / usual:.1f}x the usual weekly {usual:g} {line['unit']}.")
    if quote["total_eur"] > m["hard_limit_eur"]:
        block.append(f"Total {quote['total_eur']} EUR exceeds the mandate's hard limit {m['hard_limit_eur']} EUR.")
    if quote["total_eur"] > m["auto_approve_limit_eur"]:
        approve.append(f"Total {quote['total_eur']} EUR is above the agent's own limit {m['auto_approve_limit_eur']} EUR.")
    if is_substitute and m["substitutes_need_approval"]:
        approve.append("Contains a substitute product; substitutes need human sign-off.")
    if block:
        return {"decision": "blocked", "reasons": block}
    if approve:
        return {"decision": "needs_human_approval", "approver": buyer["approver"], "reasons": approve}
    return {"decision": "agent_may_order", "reasons": ["Within the mandate: under the spending limit, allowed categories, normal quantities."]}


def _approve_by(lines: list[dict], deliver_by: str | None) -> str | None:
    """Latest moment a human can approve and still make the delivery date."""
    if not deliver_by:
        return None
    latest_day = date.fromisoformat(deliver_by) - timedelta(days=max(l["lead_time_days"] for l in lines))
    return datetime(latest_day.year, latest_day.month, latest_day.day, ORDER_CUTOFF_HOUR).isoformat(timespec="minutes")


def place_order(buyer_id: str, quote_id: str, reason: str, is_substitute: bool = False) -> dict:
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
        decision = evaluate(buyer, quote, is_substitute=is_substitute)
        status = {"agent_may_order": "confirmed", "needs_human_approval": "pending_approval",
                  "blocked": "rejected"}[decision["decision"]]
        order = {
            "order_id": store.next_id("O"),
            "buyer_id": buyer_id,
            "buyer_name": buyer["name"],
            "mandate_id": buyer["mandate"]["id"],
            "quote_id": quote_id,
            "lines": quote["lines"],
            "total_eur": quote["total_eur"],
            "deliver_by": quote["deliver_by"],
            "agent_reason": reason,
            "is_substitute": is_substitute,
            "policy": decision,
            "status": status,
            "approve_by": _approve_by(quote["lines"], quote["deliver_by"]) if status == "pending_approval" else None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "decided_by": "door (automatic)" if status != "pending_approval" else None,
        }
        quote["used"] = True
        if status == "confirmed":
            _reserve_stock(order)
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
    core_fields = {k: order[k] for k in ("order_id", "buyer_id", "mandate_id", "lines", "total_eur", "deliver_by", "decided_by")}
    return _sha(core_fields)


def _reserve_stock(order: dict) -> None:
    for line in order["lines"]:
        if store.products[line["product_id"]]["stock"] < line["quantity"]:
            raise WholesalerError(f"{line['product_id']} no longer has enough stock.")
    for line in order["lines"]:
        store.products[line["product_id"]]["stock"] -= line["quantity"]


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
        if approve:
            _reserve_stock(order)
            if order["approve_by"] and datetime.now() > datetime.fromisoformat(order["approve_by"]):
                order["warning"] = f"Approved after the cutoff {order['approve_by']}; delivery by {order['deliver_by']} is no longer guaranteed."
        order["status"] = "confirmed" if approve else "rejected"
        order["decided_by"] = approver
        order["decision_note"] = note
        if approve:
            order["receipt"] = _receipt(order)
    audit(f"human:{approver}", "approve" if approve else "reject",
          f"Order {order_id} {order['status']} by {approver}", reason=note, data={"order_id": order_id})
    return order
