import base64

from fastapi.testclient import TestClient

from spend_tracker.analytics import dashboard
from spend_tracker.domain import Category
from spend_tracker.services import correct_category
from spend_tracker.storage import JsonStore, empty_state
from spend_tracker.synthetic import seed_june, seed_may
from spend_tracker.webapp import create_app


def test_synthetic_june_dashboard_and_corrections():
    state = empty_state()
    report = seed_june(state)
    assert report["rows_imported"] == 60
    assert report["jev_attempts"] == 0
    june = dashboard(state, "2026-06")
    assert june["transaction_count"] == 60
    assert june["review_count"] == 10
    assert len(june["weeks"]) == 5
    assert sum(item["cents"] for item in june["categories"]) == june["total_spending_cents"]
    assert state["merchant_mappings"]["costco whse"]["category"] == "Groceries"
    costco = next(
        item for item in state["transactions"] if item["normalized_merchant"] == "costco whse"
    )
    assert correct_category(state, costco["id"], Category.SHOPPING) == 1
    assert state["merchant_mappings"]["costco whse"]["category"] == "Groceries"
    assert correct_category(state, costco["id"], Category.FOOD_COFFEE, "merchant") == 4
    assert state["merchant_mappings"]["costco whse"]["category"] == "Food + coffee"
    assert {
        item["category"]
        for item in state["transactions"]
        if item["normalized_merchant"] == "costco whse"
    } == {"Food + coffee"}
    assert seed_june(state)["duplicates_skipped"] == 60


def test_previous_calendar_month_comparison():
    state = empty_state()
    seed_june(state)
    assert dashboard(state, "2026-06")["comparison"]["has_previous_data"] is False
    assert seed_may(state)["rows_imported"] == 47
    june = dashboard(state, "2026-06")
    comparison = june["comparison"]
    assert comparison["previous_month"] == "2026-05"
    assert comparison["has_previous_data"] is True
    assert comparison["current_total_cents"] == june["total_spending_cents"]
    assert comparison["previous_total_cents"] == dashboard(state, "2026-05")["total_spending_cents"]
    assert comparison["delta_cents"] == (
        comparison["current_total_cents"] - comparison["previous_total_cents"]
    )
    assert {item["category"] for item in comparison["categories"]}.isdisjoint(
        {"Income", "Transfers", "Housing", "Utilities"}
    )
    groceries = next(item for item in comparison["categories"] if item["category"] == "Groceries")
    assert groceries["current_cents"] > groceries["previous_cents"]
    assert seed_may(state)["duplicates_skipped"] == 47
    assert dashboard(empty_state(), "2026-01")["comparison"]["previous_month"] == "2025-12"


def test_saved_legacy_categories_migrate_without_counting_income_or_transfers(tmp_path):
    state = empty_state()
    amounts = {
        "Dining": 1000,
        "Health": 500,
        "Housing": 25000,
        "Utilities": 3000,
        "Income": -200000,
        "Transfers": 5000,
    }
    for category, cents in amounts.items():
        merchant = category.casefold()
        state["transactions"].append(
            {
                "id": merchant,
                "date": "2026-06-01",
                "created_at": "2026-06-01T00:00:00Z",
                "source": "legacy",
                "currency": "USD",
                "normalized_merchant": merchant,
                "category": category,
                "amount_cents": cents,
                "classification_source": "jev",
                "needs_review": False,
            }
        )
        state["merchant_mappings"][merchant] = {"category": category}
    store = JsonStore(tmp_path / "legacy.json")
    store.save(state)
    loaded = store.load()
    rows = {item["id"]: item for item in loaded["transactions"]}
    assert rows["dining"]["category"] == "Food + coffee"
    assert rows["health"]["category"] == "Health + personal wellness"
    assert rows["housing"]["category"] == "Other"
    assert rows["utilities"]["needs_review"] is True
    assert rows["income"]["excluded_from_spending"] is True
    assert rows["transfers"]["excluded_from_spending"] is True
    assert set(loaded["merchant_mappings"]) == {"dining", "health"}
    assert loaded["merchant_mappings"]["dining"]["category"] == "Food + coffee"
    assert dashboard(loaded, "2026-06")["total_spending_cents"] == 29500
    assert correct_category(loaded, "income", Category.OTHER) == 1
    assert rows["income"]["excluded_from_spending"] is False


def test_web_workflow(tmp_path, monkeypatch):
    monkeypatch.delenv("JEVMODEL_API_KEY", raising=False)
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store))

    def post(path, payload):
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    assert client.get("/docs").status_code == 200
    initial = client.get("/api/state?month=2026-06").json()
    assert initial["dashboard"]["review_count"] == 10
    assert initial["dashboard"]["comparison"]["has_previous_data"] is True
    assert len(initial["merchants"]) >= 20
    assert initial["categories"] == [category.value for category in Category]
    csv = (
        b"date,description,amount,category,transaction_id\n"
        b"2026-06-30,New Shop,9.25,Groceries,new-1\n"
    )
    payload = {
        "filename": "new.csv",
        "content_base64": base64.b64encode(csv).decode(),
        "source": "checking",
    }
    report = post("/api/import", payload)
    assert report["rows_imported"] == 1
    assert post("/api/import", payload)["duplicates_skipped"] == 1
    updated = client.get("/api/state?month=2026-06").json()
    row = next(item for item in updated["transactions"] if item["raw_description"] == "New Shop")
    assert row["category"] == "Other"
    assert row["needs_review"] is True
    assert all(item["normalized_merchant"] != "new shop" for item in updated["merchants"])
    corrected = post(
        "/api/correct",
        {"transaction_id": row["id"], "category": "Shopping", "scope": "merchant"},
    )
    assert corrected["updated_transactions"] == 1
    final = client.get("/api/state?month=2026-06").json()
    assert any(
        item["normalized_merchant"] == "new shop" and item["category"] == "Shopping"
        for item in final["merchants"]
    )
    assert final["dashboard"]["review_count"] == 10
