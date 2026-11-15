# AgentGate — design (team STEEL BALL, Junction 2026)

Challenge: **Valio Aimo — Trusted Trade Agents**

> **Any AI agent can order from Valio Aimo, but no agent can break Valio's rules.**

## The problem in one paragraph
Professional kitchens will soon let AI agents do their purchasing. Valio Aimo must decide how those
agents interact with it: what they can see, what they may decide alone, and when a human must step
in. It must define this **itself, on open standards**, before a big platform defines it for them.

## Our answer: build the door, not the robot
We build Valio Aimo's **front door for agents** (AgentGate). Any agent (ChatGPT, Gemini, Claude, or a
customer's own) connects through it. The rules live **in the door**, so a buggy, confused or hijacked
agent still cannot overspend, buy outside its mandate, or act after being switched off.

| | Front door (ours) | Smart buyer agent |
|---|---|---|
| Where rules live | At Valio: cannot be bypassed | Inside the agent: fail if the agent fails |
| Protects | Every customer, every agent type | One customer |
| Who owns the interface | Valio | Whoever builds the agent |

It is not a chatbot. One sentence goes in; structured, evidenced, rule-ranked plans come out.

## Actors
| Who | Role |
|---|---|
| Kitchen (Hotel Aurora) | Needs goods; issues its agent's mandate |
| Buyer's AI agent | Looks up, quotes, orders (off-the-shelf; our demo agent is just one example) |
| **AgentGate** (we build) | Smart Lookup, quote → order, mandate checks, unusual-order guard, audit log |
| Kitchen manager | Approves or rejects orders above the agent's authority; can revoke the mandate |
| Valio Aimo | Real product data file (17,546 products) + live Price and Availability APIs |

## Part 1: Smart Lookup ("AI understands, rules decide")
AI is used only for language: understanding the request and judging whether a product is what was
asked for. Every number, filter and ranking is a plain rule that can be shown and tested.

| Step | What happens | Milk example (1,000 L lactose-free by Monday) |
|---|---|---|
| 1. Understand | Claude turns EN/FI/SV text into a structured need: product, quantity, unit, date, claims, allergens, up to 2 substitute ideas. Offline parser if the AI is down. | milk · 1,000 L · lactose-free · substitutes: oat drink, soy drink |
| 2. Shortlist offline | Name match (incl. Finnish compounds) → category coherence (milk ≠ milk chocolate) → claims → allergens → unit. | 17,546 → 328 → 157 → 64 → 48 |
| 3. Judge | One Claude call labels the shortlist `exact / close / substitute / different`. The shortlist takes turns across pack sizes, so single cartons (Valio's own stock) are not crowded out by big supplier packs. | 16 products + 8 substitute candidates judged |
| 4. Live check | **One availability call + one price call**, shortlist only. Never the whole catalog. | 11 products, 2 API calls |
| 5. Rank products | Can it arrive in full and on time? Then price per litre, reliability, CO₂, weighted by the buyer's priorities. | |
| 6. Build plans | Up to three ways to fulfil the need: Best / Better / Good. | see below |

Every product shows its **evidence**: price API validity dates, availability timestamp, product-data
date and GTIN, how each required claim is proven, and the allergen declaration.

### Good-better-best plans
We rank **fulfilment plans, not single products**, because the real answer is often a combination.

`score = 0.40 × delivery certainty + 0.35 × match fidelity + 0.25 × the buyer's own criteria` (price by default) · total price breaks ties · the numbers are shown in the UI. One ranking: "Best" is the top score, and a plan with more stars but less certainty says so on its badge.

| Delivery certainty (per line) | | Match fidelity (per line) | |
|---|---|---|---|
| Valio's own stock, on time, cutoff open | 1.0 | Exactly what was asked | 1.0 |
| Supplier stock, on time, cutoff open | 0.6 | Close variant (other fat %, UHT, pack) | 0.8 |
| Order cutoff already passed | 0.25 | Substitute (other product type) | 0.5 |
| Quantity not covered | 0 | | |

Both are weighted by how much of the need each line covers.

| Plan kind | What it is | Confidence |
|---|---|---|
| single | One exact product from Valio's stock | confirmed |
| split | Exact, split across several in-stock products, cheapest first | confirmed |
| close variant | Confirmed by accepting a close variant | confirmed |
| stock + supplier | Valio stock first, the rest from the best supplier option | partly confirmed |
| supplier | One supplier product, quantity or date not guaranteed | not confirmed |
| substitute | Confirmed by using a substitute product | confirmed |
| partial | Nothing covers it all; shows what can be delivered | partly confirmed |

Each plan lists its **trade-offs** and the **risks the agent will declare to the door**.
We pick three different kinds where possible, so the choice is real.

Real result for the milk request: **Best** 1,000 × 1 L from Valio stock, confirmed, 1,100 € ·
**Better** a UHT close variant, confirmed, 2,860 € · **Good** a supplier pack, not confirmed, 1,190 €.

### Exceptions
- **Hard rules are never traded away.** Dietary claims and allergen exclusions apply to substitutes too.
- **Lazy substitute check.** Substitutes are checked live (2 more API calls) only when the requested
  product cannot be confirmed. The funnel says which happened.
- **Strict allergen mode** (allergy, school, daycare, hospital): "may contain" is excluded, and a
  product with **no allergen data is unverified, not safe**. It is dropped and counted for a human check.
- **A stronger claim proves a weaker one.** Vegan implies lactose-free and egg-free; the evidence says "implied by".
- **Partial availability.** If nothing covers the full quantity, the plan says how much can be delivered.
- **API outage.** The lookup still returns the shortlist and says live data is unavailable; no plan is invented.

## Part 2: The door (guardrails)
The door returns one of: `agent_may_order` · `needs_human_approval` · `blocked`, always with plain reasons.

1. **Agent mandate.** A digital power of attorney issued by the buyer: auto-approve limit, hard limit,
   food only, max lead time, validity, and a **Revoke** button that stops the agent instantly.
2. **Unusual-order guard.** Even under the money limit, more than 3× the buyer's normal weekly quantity
   in a category goes to a human (catches typos and hallucinations: 1,000 L vs. the usual 120 L).
3. **Delivery risk.** If a supplier's order cutoff has passed, a human must accept the risk.
4. **Declared risks.** The agent passes what it is unsure about (`declared_risks`). Any entry sends the
   order to a human. An agent can only **add** caution; it can never remove a rule or pass a block.
5. **Approval deadline.** The approver sees "approve by Sun 17:00 to keep the delivery date".
6. **Need-to-know data.** An agent sees product data plus its own buyer's price and availability for
   the products it asks about. Never other customers, margins or internal plans. Searches are throttled.
7. **Open, verifiable interface.** REST/OpenAPI + MCP, a public spec (`docs/AGENT_INTERFACE_SPEC.md`),
   a discovery file at `/.well-known/agent-interface.json`, a **hash-chained audit log** and a receipt per order.

### Rules for Hotel Aurora (demo mandate MND-AURORA-01)
| Situation | Door decision |
|---|---|
| Total ≤ 1,000 €, normal quantities, nothing declared | Agent orders alone |
| Total > 1,000 € | Kitchen manager approves |
| Quantity > 3× usual weekly order | Kitchen manager approves |
| Supplier cutoff passed | Kitchen manager approves |
| Substitute or close variant | Kitchen manager approves |
| Any risk declared by the agent | Kitchen manager approves |
| Total > 10,000 € | Blocked |
| Not food (VAT 25.5 %, read from the live price) | Blocked |
| Delivery later than the mandate's lead time | Blocked |
| Mandate revoked or expired | Every call refused (HTTP 403) |
| No reason given / not enough stock / no price | Refused |

## Demo script (≈ 60 s of the 2-minute video)
1. **Before.** The webshop search for "lactose free milk" lists milk chocolate first.
2. "I need 1,000 L of lactose-free milk by Monday." Funnel 17,546 → 11 checked live in 2 API calls →
   three plans. **Best**: 1,000 × 1 L from Valio stock, confirmed, 1,100 €.
3. Order Best → door: **needs approval** (over 1,000 € and 8.3× the usual week). Manager sees the
   reason and "approve by" time → **Approve** → confirmed, receipt issued.
4. Same request in Finnish → same plans.
5. "Our school kitchen needs 40 kg of chocolate cookies by Monday. A pupil has a hazelnut allergy."
   → strict mode: 3 products dropped for missing allergen data; no confirmed exact product, so
   substitutes are checked (4 API calls); Best = a confirmed substitute that needs human sign-off.
6. Small order (24 L oat drink, 34 €) → agent orders alone.
7. **Misbehaving agent**: 13,000 € order, dishwashing detergent, invented product id → all stopped.
8. Manager clicks **Revoke** → the agent's next request is refused. Audit trail: *chain verified*.

## Out of scope
A full marketplace · replacing the webshop with a chatbot · price negotiation · writing real orders
into Valio's systems (orders are saved to a local file, not to Valio's order system) · delivery-failure handling.

## Open questions for Valio's mentors
- Supplier ("Terminal") products show a cutoff that has passed but a delivery date that is still on time. Which one wins?
- Some "Stock" products report an amount like 9999999999. We read that as "in stock, no limit". Correct?
- Sales unit codes (KI, ST, PRK…) are untranslated. Is KI a case?

## Judge questions we're ready for
| Question | Our answer |
|---|---|
| What stops an agent ordering 10,000 L instead of 1,000? | Mandate hard limit + unusual-order guard, enforced at the door |
| How can the agent trust stock, price and origin claims? | Every option carries its evidence: source, timestamp, validity |
| What if the product is out of stock? | Good-better-best plans: split, close variant, stock + supplier, substitute, each with its trade-offs |
| Is a substitute safe for an allergic pupil? | Claims and allergens are hard rules for substitutes; no allergen data means excluded, not assumed safe |
| Do you query the APIs for everything? | No: 2 calls for about 11 products out of 17,546; 2 more only if substitutes are needed |
| How do you know the agent acts for Hotel Aurora? | Each agent key is bound to one buyer's mandate |
| How does the hotel switch it off? | Revoke button: instant 403 on every call |
| Manager doesn't approve in time? | Approval deadline shown; late approval flagged as "delivery not guaranteed" |
| Who's liable for a wrong order? | Hash-chained log + receipt prove which mandate allowed it and who approved |
| Where is the AI, and what if it is wrong? | Only in language steps; rules filter and rank, the door decides, and an offline parser takes over if the AI is down |
