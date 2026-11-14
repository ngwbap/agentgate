"""AgentGate as an MCP server, so any MCP-capable agent (Claude Desktop, Claude Code, Gemini CLI, ...)
can buy through the same door with the same rules.

Run:  set WHOLESALER_AGENT_KEY=demo-key-aurora  &&  python -m app.mcp_server
Note: this process keeps its own in-memory orders and audit log, separate from the web server.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import core

BUYER, AGENT = core.agent_by_key(os.environ.get("WHOLESALER_AGENT_KEY", "demo-key-aurora"))
BUYER_ID = BUYER["id"]

mcp = FastMCP("valio-aimo-agentgate")


def _wrap(fn, *args):
    try:
        core.check_agent(BUYER_ID, AGENT["id"])
        return fn(*args)
    except core.WholesalerError as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_my_mandate() -> dict:
    """Your mandate: spending limits, food only, approver, usual weekly quantities, validity."""
    return core.public_mandate(core.store.buyers[BUYER_ID])


@mcp.tool()
def smart_lookup(request: str) -> dict:
    """Best first step. One sentence (English or Finnish), e.g. 'I need 1,000 L lactose-free milk by Monday'.
    Returns ranked, live-checked options with evidence and up to three fulfilment plans
    (Best / Better / Good) with certainty, fidelity, trade-offs and approval reasons."""
    return _wrap(core.smart_lookup, BUYER_ID, request)


@mcp.tool()
def search_products(query: str) -> list[dict] | dict:
    """Offline catalog search by name (English/Finnish/Swedish). No prices - use check_availability for those."""
    return _wrap(core.search_products, BUYER_ID, query)


@mcp.tool()
def check_availability(product_id: str, quantity: float, deliver_by: str | None = None) -> dict:
    """Live price and availability for one product. quantity is in sales units; deliver_by is YYYY-MM-DD."""
    return _wrap(core.check_availability, BUYER_ID, product_id, quantity, deliver_by)


@mcp.tool()
def request_quote(items: list[dict], deliver_by: str | None = None) -> dict:
    """Fixed-price quote. items = [{product_id, quantity (sales units)}]. policy_preview shows the door's decision."""
    return _wrap(core.request_quote, BUYER_ID, items, deliver_by)


@mcp.tool()
def place_order(quote_id: str, reason: str, is_substitute: bool = False,
                declared_risks: list[str] | None = None) -> dict:
    """Order from a quote with a human-readable reason. Above your mandate it waits for human approval.
    Pass the chosen plan's approval_needed as declared_risks (and is_substitute if a line is not exact):
    any declared risk sends the order to a human."""
    return _wrap(core.place_order, BUYER_ID, quote_id, reason, is_substitute, declared_risks)


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Current status of one of your orders."""
    return _wrap(core.get_order, BUYER_ID, order_id)


if __name__ == "__main__":
    mcp.run()
