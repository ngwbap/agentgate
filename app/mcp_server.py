"""MCP server exposing the same wholesaler interface to any MCP-capable agent
(Claude Desktop, Claude Code, Gemini CLI...).

Run:  WHOLESALER_AGENT_KEY=demo-key-aurora python -m app.mcp_server
Note: this runs its own in-memory store, separate from the web server.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import core

BUYER = core.buyer_by_key(os.environ.get("WHOLESALER_AGENT_KEY", "demo-key-aurora"))
ACTOR = f"agent:{BUYER['id']}(mcp)"

mcp = FastMCP("trusted-trade-wholesaler")


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except core.WholesalerError as exc:
        return {"error": str(exc)}


@mcp.tool()
def search_products(query: str, category: str | None = None, label: str | None = None) -> list[dict] | dict:
    """Search the wholesaler catalog by text, optional category and label."""
    return _wrap(core.search_products, query, category, label, actor=ACTOR)


@mcp.tool()
def check_availability(product_id: str, quantity: float, deliver_by: str | None = None) -> dict:
    """Stock, price and earliest delivery for one product. deliver_by is YYYY-MM-DD."""
    return _wrap(core.check_availability, product_id, quantity, deliver_by, actor=ACTOR)


@mcp.tool()
def request_quote(items: list[dict], deliver_by: str | None = None) -> dict:
    """Fixed-price quote. items = [{product_id, quantity}]. Includes a policy_preview."""
    return _wrap(core.request_quote, BUYER["id"], items, deliver_by, actor=ACTOR)


@mcp.tool()
def place_order(quote_id: str, reason: str, is_substitute: bool = False) -> dict:
    """Order from a quote. Above the buyer's limits the order waits for human approval."""
    return _wrap(core.place_order, BUYER["id"], quote_id, reason, is_substitute, actor=ACTOR)


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Current status of an order placed by this buyer."""
    return _wrap(core.get_order, BUYER["id"], order_id)


if __name__ == "__main__":
    mcp.run()
