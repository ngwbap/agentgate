# Handoff: AgentGate (Junction 2026, Valio Aimo "Trusted Trade Agents")

Written 2026-11-14 ~01:30 (Helsinki) for a fresh session on **Claude Fable 5.2**.
Read this file, then `README.md`. Don't re-explore what's described here.

## The situation in one paragraph
Team STEEL BALL is at Junction 2026 (Espoo, 13–15 Nov 2026) doing Valio Aimo's challenge: let an outside AI
purchasing agent buy from a food wholesaler **safely, openly, with the right human approval**. We built
**AgentGate**, Valio's "front door" for agents: **Smart Lookup** (one sentence → ranked, live-checked options)
plus a **door** that enforces the buyer's mandate and routes risky orders to a human. It runs on Valio's
**real product data and live mock APIs**. The user is a vibe coder with no business background; they want
**a clear plan before building**, and they want to be **told before any step that needs an expensive model**.

## Judging (from Valio's handout) — optimise for this
| Weight | Criterion |
|---|---|
| 30% | Working end-to-end demo |
| 20% | Solving the user's need |
| 20% | Autonomy and controls (guardrails) |
| 15% | Robustness to exceptions |
| 15% | Openness and path to production |
Their pick when forced to choose one core area: **Lookup**. Their "Insight" list asks for: trust protocol (agent can trust
stock/price/origin claims), guardrails model, **response logic for partial availability and substitution ranking
("good-better-best") within Valio's own assortment**, human-readable audit trail, **allergen-safe substitution**
(school/hospital), standing-order drift detection. "It is not a chatbot." "Query the APIs only when needed, never for all products."

## Deadlines
- Sat 14 Nov **21:00** code freeze (team plan) · Sun 15 Nov **09:30** set submission FINAL (hard deadline 10:00)
- Sun 10:30–13:00 live peer review (everyone present) · 2-minute video max
- Junction draft #1936 (Valio, correct) exists. Duplicate #1953 (wrong challenge, Rokoko) is fixed by a scheduled job
  in the *old* session at 12:03 Sat. Teammate also has a Stoa draft #1711 — don't touch.

## How to run (Windows, PowerShell)
- Python venv is **`venv\`** (official Python 3.14). Ignore the broken `.venv\` (MSYS2 Python; can't be deleted by the tool guard).
- `venv\Scripts\python.exe -m pytest -q` → 17 offline tests (synthetic catalog + fake Valio API in `tests/fakes.py`).
- `venv\Scripts\python.exe -m uvicorn app.main:app --port 8000` → UI at http://127.0.0.1:8000.
  A server from the old session may already be running on 8000 (background task in the old session); use `--port 8001` if busy.
- Secrets in **`.env`** (git-ignored): `VALIO_API_KEY`, `ANTHROPIC_API_KEY` (verified working). Never print or commit them.
- Valio data in **`data/valio/`** (git-ignored; Junction use only). Re-download: `venv\Scripts\python.exe scripts\download_data.py`.
- Valio's API is behind Cloudflare: requests **must send a non-default User-Agent** (`config.USER_AGENT`) or get 403/1010.
- The shell's safety guard blocks `Remove-Item`/`git rm` on paths containing "D:\junction 2026" — avoid deletions.
- Repo: https://github.com/ngwbap/steel-ball-agentgate (public). Commit messages end with
  `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.

## Architecture (all in `app/`)
| File | Role |
|---|---|
| `config.py` | loads `.env`; `CLAUDE_MODEL` (default `claude-opus-5-5`), `CLAUDE_EFFORT` (default `low`) |
| `catalog.py` | offline index of 17,546 products: EN/FI/SV names, GS1 allergen codes (contains / may contain / free from), claims (FREE_FROM_LACTOSE…), labels, pack size (`content_per_sales_unit`, L or KG), reliability, CO2. `search()` = name match (prefix + compound-word substring) → **category coherence** (milk ≠ milk chocolate) → claims → allergens → unit, returning funnel `steps` |
| `valio_api.py` | Price + Availability client: batching (≤1000), 5-min cache, call counter |
| `llm.py` | `ask_json()`: Claude structured JSON (`output_config.format` json_schema + effort), server-side fallback beta, cost per call |
| `understand.py` | request → structured need (`NEED_SCHEMA`); `judge_relevance()` exact/close/different; offline parser fallback |
| `lookup.py` | Smart Lookup pipeline: understand → shortlist (40) → pack-fit sort → judge 14 → live-check exact[:8]+close[:3] (**2 API calls**) → `build_option` (tier 1–4, flags, evidence) → `rank` → `badges` → `plan` |
| `core.py` | the door: mandate (`auto_approve_limit_eur`, `hard_limit_eur`, `food_only` via VAT ≥ 20%, `max_lead_time_days`, revoke), live quotes, `evaluate()` (block / needs_human_approval / agent_may_order), unusual-quantity per category (`usual_weekly` in `data/buyers.json`), supplier-cutoff-passed → approval, hash-chained audit, receipts |
| `agent.py` | demo flows: `lookup`, `order_plan`, `run_rogue` (over hard limit, non-food by real VAT, invented id) |
| `main.py` · `mcp_server.py` | HTTP API (`/api/v1/*` agent, `/api/human/*`, `/api/demo/*`, `/.well-known/agent-interface.json`) and MCP (`smart_lookup` etc.) |
| `web/index.html` | lookup funnel, plan card, option cards with badges/flags/evidence, approvals, audit |

## Real-data facts learned
- Milk query: 17,546 → 328 → 157 → 64 lactose-free → 48 by volume → 14 judged → 8 live. ~$0.025 AI per lookup (Opus 5.5, effort low), ~20 s.
- Most big-pack products are **"Terminal"** (supplier stock, amount 999999 = unknown) with `closingTime` **already passed**
  (Thu 12 Nov 15:00) yet `availableDate` = Mon 16 Nov. We treat it as "delivery at risk → human approval". **Open mentor question.**
- "Stock" products have real amounts (e.g. Arla 1 L lactose-free: 51 ST) → partial-availability cases are real.
- Pack size: price API `catalogPrice / comparisonPrice` gives content per sales unit (authoritative); static data used for shortlisting.
- `allowedLotSize` is mostly 0.01 → we round count units up to whole units.
- Unit codes (KI, ST, PRK…) are untranslated; KI ≈ case. **Open mentor question.**

## DONE (14 Nov, Fable): substitution "good-better-best" + exception logic
Everything in the design below is built, tested (27 offline tests) and checked in Chrome on real data.
Found on real data and fixed along the way:
- The shortlist used to pick only big supplier packs, so nothing was ever confirmed. `lookup._shortlist` now takes
  turns across pack sizes (16 judged, up to 10 exact + 3 close live). Milk now: Best = 1,000 × 1 L from Valio stock,
  confirmed, 1,100 €; Better = close variant (confirmed); Good = supplier pack (not confirmed).
- Many "Stock" products report amount 9999999999 / 666666666. We treat that as Valio stock with no limit reported
  (confirmed) and say so in the evidence. **Open mentor question: is that right?**
- Oat drinks carry VEGAN, not FREE_FROM_LACTOSE. `catalog.claim_proof`: a stronger claim proves a weaker one
  (vegan ⇒ lactose-free, egg-free); the evidence shows "implied by the vegan claim".
- Demo line for strict allergens + lazy substitutes: "Our school kitchen needs 40 kg of chocolate cookies by Monday.
  A pupil has a hazelnut allergy." → 3 excluded for no allergen data, 4 API calls, Best = confirmed substitute.

### Original brief (kept for reference)
Status when written: **3 small edits already applied** in `understand.py`: `NEED_SCHEMA` items now require `substitutes`
(up to 2 other product types for the same culinary purpose, each `{label, product_terms, trade_off}`), the
`SYSTEM` prompt explains substitutes + strict_allergens, and `parse_offline` returns `"substitutes": []`.
**Not yet done:** everything below.

### Design (agreed with the user)
**Good-better-best ranks fulfilment PLANS, not single products.** Each plan scores
`0.55 × delivery certainty + 0.45 × match fidelity`, price as tie-breaker. Show the numbers in the UI.
- Line certainty: Valio stock, on time, cutoff OK = 1.0 · supplier ("Terminal"), on time, cutoff OK = 0.6 ·
  supplier with cutoff passed = 0.25 · uncovered quantity = 0. Plan certainty = content-weighted, capped at 1.
- Fidelity: exact 1.0 · close variant (other fat %, pack, brand, UHT) 0.8 · functional substitute 0.5. Content-weighted.
- Candidate plans to enumerate: (A) single exact confirmed · (B) exact split across Valio-stock products (cheapest per unit
  first) · (C) confirmed plan using close variants (alone or mixed with exact) · (D) exact Valio stock first + remainder from
  the best exact supplier option ("partly confirmed") · (E) single exact supplier option · (F) functional substitute
  (confirmed). Dedupe; take the top 3 distinct → labelled **Best / Better / Good**.
- Each plan: `label, kind, confidence (confirmed | partly confirmed | not confirmed), lines (with match + source),
  covered, total_eur, price_per_unit, certainty, fidelity, score, trade_offs[], approval_needed[], why`.
  Keep `result["plan"] = plans[0]` for backward compatibility; add `result["plans"]`.
- **Hard rules never traded away:** must_claims and excluded allergens apply to substitutes too.
- **Strict allergen mode** (allergy / school / daycare / hospital): exclude CONTAINS and MAY_CONTAIN, and a product with
  **no allergen data at all** (no contains, may_contain or free_from) is **unverified, not safe** → exclude it and show the
  count in the funnel ("n excluded: no allergen data, needs a human check").
- **Substitutes are checked lazily:** judge primary + substitute candidates in ONE Claude call (extend relevance enum to
  `exact | close | substitute | different`; reason = the trade-off). Live-check substitutes (2 more API calls) **only if no
  confirmed exact plan exists**, and say so in the funnel.
- **Option ranking polish:** price score = min_price / price (not min-max); reliability score = clip((r − 0.85) / 0.15),
  unknown = 0.6; CO2 = min / value; small penalty for over-ordering (rounding up to whole cases).
- **Door:** `place_order(..., declared_risks: list[str])` (also in HTTP + MCP); any declared risk → `needs_human_approval`
  with "Agent declared: …". `agent.order_plan` passes `is_substitute` when a line isn't exact, and the plan's
  `approval_needed` as declared risks.
- **UI:** replace the single plan card with three cards (Best / Better / Good): certainty %, fidelity, total, €/L,
  trade-offs, approval reasons, an "Order this plan" button on each.
- **Tests** (`tests/test_flow.py`, offline): with the fakes, the milk request's Best plan should be (D) 60 KI of 900001
  (600 L Valio stock) + 20 KI of 900002 (400 L supplier) = 1,400 €, "partly confirmed"; Better = (E) 900002 alone.
  Ordering Best → pending approval with: over limit, 8.3× usual, cutoff passed. Add unit tests for `build_plans` with
  hand-made options (confirmed close variant beats unconfirmed exact) and for strict-allergen evidence.
  Update the two existing tests that assumed the old single plan.
- Then: run tests, restart the server, check milk / Finnish / small order / misbehaving agent in the browser
  (Claude in Chrome works; the built-in browser can't reach localhost), commit, push.

### After that (switch back to Opus/Sonnet for these)
- Update `docs/DESIGN.md` and `docs/AGENT_INTERFACE_SPEC.md` (they still mention the old mock catalog/contract prices).
- Delete the unused old mock file `data/catalog.json` by hand (tool guard blocks it).
- Pitch prep: webshop "before" (search "lactose free milk" → milk chocolate first), 2-minute video, deck.
