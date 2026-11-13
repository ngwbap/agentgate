import os

os.environ["DEMO_TODAY"] = "2026-11-13"  # a Friday -> "by Monday" = 2026-11-16
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from app import agent, core
from app.main import app

client = TestClient(app)
AURORA = {"X-Agent-Key": "demo-key-aurora"}
LUMI = {"X-Agent-Key": "demo-key-lumi"}
MILK = ("I need 1,000 litres of lactose-free milk by Monday. "
        "Find an option that meets our requirements based on price and availability.")


@pytest.fixture(autouse=True)
def fresh_store():
    core.store.reset()


def last_order(result):
    return result["steps"][-1]["result"]


def test_milk_scenario_goes_to_manager_then_confirms():
    order = last_order(agent.run("hotel-aurora", MILK))
    assert order["status"] == "pending_approval"
    assert order["deliver_by"] == "2026-11-16"
    assert order["lines"][0]["product_id"] == "P-1002"   # only supplier with 1,000 L in time
    assert order["total_eur"] == 1450.0
    reasons = " ".join(order["policy"]["reasons"])
    assert "above the agent's own limit" in reasons and "Unusual order" in reasons
    assert order["approve_by"] == "2026-11-14T14:00"     # 2-day lead time, 14:00 cutoff

    stock = core.store.products["P-1002"]["stock"]
    res = client.post(f"/api/human/orders/{order['order_id']}/decision",
                      json={"approve": True, "approver": "Kitchen manager"})
    assert res.status_code == 200 and res.json()["status"] == "confirmed"
    assert res.json()["receipt"]
    assert core.store.products["P-1002"]["stock"] == stock - 1000


def test_finnish_request_gives_same_result():
    order = last_order(agent.run("hotel-aurora", "Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi"))
    assert order["lines"][0]["product_id"] == "P-1002"
    assert order["deliver_by"] == "2026-11-16"


def test_small_normal_order_is_confirmed_by_the_door():
    order = last_order(agent.run("hotel-aurora", "24 litres of oat drink barista by Monday"))
    assert order["status"] == "confirmed"
    assert order["decided_by"] == "door (automatic)"


def test_unusual_quantity_needs_approval_even_under_money_limit():
    # 120 trays of eggs = 852 EUR (under 1,000) but 12x the usual 10 trays a week
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-2001", "quantity": 120}]}).json()
    assert q["total_eur"] < 1000
    assert q["policy_preview"]["decision"] == "needs_human_approval"
    assert "12.0x" in q["policy_preview"]["reasons"][0]


def test_misbehaving_agent_is_stopped_every_time():
    r = agent.run_rogue("hotel-aurora")
    results = [s["result"] for s in r["steps"]]
    assert results[0]["status"] == "rejected"            # above hard limit
    assert results[1]["status"] == "rejected"            # meat not in mandate
    assert "error" in results[2]                         # invented product
    assert not core.store.orders or all(o["status"] == "rejected" for o in core.store.orders.values())


def test_revoked_mandate_blocks_agent_instantly():
    assert client.get("/api/v1/products?q=milk", headers=AURORA).status_code == 200
    client.post("/api/human/mandates/hotel-aurora/revoke", json={"by": "Kitchen manager"})
    res = client.get("/api/v1/products?q=milk", headers=AURORA)
    assert res.status_code == 403 and "revoked" in res.json()["error"]
    assert client.get("/api/v1/products?q=milk", headers=LUMI).status_code == 200   # other buyers unaffected
    client.post("/api/human/mandates/hotel-aurora/reinstate", json={"by": "Kitchen manager"})
    assert client.get("/api/v1/products?q=milk", headers=AURORA).status_code == 200


def test_each_buyer_sees_only_its_own_prices():
    a = {p["product_id"]: p for p in client.get("/api/v1/products?q=lactose-free milk", headers=AURORA).json()}
    l = {p["product_id"]: p for p in client.get("/api/v1/products?q=lactose-free milk", headers=LUMI).json()}
    assert a["P-1001"]["unit_price_eur"] == 1.35 and a["P-1001"]["price_basis"] == "your contract price"
    assert l["P-1001"]["unit_price_eur"] == 1.39 and l["P-1001"]["price_basis"] == "list price"
    assert l["P-1002"]["unit_price_eur"] == 1.52
    assert "stock" not in a["P-1002"]                    # no exact stock levels exposed


def test_search_is_throttled_against_scraping():
    for _ in range(core.SEARCH_LIMIT_PER_MINUTE):
        assert client.get("/api/v1/products?q=milk", headers=LUMI).status_code == 200
    res = client.get("/api/v1/products?q=milk", headers=LUMI)
    assert res.status_code == 400 and "anti-scraping" in res.json()["error"]


def test_audit_log_is_tamper_evident():
    agent.run("hotel-aurora", MILK)
    assert client.get("/api/human/audit/verify").json()["ok"] is True
    core.store.audit[2]["summary"] = "nothing happened here"   # someone edits history
    verdict = client.get("/api/human/audit/verify").json()
    assert verdict["ok"] is False and verdict["broken_at"] == 3


def test_auth_and_quotes_are_buyer_scoped():
    assert client.get("/api/v1/products?q=milk").status_code == 422
    assert client.get("/api/v1/products?q=milk", headers={"X-Agent-Key": "nope"}).status_code == 401
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-1005", "quantity": 12}]}).json()
    assert client.post("/api/v1/orders", headers=LUMI, json={"quote_id": q["quote_id"], "reason": "x"}).status_code == 400


def test_orders_need_a_reason_and_impossible_quotes_are_refused():
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-1005", "quantity": 12}]}).json()
    assert client.post("/api/v1/orders", headers=AURORA, json={"quote_id": q["quote_id"], "reason": " "}).status_code == 400
    res = client.post("/api/v1/quotes", headers=AURORA, json={"items": [{"product_id": "P-4003", "quantity": 2}]})
    assert res.status_code == 400 and "stock" in res.json()["error"]


def test_substitute_needs_approval_even_when_cheap():
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-1007", "quantity": 12}]}).json()
    o = client.post("/api/v1/orders", headers=AURORA,
                    json={"quote_id": q["quote_id"], "reason": "oat instead of milk", "is_substitute": True}).json()
    assert o["status"] == "pending_approval"


def test_discovery_file_describes_the_door():
    d = client.get("/.well-known/agent-interface.json").json()
    assert d["auth"]["header"] == "X-Agent-Key" and "place_order" in d["tools"]
