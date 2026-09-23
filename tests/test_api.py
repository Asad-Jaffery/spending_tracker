from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from spend_tracker.domain import Category, ClassificationSource, Prediction
from spend_tracker.models import ImportRun, MerchantMapping, Transaction

from .conftest import FakeClassifier, upload

HEADER = "date,description,amount,category,transaction_id\n"


def test_unconfigured_import_fallbacks_and_partial_validation(client):
    response = upload(
        client,
        HEADER
        + "2026-09-01,COSTCO WHSE #1029,12.34,Shopping,one\n"
        + "2026-09-02,Unknown shop,2.00,,two\n"
        + "2026-02-30,Wrong date,2.00,,three\n"
        + "2026-09-03,Bad money,NaN,,four\n",
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert (run["rows_processed"], run["rows_imported"], run["rows_failed"]) == (4, 2, 2)
    assert run["jev_attempts"] == 0
    assert [e["line"] for e in run["errors"]] == [4, 5]
    transactions = {t["normalized_merchant"]: t for t in client.get("/transactions").json()}
    assert transactions["costco"]["classification_source"] == "csv"
    assert transactions["costco"]["amount"] == "12.34"
    assert transactions["unknown shop"]["needs_review"] is True
    assert client.get("/merchants").json() == []
    assert client.get(f"/imports/{run['id']}").json() == run
    assert client.get("/health").json()["jev"] == "unconfigured"


def test_unknown_merchant_called_once_and_mapping_reused(app_factory):
    classifier = FakeClassifier()
    with TestClient(app_factory(classifier)) as client:
        response = upload(
            client,
            HEADER
            + "2026-09-01,SQ *CAFE 123,1.00,Shopping,one\n"
            + "2026-09-02,CAFE 456,2.00,Shopping,two\n",
        )
        assert response.status_code == 201, response.text
        assert response.json()["jev_attempts"] == 1
        assert classifier.calls == ["cafe"]
        sources = {t["classification_source"] for t in client.get("/transactions").json()}
        assert sources == {"jev", "merchant_mapping"}
        assert {t["category"] for t in client.get("/transactions").json()} == {"Dining"}
        upload(client, HEADER + "2026-09-03,CAFE 789,3.00,,three\n")
        assert classifier.calls == ["cafe"]


@pytest.mark.parametrize(
    "result",
    [
        Prediction(category=Category.DINING, confidence=0.2),
        TimeoutError("provider secret must never enter the response"),
        {"category": "Not a real category", "confidence": 0.99},
        {"category": "Dining", "confidence": float("nan")},
    ],
)
def test_bad_jev_results_fall_back_without_poisoning_cache(app_factory, result):
    classifier = FakeClassifier(result)
    with TestClient(app_factory(classifier)) as client:
        response = upload(
            client,
            HEADER + "2026-09-01,Shop,1.00,Health,one\n2026-09-02,Shop,2.00,,two\n",
        )
        assert response.status_code == 201
        assert classifier.calls == ["shop"]
        assert client.get("/merchants").json() == []
        entries = {t["source_transaction_id"]: t for t in client.get("/transactions").json()}
        assert entries["one"]["category"] == "Health"
        assert entries["two"]["category"] == "Other"
        assert entries["two"]["needs_review"] is True


def test_correction_scopes_and_future_imports(app_factory):
    classifier = FakeClassifier(Prediction(category=Category.SHOPPING, confidence=0.92))
    with TestClient(app_factory(classifier)) as client:
        upload(
            client,
            HEADER
            + "2026-09-01,COSTCO WHSE #1029,12.34,,one\n"
            + "2026-09-02,COSTCO WHOLESALE,5.00,,two\n",
        )
        entries = client.get("/transactions").json()
        first_id = entries[0]["id"]
        response = client.patch(
            f"/transactions/{first_id}/category",
            json={"category": "Groceries", "scope": "transaction"},
        )
        assert response.json()["updated_transactions"] == 1
        assert client.get("/merchants").json()[0]["category"] == "Shopping"
        upload(client, HEADER + "2026-09-03,COSTCO 00129,3.00,,three\n")
        newest = client.get("/transactions").json()[0]
        assert newest["category"] == "Shopping"
        response = client.patch(
            f"/transactions/{first_id}/category",
            json={"category": "Groceries", "scope": "merchant"},
        )
        assert response.json()["updated_transactions"] == 3
        mapping = client.get("/merchants").json()[0]
        assert mapping["category"] == "Groceries"
        assert mapping["source"] == "manual"
        assert mapping["confidence"] is None
        upload(client, HEADER + "2026-09-04,COSTCO WHSE #4321,4.00,,four\n")
        entries = client.get("/transactions").json()
        assert {t["category"] for t in entries} == {"Groceries"}
        assert entries[0]["classification_source"] == "merchant_mapping"
        assert classifier.calls == ["costco"]


def test_merchant_correction_creates_mapping_without_jev(client):
    upload(client, HEADER + "2026-09-01,New shop,1.00,,one\n")
    transaction = client.get("/transactions").json()[0]
    response = client.patch(
        f"/transactions/{transaction['id']}/category",
        json={"category": "Health", "scope": "merchant"},
    )
    assert response.status_code == 200
    assert client.get("/merchants").json()[0]["category"] == "Health"


def test_known_mapping_never_calls_jev(app_factory):
    classifier = FakeClassifier()
    app = app_factory(classifier)
    with app.state.sessions.begin() as session:
        session.add(
            MerchantMapping(
                normalized_merchant="spotify",
                display_name="Spotify",
                category=Category.ENTERTAINMENT,
                source=ClassificationSource.MANUAL,
            )
        )
    with TestClient(app) as client:
        upload(client, HEADER + "2026-09-01,SPOTIFY USA 1234,11.99,,one\n")
        assert classifier.calls == []
        assert client.get("/transactions").json()[0]["category"] == "Entertainment"


def test_dedupe_source_ids_and_source_isolation(client):
    csv_text = HEADER + "2026-09-01,Shop,1.00,Dining,one\n"
    assert upload(client, csv_text).json()["rows_imported"] == 1
    assert upload(client, csv_text).json()["duplicates_skipped"] == 1
    overlapping = csv_text + "2026-09-02,Shop,2.00,Dining,two\n"
    result = upload(client, overlapping).json()
    assert (result["rows_imported"], result["duplicates_skipped"]) == (1, 1)
    assert upload(client, csv_text, source="another-account").json()["rows_imported"] == 1


def test_identical_purchases_preserved_and_exact_reupload_skipped(client):
    csv_text = "date,description,amount\n2026-09-01,Shop,1.00\n2026-09-01,Shop,1.00\n"
    first = upload(client, csv_text).json()
    assert first["rows_imported"] == 2
    assert first["possible_duplicates"] == 1
    second = upload(client, csv_text).json()
    assert second["rows_imported"] == 0
    assert second["duplicates_skipped"] == 2
    changed_file = "date,description,amount\n2026-09-01,Shop,1.00\n"
    result = upload(client, changed_file).json()
    assert result["rows_imported"] == 1
    assert result["possible_duplicates"] == 1


def test_analytics_exact_money_refunds_transfers_and_currency(client):
    upload(
        client,
        HEADER
        + "2026-09-01,Shop,0.10,Shopping,one\n"
        + "2026-09-02,Shop,0.20,Shopping,two\n"
        + "2026-09-03,Shop,-0.10,Shopping,three\n"
        + "2026-09-04,Salary,-1000.00,Income,four\n"
        + "2026-09-05,Payment,500.00,Transfers,five\n"
        + "2026-10-01,Cafe,2.00,Dining,six\n",
    )
    upload(client, HEADER + "2026-09-01,Shop,50.00,Shopping,eur-one\n", currency="EUR")
    result = client.get("/analytics/summary").json()
    assert result["net_spending"] == "2.20"
    assert result["transaction_count"] == 4
    assert result["by_category"] == {"Dining": "2.00", "Shopping": "0.20"}
    assert result["by_month"] == {"2026-09": "0.20", "2026-10": "2.00"}
    assert client.get("/analytics/summary?date_to=2026-09-30").json()["net_spending"] == "0.20"
    assert client.get("/analytics/summary?currency=EUR").json()["net_spending"] == "50.00"


def test_invalid_requests_and_pagination(client):
    assert (
        client.patch("/transactions/missing/category", json={"category": "Dining"}).status_code
        == 404
    )
    assert (
        client.patch("/transactions/missing/category", json={"category": "Invalid"}).status_code
        == 422
    )
    assert client.get("/imports/missing").status_code == 404
    assert client.get("/transactions?limit=0").status_code == 422
    assert (
        client.get("/analytics/summary?date_from=2026-10-01&date_to=2026-09-01").status_code == 422
    )
    assert upload(client, "missing,headers\n1,2\n").status_code == 422
    assert client.get("/imports").json() == []
    upload(client, HEADER + "2026-09-01,A,1.00,,one\n2026-09-02,B,2.00,,two\n")
    assert len(client.get("/transactions?limit=1&offset=1").json()) == 1
    assert len(client.get("/transactions?needs_review=true").json()) == 2
    assert client.get("/transactions?needs_review=false").json() == []


def test_upload_and_row_limits_leave_no_writes(app_factory):
    with TestClient(app_factory(max_upload_bytes=40)) as client:
        assert upload(client, "x" * 41).status_code == 413
        assert client.get("/imports").json() == []
    with TestClient(app_factory(max_import_rows=1)) as client:
        response = upload(client, HEADER + "2026-09-01,A,1.00,,one\n2026-09-02,B,2.00,,two\n")
        assert response.status_code == 422
        assert client.get("/transactions").json() == []


def test_concurrent_reuploads_are_idempotent(client):
    csv_text = HEADER + "2026-09-01,Cafe,1.00,Dining,one\n"
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: upload(client, csv_text), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert sum(response.json()["rows_imported"] for response in responses) == 1
    assert len(client.get("/transactions").json()) == 1


def test_database_failure_rolls_back_import(app_factory, monkeypatch):
    import spend_tracker.services as services

    app = app_factory()
    original = services.ClassificationEngine.classify
    calls = 0

    def fail_second(self, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated mid-import failure")
        return original(self, *args)

    monkeypatch.setattr(services.ClassificationEngine, "classify", fail_second)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = upload(client, HEADER + "2026-09-01,A,1.00,,one\n2026-09-02,B,2.00,,two\n")
        assert response.status_code == 500
    with app.state.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Transaction)) == 0
        assert session.scalar(select(func.count()).select_from(ImportRun)) == 0
