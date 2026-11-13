"""Mock Valio Aimo-style wholesaler: catalog, quotes, orders, policy and audit log.

Everything an external purchasing agent may do goes through the functions in
this module (exposed over HTTP in main.py and over MCP in mcp_server.py).
Internal fields (stock levels beyond what is needed, supplier contracts, the
marketplace roadmap) never leave this module.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QUOTE_VALID_MINUTES = 30

_lock = threading.Lock()


class WholesalerError(Exception):
    """Raised for invalid agent requests; message is safe to show to the agent."""


def today() -> date:
    """Allows a fixed date for repeatable demos via DEMO_TODAY=YYYY-MM-DD."""
    fixed = os.environ.get("DEMO_TODAY")
    return date.fromisoformat(fixed) if fixed else date.today()


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        catalog = _load("catalog.json")
        self.suppliers = {s["id"]: s for s in catalog["suppliers"]}
        self.products = {p["id"]: dict(p) for p in catalog["products"]}
        self.buyers = {b["id"]: b for b in _load("buyers.json")["buyers"]}
        self.quotes: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.audit: list[dict] = []
        self._ids = itertools.count(1)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):04d}"


store = Store()


# ---------------------------------------------------------------- helpers

def audit(actor: str, action: str, summary: str, *, reason: str = "", data: dict | None = None) -> None:
    store.audit.append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "summary": summary,
        "reason": reason,
        "data": data or {},
    })


def buyer_by_key(agent_key: str) -> dict:
    for b in store.buyers.values():
        if b["agent_key"] == agent_key:
            return b
    raise WholesalerError("Unknown agent key.")


def _public(p: dict) -> dict:
    """What an external agent is allowed to see about a product."""
    return {
        "product_id": p["id"],
        "name": p["name"],
        "category": p["category"],
        "unit": p["unit"],
        "unit_price_eur": p["unit_price_eur"],
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


# ---------------------------------------------------------------- agent API

def search_products(query: str = "", category: str | None = None, label: str | None = None,
                    max_results: int = 10, *, actor: str = "agent") -> list[dict]:
    words = [w for w in query.lower().split() if w]
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
    hits.sort(key=lambda t: (-t[0], t[1]["unit_price_eur"]))
    results = [_public(p) for _, p in hits[:max_results]]
    audit(actor, "search_products", f"Searched '{query}' -> {len(results)} results",
          data={"query": query, "category": category, "label": label})
    return results


def check_availability(product_id: str, quantity: float, deliver_by: str | None = None,
                       *, actor: str = "agent") -> dict:
    p = _product(product_id)
    deadline = _parse_date(deliver_by)
    earliest = today() + timedelta(days=p["lead_time_days"])
    available_qty = min(quantity, p["stock"])
    result = {
        **_public(p),
        "requested_qty": quantity,
        "available_qty": available_qty,
        "fully_available": p["stock"] >= quantity,
        "meets_min_order": quantity >= p["min_order_qty"],
        "earliest_delivery": earliest.isoformat(),
        "meets_deadline": deadline is None or earliest <= deadline,
        "line_total_eur": round(available_qty * p["unit_price_eur"], 2),
    }
    audit(actor, "check_availability",
          f"{p['name']} ({p['id']}): {available_qty}/{quantity} {p['unit']} available, "
          f"earliest {earliest}", data={"product_id": product_id, "quantity": quantity})
    return result


def request_quote(buyer_id: str, items: list[dict], deliver_by: str | None = None,
                  *, actor: str = "agent") -> dict:
    buyer = store.buyers[buyer_id]
    deadline = _parse_date(deliver_by)
    if not items:
        raise WholesalerError("A quote needs at least one item.")
    lines, problems = [], []
    for item in items:
        p = _product(item["product_id"])
        qty = float(item["quantity"])
        earliest = today() + timedelta(days=p["lead_time_days"])
        if qty <= 0:
            raise WholesalerError("Quantities must be positive.")
        if qty < p["min_order_qty"]:
            problems.append(f"{p['id']}: below minimum order quantity {p['min_order_qty']} {p['unit']}.")
        if qty > p["stock"]:
            problems.append(f"{p['id']}: only {p['stock']} {p['unit']} in stock.")
        if deadline and earliest > deadline:
            problems.append(f"{p['id']}: earliest delivery {earliest} is after {deadline}.")
        lines.append({
            "product_id": p["id"], "name": p["name"], "category": p["category"],
            "quantity": qty, "unit": p["unit"], "unit_price_eur": p["unit_price_eur"],
            "line_total_eur": round(qty * p["unit_price_eur"], 2),
            "earliest_delivery": earliest.isoformat(),
            "supplier": store.suppliers[p["supplier_id"]]["name"],
        })
    if problems:
        audit(actor, "request_quote", "Quote refused: " + " ".join(problems), data={"items": items})
        raise WholesalerError("Cannot quote: " + " ".join(problems))
    total = round(sum(l["line_total_eur"] for l in lines), 2)
    quote = {
        "quote_id": store.next_id("Q"),
        "buyer_id": buyer_id,
        "lines": lines,
        "total_eur": total,
        "deliver_by": deadline.isoformat() if deadline else None,
        "valid_until": (datetime.now() + timedelta(minutes=QUOTE_VALID_MINUTES)).isoformat(timespec="seconds"),
        "pricing": "fixed list price, no negotiation",
    }
    quote["policy_preview"] = evaluate_policy(buyer, quote, is_substitute=False)
    store.quotes[quote["quote_id"]] = quote
    audit(actor, "request_quote", f"Quote {quote['quote_id']} for {total} EUR "
          f"({len(lines)} line(s)); policy preview: {quote['policy_preview']['decision']}",
          data={"quote_id": quote["quote_id"]})
    return quote


def evaluate_policy(buyer: dict, quote: dict, *, is_substitute: bool) -> dict:
    """Decide what the agent may do on its own. Returns decision + human-readable reasons."""
    pol = buyer["policy"]
    reasons_block, reasons_approval = [], []
    for line in quote["lines"]:
        if line["category"] not in pol["allowed_categories"]:
            reasons_block.append(f"Category '{line['category']}' is not allowed for this agent.")
        lead = (date.fromisoformat(line["earliest_delivery"]) - today()).days
        if lead > pol["max_lead_time_days"]:
            reasons_block.append(f"{line['product_id']} lead time {lead} days exceeds {pol['max_lead_time_days']}.")
    if quote["total_eur"] > pol["hard_limit_eur"]:
        reasons_block.append(f"Total {quote['total_eur']} EUR exceeds hard limit {pol['hard_limit_eur']} EUR.")
    if quote["total_eur"] > pol["auto_approve_limit_eur"]:
        reasons_approval.append(
            f"Total {quote['total_eur']} EUR is above the agent's auto-approve limit "
            f"{pol['auto_approve_limit_eur']} EUR.")
    if is_substitute and pol["substitutes_need_approval"]:
        reasons_approval.append("Order contains a substitute product; substitutes need human sign-off.")
    if reasons_block:
        return {"decision": "blocked", "reasons": reasons_block}
    if reasons_approval:
        return {"decision": "needs_human_approval", "approver": buyer["approver"], "reasons": reasons_approval}
    return {"decision": "agent_may_order", "reasons": ["Within all limits set by the buyer."]}


def place_order(buyer_id: str, quote_id: str, reason: str, is_substitute: bool = False,
                *, actor: str = "agent") -> dict:
    with _lock:
        quote = store.quotes.get(quote_id)
        if not quote or quote["buyer_id"] != buyer_id:
            raise WholesalerError(f"No quote {quote_id} for this buyer.")
        if datetime.fromisoformat(quote["valid_until"]) < datetime.now():
            raise WholesalerError(f"Quote {quote_id} has expired; request a new one.")
        if quote.get("used"):
            raise WholesalerError(f"Quote {quote_id} was already used.")
        if not reason.strip():
            raise WholesalerError("A reason is required so humans can see why the agent ordered.")
        buyer = store.buyers[buyer_id]
        policy = evaluate_policy(buyer, quote, is_substitute=is_substitute)
        status = {"agent_may_order": "confirmed", "needs_human_approval": "pending_approval",
                  "blocked": "rejected"}[policy["decision"]]
        order = {
            "order_id": store.next_id("O"),
            "buyer_id": buyer_id,
            "buyer_name": buyer["name"],
            "quote_id": quote_id,
            "lines": quote["lines"],
            "total_eur": quote["total_eur"],
            "deliver_by": quote["deliver_by"],
            "agent_reason": reason,
            "is_substitute": is_substitute,
            "policy": policy,
            "status": status,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "decided_by": "policy (automatic)" if status != "pending_approval" else None,
        }
        quote["used"] = True
        if status == "confirmed":
            _reserve_stock(order)
        store.orders[order["order_id"]] = order
    audit(actor, "place_order", f"Order {order['order_id']} {order['total_eur']} EUR -> {status}",
          reason=reason, data={"order_id": order["order_id"], "policy": policy})
    return order


def get_order(buyer_id: str, order_id: str, *, actor: str = "agent") -> dict:
    order = store.orders.get(order_id)
    if not order or order["buyer_id"] != buyer_id:
        raise WholesalerError(f"No order {order_id} for this buyer.")
    return order


def _reserve_stock(order: dict) -> None:
    for line in order["lines"]:
        p = store.products[line["product_id"]]
        if p["stock"] < line["quantity"]:
            raise WholesalerError(f"{p['id']} no longer has enough stock.")
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
        order["status"] = "confirmed" if approve else "rejected"
        order["decided_by"] = approver
        order["decision_note"] = note
    audit(f"human:{approver}", "approve" if approve else "reject",
          f"Order {order_id} {order['status']} by {approver}", reason=note,
          data={"order_id": order_id})
    return order
