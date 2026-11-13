# AgentGate — Valio Aimo's front door for AI purchasing agents

**Junction 2026 · team STEEL BALL · challenge: Valio Aimo — Trusted Trade Agents**

> Any AI agent can order from Valio Aimo, but no agent can break Valio's rules.

AgentGate is an open interface through which external purchasing agents (a hotel's or restaurant's
ordering bot) can **look up** products, get **fixed-price quotes** and **place orders** — while the
**door, not the agent,** enforces the buyer's rules:

- **Agent mandate** — spending limits, allowed categories, validity, and an instant **Revoke** switch
- **Human approval** above the agent's limit, for substitutes, and for **unusual quantities** vs. the buyer's normal orders
- **Need-to-know data** — each buyer sees only its own contract prices; no exact stock, no other customers; anti-scraping throttle
- **Explainable & verifiable** — every order carries the agent's reason; **hash-chained audit log**; receipt per order
- **Open standards** — REST/OpenAPI + MCP + a discovery file at `/.well-known/agent-interface.json`
- Works with requests in **English or Finnish**

All Valio Aimo data here is mocked (`data/`).

📄 [Design](docs/DESIGN.md) · 📐 [Agent interface spec v0.1](docs/AGENT_INTERFACE_SPEC.md) · 🗓️ [Weekend plan](docs/PLAN.md)

## Run it

```bash
python -m venv venv
venv\Scripts\activate            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
set DEMO_TODAY=2026-11-13        # optional: fixed date for a repeatable demo (macOS/Linux: export ...)
uvicorn app.main:app --port 8000
```

- UI: http://127.0.0.1:8000 · API docs: http://127.0.0.1:8000/docs
- The demo agent uses **Gemini** when `GEMINI_API_KEY` is set (`GEMINI_MODEL`, default `gemini-2.5-flash`),
  otherwise a deterministic offline planner.
- MCP (for Claude Desktop, Claude Code, Gemini CLI…): `WHOLESALER_AGENT_KEY=demo-key-aurora python -m app.mcp_server`

## Demo in the UI
1. **Send to agent** — 1,000 L lactose-free milk by Monday → 1,450 € → *needs approval* (over limit, 8.3× usual) → **Approve**
2. **Finnish** — same request in Finnish, same result
3. **Small order** — 24 L oat drink → confirmed by the door automatically
4. **Misbehaving agent** — 10,690 € order and a meat order → both rejected
5. **Revoke mandate** → the agent's next call is refused
6. Audit trail header shows **chain verified**

## Structure
| Path | What |
|---|---|
| `app/core.py` | The door: catalog, mandate checks, unusual-order guard, quotes, orders, approvals, hash-chained audit |
| `app/main.py` | HTTP API (`/api/v1` agent, `/api/human`, discovery, demo) |
| `app/mcp_server.py` | Same operations as MCP tools |
| `app/agent.py` | Demo buyer agent (Gemini or offline) + misbehaving-agent simulation |
| `web/index.html` | Mandate card, agent chat, approvals, audit trail |
| `data/` | Mock catalog and buyers (mandates, contract prices, usual order quantities) |
| `tests/` | 13 tests covering every rule |

## Tests
```bash
pytest -q
```
