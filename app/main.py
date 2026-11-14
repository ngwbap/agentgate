"""AgentGate HTTP server: agent API, human API, discovery file, demo endpoints and web UI."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import agent, config, core, llm

WEB_DIR = config.ROOT / "web"

app = FastAPI(
    title="AgentGate - Valio Aimo front door for purchasing agents",
    version="0.2.0",
    description="Open interface through which external purchasing agents look up, quote and order from Valio Aimo "
                "within a buyer-issued mandate. Static data narrows the search; live price and availability are "
                "queried only for the shortlist. The door enforces limits, routes unusual, risky or large orders to a "
                "human, and keeps a tamper-evident audit log. Spec: docs/AGENT_INTERFACE_SPEC.md",
)


@app.exception_handler(core.WholesalerError)
async def _wholesaler_error(_, exc: core.WholesalerError):
    return JSONResponse(status_code=403 if isinstance(exc, core.MandateError) else 400, content={"error": str(exc)})


def current_buyer(x_agent_key: str = Header(..., description="Agent key bound to one buyer's mandate")) -> dict:
    try:
        return core.buyer_by_key(x_agent_key)
    except core.WholesalerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# ---------------------------------------------------------------- discovery

@app.get("/.well-known/agent-interface.json", tags=["discovery"])
def discovery():
    return {
        "name": "Valio Aimo AgentGate", "version": "0.2.0",
        "spec": "docs/AGENT_INTERFACE_SPEC.md", "openapi": "/openapi.json",
        "auth": {"type": "header", "header": "X-Agent-Key",
                 "note": "Each key is bound to one buyer's mandate and can be revoked by the buyer at any time."},
        "transports": {"http": "/api/v1", "mcp": "python -m app.mcp_server (stdio)"},
        "tools": ["get_my_mandate", "smart_lookup", "search_products", "check_availability", "request_quote",
                  "place_order", "get_order_status"],
        "lookup_policy": "Narrow down with static product data first; live price/availability only for the shortlist "
                         "(typically 2 calls for ~8 products), never for the whole catalog. Substitutes are "
                         "checked live (2 more calls) only when the requested product cannot be confirmed.",
        "plans": "smart_lookup returns up to three fulfilment plans (Best / Better / Good), scored "
                 "0.55 x delivery certainty + 0.45 x match fidelity, price as tie-breaker. Dietary claims and "
                 "allergen exclusions are hard rules and apply to substitutes too.",
        "rules": {
            "pricing": "fixed Valio price per customer and date, no negotiation",
            "decisions": ["agent_may_order", "needs_human_approval", "blocked"],
            "human_approval_when": ["total above the mandate's auto-approve limit",
                                    "quantity far above the buyer's usual weekly order",
                                    "supplier order cutoff passed (delivery not guaranteed)", "substitute products",
                                    "any risk the agent declares itself (declared_risks on place_order)"],
            "blocked_when": ["mandate revoked or expired", "total above hard limit", "non-food in a food-only mandate",
                             "lead time beyond the mandate"],
            "every_order_needs": "a human-readable reason",
        },
        "data_policy": "Agents see product data, their own price and availability for the products they ask about; "
                       "never other customers, margins or internal plans. Searches are rate-limited.",
    }


# ---------------------------------------------------------------- agent API (v1)

class LookupRequest(BaseModel):
    request: str


class QuoteItem(BaseModel):
    product_id: str
    quantity: float


class QuoteRequest(BaseModel):
    items: list[QuoteItem]
    deliver_by: str | None = None


class OrderRequest(BaseModel):
    quote_id: str
    reason: str
    is_substitute: bool = False
    declared_risks: list[str] = []   # anything the agent is unsure about; any entry routes the order to a human


@app.get("/api/v1/mandate", tags=["agent"])
def api_mandate(buyer: dict = Depends(current_buyer)):
    return core.public_mandate(buyer)


@app.post("/api/v1/lookup", tags=["agent"])
def api_lookup(body: LookupRequest, buyer: dict = Depends(current_buyer)):
    return core.smart_lookup(buyer["id"], body.request)


@app.get("/api/v1/products", tags=["agent"])
def api_search(q: str = "", buyer: dict = Depends(current_buyer)):
    return core.search_products(buyer["id"], q)


@app.get("/api/v1/products/{product_id}/availability", tags=["agent"])
def api_availability(product_id: str, quantity: float, deliver_by: str | None = None,
                     buyer: dict = Depends(current_buyer)):
    return core.check_availability(buyer["id"], product_id, quantity, deliver_by)


@app.post("/api/v1/quotes", tags=["agent"])
def api_quote(body: QuoteRequest, buyer: dict = Depends(current_buyer)):
    return core.request_quote(buyer["id"], [i.model_dump() for i in body.items], body.deliver_by)


@app.post("/api/v1/orders", tags=["agent"])
def api_order(body: OrderRequest, buyer: dict = Depends(current_buyer)):
    return core.place_order(buyer["id"], body.quote_id, body.reason, body.is_substitute, body.declared_risks)


@app.get("/api/v1/orders/{order_id}", tags=["agent"])
def api_get_order(order_id: str, buyer: dict = Depends(current_buyer)):
    return core.get_order(buyer["id"], order_id)


# ---------------------------------------------------------------- human API
# (In production these sit behind the buyer's own login; open here for the demo.)

class Decision(BaseModel):
    approve: bool
    approver: str
    note: str = ""


class MandateChange(BaseModel):
    by: str


def _buyer_or_404(buyer_id: str) -> dict:
    if buyer_id not in core.store.buyers:
        raise HTTPException(status_code=404, detail="Unknown buyer")
    return core.store.buyers[buyer_id]


@app.get("/api/human/pending", tags=["human"])
def api_pending():
    return core.pending_orders()


@app.post("/api/human/orders/{order_id}/decision", tags=["human"])
def api_decide(order_id: str, body: Decision):
    return core.decide(order_id, body.approve, body.approver, body.note)


@app.post("/api/human/mandates/{buyer_id}/revoke", tags=["human"])
def api_revoke(buyer_id: str, body: MandateChange):
    _buyer_or_404(buyer_id)
    return core.set_mandate_revoked(buyer_id, True, body.by)


@app.post("/api/human/mandates/{buyer_id}/reinstate", tags=["human"])
def api_reinstate(buyer_id: str, body: MandateChange):
    _buyer_or_404(buyer_id)
    return core.set_mandate_revoked(buyer_id, False, body.by)


@app.get("/api/human/audit/verify", tags=["human"])
def api_verify():
    return core.verify_audit()


# ---------------------------------------------------------------- demo / UI

class ChatRequest(BaseModel):
    buyer_id: str
    message: str


class PlanLine(BaseModel):
    product_id: str
    units: float
    match: str = "exact"


class OrderPlanRequest(BaseModel):
    buyer_id: str
    lines: list[PlanLine]
    deliver_by: str
    reason: str
    is_substitute: bool = False
    declared_risks: list[str] = []


class BuyerRequest(BaseModel):
    buyer_id: str


@app.get("/api/demo/buyers", tags=["demo"])
def api_buyers():
    return [core.public_mandate(b) | {"buyer_id": b["id"]} for b in core.store.buyers.values()]


@app.post("/api/demo/lookup", tags=["demo"])
def api_demo_lookup(body: ChatRequest):
    _buyer_or_404(body.buyer_id)
    return agent.lookup(body.buyer_id, body.message)


@app.post("/api/demo/order-plan", tags=["demo"])
def api_order_plan(body: OrderPlanRequest):
    _buyer_or_404(body.buyer_id)
    return agent.order_plan(body.buyer_id, [l.model_dump() for l in body.lines], body.deliver_by, body.reason,
                            body.is_substitute, body.declared_risks)


@app.post("/api/demo/rogue", tags=["demo"])
def api_rogue(body: BuyerRequest):
    _buyer_or_404(body.buyer_id)
    return agent.run_rogue(body.buyer_id)


@app.get("/api/demo/orders", tags=["demo"])
def api_orders():
    return list(reversed(core.store.orders.values()))


@app.get("/api/demo/audit", tags=["demo"])
def api_audit():
    return list(reversed(core.store.audit))


@app.get("/api/demo/status", tags=["demo"])
def api_status():
    return {"ai_model": config.CLAUDE_MODEL, "ai_available": llm.available(), "ai_usage": llm.usage_totals,
            "valio_api": core.store.valio.stats, "valio_available": core.store.valio.available}


@app.post("/api/demo/reset", tags=["demo"])
def api_reset():
    core.store.reset()
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")
