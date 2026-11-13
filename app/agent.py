"""Buyer-side purchasing agent.

Uses Gemini function calling when GEMINI_API_KEY is set; otherwise falls back
to a small deterministic planner so the demo always works offline.
Every step is returned as a transcript the UI can show.
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta

from . import core

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the wholesaler catalog. Returns products with price, unit, stock flag, lead time, supplier and labels.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search, e.g. 'lactose-free milk'"},
                "category": {"type": "string", "description": "Optional category filter, e.g. 'dairy'"},
                "label": {"type": "string", "description": "Optional label filter, e.g. 'organic'"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_availability",
        "description": "Check stock, price and earliest delivery for one product and quantity.",
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity": {"type": "number"},
                "deliver_by": {"type": "string", "description": "Deadline as YYYY-MM-DD"},
            },
            "required": ["product_id", "quantity"],
        },
    },
    {
        "name": "request_quote",
        "description": "Get a binding fixed-price quote for one or more lines. The result includes policy_preview telling whether you may order alone or need human approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"product_id": {"type": "string"}, "quantity": {"type": "number"}},
                        "required": ["product_id", "quantity"],
                    },
                },
                "deliver_by": {"type": "string", "description": "Deadline as YYYY-MM-DD"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "place_order",
        "description": "Place an order from a quote. Orders above your authority automatically go to a human for approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
                "reason": {"type": "string", "description": "Short human-readable explanation of why this option was chosen"},
                "is_substitute": {"type": "boolean", "description": "True if this is not exactly what the user asked for"},
            },
            "required": ["quote_id", "reason"],
        },
    },
]


def _system_prompt(buyer: dict) -> str:
    pol = buyer["policy"]
    return (
        f"You are the purchasing agent for {buyer['name']}. Today is {core.today().isoformat()} "
        f"({core.today().strftime('%A')}). You buy from a food-service wholesaler using the tools.\n"
        "Process: search -> check availability -> request a quote -> place the order with a clear reason.\n"
        "Prefer options that fully meet quantity and deadline, then lowest total price. "
        "You may split one need across several products/suppliers in a single quote if one cannot cover it. "
        "If the exact product is impossible, propose the closest substitute and set is_substitute=true.\n"
        f"Your authority: orders up to {pol['auto_approve_limit_eur']} EUR are automatic; above that a "
        f"{buyer['approver']} must approve; never exceed {pol['hard_limit_eur']} EUR. "
        "Never invent product ids or prices. Convert relative dates like 'by Monday' to YYYY-MM-DD.\n"
        "Finish with a short summary for the user: what you ordered, total, delivery date, and order status."
    )


def _call_tool(buyer_id: str, name: str, args: dict):
    actor = f"agent:{buyer_id}"
    if name == "search_products":
        return core.search_products(args.get("query", ""), args.get("category"), args.get("label"), actor=actor)
    if name == "check_availability":
        return core.check_availability(args["product_id"], float(args["quantity"]), args.get("deliver_by"), actor=actor)
    if name == "request_quote":
        return core.request_quote(buyer_id, args["items"], args.get("deliver_by"), actor=actor)
    if name == "place_order":
        return core.place_order(buyer_id, args["quote_id"], args.get("reason", ""),
                                bool(args.get("is_substitute", False)), actor=actor)
    raise core.WholesalerError(f"Unknown tool {name}")


def _safe_call(buyer_id: str, name: str, args: dict):
    try:
        return _call_tool(buyer_id, name, args)
    except core.WholesalerError as exc:
        return {"error": str(exc)}


def run(buyer_id: str, request: str) -> dict:
    buyer = core.store.buyers[buyer_id]
    core.audit(f"user:{buyer_id}", "request", request)
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return _run_gemini(buyer, request)
    return _run_scripted(buyer, request)


# ---------------------------------------------------------------- Gemini

def _run_gemini(buyer: dict, request: str, max_steps: int = 12) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    config = types.GenerateContentConfig(
        system_instruction=_system_prompt(buyer),
        tools=[types.Tool(function_declarations=TOOLS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0,
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=request)])]
    steps = []
    for _ in range(max_steps):
        resp = client.models.generate_content(model=model, contents=contents, config=config)
        calls = resp.function_calls or []
        if not calls:
            return {"engine": f"gemini:{model}", "steps": steps, "answer": resp.text or ""}
        contents.append(resp.candidates[0].content)
        parts = []
        for call in calls:
            args = dict(call.args or {})
            result = _safe_call(buyer["id"], call.name, args)
            steps.append({"tool": call.name, "args": args, "result": result})
            parts.append(types.Part.from_function_response(
                name=call.name, response={"result": json.loads(json.dumps(result, default=str))}))
        contents.append(types.Content(role="user", parts=parts))
    return {"engine": f"gemini:{model}", "steps": steps, "answer": "Stopped: too many steps."}


# ---------------------------------------------------------------- offline fallback

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_STOP = {"i", "need", "we", "please", "buy", "order", "get", "of", "by", "the", "a", "an", "for",
         "find", "option", "that", "meets", "our", "requirements", "based", "on", "price", "and",
         "availability", "litres", "liters", "l", "kg", "pcs", "units", "next", "before", "until"}


def _parse_deadline(text: str) -> str | None:
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    for i, day in enumerate(_WEEKDAYS):
        if re.search(rf"\b{day}\b", text, re.I):
            delta = (i - core.today().weekday()) % 7 or 7
            return (core.today() + timedelta(days=delta)).isoformat()
    return None


def _run_scripted(buyer: dict, request: str) -> dict:
    steps = []

    def step(name, args):
        result = _safe_call(buyer["id"], name, args)
        steps.append({"tool": name, "args": args, "result": result})
        return result

    qty_match = re.search(r"(\d[\d\s,.]*)", request)
    quantity = float(re.sub(r"[\s,]", "", qty_match.group(1))) if qty_match else 1.0
    deadline = _parse_deadline(request)
    words = [w for w in re.findall(r"[a-zA-Z-]+", request.lower())
             if w not in _STOP and w not in _WEEKDAYS]
    query = " ".join(words)

    products = step("search_products", {"query": query})
    if not products:
        return {"engine": "scripted", "steps": steps, "answer": f"No products found for '{query}'."}
    best_score = max(sum(w in (p["name"] + " " + " ".join(p["labels"])).lower() for w in words)
                     for p in products)
    exact = [p for p in products
             if sum(w in (p["name"] + " " + " ".join(p["labels"])).lower() for w in words) == best_score]

    checks = [step("check_availability", {"product_id": p["product_id"], "quantity": quantity,
                                          "deliver_by": deadline}) for p in exact[:4]]
    viable = [c for c in checks if c["meets_deadline"] and not c.get("error")]

    # 1) one product covers everything, cheapest first
    full = sorted([c for c in viable if c["fully_available"] and c["meets_min_order"]],
                  key=lambda c: c["line_total_eur"])
    items, reason = None, ""
    if full:
        c = full[0]
        items = [{"product_id": c["product_id"], "quantity": quantity}]
        reason = (f"Cheapest option that covers all {quantity:g} {c['unit']} by {deadline or 'the requested date'}: "
                  f"{c['name']} from {c['supplier']} at {c['unit_price_eur']} EUR/{c['unit']}.")
    else:
        # 2) split across cheapest products meeting the deadline
        remaining, items, used = quantity, [], []
        for c in sorted(viable, key=lambda c: c["unit_price_eur"]):
            take = min(remaining, c["available_qty"])
            if take >= c["min_order_qty"]:
                items.append({"product_id": c["product_id"], "quantity": take})
                used.append(f"{take:g} from {c['supplier']}")
                remaining -= take
            if remaining <= 0:
                break
        if remaining > 0 or not items:
            return {"engine": "scripted", "steps": steps,
                    "answer": "No combination of products meets the quantity and deadline. A human should decide on a substitute."}
        reason = "No single supplier could cover the order in time, so it is split: " + ", ".join(used) + "."

    quote = step("request_quote", {"items": items, "deliver_by": deadline})
    if quote.get("error"):
        return {"engine": "scripted", "steps": steps, "answer": quote["error"]}
    order = step("place_order", {"quote_id": quote["quote_id"], "reason": reason, "is_substitute": False})
    if order.get("error"):
        return {"engine": "scripted", "steps": steps, "answer": order["error"]}
    status_text = {
        "confirmed": "confirmed automatically (within my authority)",
        "pending_approval": f"waiting for approval by the {buyer['approver']}",
        "rejected": "rejected by policy",
    }[order["status"]]
    answer = (f"Order {order['order_id']}: {', '.join(f'{l['quantity']:g} {l['unit']} {l['name']} ({l['supplier']})' for l in order['lines'])}. "
              f"Total {order['total_eur']} EUR, delivery by {order['deliver_by']}. Status: {status_text}. "
              f"Why: {' '.join(order['policy']['reasons'])}")
    return {"engine": "scripted", "steps": steps, "answer": answer}
