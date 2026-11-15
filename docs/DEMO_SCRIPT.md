# AgentGate - Demo Video Script

Target length: **about 3 minutes** (~400 spoken words at a calm pace).
Record the browser window only, 1920×1080, browser zoom 110-125 % so text is readable.

---

## Before you record (checklist)

1. Start the server: `venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`
2. Check `.env` has `VALIO_API_KEY` (live prices) and `ANTHROPIC_API_KEY` (better understanding).
3. Open **http://127.0.0.1:8000/?demo**, pick **Hotel Aurora**, press **Reset demo**.
4. Open **http://127.0.0.1:8000/orders** in a second tab. If you are signed in, press **Sign out**.
5. Do one full dry run (steps below) without recording, then **Reset demo** again.
   The first run also warms the AI cache, so the recorded run is fast.
6. Close other tabs, turn off notifications (Windows: Focus assist), hide bookmarks bar.
7. Record with **Win+Alt+R** (Xbox Game Bar) or OBS. Use a headset mic if you have one.

Tip: record each part separately and join them later. A mistake then costs you 20 seconds, not 3 minutes.

---

## Part 1 - The problem (0:00-0:20)

**Screen:** Buying page (`/?demo`), empty, Hotel Aurora selected.

> "Professional kitchens are starting to let AI agents do their purchasing. That's a problem for a wholesaler like Valio Aimo. Today, if you search the webshop for lactose-free milk, milk chocolate comes up first - and an AI agent with a credit line can order far too much, or the wrong thing, with nobody watching."

## Part 2 - The idea (0:20-0:30)

**Screen:** same page; slowly move the mouse over the top bar and the step rail.

> "We built AgentGate: Valio's front door for AI purchasing agents. Any agent can order from Valio - but no agent can break Valio's rules, or the customer's."

## Part 3 - Ask in plain language (0:30-1:10)

**Screen / clicks:**
1. Type: `120 L lactose free milk by Friday` and press Enter.
2. Pause on the **understood criteria** (product, quantity, unit, deadline, claim). Point at them.
3. Pause on the **Best / Better / Good** options. Point at price per litre and delivery.
4. Point at the **funnel** in the demo panel.
5. Pick **Best** and press **send** on the order sheet.

> "A kitchen manager - or their agent - just says what they need, in English or Finnish. Claude turns it into clear criteria: 120 litres, lactose-free, by Friday. You can check and fix every box.
>
> Then the rules take over. We search Valio's 17,546 products offline, keep only real lactose-free milk - no chocolate - and check live price and stock for just the shortlist: one availability call, one price call.
>
> You get three ways to fulfil the order - Best, Better, Good - with price per litre, whether it arrives in full and on time, and the trade-offs. I'll take Best and send it."

## Part 4 - A human approves (1:10-1:55)

**Screen / clicks:**
1. Switch to the **Orders** tab (`/orders`). The order is under **Waiting for you**.
2. Type a wrong code, e.g. `1234`, press **Sign in** - "That code isn't right."
3. Type `demo-approver-aurora`, press **Sign in**.
4. Open **Full reasons** on the order card.
5. Press **Change**, lower the quantity, press **Approve with changes**.
6. Open **Done** and point at the receipt hash.

> "The order didn't go through by itself. It's waiting for the kitchen manager. And here's the key idea: approving is a human's job. It needs the manager's code - the agents don't have it.
>
> A wrong code is refused. With the right one, I see exactly why this order needs me, and the agent's own reason for it.
>
> I can approve, reject, or change it. Let's lower the amount. The new total is re-priced live and checked against the rules again. Approved - and the order gets a signed receipt, so anyone can prove what was promised and who said yes."

## Part 5 - When an agent misbehaves (1:55-2:30)

**Screen / clicks:**
1. Back on the Buying tab (`/?demo`), press **Misbehaving agent**. Let the results appear.
2. Go to **Orders → Settings**. Press **Pause** on "Hotel's own AI agent".

> "Now the scary case: a confused or hijacked agent. It tries to order far above the limit, to buy things that aren't food… and the door stops every attempt.
>
> And if the manager doesn't trust an agent any more, one click pauses it. From that moment, every request it makes is refused at Valio's door - the other agents keep working."

## Part 6 - Trust and openness (2:30-2:50)

**Screen / clicks:**
1. Open **History**. Point at the green "History verified" banner.
2. (Optional) Show `/docs` or a Claude Desktop window using the MCP server for 3 seconds.

> "Every step is written to a hash-chained log, so nobody can quietly change history afterwards. And the door is open: REST, OpenAPI and MCP - so Valio's own assistant, Claude, or a customer's own bot all go through the same rules."

## Part 7 - Close (2:50-3:00)

**Screen:** back to the Buying page or a title slide with the team name.

> "AgentGate: AI understands, rules decide, humans approve. Any agent can order from Valio Aimo - no agent can break the rules. We're team STEEL BALL. Thank you."

---

## Full transcript (read straight through)

Professional kitchens are starting to let AI agents do their purchasing. That's a problem for a wholesaler like Valio Aimo. Today, if you search the webshop for lactose-free milk, milk chocolate comes up first - and an AI agent with a credit line can order far too much, or the wrong thing, with nobody watching.

We built AgentGate: Valio's front door for AI purchasing agents. Any agent can order from Valio - but no agent can break Valio's rules, or the customer's.

A kitchen manager - or their agent - just says what they need, in English or Finnish. Claude turns it into clear criteria: 120 litres, lactose-free, by Friday. You can check and fix every box.

Then the rules take over. We search Valio's 17,546 products offline, keep only real lactose-free milk - no chocolate - and check live price and stock for just the shortlist: one availability call, one price call.

You get three ways to fulfil the order - Best, Better, Good - with price per litre, whether it arrives in full and on time, and the trade-offs. I'll take Best and send it.

The order didn't go through by itself. It's waiting for the kitchen manager. And here's the key idea: approving is a human's job. It needs the manager's code - the agents don't have it.

A wrong code is refused. With the right one, I see exactly why this order needs me, and the agent's own reason for it.

I can approve, reject, or change it. Let's lower the amount. The new total is re-priced live and checked against the rules again. Approved - and the order gets a signed receipt, so anyone can prove what was promised and who said yes.

Now the scary case: a confused or hijacked agent. It tries to order far above the limit, to buy things that aren't food… and the door stops every attempt.

And if the manager doesn't trust an agent any more, one click pauses it. From that moment, every request it makes is refused at Valio's door - the other agents keep working.

Every step is written to a hash-chained log, so nobody can quietly change history afterwards. And the door is open: REST, OpenAPI and MCP - so Valio's own assistant, Claude, or a customer's own bot all go through the same rules.

AgentGate: AI understands, rules decide, humans approve. Any agent can order from Valio Aimo - no agent can break the rules. We're team STEEL BALL. Thank you.

---

## If something goes wrong on camera

- **Search is slow:** keep talking over it ("we're checking live stock with Valio now"), or cut it out in editing.
- **No options appear:** the Valio API key or data files are missing - check `.env` and `data/valio/`, then reset.
- **Sign-in box doesn't show:** you're still signed in from the dry run - press **Sign out**.
- **Order shows under the wrong customer:** the customer picker at the top must match on both tabs.
