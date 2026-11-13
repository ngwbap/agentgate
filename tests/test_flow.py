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


@pytest.fixture(autouse=True)
def fresh_store():
    core.store.reset()


def test_milk_scenario_needs_human_approval_then_confirms():
    r = agent.run("hotel-aurora", "I need 1,000 litres of lactose-free milk by Monday. "
                                  "Find an option that meets our requirements based on price and availability.")
    order = r["steps"][-1]["result"]
    assert order["status"] == "pending_approval"
    assert order["deliver_by"] == "2026-11-16"
    assert order["total_eur"] == 1450.0          # 1000 L x 1.45 from the only supplier with enough stock
    assert order["lines"][0]["product_id"] == "P-1002"
    stock_before = core.store.products["P-1002"]["stock"]

    res = client.post(f"/api/human/orders/{order['order_id']}/decision",
                      json={"approve": True, "approver": "Kitchen manager"})
    assert res.status_code == 200 and res.json()["status"] == "confirmed"
    assert core.store.products["P-1002"]["stock"] == stock_before - 1000
    actions = [a["action"] for a in core.store.audit]
    assert actions[0] == "request" and "place_order" in actions and actions[-1] == "approve"


def test_small_order_is_confirmed_automatically():
    r = agent.run("hotel-aurora", "24 litres of oat drink barista by Monday")
    order = r["steps"][-1]["result"]
    assert order["status"] == "confirmed"
    assert order["decided_by"] == "policy (automatic)"


def test_disallowed_category_is_rejected_by_policy():
    q = client.post("/api/v1/quotes", headers=LUMI,
                    json={"items": [{"product_id": "P-3001", "quantity": 4}]}).json()
    assert q["policy_preview"]["decision"] == "blocked"
    o = client.post("/api/v1/orders", headers=LUMI,
                    json={"quote_id": q["quote_id"], "reason": "test"}).json()
    assert o["status"] == "rejected"


def test_agent_key_required_and_quotes_are_buyer_scoped():
    assert client.get("/api/v1/products?q=milk").status_code == 422
    assert client.get("/api/v1/products?q=milk", headers={"X-Agent-Key": "nope"}).status_code == 401
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-1005", "quantity": 12}]}).json()
    other = client.post("/api/v1/orders", headers=LUMI, json={"quote_id": q["quote_id"], "reason": "x"})
    assert other.status_code == 400


def test_quote_refuses_impossible_requests():
    res = client.post("/api/v1/quotes", headers=AURORA,
                      json={"items": [{"product_id": "P-4003", "quantity": 2}]})  # out of stock
    assert res.status_code == 400 and "in stock" in res.json()["error"]


def test_substitute_needs_approval_even_when_cheap():
    q = client.post("/api/v1/quotes", headers=AURORA,
                    json={"items": [{"product_id": "P-1007", "quantity": 12}]}).json()
    o = client.post("/api/v1/orders", headers=AURORA,
                    json={"quote_id": q["quote_id"], "reason": "oat instead of milk", "is_substitute": True}).json()
    assert o["status"] == "pending_approval"
