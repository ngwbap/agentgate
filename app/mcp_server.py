"""AgentGate as an MCP server, so any MCP-capable agent (Claude Desktop, Claude Code,
Gemini CLI, ...) can buy through the same door with the same rules.

Run:  set WHOLESALER_AGENT_KEY=demo-key-aurora  &&  python -m app.mcp_server
Note: this process keeps its own in-memory store, separate from the web server.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import core

BUYER = core.buyer_by_key(os.environ.get("WHOLESALER_AGENT_KEY", "demo-key-aurora"))
BUYER_ID = BUYER["id"]

mcp = FastMCP("valio-aimo-agentgate")


def _wrap(fn, *args):
    try:
        return fn(*args)
    except core.WholesalerError as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_my_mandate() -> dict:
    """Your mandate: spending limits, allowed categories, approver, validity."""
    return core.public_mandate(core.store.buyers[BUYER_ID])


@mcp.tool()
def search_products(query: str, category: str | None = None, label: str | None = None) -> list[dict] | dict:
    """Search the catalog in English or Finnish. Returns your prices."""
    return _wrap(core.search_products, BUYER_ID, query, category, label)


@mcp.tool()
def check_availability(product_id: str, quantity: float, deliver_by: str | None = None) -> dict:
    """Stock, price and earliest delivery for one product. deliver_by is YYYY-MM-DD."""
    return _wrap(core.check_availability, BUYER_ID, product_id, quantity, deliver_by)


@mcp.tool()
def request_quote(items: list[dict], deliver_by: str | None = None) -> dict:
    """Fixed-price quote. items = [{product_id, quantity}]. policy_preview tells you what the door will decide."""
    return _wrap(core.request_quote, BUYER_ID, items, deliver_by)


@mcp.tool()
def place_order(quote_id: str, reason: str, is_substitute: bool = False) -> dict:
    """Order from a quote with a human-readable reason. Above your mandate it waits for human approval."""
    return _wrap(core.place_order, BUYER_ID, quote_id, reason, is_substitute)


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Current status of one of your orders."""
    return _wrap(core.get_order, BUYER_ID, order_id)


if __name__ == "__main__":
    mcp.run()
