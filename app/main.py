"""HTTP server: agent-facing API, human approval API, demo endpoints and the web UI."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, core

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title="Trusted Trade Agents - mock wholesaler",
    description="Open interface through which external purchasing agents can search, quote and order, "
                "within limits set by the buyer, with human approval above those limits.",
)


@app.exception_handler(core.WholesalerError)
async def _wholesaler_error(_, exc: core.WholesalerError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=400, content={"error": str(exc)})


def current_buyer(x_agent_key: str = Header(..., description="Per-buyer agent key")) -> dict:
    try:
        return core.buyer_by_key(x_agent_key)
    except core.WholesalerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


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


@app.get("/api/v1/products", tags=["agent"])
def api_search(q: str = "", category: str | None = None, label: str | None = None,
               buyer: dict = Depends(current_buyer)):
    return core.search_products(q, category, label, actor=f"agent:{buyer['id']}")


@app.get("/api/v1/products/{product_id}/availability", tags=["agent"])
def api_availability(product_id: str, quantity: float, deliver_by: str | None = None,
                     buyer: dict = Depends(current_buyer)):
    return core.check_availability(product_id, quantity, deliver_by, actor=f"agent:{buyer['id']}")


@app.post("/api/v1/quotes", tags=["agent"])
def api_quote(body: QuoteRequest, buyer: dict = Depends(current_buyer)):
    return core.request_quote(buyer["id"], [i.model_dump() for i in body.items], body.deliver_by,
                              actor=f"agent:{buyer['id']}")


@app.post("/api/v1/orders", tags=["agent"])
def api_order(body: OrderRequest, buyer: dict = Depends(current_buyer)):
    return core.place_order(buyer["id"], body.quote_id, body.reason, body.is_substitute,
                            actor=f"agent:{buyer['id']}")


@app.get("/api/v1/orders/{order_id}", tags=["agent"])
def api_get_order(order_id: str, buyer: dict = Depends(current_buyer)):
    return core.get_order(buyer["id"], order_id)


# ---------------------------------------------------------------- human approval API

class Decision(BaseModel):
    approve: bool
    approver: str
    note: str = ""


@app.get("/api/human/pending", tags=["human"])
def api_pending():
    return core.pending_orders()


@app.post("/api/human/orders/{order_id}/decision", tags=["human"])
def api_decide(order_id: str, body: Decision):
    return core.decide(order_id, body.approve, body.approver, body.note)


# ---------------------------------------------------------------- demo / UI

class ChatRequest(BaseModel):
    buyer_id: str
    message: str


@app.get("/api/demo/buyers", tags=["demo"])
def api_buyers():
    return [{"id": b["id"], "name": b["name"], "approver": b["approver"], "policy": b["policy"]}
            for b in core.store.buyers.values()]


@app.post("/api/demo/chat", tags=["demo"])
def api_chat(body: ChatRequest):
    if body.buyer_id not in core.store.buyers:
        raise HTTPException(status_code=404, detail="Unknown buyer")
    return agent.run(body.buyer_id, body.message)


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
