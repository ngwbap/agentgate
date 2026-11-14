import os

from app import config  # noqa: F401  (loads .env first, so we can switch the real APIs off below)

os.environ["DEMO_TODAY"] = "2026-11-14"           # a Saturday -> "by Monday" = 2026-11-16
os.environ.pop("ANTHROPIC_API_KEY", None)         # tests use the offline parser, never the paid API

import pytest
from fastapi.testclient import TestClient

from app import agent, core, lookup
from app import understand as und
from app.catalog import allergen_evidence, claim_proof
from app.main import app
from tests import fakes
from tests.fakes import FakeValio, make_catalog

client = TestClient(app)
AURORA = {"X-Agent-Key": "demo-key-aurora"}
LUMI = {"X-Agent-Key": "demo-key-lumi"}
MILK = "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability."


@pytest.fixture(autouse=True)
def fresh_store():
    core.store.catalog = make_catalog()
    core.store.valio = FakeValio()
    core.store.reset()


def run_lookup(text, buyer="hotel-aurora"):
    return core.smart_lookup(buyer, text)


# ---------------------------------------------------------------- Smart Lookup

def test_lookup_narrows_offline_then_checks_only_the_shortlist_live():
    r = run_lookup(MILK)
    res = r["results"][0]
    assert r["api"]["calls"] == 2                          # one availability + one price call
    assert r["api"]["products_checked"] < r["api"]["catalog_size"]
    ids = {o["product_id"] for o in res["options"]}
    assert "900003" not in ids                             # milk chocolate dropped by category coherence
    assert "900007" not in ids                             # not lactose-free
    assert {"900001", "900002"} <= ids
    assert r["deliver_by"] == "2026-11-16"


def test_lookup_is_honest_when_nothing_is_confirmed():
    res = run_lookup(MILK)["results"][0]
    by_id = {o["product_id"]: o for o in res["options"]}
    assert by_id["900001"]["status"] == "partial"          # only 60 cases x 10 L = 600 L in Valio stock
    assert by_id["900002"]["status"] == "supplier" and by_id["900002"]["cutoff_passed"]
    assert by_id["900002"]["units_needed"] == 50           # 1,000 L / (2 x 10 L)
    assert any("cutoff" in f for f in by_id["900002"]["flags"])
    assert not any(p["confidence"] == "confirmed" for p in res["plans"])


def test_milk_gets_a_best_and_a_better_plan():
    res = run_lookup(MILK)["results"][0]
    best, better = res["plans"][0], res["plans"][1]
    assert res["plan"] == best and [p["label"] for p in res["plans"]] == ["Best", "Better"]
    # Best: 600 L from Valio's own stock now + the remaining 400 L from the supplier
    assert best["kind"] == "stock + supplier" and best["confidence"] == "partly confirmed"
    assert [(l["product_id"], l["units"]) for l in best["lines"]] == [("900001", 60), ("900002", 20)]
    assert best["total_eur"] == 1400.0 and best["covered"] == 1000
    assert best["certainty"] == 0.7 and best["fidelity"] == 1.0          # (600 x 1.0 + 400 x 0.25) / 1000
    assert best["score"] == round(0.55 * 0.7 + 0.45 * 1.0, 3)
    assert any("cutoff" in r for r in best["approval_needed"])
    # Better: everything from the supplier in one delivery, nothing confirmed
    assert better["kind"] == "supplier" and better["confidence"] == "not confirmed"
    assert [(l["product_id"], l["units"]) for l in better["lines"]] == [("900002", 50)]
    assert better["certainty"] == 0.25 and better["score"] < best["score"]


def test_finnish_request_understood_the_same_way():
    item = run_lookup("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi")["results"][0]["item"]
    assert item["quantity"] == 1000 and item["unit"] == "L" and item["must_claims"] == ["FREE_FROM_LACTOSE"]


def test_every_option_carries_evidence():
    for o in run_lookup(MILK)["results"][0]["options"]:
        assert "price" in o["evidence"] and "availability" in o["evidence"] and "product" in o["evidence"]


def test_lookup_survives_a_live_api_outage():
    core.store.valio = FakeValio(fail=True)
    r = run_lookup(MILK)
    assert r["live_error"] and r["results"][0]["plan"]["kind"] == "none" and r["results"][0]["plans"] == []


def test_allergen_exclusion_filters_the_catalog():
    found = core.store.get_catalog().search(["chocolate", "suklaa"], exclude_allergens=["SH", "AM"])
    assert {p["id"] for p in found["candidates"]} == {"900008", "900010", "900012"}


def test_strict_allergen_mode_needs_evidence_not_silence():
    cat = core.store.get_catalog()
    found = cat.search(["chocolate", "suklaa"], exclude_allergens=["SH", "AM"], strict_allergens=True)
    # 900012 may contain hazelnut -> out. 900008 has NO allergen data at all -> unverified, out.
    assert {p["id"] for p in found["candidates"]} == {"900010"}
    last = found["steps"][-1]
    assert last["excluded_unverified"] == 1 and "needs a human check" in last["step"]
    ev = allergen_evidence(cat.products["900010"], ["SH", "AM"])
    assert all("declared free from" in e for e in ev)
    assert "unverified" in allergen_evidence(cat.products["900008"], ["SH"])[0]
    assert "MAY CONTAIN" in allergen_evidence(cat.products["900012"], ["SH"])[0]


def test_a_stronger_claim_proves_a_weaker_one():
    oat = {"claims": ["VEGAN"]}
    assert claim_proof(oat, "FREE_FROM_LACTOSE") == "implied by the vegan claim"      # no milk, so no lactose
    assert claim_proof(oat, "FREE_FROM_GLUTEN") is None                               # never guessed
    assert claim_proof({"claims": ["FREE_FROM_LACTOSE"]}, "FREE_FROM_LACTOSE") == "declared"


def test_shortlist_covers_every_pack_size():
    big = [{"id": f"B{i}", "sales_unit": "KI", "content_per_sales_unit": 20.0} for i in range(5)]
    single = [{"id": "S1", "sales_unit": "ST", "content_per_sales_unit": 1.0}]
    picked = [p["id"] for p in lookup._shortlist(big + single, 1000, 3)]
    assert picked == ["B0", "S1", "B1"]        # best-fitting size first, but the single carton is not crowded out


# ---------------------------------------------------------------- good-better-best plans

def opt(pid, match="exact", status="in_stock", amount=1000, price=10.0, size=10.0, cutoff_passed=False, on_time=True):
    """A hand-made live-checked option (10 L cases by default)."""
    return {"product_id": pid, "name": f"Product {pid}", "match": match, "match_reason": "test", "sales_unit": "KI",
            "content_per_sales_unit": size, "unit_price_eur": price, "status": status,
            "available_amount": None if status == "supplier" else amount, "on_time": on_time,
            "cutoff_passed": cutoff_passed}


ITEM = {"label": "milk", "quantity": 100, "unit": "L"}


def test_confirmed_close_variant_beats_unconfirmed_exact():
    plans = lookup.build_plans([opt("EXACT", status="supplier", cutoff_passed=True, price=8.0),
                                opt("CLOSE", match="close", price=12.0)], ITEM)
    assert [p["label"] for p in plans] == ["Best", "Better"]
    assert plans[0]["lines"][0]["product_id"] == "CLOSE" and plans[0]["confidence"] == "confirmed"
    assert plans[0]["score"] == round(0.55 * 1.0 + 0.45 * 0.8, 3)        # 0.91
    assert plans[1]["lines"][0]["product_id"] == "EXACT" and plans[1]["score"] == round(0.55 * 0.25 + 0.45, 3)
    assert plans[0]["is_substitute"] and any("close variant" in r for r in plans[0]["approval_needed"])


def test_confirmed_exact_wins_and_price_breaks_ties():
    plans = lookup.build_plans([opt("DEAR", price=12.0), opt("CHEAP", price=9.0), opt("SUP", status="supplier", price=5.0),
                                opt("OAT", match="substitute", price=4.0)], ITEM)
    assert plans[0]["lines"][0]["product_id"] == "CHEAP" and plans[0]["score"] == 1.0
    assert plans[0]["approval_needed"] == [] and plans[0]["trade_offs"] == []
    assert lookup.has_confirmed_exact(plans)
    assert {p["kind"] for p in plans} == {"single", "substitute", "supplier"}   # three different kinds of choice
    assert plans[-1]["kind"] == "substitute"                                    # fidelity 0.5 ranks last


def test_split_across_stock_and_honest_partial_plan():
    split = lookup.build_plans([opt("A", status="partial", amount=6, price=9.0), opt("B", status="partial", amount=7)], ITEM)
    assert split[0]["kind"] == "split" and split[0]["confidence"] == "confirmed"
    assert [(l["product_id"], l["units"]) for l in split[0]["lines"]] == [("A", 6), ("B", 4)]   # cheapest first
    partial = lookup.build_plans([opt("A", status="partial", amount=6)], ITEM)
    assert partial[0]["kind"] == "partial" and partial[0]["certainty"] == 0.6 and partial[0]["covered"] == 60
    assert any("Only 60 of 100" in r for r in partial[0]["approval_needed"])
    assert lookup.build_plans([opt("LATE", on_time=False)], ITEM) == []


def with_substitute(monkeypatch, quantity=None):
    """The offline parser never proposes substitutes; pretend the AI suggested oat drink."""
    real = und.parse_offline
    def fake(request, today):
        need = real(request, today)
        need["items"][0]["substitutes"] = [{"label": "oat drink", "product_terms": ["oat drink", "kaurajuoma"],
                                            "trade_off": "different taste, plant-based"}]
        if quantity:
            need["items"][0]["quantity"] = quantity
        return need
    monkeypatch.setattr(und, "parse_offline", fake)


def test_valio_substitution_list_is_used_but_never_breaks_a_requirement():
    res = run_lookup(MILK)["results"][0]
    by_id = {o["product_id"]: o for o in res["options"]}
    assert "900013" in by_id and by_id["900013"]["valio_pick"]          # found only through Valio's list
    assert by_id["900013"]["valio_substitute_for"] == ["900002"]
    assert "900007" not in by_id                                          # Valio lists it, but it is not lactose-free
    assert res["funnel"]["valio_substitutes_added"] == 1 and res["funnel"]["valio_substitutes_dropped"] == 1


def test_valio_pick_beats_an_ai_guessed_variant(monkeypatch):
    monkeypatch.setitem(fakes.LIVE, "900013", ("Stock", 100, 15.6, 1.30, "L", 13.5))   # 100 cases x 12 L in stock
    res = run_lookup(MILK)["results"][0]
    best = res["plans"][0]
    # Best: all 600 L of the exact milk in stock, the rest from Valio's own pick, fully confirmed.
    assert best["confidence"] == "confirmed" and [l["product_id"] for l in best["lines"]] == ["900001", "900013"]
    assert best["fidelity"] == 0.96                                       # (600 x 1.0 + 400 x 0.9) / 1000
    assert any("Valio's own recommended replacement" in t for t in best["trade_offs"])
    assert best["is_substitute"]                                          # still needs a human, like any variant
    valio_line = best["lines"][1]
    assert lookup._line_fidelity(valio_line) == 0.9                       # close variant 0.8 + 0.1 for Valio's pick
    assert lookup._line_fidelity({**valio_line, "valio_pick": False}) == 0.8   # the same variant guessed by the AI


def test_substitutes_are_checked_live_only_when_needed(monkeypatch):
    with_substitute(monkeypatch)
    r = run_lookup(MILK)                                   # no confirmed exact plan -> substitutes checked too
    res = r["results"][0]
    assert r["api"]["calls"] == 4 and res["funnel"]["substitutes_checked_live"] == 1
    oat = next(o for o in res["options"] if o["product_id"] == "900004")
    assert oat["match"] == "substitute" and "lactose-free" in oat["claims"]     # hard rules hold for substitutes
    r = run_lookup("24 litres of lactose-free milk by Monday")                 # confirmed -> no extra calls
    assert r["api"]["calls"] == 2 and r["results"][0]["funnel"]["substitutes_checked_live"] == 0
    assert "not checked live" in r["results"][0]["funnel"]["substitutes_note"]


def test_substitute_plan_is_declared_and_needs_a_human(monkeypatch):
    with_substitute(monkeypatch, quantity=650)             # 600 L milk in stock + 50 L oat drink = confirmed
    res = run_lookup(MILK)["results"][0]
    best = res["plans"][0]
    assert best["kind"] == "substitute" and best["confidence"] == "confirmed" and best["is_substitute"]
    assert [(l["product_id"], l["match"]) for l in best["lines"]] == [("900001", "exact"), ("900004", "substitute")]
    order = agent.order_plan("hotel-aurora", best["lines"], "2026-11-16", best["why"],
                             declared_risks=best["approval_needed"])["order"]
    reasons = " ".join(order["policy"]["reasons"])
    assert order["status"] == "pending_approval" and order["is_substitute"]
    assert "substitutes need human sign-off" in reasons and "Agent declared: Product" not in reasons
    assert "Agent declared: Oat drink barista 1 l is a substitute" in reasons


# ---------------------------------------------------------------- door: quote, mandate, approval

def test_milk_plan_goes_to_the_manager_with_all_reasons_then_confirms():
    best = run_lookup(MILK)["results"][0]["plans"][0]
    out = agent.order_plan("hotel-aurora", best["lines"], "2026-11-16", best["why"], declared_risks=best["approval_needed"])
    order = out["order"]
    assert order["status"] == "pending_approval" and order["total_eur"] == 1400.0
    reasons = " ".join(order["policy"]["reasons"])
    assert "Agent declared:" in reasons                        # the agent's own honesty also routes to a human
    assert "above the agent's own limit" in reasons
    assert "Unusual order" in reasons and "8.3x" in reasons    # 1,000 L vs usual 120 L
    assert "cutoff has passed" in reasons
    done = client.post(f"/api/human/orders/{order['order_id']}/decision", json={"approve": True, "approver": "Kitchen manager"}).json()
    assert done["status"] == "confirmed" and done["receipt"]


def allow_small_orders(buyer="hotel-aurora"):
    client.post(f"/api/human/mandates/{buyer}/settings", json={"by": "Kitchen manager", "always_ask": False})


def test_by_default_a_human_confirms_every_order():
    plan = run_lookup("24 litres of oat drink by Monday")["results"][0]["plan"]
    order = agent.order_plan("hotel-aurora", plan["lines"], "2026-11-16", plan["why"])["order"]
    assert order["status"] == "pending_approval"
    assert order["policy"]["tags"] == [{"kind": "settings", "text": "You confirm every order"}]


def test_small_normal_order_is_confirmed_by_the_door_when_the_buyer_allows_it():
    allow_small_orders()
    res = run_lookup("24 litres of oat drink by Monday")["results"][0]
    plan = res["plan"]
    assert plan["confidence"] == "confirmed" and plan["label"] == "Best" and plan["approval_needed"] == []
    order = agent.order_plan("hotel-aurora", plan["lines"], "2026-11-16", plan["why"],
                             declared_risks=plan["approval_needed"])["order"]
    assert order["status"] == "confirmed" and order["decided_by"] == "door (automatic)"
    assert "always_ask True -> False" in core.store.audit[0]["summary"]                  # the change is logged


def test_settings_are_validated():
    res = client.post("/api/human/mandates/hotel-aurora/settings",
                      json={"by": "Kitchen manager", "auto_approve_limit_eur": 20000})
    assert res.status_code == 400 and "above the hard limit" in res.json()["error"]


def test_any_declared_risk_sends_a_small_order_to_a_human():
    allow_small_orders()
    q = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900004", "quantity": 6}]}).json()
    assert q["policy_preview"]["decision"] == "agent_may_order"
    o = client.post("/api/v1/orders", headers=AURORA, json={"quote_id": q["quote_id"], "reason": "weekly oat drink",
                                                           "declared_risks": ["I am not sure this is the barista kind"]}).json()
    assert o["status"] == "pending_approval"
    assert "Agent declared: I am not sure this is the barista kind" in o["policy"]["reasons"]


def test_misbehaving_agent_is_stopped_every_time():
    r = agent.run_rogue("hotel-aurora")
    results = [s["result"] for s in r["steps"]]
    assert len(results) == 3
    assert results[0]["status"] == "rejected" and "hard limit" in " ".join(results[0]["policy"]["reasons"])
    assert results[1]["status"] == "rejected" and "not food" in " ".join(results[1]["policy"]["reasons"])
    assert "error" in results[2]
    assert all(o["status"] == "rejected" for o in core.store.orders.values())


def test_approver_reasons_are_short_tags_without_repeats():
    best = run_lookup(MILK)["results"][0]["plans"][0]
    order = agent.order_plan("hotel-aurora", best["lines"], "2026-11-16", best["why"],
                             declared_risks=best["approval_needed"])["order"]
    kinds = [t["kind"] for t in order["policy"]["tags"]]
    assert sorted(kinds) == ["delivery", "money", "quantity"]           # the declared cutoff risk is not repeated
    assert {"kind": "quantity", "text": "8× your usual week"} in order["policy"]["tags"]


def test_quote_preview_matches_what_the_order_will_say(monkeypatch):
    with_substitute(monkeypatch, quantity=650)
    best = run_lookup(MILK)["results"][0]["plans"][0]
    lines = [{"product_id": l["product_id"], "units": l["units"]} for l in best["lines"]]
    q = client.post("/api/demo/quote", json={"buyer_id": "hotel-aurora", "deliver_by": "2026-11-16", "lines": lines,
                                             "is_substitute": True, "declared_risks": best["approval_needed"]}).json()
    assert "substitute" in [t["kind"] for t in q["policy_preview"]["tags"]]
    o = client.post("/api/demo/order", json={"buyer_id": "hotel-aurora", "quote_id": q["quote_id"], "reason": best["why"],
                                             "is_substitute": True, "declared_risks": best["approval_needed"]}).json()
    assert o["policy"]["tags"] == q["policy_preview"]["tags"]


def test_approver_can_approve_with_changed_amounts():
    best = run_lookup(MILK)["results"][0]["plans"][0]
    order = agent.order_plan("hotel-aurora", best["lines"], "2026-11-16", best["why"])["order"]
    done = client.post(f"/api/human/orders/{order['order_id']}/decision", json={
        "approve": True, "approver": "Kitchen manager", "changes": [{"product_id": "900001", "quantity": 30}]}).json()
    assert done["status"] == "confirmed" and done["total_eur"] == 390.0 and done["original_total_eur"] == 1400.0
    assert len(done["lines"]) == 1 and done["changed_by"] == "Kitchen manager" and done["receipt"]
    assert "with changes" in core.store.audit[-1]["summary"]


def test_a_change_can_not_break_a_hard_rule():
    best = run_lookup(MILK)["results"][0]["plans"][0]
    order = agent.order_plan("hotel-aurora", best["lines"], "2026-11-16", best["why"])["order"]
    res = client.post(f"/api/human/orders/{order['order_id']}/decision", json={
        "approve": True, "approver": "Kitchen manager", "changes": [{"product_id": "900002", "quantity": 400}]})
    assert res.status_code == 400 and "hard limit" in res.json()["error"]       # 12,400 EUR > 10,000 EUR
    assert core.store.orders[order["order_id"]]["status"] == "pending_approval"


def test_buyer_can_withdraw_a_waiting_order():
    q = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900004", "quantity": 6}]}).json()
    o = client.post("/api/v1/orders", headers=AURORA, json={"quote_id": q["quote_id"], "reason": "weekly oat drink"}).json()
    done = client.post(f"/api/human/orders/{o['order_id']}/cancel", json={"by": "Kitchen manager"}).json()
    assert done["status"] == "cancelled"
    again = client.post(f"/api/human/orders/{o['order_id']}/decision", json={"approve": True, "approver": "x"})
    assert again.status_code == 400


def test_pausing_one_agent_leaves_the_others_working():
    client.post("/api/human/agents/hotel-aurora/own-agent/pause", json={"by": "Kitchen manager"})
    res = client.get("/api/v1/products?q=milk", headers=AURORA)
    assert res.status_code == 403 and "paused" in res.json()["error"]
    assert "error" not in agent.lookup("hotel-aurora", "24 litres of oat drink by Monday")     # Valio's assistant still works
    client.post("/api/human/agents/hotel-aurora/valio-assistant/pause", json={"by": "Kitchen manager"})
    assert "paused" in agent.lookup("hotel-aurora", "24 litres of oat drink by Monday")["error"]
    client.post("/api/human/agents/hotel-aurora/own-agent/resume", json={"by": "Kitchen manager"})
    assert client.get("/api/v1/products?q=milk", headers=AURORA).status_code == 200


# ---------------------------------------------------------------- what matters most: stars and reasons

def test_several_criteria_count_equally():
    assert lookup._weights(["price", "reliability", "low_co2"]) == {"price": 0.3333, "reliability": 0.3333, "co2": 0.3333}
    assert lookup._weights(["reliability", "finnish_origin"]) == {"price": 0.0, "reliability": 1.0, "co2": 0.0}
    assert lookup._weights([]) == {"price": 1.0, "reliability": 0.0, "co2": 0.0}
    assert [lookup.stars(x) for x in (0, 0.5, 0.6, 0.94, 1)] == [1.0, 3.0, 3.5, 5.0, 5.0]


def test_stars_explain_themselves_with_valio_data():
    r = run_lookup(MILK)
    item = r["results"][0]["item"]
    need = {**r["need"], "priorities": ["price", "reliability", "low_co2"], "items": [item]}
    res = core.smart_lookup("hotel-aurora", MILK, need=need)["results"][0]
    by_id = {o["product_id"]: o for o in res["options"]}
    cheap, dear = by_id["900001"], by_id["900002"]                 # 1.30 vs 1.55 EUR per litre
    assert cheap["criteria"] == ["price", "reliability", "co2"]
    assert cheap["ratings"]["price"]["stars"] == 5.0 and "1.30 € per L" in cheap["ratings"]["price"]["why"]
    assert dear["ratings"]["price"]["stars"] == 4.5 and "84% of the best price" in dear["ratings"]["price"]["why"]
    assert "99.0%" in cheap["ratings"]["reliability"]["why"] and dear["ratings"]["reliability"]["stars"] == 5.0
    co2 = cheap["ratings"]["co2"]["why"]
    assert "1.1 kg CO₂e per kg" in co2 and "average for this product's group" in co2   # honest about what CO2 means
    best = res["plans"][0]
    assert set(best["ratings"]) == {"price", "reliability", "co2"} and best["rating_stars"] is not None
    assert "Weighted by how much each product covers" in best["ratings"]["price"]["why"]    # 900001 + 900002


def test_highest_rated_badge_explains_itself():
    plans = [{"label": "Best", "certainty": 1.0, "fidelity": 1.0, "confidence": "confirmed", "over_budget": False,
              "rating": 0.75, "rating_stars": 4.0, "criteria": ["price"], "trade_offs": [],
              "ratings": {"price": {"name": "Price", "stars": 4.0}}},
             {"label": "Better", "certainty": 0.25, "fidelity": 1.0, "confidence": "not confirmed", "over_budget": False,
              "rating": 1.0, "rating_stars": 5.0, "criteria": ["price"],
              "trade_offs": ["1000 L from the supplier's warehouse: quantity not confirmed and the order cutoff has passed"],
              "ratings": {"price": {"name": "Price", "stars": 5.0}}}]
    lookup.mark_highest_rated(plans)
    assert not plans[0]["highest_rated"] and plans[1]["highest_rated"]
    why = plans[1]["highest_rated_why"]
    assert "5.0★ against 4.0★ for Best" in why and "Price 5.0★ vs 4.0★" in why
    assert "less certain (not confirmed: 25% certain, against 100% for Best)" in why and "cutoff has passed" in why
    plans[0]["rating"] = 1.0                                              # Best is also the best rated
    lookup.mark_highest_rated(plans)
    assert plans[0]["highest_rated"] and "most certain way to get your order (100% certain, confirmed)" in plans[0]["highest_rated_why"]


def test_unknown_reliability_is_said_out_loud():
    res = run_lookup(MILK)["results"][0]
    sour = next((o for o in res["options"] if o["reliability"] is None), None)
    if sour:
        assert "no history" in sour["ratings"]["reliability"]["why"] and sour["ratings"]["reliability"]["stars"] == 3.5


# ---------------------------------------------------------------- the request: budget, assumptions, edits

def test_budget_is_understood_and_ranks_plans():
    r = run_lookup("I need 1,000 litres of lactose-free milk by Monday, max 1,000 €")
    assert r["budget_eur"] == 1000 and r["results"][0]["item"]["quantity"] == 1000   # the budget is not the amount
    plans = r["results"][0]["plans"]
    assert all(p["over_budget"] for p in plans)                                       # 1,400 and 1,550 EUR
    assert any("over your 1,000 € budget" in t for t in plans[0]["trade_offs"])
    assert {"field": "budget_eur", "text": "No budget set"} not in r["assumptions"]


def test_missing_date_and_budget_are_reported_as_assumptions():
    r = run_lookup("24 litres of oat drink")
    fields = [a["field"] for a in r["assumptions"]]
    assert fields == ["deliver_by", "budget_eur"] and r["deliver_by"] == "2026-11-16"


def test_buyer_can_edit_the_understood_need_and_search_again():
    r = run_lookup(MILK)
    item = {**r["results"][0]["item"], "quantity": 500}
    edited = core.smart_lookup("hotel-aurora", MILK, need={**r["need"], "items": [item], "budget_eur": 800})
    assert edited["need"]["engine"] == "the buyer's criteria"
    best = edited["results"][0]["plans"][0]
    assert best["kind"] == "split" or best["confidence"] == "confirmed"           # 500 L fits in Valio stock (600 L)
    assert best["total_eur"] == 650.0 and not best["over_budget"]


def test_only_the_product_is_required():
    need = {"items": [{"label": "oat drink", "product_terms": ["oat drink", "kaurajuoma"]}]}
    r = core.smart_lookup("hotel-aurora", "oat drink", need=need)
    item = r["results"][0]["item"]
    assert item["quantity"] == 1 and item["unit"] == "PCS" and item["amount_given"] is False
    assert r["assumptions"][0] == {"field": "quantity", "text": "No amount given, so prices are for one pack"}
    assert r["results"][0]["plans"][0]["lines"][0]["units"] == 1
    with pytest.raises(core.WholesalerError, match="which product"):
        core.smart_lookup("hotel-aurora", "", need={"items": [{"label": " ", "quantity": 5}]})


def test_fill_in_reads_the_request_without_searching():
    calls = core.store.valio.stats["calls"]
    r = client.post("/api/demo/understand", json={"buyer_id": "hotel-aurora",
                                                  "message": "24 litres of lactose-free milk by Monday, max 50 €"}).json()
    n = r["need"]
    assert n["items"][0]["quantity"] == 24 and n["items"][0]["must_claims"] == ["FREE_FROM_LACTOSE"]
    assert n["budget_eur"] == 50 and n["deliver_by"] == "2026-11-16" and n["other_wishes"] == []
    assert core.store.valio.stats["calls"] == calls                       # no live API calls for reading a request
    assert "error" in client.post("/api/demo/understand", json={"buyer_id": "hotel-aurora", "message": " "}).json()


def test_built_in_reader_finds_product_allergy_and_wishes():
    from app.understand import parse_offline
    from datetime import date
    n = parse_offline("Our school needs 40 kg of chocolate cookies by Monday, max 600 €. "
                      "A pupil has a hazelnut allergy. Deliver to the back door.", date(2026, 11, 14))
    it = n["items"][0]
    assert it["label"] == "chocolate cookies" and it["quantity"] == 40 and it["unit"] == "KG"
    assert it["exclude_allergens"] == ["SH"] and it["strict_allergens"]
    assert n["budget_eur"] == 600 and n["other_wishes"] == ["Deliver to the back door"]


def test_a_request_without_a_product_adds_to_the_current_order():
    r = client.post("/api/demo/understand", json={"buyer_id": "hotel-aurora", "message": "my budget is 50 euro"}).json()
    n = r["need"]
    assert n["adds_to_current_order"] and n["budget_eur"] == 50
    assert n["items"][0]["label"] == "" and n["items"][0]["product_terms"] == []      # never "my budget" as a product
    r = client.post("/api/demo/understand", json={"buyer_id": "hotel-aurora", "message": "24 litres of oat drink"}).json()
    assert not r["need"]["adds_to_current_order"]


def test_right_unit_for_the_product():
    milk = client.post("/api/demo/unit-check", json={"product_terms": ["milk", "maito"]}).json()
    choc = client.post("/api/demo/unit-check", json={"product_terms": ["chocolate", "suklaa"]}).json()
    assert milk["unit"] == "L" and milk["share"] == 1.0                   # milk is sold by litres
    assert choc["unit"] == "KG"                                            # chocolate by weight
    r = client.post("/api/demo/understand", json={"buyer_id": "hotel-aurora", "message": "40 kg of milk by Monday"}).json()
    item = r["need"]["items"][0]
    assert item["unit"] == "KG" and item["usual_unit"]["unit"] == "L"      # kept as typed; the page warns, never changes it


def test_boxes_fit_the_product():
    """The tick boxes come from the matching products: 'avoid milk' means nothing for milk, a lot for chocolate."""
    milk = client.post("/api/demo/unit-check", json={"product_terms": ["milk", "maito"]}).json()["choices"]
    choc = client.post("/api/demo/unit-check", json={"product_terms": ["chocolate", "suklaa"]}).json()["choices"]
    assert milk["matched"] and milk["allergens"].get("AM") == milk["matched"]          # every milk contains milk
    assert 0 < milk["claims"].get("FREE_FROM_LACTOSE", 0) < milk["matched"]          # lactose-free splits them
    assert 0 < choc["allergens"].get("SH", 0) < choc["matched"]                      # some chocolate has hazelnut
    assert set(milk["labels"]) == {"finnish", "organic"}


def test_delivery_notes_become_wishes_not_product_words():
    from app.understand import parse_offline
    from datetime import date
    n = parse_offline("I need 1000 litres of lactose-free milk by Monday, order put it in my back door", date(2026, 11, 14))
    assert n["items"][0]["label"] == "milk" and n["other_wishes"] == ["Put it in my back door"]
    n = parse_offline("Deliver to the back door by Monday, 24 litres of oat drink", date(2026, 11, 14))
    assert n["deliver_by"] == "2026-11-16" and n["items"][0]["label"] == "oat drink"      # the date isn't lost
    assert n["other_wishes"] == ["Deliver to the back door by Monday"]


def test_buyers_wishes_travel_with_the_order_but_are_not_rules():
    plan = run_lookup("24 litres of oat drink by Monday")["results"][0]["plan"]
    lines = [{"product_id": l["product_id"], "units": l["units"]} for l in plan["lines"]]
    q = client.post("/api/demo/quote", json={"buyer_id": "hotel-aurora", "deliver_by": "2026-11-16", "lines": lines}).json()
    o = client.post("/api/demo/order", json={"buyer_id": "hotel-aurora", "quote_id": q["quote_id"], "reason": plan["why"],
                                             "notes": ["Deliver to the back door"]}).json()
    assert o["notes"] == ["Deliver to the back door"]
    assert [t["kind"] for t in o["policy"]["tags"]] == ["settings"]       # a wish never changes the door's decision


def test_quote_refuses_more_than_valio_has_in_stock():
    res = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900001", "quantity": 100}]})
    assert res.status_code == 400 and "only 60" in res.json()["error"]


def test_orders_need_a_reason():
    q = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900007", "quantity": 12}]}).json()
    assert client.post("/api/v1/orders", headers=AURORA, json={"quote_id": q["quote_id"], "reason": " "}).status_code == 400


def test_substitute_needs_approval_even_when_cheap():
    q = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900004", "quantity": 6}]}).json()
    o = client.post("/api/v1/orders", headers=AURORA,
                    json={"quote_id": q["quote_id"], "reason": "oat instead of milk", "is_substitute": True}).json()
    assert o["status"] == "pending_approval"


# ---------------------------------------------------------------- trust: mandate, data, audit

def test_revoked_mandate_blocks_agent_instantly():
    assert client.get("/api/v1/products?q=milk", headers=AURORA).status_code == 200
    client.post("/api/human/mandates/hotel-aurora/revoke", json={"by": "Kitchen manager"})
    res = client.post("/api/v1/lookup", headers=AURORA, json={"request": MILK})
    assert res.status_code == 403 and "revoked" in res.json()["error"]
    assert client.get("/api/v1/products?q=milk", headers=LUMI).status_code == 200   # other buyers unaffected
    client.post("/api/human/mandates/hotel-aurora/reinstate", json={"by": "Kitchen manager"})
    assert client.get("/api/v1/products?q=milk", headers=AURORA).status_code == 200


def test_search_shows_no_internal_fields_and_is_throttled():
    first = client.get("/api/v1/products?q=milk", headers=LUMI).json()[0]
    assert "_tokens" not in first and "stock" not in first and "unit_price_eur" not in first
    for _ in range(core.SEARCH_LIMIT_PER_MINUTE - 1):
        client.get("/api/v1/products?q=milk", headers=LUMI)
    res = client.get("/api/v1/products?q=milk", headers=LUMI)
    assert res.status_code == 400 and "anti-scraping" in res.json()["error"]


def test_audit_log_is_tamper_evident():
    run_lookup(MILK)
    assert client.get("/api/human/audit/verify").json()["ok"] is True
    core.store.audit[0]["summary"] = "nothing happened here"
    verdict = client.get("/api/human/audit/verify").json()
    assert verdict["ok"] is False and verdict["broken_at"] == 1


def test_auth_is_required_and_quotes_are_buyer_scoped():
    assert client.get("/api/v1/products?q=milk").status_code == 422
    assert client.get("/api/v1/products?q=milk", headers={"X-Agent-Key": "nope"}).status_code == 401
    q = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "900007", "quantity": 12}]}).json()
    assert client.post("/api/v1/orders", headers=LUMI, json={"quote_id": q["quote_id"], "reason": "x"}).status_code == 400


def test_discovery_file_describes_the_door():
    d = client.get("/.well-known/agent-interface.json").json()
    assert d["auth"]["header"] == "X-Agent-Key" and "smart_lookup" in d["tools"]
