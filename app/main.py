"""AgentGate HTTP server: agent API, human API, discovery file, demo endpoints and web UI."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from . import agent, config, core, llm, signing

WEB_DIR = config.ROOT / "web"
NO_CACHE = {"Cache-Control": "no-cache"}     # browsers re-check the demo pages, so changes show without a hard refresh

app = FastAPI(
    title="AgentGate - Valio Aimo front door for purchasing agents",
    version="0.3.0",
    description="Open interface through which external purchasing agents look up, quote and order from Valio Aimo "
                "within a buyer-issued mandate. Static data narrows the search; live price and availability are "
                "queried only for the shortlist. The door enforces limits, routes unusual, risky or large orders to a "
                "human, and keeps a tamper-evident audit log. Spec: docs/AGENT_INTERFACE_SPEC.md",
)


@app.exception_handler(core.WholesalerError)
async def _wholesaler_error(_, exc: core.WholesalerError):
    return JSONResponse(status_code=403 if isinstance(exc, core.MandateError) else 400, content={"error": str(exc)})


if config.PERSIST:                                        # orders, quotes and the audit log survive a restart
    core.store.enable_persistence(config.STATE_FILE)

# Two kinds of key, declared as standard OpenAPI security schemes. An agent key can look up, quote and order.
# Only an approver key (held by the buyer's human approver) can approve, reject or change the agent's authority.
agent_key = APIKeyHeader(name="X-Agent-Key", scheme_name="AgentKey", auto_error=False,
                         description="Agent key bound to one buyer's mandate")
approver_key = APIKeyHeader(name="X-Approver-Key", scheme_name="ApproverKey", auto_error=False,
                            description="The human approver's key. Agent keys are refused here.")


def current_buyer(key: str | None = Security(agent_key)) -> dict:
    if not key:
        raise HTTPException(status_code=401, detail="Send your agent key in the X-Agent-Key header.")
    try:
        buyer, agent_ = core.agent_by_key(key)
    except core.WholesalerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    core.check_agent(buyer["id"], agent_["id"])          # a paused agent gets 403, the buyer's others keep working
    return buyer


def current_approver(key: str | None = Security(approver_key)) -> dict:
    """The buyer whose human approver is calling. Who approved is taken from the key, never from the request body."""
    if not key:
        raise HTTPException(status_code=401, detail="Approving needs the approver's key in the X-Approver-Key header.")
    try:
        return core.approver_by_key(key)
    except core.WholesalerError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def own_buyer(buyer_id: str, approver: dict = Depends(current_approver)) -> dict:
    """An approver can only change their own buyer's agents and mandate."""
    if approver["id"] != buyer_id:
        raise HTTPException(status_code=403, detail="This approver key belongs to another buyer.")
    return approver


# ---------------------------------------------------------------- discovery

@app.get("/.well-known/agent-interface.json", tags=["discovery"])
def discovery():
    return {
        "name": "Valio Aimo AgentGate", "version": "0.3.0",
        "spec": "docs/AGENT_INTERFACE_SPEC.md", "openapi": "/openapi.json",
        "auth": {"type": "header", "header": "X-Agent-Key",
                 "note": "Each key is bound to one buyer's mandate and can be revoked by the buyer at any time. "
                         "Approving is a separate, human-only key (X-Approver-Key): an agent can never approve."},
        "transports": {"http": "/api/v1", "mcp": "python -m app.mcp_server (stdio)"},
        "standards": {"api_description": "OpenAPI 3.1 at /openapi.json (security schemes AgentKey and ApproverKey)",
                      "agent_tools": "Model Context Protocol (MCP): the same seven tools",
                      "signing_keys": "JWKS (RFC 7517) at /.well-known/jwks.json; quotes and receipts are signed with Ed25519 (EdDSA)",
                      "allergens_and_claims": "GS1 codes from Valio's product data"},
        "signatures": {"jwks": "/.well-known/jwks.json", "verify": "POST /api/v1/verify",
                       "signed": ["quote (the fixed price, stock source and date Valio promised)",
                                  "receipt (what was ordered, the total, and who decided)"]},
        "tools": ["get_my_mandate", "smart_lookup", "search_products", "check_availability", "request_quote",
                  "place_order", "get_order_status"],
        "lookup_policy": "Narrow down with static product data first; live price/availability only for the shortlist "
                         "(typically 2 calls for about 15 products), never for the whole catalog. Substitutes are "
                         "checked live (2 more calls) only when the requested product cannot be confirmed. "
                         "The same request gives the same answer: the language step is cached.",
        "plans": "smart_lookup returns up to three fulfilment plans (Best / Better / Good), scored "
                 "0.45 x delivery certainty + 0.30 x match fidelity + 0.25 x the buyer's own criteria (price by "
                 "default; reliability and carbon footprint if asked). Dietary claims and allergen exclusions are "
                 "hard rules and apply to substitutes too. A named brand or pack size is honoured: other brands "
                 "and sizes count as close variants and need approval.",
        "rules": {
            "pricing": "fixed Valio price per customer and date, no negotiation",
            "decisions": ["agent_may_order", "needs_human_approval", "blocked"],
            "human_approval_when": ["the buyer's 'always ask me' setting is on (the default)",
                                    "total above the mandate's auto-approve limit",
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


@app.get("/.well-known/jwks.json", tags=["discovery"])
def jwks():
    """The door's public signing key (JWKS). Use it to check signed quotes and receipts offline."""
    return signing.jwks()


class Signed(BaseModel):
    payload: dict
    signature: str


@app.post("/api/v1/verify", tags=["discovery"])
def api_verify_signed(body: Signed):
    """Did this door sign this quote or receipt? Open to anyone: a signature is only useful if all can check it."""
    return core.verify_signed(body.model_dump())


# ---------------------------------------------------------------- human API
# Every call needs the approver's key (X-Approver-Key). In production that key comes from the buyer's own
# sign-in; in the demo the approver types it once on the orders page. Agent keys are refused.

class Change(BaseModel):
    product_id: str
    quantity: float


class Decision(BaseModel):
    approve: bool
    approver: str | None = None            # ignored: who approved is taken from the approver key
    note: str = ""
    changes: list[Change] | None = None    # approve with changed quantities (re-priced live)


class Settings(BaseModel):
    by: str | None = None                  # ignored: taken from the approver key
    always_ask: bool | None = None
    auto_approve_limit_eur: float | None = None
    hard_limit_eur: float | None = None
    max_lead_time_days: int | None = None


class MandateChange(BaseModel):
    by: str | None = None                  # ignored: taken from the approver key


def _buyer_or_404(buyer_id: str) -> dict:
    if buyer_id not in core.store.buyers:
        raise HTTPException(status_code=404, detail="Unknown buyer")
    return core.store.buyers[buyer_id]


@app.get("/api/human/session", tags=["human"])
def api_session(me: dict = Depends(current_approver)):
    """Who this approver key belongs to. The orders page uses it to sign the approver in."""
    return {"buyer_id": me["id"], "buyer": me["name"], "approver": me["approver"]}


@app.get("/api/human/pending", tags=["human"])
def api_pending(me: dict = Depends(current_approver)):
    return core.pending_orders(me["id"])


@app.post("/api/human/orders/{order_id}/decision", tags=["human"])
def api_decide(order_id: str, body: Decision, me: dict = Depends(current_approver)):
    changes = [c.model_dump() for c in body.changes] if body.changes is not None else None
    return core.decide(order_id, body.approve, me["approver"], body.note, changes, buyer_id=me["id"])


@app.post("/api/human/orders/{order_id}/cancel", tags=["human"])
def api_cancel(order_id: str, me: dict = Depends(current_approver)):
    return core.cancel_order(order_id, me["approver"], buyer_id=me["id"])


@app.post("/api/human/mandates/{buyer_id}/settings", tags=["human"])
def api_settings(buyer_id: str, body: Settings, me: dict = Depends(own_buyer)):
    return core.update_settings(buyer_id, body.model_dump(exclude={"by"}), me["approver"])


@app.post("/api/human/agents/{buyer_id}/{agent_id}/pause", tags=["human"])
def api_pause(buyer_id: str, agent_id: str, me: dict = Depends(own_buyer)):
    return core.set_agent_paused(buyer_id, agent_id, True, me["approver"])


@app.post("/api/human/agents/{buyer_id}/{agent_id}/resume", tags=["human"])
def api_resume(buyer_id: str, agent_id: str, me: dict = Depends(own_buyer)):
    return core.set_agent_paused(buyer_id, agent_id, False, me["approver"])


@app.post("/api/human/mandates/{buyer_id}/revoke", tags=["human"])
def api_revoke(buyer_id: str, me: dict = Depends(own_buyer)):
    return core.set_mandate_revoked(buyer_id, True, me["approver"])


@app.post("/api/human/mandates/{buyer_id}/reinstate", tags=["human"])
def api_reinstate(buyer_id: str, me: dict = Depends(own_buyer)):
    return core.set_mandate_revoked(buyer_id, False, me["approver"])


@app.get("/api/human/audit/verify", tags=["human"])
def api_verify():
    return core.verify_audit()


# ---------------------------------------------------------------- demo / UI

class ChatRequest(BaseModel):
    buyer_id: str
    message: str
    need: dict | None = None        # the "Here's what I understood" fields, edited by the buyer


class DraftLine(BaseModel):
    product_id: str
    units: float


class DraftQuote(BaseModel):
    buyer_id: str
    lines: list[DraftLine]
    deliver_by: str
    is_substitute: bool = False
    declared_risks: list[str] = []


class DraftOrder(BaseModel):
    buyer_id: str
    quote_id: str
    reason: str
    is_substitute: bool = False
    declared_risks: list[str] = []
    notes: list[str] = []            # the buyer's other wishes, shown to the approver


class UnderstandRequest(BaseModel):
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
    return agent.lookup(body.buyer_id, body.message, body.need)


@app.post("/api/demo/quote", tags=["demo"])
def api_demo_quote(body: DraftQuote):
    _buyer_or_404(body.buyer_id)
    return agent.quote(body.buyer_id, [l.model_dump() for l in body.lines], body.deliver_by, body.is_substitute,
                       body.declared_risks)


@app.post("/api/demo/order", tags=["demo"])
def api_demo_order(body: DraftOrder):
    _buyer_or_404(body.buyer_id)
    return agent.order(body.buyer_id, body.quote_id, body.reason, body.is_substitute, body.declared_risks, body.notes)


class UnitCheck(BaseModel):
    product_terms: list[str]


@app.post("/api/demo/unit-check", tags=["demo"])
def api_unit_check(body: UnitCheck):
    """How Valio sells a product (by litres or by kg), so the boxes can flag a wrong unit."""
    return agent.unit_check(body.product_terms)


@app.post("/api/demo/understand", tags=["demo"])
def api_demo_understand(body: UnderstandRequest):
    """Read a request into criteria only (no search). The buyer checks the tick boxes, then searches."""
    _buyer_or_404(body.buyer_id)
    return agent.understand(body.buyer_id, body.message)


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
def api_orders(buyer_id: str | None = None):
    return core.orders_for(buyer_id) if buyer_id else list(reversed(core.store.orders.values()))


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
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


@app.get("/orders", include_in_schema=False)
def orders_page():
    return FileResponse(WEB_DIR / "orders.html", headers=NO_CACHE)


@app.get("/shared.css", include_in_schema=False)
def shared_css():
    return FileResponse(WEB_DIR / "shared.css", media_type="text/css", headers=NO_CACHE)
