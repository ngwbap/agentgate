"""Demo purchasing agent for the UI.

AgentGate does not depend on this agent: any HTTP or MCP agent (Claude Desktop, Claude Code,
Gemini CLI, a customer's own bot) can use the same door with the same rules. This one runs the
recommended flow: Smart Lookup -> quote the plan -> order with a reason -> the door decides.
"""
from __future__ import annotations

import math

from . import core
from . import understand as und


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except core.WholesalerError as exc:
        return {"error": str(exc)}


ASSISTANT = "valio-assistant"   # Valio's own buying assistant (the web page) is one agent among others


def understand(buyer_id: str, request: str) -> dict:
    """Only read the request into criteria (no search, no live calls), so the buyer can check and change them."""
    try:
        core.check_agent(buyer_id, ASSISTANT)
    except core.WholesalerError as exc:
        return {"error": str(exc)}
    if not request.strip():
        return {"error": "Write what you need first."}
    need = und.understand(request, core.today())
    usage = need.pop("usage", None)
    catalog = core.store.get_catalog()
    for item in need.get("items", []):
        item["usual_unit"] = catalog.usual_unit(item["product_terms"])     # e.g. milk -> L, meat -> KG
    core.audit(f"user:{buyer_id}", "request", request)
    return {"need": need, "ai_usage": {"calls": 1 if usage else 0, "usd": usage["usd"] if usage else 0}}


def unit_check(product_terms: list[str]) -> dict:
    """For the boxes: is this product sold by litres or by kg? Offline, no live calls."""
    terms = [t for t in product_terms if str(t).strip()]
    return core.store.get_catalog().usual_unit(terms) if terms else {"unit": None, "share": 0.0, "products": 0}


def lookup(buyer_id: str, request: str, need: dict | None = None) -> dict:
    try:
        core.check_agent(buyer_id, ASSISTANT)
    except core.WholesalerError as exc:
        return {"error": str(exc)}
    if not need:
        core.audit(f"user:{buyer_id}", "request", request)
    return _safe(core.smart_lookup, buyer_id, request, need)


def quote(buyer_id: str, lines: list[dict], deliver_by: str, is_substitute: bool = False,
          declared_risks: list[str] | None = None) -> dict:
    """Fixed-price quote for the draft order, with the door's verdict BEFORE anything is committed.
    The preview uses what the agent will tell the door (substitute, declared risks), so it matches the real order."""
    try:
        core.check_agent(buyer_id, ASSISTANT)
    except core.WholesalerError as exc:
        return {"error": str(exc)}
    q = _safe(core.request_quote, buyer_id,
              [{"product_id": l["product_id"], "quantity": l["units"]} for l in lines if l["units"] > 0], deliver_by)
    if not q.get("error"):
        q["policy_preview"] = core.evaluate(core.store.buyers[buyer_id], q, is_substitute=is_substitute,
                                            declared_risks=declared_risks)
    return q


def order(buyer_id: str, quote_id: str, reason: str, is_substitute: bool, declared_risks: list[str],
          notes: list[str] | None = None) -> dict:
    try:
        core.check_agent(buyer_id, ASSISTANT)
    except core.WholesalerError as exc:
        return {"error": str(exc)}
    return _safe(core.place_order, buyer_id, quote_id, reason, is_substitute, declared_risks, notes)


def order_plan(buyer_id: str, lines: list[dict], deliver_by: str, reason: str, is_substitute: bool = False,
               declared_risks: list[str] | None = None) -> dict:
    """Quote the chosen plan lines and place the order; the door decides what happens.

    An honest agent tells the door what it knows: a line that is not exactly what was asked is a
    substitute, and the plan's own risks (approval_needed) are passed on as declared risks.
    """
    quote = _safe(core.request_quote, buyer_id,
                  [{"product_id": l["product_id"], "quantity": l["units"]} for l in lines], deliver_by)
    if quote.get("error"):
        return {"quote": quote, "order": None}
    is_substitute = is_substitute or any(l.get("match", "exact") != "exact" for l in lines)
    order = _safe(core.place_order, buyer_id, quote["quote_id"], reason, is_substitute, declared_risks or [])
    return {"quote": quote, "order": order}


def run_rogue(buyer_id: str) -> dict:
    """A confused or hijacked agent tries to break the rules. The door must stop every attempt."""
    core.audit(f"user:{buyer_id}", "request", "DEMO: simulate a misbehaving agent")
    buyer = core.store.buyers[buyer_id]
    cat = core.store.get_catalog()
    steps = []

    def attempt(label, fn, *args):
        result = _safe(fn, *args)
        if isinstance(result, dict) and result.get("quote_id"):
            result = _safe(core.place_order, buyer_id, result["quote_id"], "Agent decided to stock up.")
        steps.append({"attempt": label, "result": result})

    # Pick a product with enough supply, so the spending limit (not stock) is what stops the agent.
    for p in cat.search(["milk", "maito"], must_claims=["FREE_FROM_LACTOSE"], content_unit="L", limit=8)["candidates"]:
        info = _safe(core.check_availability, buyer_id, p["id"], 1)
        if info.get("error") or not info.get("unit_price_eur") or info.get("sales_blocked"):
            continue
        units = math.ceil(buyer["mandate"]["hard_limit_eur"] * 1.3 / info["unit_price_eur"])
        if info["available_units"] is None or info["available_units"] >= units:
            attempt(f"Order {units} {p['sales_unit']} of {p['name']} (far above the hard limit)",
                    core.request_quote, buyer_id, [{"product_id": p["id"], "quantity": units}])
            break
    # Find a real non-food item (VAT 25.5 %) - names alone can mislead ("rice wine vinegar" is food).
    nonfood = None
    for terms in (["viini", "wine"], ["olut", "beer"], ["astianpesu", "dishwashing"], ["lautasliina", "napkin"]):
        for p in cat.search(terms, limit=6)["candidates"]:
            info = _safe(core.check_availability, buyer_id, p["id"], 1)
            if (info.get("tax_rate") or 0) >= core.NON_FOOD_TAX_RATE and not info.get("sales_blocked"):
                nonfood = p
                break
        if nonfood:
            break
    if nonfood:
        attempt(f"Buy {nonfood['name']} (not food: outside a food-only mandate)",
                core.request_quote, buyer_id, [{"product_id": nonfood["id"], "quantity": 1}])
    attempt("Order an invented product id P-9999", core.request_quote, buyer_id, [{"product_id": "P-9999", "quantity": 10}])
    outcomes = [(s["result"].get("status") if isinstance(s["result"], dict) else None) or
                ("refused" if isinstance(s["result"], dict) and s["result"].get("error") else "?") for s in steps]
    return {"steps": steps, "answer": "Every attempt was stopped by the door: " + ", ".join(outcomes) + "."}
