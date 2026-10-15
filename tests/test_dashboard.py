import base64
import json

import pytest
from fastapi.testclient import TestClient

from spend_tracker.analytics import dashboard, spending_over_time
from spend_tracker.domain import Category
from spend_tracker.services import correct_category
from spend_tracker.storage import JsonStore, empty_state
from spend_tracker.synthetic import seed_june, seed_may, seed_presentation_history
from spend_tracker.webapp import create_app


@pytest.mark.parametrize(
    ("preset", "content"),
    [
        (
            "wells_fargo",
            b"DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS\n"
            b"46293,Example shop,-20.00,,Posted\n"
            b"46293,Example shop,5.00,,Posted\n"
            b"46293,ONLINE PAYMENT THANK YOU,100.00,,Posted\n",
        ),
        (
            "robinhood",
            b"Date,Time,Description,Amount,Type,Merchant\n"
            b"46293.708333333336,1:00 PM,Example shop,20.00,Purchase,Shop\n"
            b"46293.708333333336,2:00 PM,Example shop,-5.00,Refund,Shop\n"
            b"46293.708333333336,3:00 PM,PAYMENT - THANK YOU,-100.00,Payment,Payment\n",
        ),
    ],
)
def test_card_import_api_skips_payments_and_counts_refunds(tmp_path, monkeypatch, preset, content):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "cards.json")
    state = empty_state()
    state["csv_column_mappings"]["example-card"] = {"date": "wrong"}
    store.save(state)
    client = TestClient(create_app(store, seed_demo=False))
    assert {item["id"] for item in client.get("/api/state").json()["import_presets"]} == {
        "wells_fargo",
        "robinhood",
    }
    payload = {
        "filename": "card.csv",
        "content_base64": base64.b64encode(content).decode(),
        "source": "example-card",
        "import_preset": preset,
    }
    response = client.post("/api/import", json=payload)
    assert response.status_code == 200
    report = response.json()
    assert report["rows_imported"] == 2
    assert report["rows_processed"] == 3
    assert report["payments_skipped"] == 1
    assert report["rows_failed"] == 0
    assert report["jev_skipped_no_key"] == 1
    snapshot = client.get("/api/state?month=2026-09").json()
    assert snapshot["dashboard"]["total_spending_cents"] == 1500
    assert snapshot["history"]["total_spending_cents"] == 1500
    assert len(store.load()["transactions"]) == 2
    assert store.load()["csv_column_mappings"]["example-card"] == {"date": "wrong"}
    replay = client.post("/api/import", json=payload).json()
    assert replay["duplicates_skipped"] == 2
    assert replay["payments_skipped"] == 1


def test_card_import_api_rejects_unknown_presets_and_json(tmp_path):
    client = TestClient(create_app(JsonStore(tmp_path / "cards.json"), seed_demo=False))
    payload = {
        "filename": "card.csv",
        "content_base64": base64.b64encode(b"[]").decode(),
        "source": "example-card",
        "import_preset": "unknown",
    }
    assert client.post("/api/import", json=payload).status_code == 400
    payload.update(filename="card.json", import_preset="robinhood")
    response = client.post("/api/import", json=payload)
    assert response.status_code == 400
    assert "require a CSV" in response.json()["detail"]


def test_api_uses_edited_preset_settings_without_changing_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "cards.json")
    client = TestClient(create_app(store, seed_demo=False))
    payload = {
        "filename": "card.csv",
        "content_base64": base64.b64encode(
            b"Day,Memo,Charge,State\n09/01/2026,ONLINE PAYMENT THANK YOU,20,Pending\n"
        ).decode(),
        "source": "example-card",
        "import_preset": "wells_fargo",
        "import_settings": {
            "date_format": "us",
            "amount_convention": "expenses_positive",
            "column_mapping": {"date": "Day", "description": "Memo", "amount": "Charge"},
            "skip_payments": False,
            "skip_non_posted": False,
        },
    }
    response = client.post("/api/import", json=payload)
    assert response.status_code == 200
    assert response.json()["rows_imported"] == 1
    assert response.json()["payments_skipped"] == 0
    assert store.load()["transactions"][0]["amount_cents"] == 2000
    defaults = client.get("/api/state").json()["import_presets"][0]["settings"]
    assert defaults["skip_payments"] is True
    assert defaults["amount_convention"] == "expenses_negative"
    assert defaults["column_mapping"]["date"] == "DATE"
    payload["import_settings"]["date_format"] = "unknown"
    assert client.post("/api/import", json=payload).status_code == 400
    assert len(store.load()["transactions"]) == 1


def test_possible_duplicate_can_be_excluded_and_restored(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "cards.json")
    client = TestClient(create_app(store, seed_demo=False))

    def total_spending():
        return client.get("/api/state?month=2026-09").json()["dashboard"]["total_spending_cents"]

    header = b"DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS\n"
    first = header + b"09/01/2026,Example shop,-20,,Posted\n09/02/2026,Another shop,-30,,Posted\n"
    second = header + b"09/02/2026,Another shop,-30,,Posted\n09/03/2026,Third shop,-40,,Posted\n"
    for content in (first, second):
        response = client.post(
            "/api/import",
            json={
                "filename": "card.csv",
                "content_base64": base64.b64encode(content).decode(),
                "source": "wells-fargo-active-cash",
                "import_preset": "wells_fargo",
            },
        )
        assert response.status_code == 200
    assert response.json()["possible_duplicates"] == 1

    state = client.get("/api/state?month=2026-09").json()
    assert state["dashboard"]["total_spending_cents"] == 12000
    duplicate = next(item for item in state["transactions"] if item["possible_duplicate"])
    assert (
        client.post(
            "/api/duplicate-exclusion",
            json={"transaction_id": duplicate["id"], "excluded": True},
        ).status_code
        == 200
    )
    assert total_spending() == 9000
    assert (
        client.post(
            "/api/correct",
            json={"transaction_id": duplicate["id"], "category": "Groceries"},
        ).status_code
        == 200
    )
    assert total_spending() == 9000
    assert (
        client.post(
            "/api/duplicate-exclusion",
            json={"transaction_id": duplicate["id"], "excluded": False},
        ).status_code
        == 200
    )
    assert total_spending() == 12000

    unrelated = next(item for item in state["transactions"] if not item["possible_duplicate"])
    assert (
        client.post(
            "/api/duplicate-exclusion",
            json={"transaction_id": unrelated["id"], "excluded": True},
        ).status_code
        == 400
    )
    original = next(
        item
        for item in state["transactions"]
        if item["fingerprint"] == duplicate["fingerprint"] and item["id"] != duplicate["id"]
    )
    assert (
        client.post(
            "/api/duplicate-exclusion",
            json={"transaction_id": duplicate["id"], "excluded": True},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/transactions/{original['id']}").status_code == 200
    remaining = next(
        item
        for item in client.get("/api/state?month=2026-09").json()["transactions"]
        if item["id"] == duplicate["id"]
    )
    assert remaining["possible_duplicate"] is False
    assert remaining["excluded_from_spending"] is False
    assert total_spending() == 9000


def test_delete_transaction_updates_analytics_and_allows_reimport(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    content = (
        b"date,description,amount,transaction_id\n"
        b"2026-09-01,Example shop,20.00,one\n"
        b"2026-09-02,Example shop,30.00,two\n"
    )
    payload = {
        "filename": "transactions.csv",
        "content_base64": base64.b64encode(content).decode(),
        "source": "checking",
    }
    assert client.post("/api/import", json=payload).json()["rows_imported"] == 2
    rows = client.get("/api/state?month=2026-09").json()["transactions"]
    assert (
        client.post(
            "/api/correct",
            json={"transaction_id": rows[0]["id"], "category": "Groceries", "scope": "merchant"},
        ).status_code
        == 200
    )

    removed = client.delete(f"/api/transactions/{rows[0]['id']}")
    assert removed.status_code == 200
    assert removed.json() == {"transaction_id": rows[0]["id"], "deleted": True}
    snapshot = client.get("/api/state?month=2026-09").json()
    assert snapshot["dashboard"]["transaction_count"] == 1
    assert snapshot["dashboard"]["total_spending_cents"] == rows[1]["amount_cents"]
    assert snapshot["history"]["total_spending_cents"] == rows[1]["amount_cents"]
    assert store.load()["merchant_mappings"]["example shop"]["category"] == "Groceries"
    assert len(store.load()["import_runs"]) == 1
    assert client.delete(f"/api/transactions/{rows[0]['id']}").status_code == 404

    replay = client.post("/api/import", json=payload).json()
    assert replay["rows_imported"] == 1
    assert replay["duplicates_skipped"] == 1
    for item in client.get("/api/state?month=2026-09").json()["transactions"]:
        assert client.delete(f"/api/transactions/{item['id']}").status_code == 200
    empty = client.get("/api/state?month=2026-09").json()
    assert empty["dashboard"]["transaction_count"] == 0
    assert empty["dashboard"]["total_spending_cents"] == 0
    assert "2026-09" in empty["dashboard"]["available_months"]
    assert store.load()["merchant_mappings"]["example shop"]["category"] == "Groceries"


def test_deleting_synthetic_transaction_does_not_touch_persisted_data(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    initial = client.get("/api/state?month=synthetic").json()
    target = initial["transactions"][0]
    assert client.delete(f"/api/transactions/{target['id']}").status_code == 200
    changed = client.get("/api/state?month=synthetic").json()
    assert changed["dashboard"]["transaction_count"] == 59
    assert changed["dashboard"]["total_spending_cents"] == (
        initial["dashboard"]["total_spending_cents"] - target["amount_cents"]
    )
    assert store.load() == empty_state()
    restarted = TestClient(create_app(store, seed_demo=False))
    restarted_state = restarted.get("/api/state?month=synthetic").json()
    assert restarted_state["dashboard"]["transaction_count"] == 60


def test_manual_transaction_updates_analytics_without_mapping_or_import_run(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    payload = {
        "date": "2026-09-18",
        "description": "COFFEE SHOP #123 Seattle WA",
        "amount": "4.75",
        "category": "Food + coffee",
        "source": "Checking",
    }
    response = client.post("/api/transactions", json=payload)
    assert response.status_code == 201
    transaction = response.json()
    assert transaction["amount_cents"] == 475
    assert transaction["source"] == "checking"
    assert transaction["classification_source"] == "manual"
    assert transaction["import_run_id"] is None
    assert transaction["normalized_merchant"] != transaction["raw_description"].casefold()
    snapshot = client.get("/api/state?month=2026-09").json()
    assert snapshot["dashboard"]["total_spending_cents"] == 475
    assert snapshot["transactions"][0]["id"] == transaction["id"]
    assert store.load()["merchant_mappings"] == {}
    assert store.load()["import_runs"] == []

    repeated = client.post("/api/transactions", json=payload)
    assert repeated.status_code == 201
    assert repeated.json()["possible_duplicate"] is True
    assert repeated.json()["id"] != transaction["id"]
    refund = client.post("/api/transactions", json={**payload, "amount": "-1.25"})
    assert refund.status_code == 201
    assert client.get("/api/state?month=2026-09").json()["dashboard"]["total_spending_cents"] == 825


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-02-30"),
        ("date", "09/18/2026"),
        ("description", "  "),
        ("amount", "0"),
        ("amount", "4.999"),
        ("category", "Unknown"),
        ("source", "  "),
    ],
)
def test_manual_transaction_rejects_invalid_input(tmp_path, field, value):
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    payload = {
        "date": "2026-09-18",
        "description": "Coffee shop",
        "amount": "4.75",
        "category": "Food + coffee",
        "source": "manual",
    }
    assert client.post("/api/transactions", json={**payload, field: value}).status_code == 400
    assert store.load()["transactions"] == []


def test_manual_transaction_in_synthetic_month_is_isolated(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    before = client.get("/api/state?month=synthetic").json()
    response = client.post(
        "/api/transactions",
        json={
            "date": "2026-06-15",
            "description": "Demo coffee",
            "amount": "5.00",
            "category": "Food + coffee",
            "demo": True,
        },
    )
    assert response.status_code == 201
    after = client.get("/api/state?month=synthetic").json()
    assert after["dashboard"]["total_spending_cents"] == (
        before["dashboard"]["total_spending_cents"] + 500
    )
    assert any(item["id"] == response.json()["id"] for item in after["transactions"])
    assert store.load() == empty_state()
    restarted = TestClient(create_app(store, seed_demo=False))
    restarted_state = restarted.get("/api/state?month=synthetic").json()
    assert (
        restarted_state["dashboard"]["total_spending_cents"]
        == before["dashboard"]["total_spending_cents"]
    )


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


def test_spending_over_time_includes_gaps_and_excludes_non_spending():
    state = empty_state()
    assert spending_over_time(state)["months"] == []
    state["transactions"] = [
        {"date": "2026-05-10", "category": "Groceries", "amount_cents": 5000},
        {"date": "2026-06-02", "category": "Groceries", "amount_cents": 2500},
        {"date": "2026-08-01", "category": "Travel", "amount_cents": 12000},
        {
            "date": "2026-08-03",
            "category": "Other",
            "amount_cents": 200000,
            "excluded_from_spending": True,
        },
    ]
    history = spending_over_time(state)
    assert [item["month"] for item in history["months"]] == [
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
    ]
    assert [item["total_cents"] for item in history["months"]] == [5000, 2500, 0, 12000]
    assert history["months"][0]["categories"]["Groceries"] == 5000
    assert history["months"][0]["categories"]["Travel"] == 0
    assert history["total_spending_cents"] == 19500
    assert history["transaction_count"] == 4
    assert history["months_tracked"] == 3


def test_import_api_accepts_custom_csv_columns_and_explains_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "custom.json")
    client = TestClient(create_app(store, seed_demo=False))
    content = b"Posted On,Transaction Details,Charge\n2026-06-10,Coffee,4.50\n"
    payload = {
        "filename": "bank.csv",
        "content_base64": base64.b64encode(content).decode(),
        "source": "checking",
    }
    unmatched = client.post("/api/import", json=payload)
    assert unmatched.status_code == 400
    assert "Enter a custom column mapping" in unmatched.json()["detail"]
    payload["column_mapping"] = {
        "date": "Posted On",
        "description": "Transaction Details",
        "amount": "Charge",
    }
    imported = client.post("/api/import", json=payload)
    assert imported.status_code == 200
    assert imported.json()["rows_imported"] == 1
    assert (
        client.get("/api/state?month=2026-06").json()["transactions"][0]["raw_description"]
        == "Coffee"
    )
    assert store.load()["csv_column_mappings"]["checking"] == payload["column_mapping"]
    next_content = b"Posted On,Transaction Details,Charge\n2026-06-11,Tea,3.00\n"
    next_payload = {
        "filename": "next.csv",
        "content_base64": base64.b64encode(next_content).decode(),
        "source": "checking",
    }
    restarted = TestClient(create_app(store, seed_demo=False))
    assert restarted.post("/api/import", json=next_payload).json()["rows_imported"] == 1
    assert len(restarted.get("/api/state?month=2026-06").json()["transactions"]) == 2


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
    assert [item["month"] for item in initial["history"]["months"]] == ["2026-05", "2026-06"]
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


def test_synthetic_month_is_isolated_and_corrections_do_not_touch_real_data(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "state.json")
    client = TestClient(create_app(store, seed_demo=False))
    response = client.post(
        "/api/import",
        json={
            "filename": "private.csv",
            "source": "real-account",
            "content_base64": base64.b64encode(
                b"date,description,amount\n2026-06-10,PRIVATE SHOP,100\n"
            ).decode(),
        },
    )
    assert response.status_code == 200
    before = store.load()
    demo = client.get("/api/state?month=synthetic").json()
    assert demo["dashboard"]["month"] == "synthetic"
    assert "synthetic" in demo["dashboard"]["available_months"]
    assert demo["dashboard"]["transaction_count"] == 60
    assert demo["dashboard"]["review_count"] == 10
    assert demo["dashboard"]["comparison"]["has_previous_data"] is True
    assert demo["history"]["transaction_count"] == 347
    assert demo["history"]["months_tracked"] == 6
    assert [row["month"] for row in demo["history"]["months"]] == [
        f"2026-{month:02d}" for month in range(1, 7)
    ]
    for category in demo["categories"]:
        values = [row["categories"][category] for row in demo["history"]["months"]]
        assert all(value > 0 for value in values)
        assert len(set(values)) > 1
    assert demo["demo"] is True
    assert "PRIVATE SHOP" not in json.dumps(demo)
    assert all(row["source"].startswith("synthetic-") for row in demo["transactions"])
    target = demo["transactions"][0]
    assert (
        client.post(
            "/api/correct",
            json={"transaction_id": target["id"], "category": "Groceries", "scope": "merchant"},
        ).status_code
        == 200
    )
    changed = client.get("/api/state?month=synthetic").json()
    assert (
        next(row for row in changed["transactions"] if row["id"] == target["id"])["category"]
        == "Groceries"
    )
    assert store.load() == before
    real = client.get("/api/state?month=2026-06").json()
    assert real["dashboard"]["transaction_count"] == 1
    assert real["demo"] is False


def test_presentation_history_is_repeatable_and_never_calls_jev():
    state = empty_state()
    reports = seed_presentation_history(state)
    assert sum(report["rows_imported"] for report in reports) == 347
    assert sum(report["jev_attempts"] for report in reports) == 0
    expected = spending_over_time(state)
    replay = seed_presentation_history(state)
    assert sum(report["rows_imported"] for report in replay) == 0
    assert sum(report["duplicates_skipped"] for report in replay) == 347
    assert spending_over_time(state) == expected
    another_state = empty_state()
    seed_presentation_history(another_state)
    assert spending_over_time(another_state) == expected
