# AgentGate — design (team STEEL BALL, Junction 2026)

Challenge: **Valio Aimo — Trusted Trade Agents**

> **Any AI agent can order from Valio Aimo, but no agent can break Valio's rules.**

## The problem in one paragraph
Professional kitchens will soon let AI agents do their purchasing. Valio Aimo (20,000+ customers,
25,000 products) must decide how those agents interact with it: what they can see, what they may
decide alone, and when a human must step in — and it must define this **itself, on open standards**,
before a big platform defines it for them.

## Our answer: build the door, not the robot
We build Valio Aimo's **front door for agents** (AgentGate). Any agent — ChatGPT, Gemini, Claude, or a
customer's own — connects through it. The rules live **in the door**, so a buggy, confused or hijacked
agent still cannot overspend, see secret data, or act after being switched off.

| | Front door (ours) | Smart buyer agent |
|---|---|---|
| Where rules live | At Valio — cannot be bypassed | Inside the agent — fail if the agent fails |
| Protects | Every customer, every agent type | One customer |
| Who owns the interface | Valio | Whoever builds the agent |

## Actors
| Who | Role |
|---|---|
| Kitchen (Hotel Aurora) | Needs goods; sets its agent's mandate |
| Buyer's AI agent | Searches, compares, quotes, orders (off-the-shelf; our demo agent is just one example) |
| **AgentGate** (we build) | Lookup, quote → order, mandate checks, unusual-order guard, audit log |
| Kitchen manager | Approves or rejects orders above the agent's authority; can revoke the mandate |
| Valio Aimo systems | Catalog, prices, stock (mocked) |

## The three core requirements
1. **Lookup** — search (English or Finnish), price, availability, delivery date.
2. **Inquiry → order** — fixed-price quote (no negotiation), then order with a mandatory human-readable reason.
3. **Permissions & approval** — the door returns one of: `agent_may_order` · `needs_human_approval` · `blocked`.

## Standout features
1. **Agent mandate card** — a digital power of attorney issued by the buyer: spending limits, allowed
   categories, max lead time, validity, and a **Revoke** button that stops the agent instantly.
2. **Unusual-order guard** — even under the money limit, a quantity more than 3× the buyer's normal
   weekly order goes to a human (catches typos and hallucinations: 1,000 L vs. the usual 120 L).
3. **Need-to-know data** — each buyer sees only its own contract prices; no exact stock levels, no other
   customers, no internal data; searches are throttled against scraping.
4. **Open, verifiable interface** — REST/OpenAPI + MCP, a public spec (`docs/AGENT_INTERFACE_SPEC.md`),
   a discovery file at `/.well-known/agent-interface.json`, a **hash-chained audit log** and a receipt hash per order.
5. **Approval deadline** — the approver sees "approve by Sat 14:00 to keep Monday delivery".

## Rules for Hotel Aurora (demo mandate MND-AURORA-01)
| Situation | Door decision |
|---|---|
| Total ≤ 1,000 € and normal quantities | Agent orders alone |
| Total > 1,000 € | Kitchen manager approves |
| Quantity > 3× usual weekly order | Kitchen manager approves |
| Substitute product | Kitchen manager approves |
| Total > 10,000 € | Blocked |
| Category not in mandate (e.g. meat) | Blocked |
| Mandate revoked or expired | Every call refused (HTTP 403) |
| No reason given / can't deliver in time / not enough stock | Refused |

## Demo script (≈ 60 s of the 2-minute video)
1. "I need 1,000 L of lactose-free milk by Monday." Agent compares 4 options; only Lakeland Farms has
   1,000 L in time → quote 1,450 € → door: **needs approval** (over 1,000 € and 8.3× the usual order).
2. Manager sees the order, the agent's reason, and "approve by Sat 14:00" → **Approve** → confirmed, receipt issued.
3. Same request in Finnish ("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi") → same result.
4. Small order (24 L oat drink, 43 €) → agent orders alone.
5. **Misbehaving agent**: tries 10,690 € and a meat order → both **rejected** by the door.
6. Manager clicks **Revoke** → the agent's next request is refused. Audit trail: *chain verified*.

## Out of scope
A full marketplace · replacing the webshop with a chatbot · price negotiation · real Valio systems · delivery-failure handling (last year's challenge).

## Judge questions we're ready for
| Question | Our answer |
|---|---|
| What stops an agent ordering 10,000 L instead of 1,000? | Mandate hard limit + unusual-order guard, enforced at the door |
| How do you know the agent acts for Hotel Aurora? | Each agent key is bound to one buyer's mandate |
| How does the hotel switch it off? | Revoke button: instant 403 on every call |
| What can a competitor's agent learn? | Only its own prices; no stock levels, no other customers; scraping throttled |
| Manager doesn't approve in time? | Approval deadline shown; late approval flagged as "delivery not guaranteed" |
| Who's liable for a wrong order? | Hash-chained log + receipt prove which mandate allowed it and who approved |
| Why not let a big AI platform integrate? | Then the platform owns the customer relationship; this way Valio sets the rules for all agents |
