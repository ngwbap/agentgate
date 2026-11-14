# Valio Aimo Agent Interface — v0.2 (draft)

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
- Header `X-Agent-Key: <key>`; each key is bound to exactly one buyer's mandate.
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
`score = 0.55 × certainty + 0.45 × fidelity`, total price breaks ties.

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
- Every confirmed order gets a `receipt` (SHA-256 of its key fields).

In production these endpoints sit behind the buyer's own login; they are open in the demo.

## 9. Open questions for Valio Aimo
- Should Valio also be able to require approval (e.g. new customers, very large orders)?
- Which identity standard should bind agent keys to legal entities in production?
- When a supplier's order cutoff has passed but a delivery date is still offered, which is binding?
- What does a very large stock amount (e.g. 9999999999) mean: no limit, or unknown?
- Should sales unit codes (KI, ST, PRK…) be translated in the standard response?
