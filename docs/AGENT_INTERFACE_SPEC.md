# Valio Aimo Agent Interface — v0.1 (draft)

An open specification for how external purchasing agents interact with Valio Aimo.
Reference implementation: AgentGate (this repository).

## 1. Principles
1. **The door enforces the rules.** Agents are never trusted to limit themselves.
2. **Agents act only under a mandate** issued by the buyer, and the buyer can revoke it at any time.
3. **Need-to-know.** An agent sees only what its buyer may see.
4. **Fixed prices.** No negotiation through the agent interface.
5. **Everything is explainable and verifiable.** Every order carries the agent's reason; every action is logged in a tamper-evident chain.
6. **Open transports.** HTTP (OpenAPI) and MCP expose the same operations with the same rules.

## 2. Discovery
`GET /.well-known/agent-interface.json` — name, version, auth method, transports, tools, rules and data policy.

## 3. Authentication & mandate
- Header `X-Agent-Key: <key>`; each key is bound to exactly one buyer's mandate.
- A mandate contains: `id`, `agent_name`, `issued_by`, `valid_until`, `auto_approve_limit_eur`,
  `hard_limit_eur`, `allowed_categories`, `max_lead_time_days`, `substitutes_need_approval`,
  `unusual_order_factor`, `revoked`.
- Revoked or expired mandate → every call returns **403** with an explanation.
- `GET /api/v1/mandate` — an agent can always read its own authority.

## 4. Operations
| Operation | HTTP | MCP tool |
|---|---|---|
| Read own mandate | `GET /api/v1/mandate` | `get_my_mandate` |
| Search catalog (EN/FI) | `GET /api/v1/products?q=` | `search_products` |
| Availability & delivery | `GET /api/v1/products/{id}/availability?quantity=&deliver_by=` | `check_availability` |
| Fixed-price quote | `POST /api/v1/quotes` | `request_quote` |
| Place order (reason required) | `POST /api/v1/orders` | `place_order` |
| Order status | `GET /api/v1/orders/{id}` | `get_order_status` |

Every quote includes a `policy_preview` so the agent knows the outcome **before** ordering.

## 5. Decisions
| Decision | When | Order status |
|---|---|---|
| `agent_may_order` | Within every mandate rule | `confirmed` |
| `needs_human_approval` | Total above auto-approve limit; quantity > `unusual_order_factor` × usual weekly quantity; substitute | `pending_approval` (with `approve_by`) |
| `blocked` | Total above hard limit; category or lead time outside mandate | `rejected` |

Reasons are always returned in plain language.

## 6. Data the agent may see
**Visible:** product id, name, category, unit, the buyer's own price (`price_basis`: contract or list),
minimum order, in-stock flag, availability for the requested quantity only, lead time, supplier, labels, CO₂ estimate.

**Never visible:** exact stock levels, other customers' prices or orders, margins, internal plans.
Search is rate-limited per buyer (anti-scraping).

## 7. Human side
- `GET /api/human/pending`, `POST /api/human/orders/{id}/decision` — approve or reject with a note.
- `POST /api/human/mandates/{buyer}/revoke` and `/reinstate`.
- `GET /api/human/audit/verify` — recomputes the hash chain; any edited entry breaks it.
- Every confirmed order gets a `receipt` (SHA-256 of its key fields).

## 8. Open questions for Valio Aimo
- Should Valio also be able to require approval (e.g. new customers, very large orders)?
- Which identity standard should bind agent keys to legal entities in production?
- Should sustainability data (certificates, CO₂ ranges) be part of the standard response?
