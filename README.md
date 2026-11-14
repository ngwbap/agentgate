# AgentGate: Valio Aimo's front door for AI purchasing agents

**Junction 2026 · team STEEL BALL · challenge: Valio Aimo, Trusted Trade Agents**

> Any AI agent can order from Valio Aimo, but no agent can break Valio's rules.

Today, searching the webshop for "lactose free milk" lists milk chocolate first and leaves price per
litre, stock and delivery dates to the buyer. AgentGate turns **one sentence into a few trusted,
deliverable options**, and lets any agent order them **only within the buyer's mandate**.

## Smart Lookup ("AI understands, rules decide")
1. **Understand.** Claude turns an English or Finnish request into a structured need (product, quantity, unit, deadline, dietary claims, allergens). An offline parser takes over if the AI is unavailable.
2. **Shortlist offline.** It searches Valio's 17,546-product file by name (EN/FI/SV, Finnish compounds), checks **category coherence** (so "milk" means milk drinks, not milk chocolate), applies claims, allergens (incl. "may contain") and unit, and then has Claude judge *exact / close / different*.
3. **Live check only the shortlist.** It makes **one availability call and one price call** for about 8 products, never the whole catalog, as Valio's API docs ask.
4. **Rank by rules.** First, can it arrive **in full and on time** (Valio stock vs supplier stock, order cutoff)? Then price per litre/kg, delivery reliability and CO2, using the buyer's priorities as weights.
5. **Good-better-best plans.** It offers up to three ways to fulfil the need (single product, split across stock, close variant, Valio stock plus supplier, substitute), each scored `0.55 × delivery certainty + 0.45 × match fidelity` with price as tie-breaker, and each listing its trade-offs and why a human would have to approve it.

**Exceptions.** Substitutes (e.g. oat drink for milk) are checked live only when the requested product cannot be confirmed (2 more API calls). Dietary claims and allergen exclusions are hard rules that apply to substitutes too. In strict allergen mode (allergy, school, hospital) "may contain" is excluded and a product with **no allergen data is unverified, not safe**: it is dropped and counted in the funnel for a human check.

Every option shows its **evidence**: price API validity, availability timestamp, product-data date and GTIN, and the reliability and CO2 sources.

## The door (guardrails)
- **Agent mandate** issued by the buyer: **"always ask me"** (on by default: a human confirms every order), auto-approve limit, hard limit, food only, max lead time, validity. Each connected agent can be **paused** on its own.
- **Approve with changes:** the approver can change amounts; the order is re-priced live and Valio's hard rules still apply.
- **Human approval** above the limit, for **unusual quantities** vs. the buyer's normal week, when a **supplier cutoff has passed**, for substitutes, or for **any risk the agent declares itself** (`declared_risks`). An agent can only add caution, never remove a rule.
- **Need-to-know.** Agents never see other customers or internal data, and searches are throttled.
- **Hash-chained audit log**, plus a verifiable receipt for every order.
- **Open interfaces:** REST/OpenAPI, MCP (with a `smart_lookup` tool), and `/.well-known/agent-interface.json`.

📄 [Design](docs/DESIGN.md) · 📐 [Agent interface spec](docs/AGENT_INTERFACE_SPEC.md) · 🗓️ [Weekend plan](docs/PLAN.md)

## Run it

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt     # macOS/Linux: venv/bin/python ...
```

Create `.env` (git-ignored) with:
```
VALIO_API_KEY=...        # Junction platform -> Trusted Trade Agents -> "API authentication"
ANTHROPIC_API_KEY=...    # optional; without it the offline parser is used
CLAUDE_MODEL=claude-opus-5-5   # optional
```

```bash
venv\Scripts\python.exe scripts\download_data.py   # Valio's data files -> data/valio/ (Junction use only, never committed)
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```
- **Buying assistant:** http://127.0.0.1:8000 (type a request, check what was understood, pick a way, adjust the draft order, send it)
- **Orders and approvals:** http://127.0.0.1:8000/orders (approve, change, reject; history; agent settings and pause)
- **Demo tools for the pitch:** http://127.0.0.1:8000/?demo (misbehaving agent, reset, the funnel and plan scores)
- API docs: http://127.0.0.1:8000/docs

**MCP** (Claude Desktop, Claude Code and others): `WHOLESALER_AGENT_KEY=demo-key-aurora python -m app.mcp_server`

## Structure
| Path | What |
|---|---|
| `app/lookup.py` | Smart Lookup: understand, shortlist, live check, rank, plan |
| `app/catalog.py` | Offline index of Valio's product data (allergens, claims, labels, pack sizes) |
| `app/valio_api.py` | Price and Availability API client (batching, cache, call counter) |
| `app/understand.py` | Request understanding and relevance judging (Claude + offline fallback) |
| `app/llm.py` | Claude structured-JSON calls with cost accounting |
| `app/core.py` | The door: mandate, quotes, orders, approvals, hash-chained audit |
| `app/main.py` · `app/mcp_server.py` | HTTP API and MCP server |
| `web/index.html` | Buying assistant: request, what was understood (editable), Best / Better / Good, draft order, live quote |
| `web/orders.html` | Orders window: waiting / done, approve with changes, history, agent settings |
| `tests/` | 39 offline tests with a synthetic catalog and a fake Valio API |

```bash
venv\Scripts\python.exe -m pytest -q
```
