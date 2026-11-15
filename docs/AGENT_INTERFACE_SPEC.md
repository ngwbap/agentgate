# Valio Aimo Agent Interface — v0.3 (draft)

An open specification for how external purchasing agents interact with Valio Aimo.
Reference implementation: AgentGate (this repository).

## 1. Principles
1. **The door enforces the rules.** Agents are never trusted to limit themselves.
2. **Agents act only under a mandate** issued by the buyer, and the buyer can revoke it at any time.
3. **Need-to-know.** An agent sees only what its buyer may see.
4. **Fixed prices.** Valio's price for that customer and date. No negotiation through the agent interface.
5. **Narrow first, ask live last.** Static product data narrows the search; live price and availability are asked only for a shortlist.
6. **Everything is explainable and verifiable.** Every option carries its evidence, every order carries the agent's reason, every action is logged in a tamper-evident chain.
7. **An agent can add caution, never remove it.** Declared risks route an order to a human; nothing an agent says can pass a block.
8. **Open transports.** HTTP (OpenAPI) and MCP expose the same operations with the same rules.

## 2. Discovery
`GET /.well-known/agent-interface.json` — name, version, auth method, transports, tools, lookup policy, plan scoring, rules and data policy.

## 3. Authentication & mandate
- Header `X-Agent-Key: <key>`; each key is bound to exactly one buyer's mandate. A missing or unknown key returns **401**.
- Approving is a different, human-only key: header `X-Approver-Key`. An agent key is refused on every approver
  endpoint, so an agent can never approve its own order, change its own limits or un-pause itself.
- Both key types are declared as OpenAPI security schemes (`AgentKey`, `ApproverKey`) in `/openapi.json`.
- A mandate contains: `id`, `agent_name`, `issued_by`, `valid_until`, `auto_approve_limit_eur`,
  `hard_limit_eur`, `food_only`, `max_lead_time_days`, `substitutes_need_approval`,
  `unusual_order_factor`, `revoked`. The buyer's `usual_weekly` quantities per category are returned with it.
- Revoked or expired mandate → every call returns **403** with an explanation.
- `GET /api/v1/mandate` — an agent can always read its own authority.

## 4. Operations
| Operation | HTTP | MCP tool |
|---|---|---|
| Read own mandate | `GET /api/v1/mandate` | `get_my_mandate` |
| **Smart Lookup** (one sentence, EN/FI/SV) | `POST /api/v1/lookup` `{request}` | `smart_lookup` |
| Search catalog (offline, no prices) | `GET /api/v1/products?q=` | `search_products` |
| Live price & availability, one product | `GET /api/v1/products/{id}/availability?quantity=&deliver_by=` | `check_availability` |
| Fixed-price quote | `POST /api/v1/quotes` `{items:[{product_id, quantity}], deliver_by}` | `request_quote` |
| Place order (reason required) | `POST /api/v1/orders` `{quote_id, reason, is_substitute, declared_risks}` | `place_order` |
| Order status | `GET /api/v1/orders/{id}` | `get_order_status` |

Quantities in quotes are in **sales units** (e.g. cases). Every quote includes a `policy_preview`,
so the agent knows the door's decision **before** ordering. Quotes are valid for 30 minutes and can be used once.

## 5. Smart Lookup response
Recommended first call. Per requested item:

| Field | Content |
|---|---|
| `item` | The structured need: `label`, `product_terms`, `quantity`, `unit` (L, KG, PCS), `must_claims`, `exclude_allergens`, `strict_allergens`, `preferred_labels`, `substitutes` |
| `funnel` | How the catalog was narrowed: each filter step with the number remaining, how many were judged and checked live, and what happened with substitutes |
| `options` | Each live-checked product: match (`exact`, `close`, `substitute`), pack size, units needed, price, status (`in_stock`, `partial`, `supplier`, `unavailable`, `blocked`), delivery date, cutoff, reliability, CO₂, claims, labels, allergens, `flags`, `evidence`, score |
| `plans` | Up to three fulfilment plans, best first (`plan` = the first) |
| `rejected` | Products judged a different kind of product, with the reason |

Top level: `deliver_by`, `live_error`, `plan_scoring`, `api` (calls made, products checked), `ai_usage`.

### 5.1 Plans (good-better-best)
`score = 0.40 × certainty + 0.35 × fidelity + 0.25 × criteria`, where criteria (0 to 1) is how the plan does on what the buyer said matters: price by default, reliability and carbon footprint if asked. Total price breaks ties.

A brand or pack size named in the request is a requirement: products of another brand or size are never `exact`, only `close` (reason e.g. "another brand, not Arla"), and so need approval. This is a rule on Valio's product data, not an AI opinion.

The same request returns the same plans: the two language steps are cached by a fingerprint of the exact question.

Requests that cannot work are refused plainly and before any live call: a delivery date in the past returns **400** with the date; a product Valio does not carry returns no plans and says so.

| | Value |
|---|---|
| Certainty per line | Valio stock, on time, cutoff open 1.0 · supplier stock 0.6 · cutoff passed 0.25 · not covered 0 |
| Fidelity per line | exact 1.0 · close 0.8 · substitute 0.5 |

Each plan: `label` (Best, Better, Good), `kind` (`single`, `split`, `close variant`, `stock + supplier`,
`supplier`, `substitute`, `partial`), `confidence` (`confirmed`, `partly confirmed`, `not confirmed`),
`lines` (`product_id`, `units`, `content`, `match`, `source`, `certainty`, price), `covered`, `total_eur`,
`price_per_unit`, `certainty`, `fidelity`, `score`, `trade_offs`, `approval_needed`, `is_substitute`, `why`.

**To order a plan:** quote its lines (`product_id`, `units`), then place the order with the plan's `why`
as the reason, `is_substitute` as given, and `approval_needed` as `declared_risks`.

### 5.2 Hard rules in lookup
- `must_claims` and `exclude_allergens` apply to every option, substitutes included.
- A required claim is proven when it is declared, or implied by a stronger declared claim (vegan ⇒ lactose-free, egg-free, vegetarian). The evidence states which.
- `strict_allergens` (allergy, school, daycare, hospital): products that may contain the allergen are excluded, and products with **no allergen declaration at all are excluded as unverified**. The funnel reports the count.
- Live calls: one availability call and one price call for the shortlist. Substitutes are checked live (two more calls) only when no confirmed plan exists for the requested product.

## 6. Decisions
| Decision | When | Order status |
|---|---|---|
| `agent_may_order` | Within every mandate rule and nothing declared | `confirmed` |
| `needs_human_approval` | Total above the auto-approve limit · quantity above `unusual_order_factor` × the usual weekly quantity in a category · a supplier's order cutoff has passed · `is_substitute` · any `declared_risks` entry | `pending_approval` (with `approve_by`) |
| `blocked` | Total above the hard limit · non-food under a food-only mandate (VAT rate from the live price) · lead time beyond the mandate | `rejected` |

A block always wins over an approval. Reasons are always returned in plain language; declared risks
appear as `Agent declared: …`. Up to 10 declared risks of 300 characters each are kept.

Refused before any decision (HTTP 400): unknown product, no price, sales block, more than Valio has
in stock, delivery after the requested date, expired or reused quote, missing reason.

## 7. Data the agent may see
**Visible:** product data (names, brand, pack size, origin, claims, labels, allergens, delivery
reliability, CO₂ per kg), and for the products it asks about: the buyer's own price, availability,
delivery date, order cutoff and VAT rate.

**Never visible:** other customers' prices or orders, margins, internal plans.
Search is rate-limited per buyer (30 per minute, anti-scraping).

## 8. Human side
- `GET /api/human/pending`, `POST /api/human/orders/{id}/decision` — approve or reject with a note. Approving after `approve_by` adds a warning that the delivery date is no longer guaranteed.
- `POST /api/human/mandates/{buyer}/revoke` and `/reinstate`.
- `GET /api/human/audit/verify` — recomputes the hash chain; any edited entry breaks it.
- Every confirmed order gets a `receipt` (SHA-256 of its key fields) and `receipt_signed`.
- All of these except `audit/verify` need `X-Approver-Key`. The approver's name on a decision comes from the key,
  never from the request body, and an approver can only see and decide their own buyer's orders.
  In production the key is issued by the buyer's own sign-in; in the demo the approver types it once on the orders page.

## 8.1 Signed quotes and receipts (the trust protocol)
An agent has to be able to trust what the door says, and prove it later.
- Every quote carries `signed`: `{payload, alg: "Ed25519", key_id, signature}`. The payload holds the quote id, buyer,
  each line's product, quantity, fixed unit price, **stock source** and **available date**, the total, and `valid_until`.
- Every confirmed order carries `receipt_signed` with the order, the total, the lines and **who decided**.
- The signature is Ed25519 over the canonical JSON of `payload` (sorted keys, no spaces, UTF-8).
- The public key is published as a standard JWKS at `/.well-known/jwks.json`, so anyone can verify offline.
  `POST /api/v1/verify` does the same check for you.

## 8.2 Standards used
| Need | Standard |
|---|---|
| API description | OpenAPI 3.1 (`/openapi.json`) |
| Agent tools | Model Context Protocol (MCP), same seven operations |
| Signing keys | JWKS, RFC 7517; signatures EdDSA / Ed25519, RFC 8032 |
| Allergens, claims, labels | GS1 codes from Valio's product data |

Not implemented, and the natural next step for production: binding agent keys to legal entities with an existing
identity standard (see open questions), and publishing an A2A agent card once the door speaks that protocol.

## 9. Open questions for Valio Aimo
- Should Valio also be able to require approval (e.g. new customers, very large orders)?
- Which identity standard should bind agent keys to legal entities in production?
- When a supplier's order cutoff has passed but a delivery date is still offered, which is binding?
- What does a very large stock amount (e.g. 9999999999) mean: no limit, or unknown?
- Should sales unit codes (KI, ST, PRK…) be translated in the standard response?
