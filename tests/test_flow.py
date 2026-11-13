import os

from app import config  # noqa: F401  (loads .env first, so we can switch the real APIs off below)

os.environ["DEMO_TODAY"] = "2026-11-14"           # a Saturday -> "by Monday" = 2026-11-16
os.environ.pop("ANTHROPIC_API_KEY", None)         # tests use the offline parser, never the paid API

import pytest
from fastapi.testclient import TestClient

from app import agent, core, lookup
from app.main import app
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
    plan = res["plan"]
    assert plan["confidence"] == "not confirmed" and plan["lines"][0]["product_id"] == "900002"
    assert any("cutoff" in f for f in by_id["900002"]["flags"])


def test_finnish_request_understood_the_same_way():
    item = run_lookup("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi")["results"][0]["item"]
    assert item["quantity"] == 1000 and item["unit"] == "L" and item["must_claims"] == ["FREE_FROM_LACTOSE"]


def test_every_option_carries_evidence():
    for o in run_lookup(MILK)["results"][0]["options"]:
        assert "price" in o["evidence"] and "availability" in o["evidence"] and "product" in o["evidence"]


def test_lookup_survives_a_live_api_outage():
    core.store.valio = FakeValio(fail=True)
    r = run_lookup(MILK)
    assert r["live_error"] and r["results"][0]["plan"]["type"] == "none"


def test_allergen_exclusion_filters_the_catalog():
    found = core.store.get_catalog().search(["chocolate", "suklaa"], exclude_allergens=["SH", "AM"])
    assert {p["id"] for p in found["candidates"]} == {"900008", "900010"}


# ---------------------------------------------------------------- door: quote, mandate, approval

def test_milk_plan_goes_to_the_manager_with_all_reasons_then_confirms():
    res = run_lookup(MILK)["results"][0]
    out = agent.order_plan("hotel-aurora", res["plan"]["lines"], "2026-11-16", res["plan"]["why"])
    order = out["order"]
    assert order["status"] == "pending_approval" and order["total_eur"] == 1550.0
    reasons = " ".join(order["policy"]["reasons"])
    assert "above the agent's own limit" in reasons
    assert "Unusual order" in reasons and "8.3x" in reasons    # 1,000 L vs usual 120 L
    assert "cutoff has passed" in reasons
    done = client.post(f"/api/human/orders/{order['order_id']}/decision", json={"approve": True, "approver": "Kitchen manager"}).json()
    assert done["status"] == "confirmed" and done["receipt"]


def test_small_normal_order_is_confirmed_by_the_door():
    res = run_lookup("24 litres of oat drink by Monday")["results"][0]
    assert res["plan"]["confidence"] == "confirmed"
    order = agent.order_plan("hotel-aurora", res["plan"]["lines"], "2026-11-16", res["plan"]["why"])["order"]
    assert order["status"] == "confirmed" and order["decided_by"] == "door (automatic)"


def test_misbehaving_agent_is_stopped_every_time():
    r = agent.run_rogue("hotel-aurora")
    results = [s["result"] for s in r["steps"]]
    assert len(results) == 3
    assert results[0]["status"] == "rejected" and "hard limit" in " ".join(results[0]["policy"]["reasons"])
    assert results[1]["status"] == "rejected" and "not food" in " ".join(results[1]["policy"]["reasons"])
    assert "error" in results[2]
    assert all(o["status"] == "rejected" for o in core.store.orders.values())


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
