"""Demo buyer-side purchasing agent.

AgentGate itself does not depend on this agent: any MCP or HTTP agent can use the
door. This one exists to demo the flow. It uses Gemini function calling when
GEMINI_API_KEY is set, otherwise a small deterministic planner so the demo always
works offline. Every step is returned as a transcript the UI can show.
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta

from . import core

TOOLS = [
    {
        "name": "get_my_mandate",
        "description": "Read your own mandate: spending limits, allowed categories, who approves, validity.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "search_products",
        "description": "Search the wholesaler catalog (English or Finnish). Returns products with your price, unit, stock flag, lead time, supplier and labels.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text, e.g. 'lactose-free milk' or 'laktoositonta maitoa'"},
                "category": {"type": "string"},
                "label": {"type": "string"},
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
                "deliver_by": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["product_id", "quantity"],
        },
    },
    {
        "name": "request_quote",
        "description": "Get a fixed-price quote. The result's policy_preview says whether you may order alone, need human approval, or are blocked.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {
                    "product_id": {"type": "string"}, "quantity": {"type": "number"}},
                    "required": ["product_id", "quantity"]}},
                "deliver_by": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "place_order",
        "description": "Order from a quote, with a short human-readable reason. The door decides: confirmed, pending human approval, or rejected.",
        "parameters": {
            "type": "object",
            "properties": {
                "quote_id": {"type": "string"},
                "reason": {"type": "string"},
                "is_substitute": {"type": "boolean"},
            },
            "required": ["quote_id", "reason"],
        },
    },
]


def _system_prompt(buyer: dict) -> str:
    m = buyer["mandate"]
    return (
        f"You are the purchasing agent for {buyer['name']}. Today is {core.today().isoformat()} "
        f"({core.today().strftime('%A')}). You buy from Valio Aimo through its AgentGate tools.\n"
        "Process: search -> check availability -> request a quote -> place the order with a clear reason.\n"
        "Prefer options that fully meet quantity and deadline, then lowest total price. If the exact product "
        "is impossible, propose the closest substitute with is_substitute=true.\n"
        f"Your mandate: up to {m['auto_approve_limit_eur']} EUR alone; above that the {buyer['approver']} approves; "
        f"never above {m['hard_limit_eur']} EUR. The door enforces this anyway.\n"
        "Never invent product ids or prices. Convert relative dates like 'by Monday' to YYYY-MM-DD. "
        "Answer in the user's language. Finish with: what you ordered, total, delivery date, order status and why."
    )


def _call_tool(buyer_id: str, name: str, args: dict):
    if name == "get_my_mandate":
        return core.public_mandate(core.store.buyers[buyer_id])
    if name == "search_products":
        return core.search_products(buyer_id, args.get("query", ""), args.get("category"), args.get("label"))
    if name == "check_availability":
        return core.check_availability(buyer_id, args["product_id"], float(args["quantity"]), args.get("deliver_by"))
    if name == "request_quote":
        return core.request_quote(buyer_id, args["items"], args.get("deliver_by"))
    if name == "place_order":
        return core.place_order(buyer_id, args["quote_id"], args.get("reason", ""), bool(args.get("is_substitute", False)))
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


# ---------------------------------------------------------------- offline planner

_WEEKDAYS = [("monday", "maanantai"), ("tuesday", "tiistai"), ("wednesday", "keskiviikko"),
             ("thursday", "torstai"), ("friday", "perjantai"), ("saturday", "lauantai"), ("sunday", "sunnuntai")]
_STOP = {"i", "need", "we", "please", "buy", "order", "get", "of", "by", "the", "a", "an", "for", "find",
         "option", "that", "meets", "our", "requirements", "based", "on", "price", "and", "availability",
         "litres", "liters", "l", "kg", "pcs", "units", "next", "before", "until",
         "tarvitsen", "tarvitaan", "litraa", "kiloa", "mennessä", "ensi", "tilaa", "kpl"}


def _parse_deadline(text: str) -> str | None:
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    for i, names in enumerate(_WEEKDAYS):
        if any(re.search(rf"\b{n}", text, re.I) for n in names):
            delta = (i - core.today().weekday()) % 7 or 7
            return (core.today() + timedelta(days=delta)).isoformat()
    return None


def _is_weekday(word: str) -> bool:
    return any(word.startswith(n) for pair in _WEEKDAYS for n in pair)


def _run_scripted(buyer: dict, request: str) -> dict:
    steps = []

    def step(name, args):
        result = _safe_call(buyer["id"], name, args)
        steps.append({"tool": name, "args": args, "result": result})
        return result

    def done(answer):
        return {"engine": "offline planner", "steps": steps, "answer": answer}

    qty_match = re.search(r"(\d[\d\s,.]*)", request)
    quantity = float(re.sub(r"[\s,]", "", qty_match.group(1)).rstrip(".")) if qty_match else 1.0
    deadline = _parse_deadline(request)
    raw = [w for w in re.findall(r"[a-zA-ZäöåÄÖÅ-]+", request.lower()) if w not in _STOP and not _is_weekday(w)]
    query = " ".join(raw)
    words = core.normalize_terms(query)

    mandate = step("get_my_mandate", {})
    products = step("search_products", {"query": query})
    if isinstance(products, dict) and products.get("error"):
        return done(products["error"])
    if not products:
        return done(f"No products found for '{query}'.")

    def score(p):
        text = (p["name"] + " " + " ".join(p["labels"])).lower()
        return sum(w in text for w in words)

    best = max(score(p) for p in products)
    exact = [p for p in products if score(p) == best]
    checks = [step("check_availability", {"product_id": p["product_id"], "quantity": quantity,
                                          "deliver_by": deadline}) for p in exact[:4]]
    viable = [c for c in checks if not c.get("error") and c["meets_deadline"]]

    full = sorted([c for c in viable if c["fully_available"] and c["meets_min_order"]],
                  key=lambda c: c["line_total_eur"])
    if full:
        c = full[0]
        items = [{"product_id": c["product_id"], "quantity": quantity}]
        others = len(checks) - 1
        reason = (f"Cheapest option that covers all {quantity:g} {c['unit']} by {deadline or 'the requested date'}: "
                  f"{c['name']} from {c['supplier']} at {c['unit_price_eur']} EUR/{c['unit']} ({c['price_basis']}). "
                  f"Compared {others} other option(s); they lacked stock or missed the deadline.")
    else:
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
            return done("No product combination meets the quantity and deadline. A human should choose a substitute.")
        reason = "No single supplier could cover the order in time, so it is split: " + ", ".join(used) + "."

    quote = step("request_quote", {"items": items, "deliver_by": deadline})
    if quote.get("error"):
        return done(quote["error"])
    order = step("place_order", {"quote_id": quote["quote_id"], "reason": reason})
    if order.get("error"):
        return done(order["error"])
    status_text = {
        "confirmed": "confirmed automatically (within my mandate)",
        "pending_approval": f"waiting for the {buyer['approver']} (approve by {order['approve_by']})",
        "rejected": "rejected by the door",
    }[order["status"]]
    lines = ", ".join(f"{l['quantity']:g} {l['unit']} {l['name']} ({l['supplier']})" for l in order["lines"])
    return done(f"Order {order['order_id']}: {lines}. Total {order['total_eur']} EUR, delivery by "
                f"{order['deliver_by']}. Status: {status_text}. Why: {' '.join(order['policy']['reasons'])}")


# ---------------------------------------------------------------- misbehaving agent

def run_rogue(buyer_id: str) -> dict:
    """Simulates a confused or hijacked agent trying to break the rules. The door must stop every attempt."""
    core.audit(f"user:{buyer_id}", "request", "DEMO: simulate a misbehaving agent")
    attempts = [
        ("Huge order far above the hard limit",
         "request_quote", {"items": [{"product_id": "P-5001", "quantity": 500}, {"product_id": "P-1005", "quantity": 2000}]}),
        ("Category the buyer never allowed (meat)",
         "request_quote", {"items": [{"product_id": "P-3001", "quantity": 4}]}),
        ("Invented product id", "check_availability", {"product_id": "P-9999", "quantity": 10}),
    ]
    steps = []
    for label, tool, args in attempts:
        result = _safe_call(buyer_id, tool, args)
        if tool == "request_quote" and not result.get("error"):
            result = _safe_call(buyer_id, "place_order", {"quote_id": result["quote_id"],
                                                          "reason": "Agent decided to stock up."})
        steps.append({"tool": f"{tool}  [{label}]", "args": args, "result": result})
    outcome = [s["result"].get("status") or ("refused" if s["result"].get("error") else "?") for s in steps]
    return {"engine": "misbehaving agent (simulation)", "steps": steps,
            "answer": "Every attempt was stopped by the door: " + ", ".join(outcome) + "."}
