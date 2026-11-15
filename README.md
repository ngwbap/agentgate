# AgentGate - Valio Aimo's Front Door for AI Purchasing Agents

**Junction 2026 · Team STEEL BALL · Challenge: Valio Aimo, Trusted Trade Agents**

> Any AI agent can order from Valio Aimo, but no agent can break Valio's rules.

AgentGate turns **one sentence into a few trusted, deliverable options**, and lets any AI agent (Valio's own assistant, Claude Desktop, Claude Code, Gemini CLI or a customer's own bot) place orders **only within the buyer's mandate**. Today, searching the webshop for "lactose free milk" lists milk chocolate first and leaves price per litre, stock and delivery dates to the buyer. AgentGate fixes the search, and puts a door with hard rules between agents and the order book.

## 🚀 Features

### For Professional Kitchens (Buying Assistant)

- **Smart Lookup, "AI understands, rules decide"**
  - Type a request in English or Finnish ("120 L lactose free milk by Friday")
  - Check and edit what was understood: product, quantity, unit, deadline, dietary claims, allergens
  - Get up to three ways to fulfil the need: **Best / Better / Good**
  - Every option shows its trade-offs and its evidence (price validity, availability timestamp, GTIN, reliability and CO2 sources)

- **Order Sheet, Always in View**
  - The cart sits on the right and stays visible: the request, order lines, total excl. VAT, who must approve and why, and the send button
  - Adjust amounts before sending; prices and availability are checked live with Valio's APIs
  - Price per litre/kg shown for fair comparison
  - On narrower screens the sheet follows the page and its total becomes a bar at the bottom

- **Guided Steps**
  - A step rail (**Ask → Check the boxes → Choose a way → Order**) shows where you are
  - Editing the understood criteria after a search greys out the old results and steps back
  - Voice input in English or Finnish (Chrome, Edge)

- **Safe by Default**
  - Dietary claims and allergen exclusions are hard rules, also for substitutes
  - Strict allergen mode (allergy, school, hospital): "may contain" is excluded and a product with **no allergen data is unverified, not safe**

### For Approvers (Orders Window)

- **Orders & Approvals**
  - Sidebar sections: Waiting for approval, Done, History and Agent settings (the address keeps the section, e.g. `/orders#history`)
  - Number tiles for waiting, confirmed and rejected orders
  - Approve, reject, or **approve with changes** (re-priced live, Valio's hard rules still apply)

- **Agent Mandate Settings**
  - **"Always ask me"** (on by default: a human confirms every order)
  - Auto-approve limit, hard limit, food only, max lead time, validity
  - Pause or resume each connected agent on its own; revoke or reinstate the whole mandate

### For AI Agents (The Door)

- **Open Interfaces**
  - REST / OpenAPI, MCP (with a `smart_lookup` tool) and `/.well-known/agent-interface.json`
  - An open [Agent Interface Spec](docs/AGENT_INTERFACE_SPEC.md) any agent can follow

- **Guardrails Agents Cannot Remove**
  - Human approval above the limit, for **unusual quantities** vs. the buyer's normal week, when a **supplier cutoff has passed**, for substitutes, or for **any risk the agent declares itself** (`declared_risks`)
  - An agent can only add caution, never remove a rule
  - **Need-to-know:** agents never see other customers or internal data, and searches are throttled

- **Trust & Audit**
  - **Hash-chained audit log**, verifiable at `/api/human/audit/verify`
  - **Signed quotes and receipts** (Ed25519): an agent can prove what price, stock source and date the door promised, and who approved. Public key in JWKS format at `/.well-known/jwks.json`; check any signed object at `POST /api/v1/verify`
  - **Approving is human-only**: approver endpoints need a separate `X-Approver-Key`. An agent key is refused there, so an agent can never approve its own order
  - Orders, quotes and the audit log are saved to disk and survive a restart
  - The same request gives the same answer: the AI's language step is cached

## 🔐 Demo Accounts

Two demo buyers live in `data/buyers.json`, each with two agents:

| Buyer | Agent | Key |
|---|---|---|
| Hotel Aurora Helsinki (kitchen) | Valio Aimo assistant | `demo-assistant-aurora` |
| Hotel Aurora Helsinki (kitchen) | Hotel's own AI agent | `demo-key-aurora` |
| Cafe Lumi | Valio Aimo assistant | `demo-assistant-lumi` |
| Cafe Lumi | Cafe's own AI agent | `demo-key-lumi` |

Approving needs the approver's code. On the Orders page, sign in once per browser:

| Buyer | Approver | Approver code (`X-Approver-Key`) |
|---|---|---|
| Hotel Aurora Helsinki (kitchen) | Kitchen manager | `demo-approver-aurora` |
| Cafe Lumi | Owner | `demo-approver-lumi` |

These are demo keys. For real use, point `BUYERS_FILE` at a private file; keys can be stored there as SHA-256 hashes (`key_sha256`, `approver_key_sha256`) instead of plain text.

## 🛠️ Technology Stack

### Backend

- **Framework**: FastAPI + Uvicorn
- **AI**: Claude (Anthropic SDK) for request understanding and relevance judging, with an offline parser as fallback
- **Agent protocol**: MCP (Model Context Protocol) server
- **Data**: Valio's 17,546-product file, indexed offline; live Price and Availability APIs for the shortlist only
- **Audit**: hash-chained, tamper-evident log
- **API Documentation**: OpenAPI/Swagger (auto-generated)
- **Tests**: pytest with a synthetic catalog and a fake Valio API

### Frontend

- **Plain HTML, CSS and JavaScript** served by FastAPI (`web/`), no build step
- **Portal layout**: shared top bar (logo, customer picker, Buyer/Approver switch) and sidebar navigation on both pages
- **Design**: one shared stylesheet (`web/shared.css`) with design tokens and dark mode; colour carries meaning only (green/amber/red for stock and approval status, purple for what the AI did)
- **Icons**: Lucide shapes as CSS masks, no emoji
- **Accessibility**: 40px controls, visible focus rings, no sideways scrolling at 375px
- **Voice input**: Web Speech API where the browser offers it

## 📦 Installation & Setup

### Prerequisites

- **Python** 3.10+
- A **Valio API key** (Junction platform → Trusted Trade Agents → "API authentication")
- An **Anthropic API key** (optional; without it the offline parser is used)

### Setup

1. **Clone the repository**:
```bash
git clone https://github.com/ngwbap/agentgate.git
cd agentgate
```

2. **Create a virtual environment and install dependencies**:
```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt   # macOS/Linux: venv/bin/python -m pip ...
```

3. **Set up environment variables**: create a `.env` file (git-ignored):
```env
VALIO_API_KEY=...                 # required for live price and availability
ANTHROPIC_API_KEY=...             # optional
CLAUDE_MODEL=claude-opus-5-5      # optional
```

4. **Download Valio's data files** (to `data/valio/`, Junction use only, never committed):
```bash
venv\Scripts\python.exe scripts\download_data.py
```

## 🚀 Running the Application

```bash
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

The application will be available at:
- **Buying Assistant**: http://127.0.0.1:8000 (type or speak a request, check what was understood, pick a way, send it from the order sheet)
- **Orders & Approvals**: http://127.0.0.1:8000/orders (sign in with the approver code, then approve, change, reject; jump to `#done`, `#history` or `#settings`)
- **Demo Tools for the Pitch**: http://127.0.0.1:8000/?demo (misbehaving agent, reset, the funnel and plan scores)
- **Interactive API Docs**: http://127.0.0.1:8000/docs

### MCP Server (Claude Desktop, Claude Code and others)

```bash
WHOLESALER_AGENT_KEY=demo-key-aurora python -m app.mcp_server
```

On Windows (cmd): `set WHOLESALER_AGENT_KEY=demo-key-aurora && python -m app.mcp_server`

### Tests

```bash
venv\Scripts\python.exe -m pytest -q
```

65 offline tests, no API keys needed.

## 📁 Project Structure

```
agentgate/
├── app/
│   ├── main.py            # FastAPI app: agent, human and demo endpoints
│   ├── mcp_server.py      # MCP server for external agents
│   ├── core.py            # The door: mandate, quotes, orders, approvals, hash-chained audit
│   ├── lookup.py          # Smart Lookup: understand, shortlist, live check, rank, plan
│   ├── catalog.py         # Offline index of Valio's product data (allergens, claims, pack sizes)
│   ├── valio_api.py       # Price and Availability API client (batching, cache, call counter)
│   ├── understand.py      # Request understanding and relevance judging (Claude + offline fallback)
│   ├── llm.py             # Claude structured-JSON calls with cost accounting
│   ├── agent.py           # Demo purchasing agent used by the web UI
│   └── config.py          # Settings from environment and .env
├── web/
│   ├── index.html         # Buying assistant: step rail, understood criteria, Best / Better / Good, order sheet
│   ├── orders.html        # Orders window: waiting, done, history, agent settings; approve with changes
│   └── shared.css         # Shared shell (top bar, sidebar), design tokens, icons, dark mode
├── data/
│   ├── buyers.json        # Demo buyers, agents and mandates
│   ├── catalog.json
│   └── valio/             # Valio's data files (downloaded, git-ignored)
├── docs/
│   ├── DESIGN.md
│   ├── AGENT_INTERFACE_SPEC.md
│   ├── PLAN.md
│   └── HANDOFF.md
├── scripts/
│   └── download_data.py   # Fetches Valio's data files
├── tests/                 # Offline tests with a synthetic catalog and fake Valio API
├── requirements.txt
└── README.md
```

## 🔑 Key Features Explained

### Smart Lookup Flow

1. **Understand.** Claude turns an English or Finnish request into a structured need (product, quantity, unit, deadline, dietary claims, allergens). An offline parser takes over if the AI is unavailable.
2. **Shortlist offline.** Search Valio's 17,546-product file by name (EN/FI/SV, Finnish compounds), check **category coherence** (so "milk" means milk drinks, not milk chocolate), apply claims, allergens (incl. "may contain") and unit, then let Claude judge *exact / close / different*.
3. **Live check only the shortlist.** **One availability call and one price call** for about 15 products, never the whole catalog, as Valio's API docs ask.
4. **Rank by rules.** First: can it arrive **in full and on time** (Valio stock vs supplier stock, order cutoff)? Then price per litre/kg, delivery reliability and CO2, weighted by the buyer's priorities.
5. **Good-better-best plans.** Up to three plans (single product, split across stock, close variant, Valio stock plus supplier, substitute).

### Plan Scoring

```
score = 0.40 × delivery certainty + 0.35 × match fidelity + 0.25 × your criteria
        (your criteria = price by default; reliability and carbon footprint if you tick them)
```

Each plan lists its trade-offs and why a human would have to approve it. "Best" is the top score. If another plan
has more stars on your criteria, its badge says the catch in the same breath ("More stars, but less certain").

A brand or pack size you name is honoured: "Arla, 1 l cartons" puts Arla 1 l products first; other brands and sizes
are offered only as close variants, which need approval.

### Substitutes

- Checked live only when the requested product cannot be confirmed (2 more API calls)
- Dietary claims and allergen exclusions apply to substitutes too
- Substitutes always need human approval by default

### Order Decision at the Door

1. Agent requests a quote, then places an order with a **reason**
2. The door checks the mandate: validity, agent paused or revoked, food only, lead time, hard limit
3. Hard-rule violations are **rejected**
4. Anything above the auto-approve limit, unusual vs. the usual week, past a supplier cutoff, a substitute, or with `declared_risks` goes to **human approval**
5. Otherwise (and only if "always ask me" is off) it is **auto-approved**
6. Every step is written to the hash-chained audit log, and a confirmed order gets a signed receipt
7. A human approves with the approver key; the name on the approval comes from that key, not from the request

## 🔌 API Endpoints

### Discovery
- `GET /.well-known/agent-interface.json` - Machine-readable interface description
- `GET /.well-known/jwks.json` - The door's public signing key (JWKS)
- `POST /api/v1/verify` - Check a signed quote or receipt
- `GET /openapi.json` - OpenAPI 3.1, with the two key types declared as security schemes

### Agent Endpoints
- `GET /api/v1/mandate` - Get the agent's mandate
- `POST /api/v1/lookup` - Smart Lookup from a natural-language request
- `GET /api/v1/products` - Search products
- `GET /api/v1/products/{product_id}/availability` - Check availability
- `POST /api/v1/quotes` - Request a quote
- `POST /api/v1/orders` - Place an order from a quote
- `GET /api/v1/orders/{order_id}` - Get order status

### Human (Approver) Endpoints
All need the header `X-Approver-Key` (except verifying the audit chain). An approver sees and decides only their own buyer's orders.
- `GET /api/human/session` - Who this approver key belongs to
- `GET /api/human/pending` - Orders waiting for approval
- `POST /api/human/orders/{order_id}/decision` - Approve, approve with changes, or reject
- `POST /api/human/orders/{order_id}/cancel` - Cancel an order
- `POST /api/human/mandates/{buyer_id}/settings` - Update mandate settings
- `POST /api/human/agents/{buyer_id}/{agent_id}/pause` - Pause an agent
- `POST /api/human/agents/{buyer_id}/{agent_id}/resume` - Resume an agent
- `POST /api/human/mandates/{buyer_id}/revoke` - Revoke a mandate
- `POST /api/human/mandates/{buyer_id}/reinstate` - Reinstate a mandate
- `GET /api/human/audit/verify` - Verify the audit chain

### Demo Endpoints
- `/api/demo/*` - Buyers, lookup, quote, order, rogue agent, audit, status and reset, used by the web UI

For full details, visit http://127.0.0.1:8000/docs when the server is running.

### MCP Tools
- `get_my_mandate` · `smart_lookup` · `search_products` · `check_availability` · `request_quote` · `place_order` · `get_order_status`

## 🔧 Troubleshooting

### "Python was not found" on Windows
Use the virtual environment's interpreter directly: `venv\Scripts\python.exe`.

### No live prices or availability
Check that `VALIO_API_KEY` is set in `.env` and that `scripts/download_data.py` has filled `data/valio/`.

### Requests are understood less well
Without `ANTHROPIC_API_KEY` the offline parser is used. Add the key for Claude-based understanding and relevance judging.

### Reset the demo
Open http://127.0.0.1:8000/?demo and press reset, or call `POST /api/demo/reset`. This also clears the saved state
(`data/state.json`). The AI answer cache is `data/llm_cache.json`; delete it to force fresh answers.

## 📝 Notes

- Valio's data files are for Junction use only and are never committed
- `.env` is git-ignored; never commit API keys
- AgentGate does not depend on its own demo agent: any HTTP or MCP agent uses the same door with the same rules

## 🔗 Links

- 📄 [Design](docs/DESIGN.md)
- 📐 [Agent Interface Spec](docs/AGENT_INTERFACE_SPEC.md)
- 🗓️ [Weekend Plan](docs/PLAN.md)

---

**Junction 2026** · Team STEEL BALL
