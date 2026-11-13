# Trusted Trade Agents — Junction 2026 (Valio Aimo challenge)

An open interface through which an **external purchasing agent** can search a food-service
wholesaler's catalog, get fixed-price quotes and place orders — **within limits the buyer sets**,
with **human approval** above those limits and a **human-readable audit trail** of every step.

All wholesaler data is mocked (`data/catalog.json`, `data/buyers.json`).

## Run

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (UI) and http://127.0.0.1:8000/docs (OpenAPI spec of the agent interface).

- Without an API key the buyer agent uses a small deterministic planner (good for offline demos).
- With `GEMINI_API_KEY` set it uses Gemini function calling (`GEMINI_MODEL`, default `gemini-2.5-flash`).
- `DEMO_TODAY=2026-11-13` fixes "today" so the demo is identical every run.

## Demo scenario

"I need 1,000 litres of lactose-free milk by Monday." →
agent searches → checks 4 options → only one supplier can deliver 1,000 L in time →
quote 1,450 € → above the hotel's 1,000 € auto-approve limit → **pending approval** →
kitchen manager approves in the UI → confirmed, stock reserved, every step in the audit trail.

## Pieces

| File | What |
|---|---|
| `app/core.py` | Catalog, quotes, orders, **policy engine**, audit log |
| `app/main.py` | Agent API `/api/v1/*` (per-buyer `X-Agent-Key`), human approval `/api/human/*`, demo endpoints |
| `app/agent.py` | Buyer agent (Gemini tool calling, offline fallback) |
| `app/mcp_server.py` | Same interface as an MCP server for any MCP agent |
| `web/index.html` | Agent chat · approvals · audit trail |

Policy per buyer (`data/buyers.json`): auto-approve limit, hard limit, allowed categories,
max lead time, substitutes need approval.

## Tests

```bash
pytest -q
```
