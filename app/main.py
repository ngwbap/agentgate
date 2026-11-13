"""AgentGate HTTP server: agent API, human API, discovery file, demo endpoints and web UI."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import agent, core

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

app = FastAPI(
    title="AgentGate - Valio Aimo front door for purchasing agents",
    version="0.1.0",
    description="Open interface through which external purchasing agents can search, quote and order "
                "within a buyer-issued mandate. The door enforces limits, routes unusual or large orders "
                "to a human, and keeps a tamper-evident audit log. Spec: docs/AGENT_INTERFACE_SPEC.md",
)


@app.exception_handler(core.WholesalerError)
async def _wholesaler_error(_, exc: core.WholesalerError):
    status = 403 if isinstance(exc, core.MandateError) else 400
    return JSONResponse(status_code=status, content={"error": str(exc)})


def current_buyer(x_agent_key: str = Header(..., description="Agent key bound to one buyer's mandate")) -> dict:
    try:
        return core.buyer_by_key(x_agent_key)
    except core.WholesalerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


# ---------------------------------------------------------------- discovery

@app.get("/.well-known/agent-interface.json", tags=["discovery"])
def discovery():
    """Machine-readable entry point so any agent can find out how to connect."""
    return {
        "name": "Valio Aimo AgentGate",
        "version": "0.1.0",
        "spec": "docs/AGENT_INTERFACE_SPEC.md",
        "openapi": "/openapi.json",
        "auth": {"type": "header", "header": "X-Agent-Key",
                 "note": "Each key is bound to one buyer's mandate and can be revoked by the buyer at any time."},
        "transports": {"http": "/api/v1", "mcp": "python -m app.mcp_server (stdio)"},
        "tools": ["get_my_mandate", "search_products", "check_availability", "request_quote", "place_order", "get_order_status"],
        "rules": {
            "pricing": "fixed contract or list price per buyer, no negotiation",
            "decisions": ["agent_may_order", "needs_human_approval", "blocked"],
            "human_approval_when": ["total above the mandate's auto-approve limit",
                                    "quantity unusually high vs. the buyer's normal orders",
                                    "substitute products"],
            "blocked_when": ["mandate revoked or expired", "total above hard limit",
                             "category or lead time outside the mandate"],
            "every_order_needs": "a human-readable reason",
        },
        "data_policy": "Agents see only their own buyer's prices and orders; no exact stock levels, "
                       "no other customers, no internal data. Searches are rate-limited.",
    }


# ---------------------------------------------------------------- agent API (v1)

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


@app.get("/api/v1/mandate", tags=["agent"])
def api_mandate(buyer: dict = Depends(current_buyer)):
    return core.public_mandate(buyer)


@app.get("/api/v1/products", tags=["agent"])
def api_search(q: str = "", category: str | None = None, label: str | None = None,
               buyer: dict = Depends(current_buyer)):
    return core.search_products(buyer["id"], q, category, label)


@app.get("/api/v1/products/{product_id}/availability", tags=["agent"])
def api_availability(product_id: str, quantity: float, deliver_by: str | None = None,
                     buyer: dict = Depends(current_buyer)):
    return core.check_availability(buyer["id"], product_id, quantity, deliver_by)


@app.post("/api/v1/quotes", tags=["agent"])
def api_quote(body: QuoteRequest, buyer: dict = Depends(current_buyer)):
    return core.request_quote(buyer["id"], [i.model_dump() for i in body.items], body.deliver_by)


@app.post("/api/v1/orders", tags=["agent"])
def api_order(body: OrderRequest, buyer: dict = Depends(current_buyer)):
    return core.place_order(buyer["id"], body.quote_id, body.reason, body.is_substitute)


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


class BuyerRequest(BaseModel):
    buyer_id: str


def _buyer_or_404(buyer_id: str) -> dict:
    if buyer_id not in core.store.buyers:
        raise HTTPException(status_code=404, detail="Unknown buyer")
    return core.store.buyers[buyer_id]


@app.get("/api/demo/buyers", tags=["demo"])
def api_buyers():
    return [core.public_mandate(b) | {"buyer_id": b["id"]} for b in core.store.buyers.values()]


@app.post("/api/demo/chat", tags=["demo"])
def api_chat(body: ChatRequest):
    _buyer_or_404(body.buyer_id)
    return agent.run(body.buyer_id, body.message)


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


@app.post("/api/demo/reset", tags=["demo"])
def api_reset():
    core.store.reset()
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")
