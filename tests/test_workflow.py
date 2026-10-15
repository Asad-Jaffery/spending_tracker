import json
import urllib.error
from io import BytesIO

import pytest

from spend_tracker.__main__ import main
from spend_tracker.classifiers import JevClassifier, JevError, UnconfiguredJevClassifier
from spend_tracker.config import database_url, jev_api_key
from spend_tracker.domain import Category, Prediction
from spend_tracker.parsing import parse_csv, parse_json
from spend_tracker.services import (
    correct_category,
    import_transactions,
    retry_unclassified,
    spending_summary,
)
from spend_tracker.storage import JsonStore, empty_state


def test_existing_json_store_without_csv_mappings_still_loads(tmp_path):
    path = tmp_path / "existing.json"
    previous_state = empty_state()
    del previous_state["csv_column_mappings"]
    path.write_text(json.dumps(previous_state), encoding="utf-8")
    store = JsonStore(path)
    assert store.load()["csv_column_mappings"] == {}


def test_database_url_reads_local_env_with_shell_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPEND_TRACKER_DATABASE_URL", raising=False)
    (tmp_path / ".env").write_text(
        "SPEND_TRACKER_DATABASE_URL=postgresql:///local_test\n", encoding="utf-8"
    )
    assert database_url() == "postgresql:///local_test"
    monkeypatch.setenv("SPEND_TRACKER_DATABASE_URL", "postgresql:///shell_test")
    assert database_url() == "postgresql:///shell_test"


def test_json_to_postgres_migration_preserves_source_and_refuses_overwrite(
    tmp_path, monkeypatch, capsys
):
    source = JsonStore(tmp_path / "source.json")
    target = JsonStore(tmp_path / "target.json")
    state = empty_state()
    state["csv_column_mappings"]["checking"] = {
        "date": "Posted On",
        "description": "Details",
        "amount": "Charge",
    }
    source.save(state)
    monkeypatch.setattr("spend_tracker.__main__.PostgresStore", lambda url: target)
    command = [
        "--data",
        str(source.path),
        "--database-url",
        "postgresql:///test",
        "migrate-to-postgres",
    ]
    assert main(command) == 0
    assert target.load() == source.load()
    assert main(command) == 1
    assert "did not overwrite" in capsys.readouterr().err
    assert source.load() == state


class FakeClassifier:
    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = []

    def classify(self, merchant, categories):
        self.calls.append(merchant)
        if isinstance(self.prediction, Exception):
            raise self.prediction
        return self.prediction


def sample():
    content = (
        b"date,description,amount,category,transaction_id\n"
        b"2026-09-01,COSTCO WHSE #1029,10.00,Dining,one\n"
        b"2026-09-02,COSTCO WHSE #928,20.00,Dining,two\n"
    )
    return content, parse_csv(content)


def test_jev_mapping_reused_csv_label_ignored_and_duplicates():
    state = empty_state()
    content, parsed = sample()
    classifier = FakeClassifier(Prediction(Category.GROCERIES, 0.01))
    first = import_transactions(
        state,
        parsed,
        filename="one.csv",
        content=content,
        source="checking",
        currency="USD",
        classifier=classifier,
    )
    assert first["rows_imported"] == 2
    assert first["jev_attempts"] == 1
    assert classifier.calls == ["costco whse"]
    assert state["merchant_mappings"]["costco whse"]["category"] == "Groceries"
    assert [item["classification_source"] for item in state["transactions"]] == [
        "jev",
        "merchant_mapping",
    ]
    assert {item["category"] for item in state["transactions"]} == {"Groceries"}
    second = import_transactions(
        state,
        parsed,
        filename="replay.csv",
        content=content,
        source="checking",
        currency="USD",
        classifier=classifier,
    )
    assert second["duplicates_skipped"] == 2
    assert second["jev_attempts"] == 0


def test_failure_and_explicit_other_are_different():
    content = b"date,description,amount,category\n2026-09-01,Unknown Shop,1.00,Dining\n"
    state = empty_state()
    classifier = FakeClassifier(JevError("offline"))
    import_transactions(
        state,
        parse_csv(content),
        filename="fail.csv",
        content=content,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert state["transactions"][0]["category"] == "Other"
    assert state["transactions"][0]["needs_review"] is True
    assert state["merchant_mappings"] == {}
    assert state["import_runs"][0]["jev_failures"] == 1
    classifier.prediction = Prediction(Category.OTHER, 0.2)
    next_content = b"date,description,amount\n2026-09-02,Unknown Shop,2.00\n"
    import_transactions(
        state,
        parse_csv(next_content),
        filename="retry.csv",
        content=next_content,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert state["merchant_mappings"]["unknown shop"]["category"] == "Other"
    assert state["transactions"][1]["needs_review"] is False


def test_retry_classifies_existing_review_rows_without_reimporting():
    state = empty_state()
    content, parsed = sample()
    first = import_transactions(
        state,
        parsed,
        filename="first.csv",
        content=content,
        source="checking",
        currency="USD",
        classifier=UnconfiguredJevClassifier(),
    )
    assert first["jev_skipped_no_key"] == 1
    assert first["jev_attempts"] == 0
    assert all(item["needs_review"] for item in state["transactions"])
    classifier = FakeClassifier(Prediction(Category.GROCERIES, 0.7))
    result = retry_unclassified(state, classifier)
    assert result["merchants_attempted"] == 1
    assert result["transactions_updated"] == 2
    assert classifier.calls == ["costco whse"]
    assert all(item["category"] == "Groceries" for item in state["transactions"])
    assert all(not item["needs_review"] for item in state["transactions"])
    assert state["merchant_mappings"]["costco whse"]["category"] == "Groceries"
    assert retry_unclassified(state, classifier)["transactions_updated"] == 0


def test_retry_preserves_manual_corrections_and_failed_rows():
    state = empty_state()
    content = b"date,description,amount\n2026-09-01,New Cafe,4.00\n2026-09-02,New Cafe,5.00\n"
    import_transactions(
        state,
        parse_csv(content),
        filename="first.csv",
        content=content,
        source="checking",
        currency="USD",
        classifier=UnconfiguredJevClassifier(),
    )
    correct_category(state, state["transactions"][0]["id"], Category.SHOPPING)
    failure = retry_unclassified(state, FakeClassifier(JevError("Jev returned HTTP 401")))
    assert failure["jev_failures"] == 1
    assert failure["jev_errors"] == ["Jev returned HTTP 401"]
    assert state["merchant_mappings"] == {}
    success = retry_unclassified(state, FakeClassifier(Prediction(Category.FOOD_COFFEE)))
    assert success["transactions_updated"] == 1
    assert [item["category"] for item in state["transactions"]] == [
        "Shopping",
        "Food + coffee",
    ]


def test_jev_key_can_be_read_from_ignored_local_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JEVMODEL_API_KEY", raising=False)
    (tmp_path / ".env").write_text("# local secret\nJEVMODEL_API_KEY='sk-local'\n")
    assert jev_api_key() == "sk-local"
    monkeypatch.setenv("JEVMODEL_API_KEY", "sk-shell")
    assert jev_api_key() == "sk-shell"


def test_corrections_and_future_mapping():
    state = empty_state()
    content, parsed = sample()
    classifier = FakeClassifier(Prediction(Category.SHOPPING))
    import_transactions(
        state,
        parsed,
        filename="one.csv",
        content=content,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    first_id = state["transactions"][0]["id"]
    assert correct_category(state, first_id, Category.GROCERIES) == 1
    assert state["merchant_mappings"]["costco whse"]["category"] == "Shopping"
    assert state["transactions"][1]["category"] == "Shopping"
    assert correct_category(state, first_id, Category.GROCERIES, "merchant") == 2
    assert {item["category"] for item in state["transactions"]} == {"Groceries"}
    new = b"date,description,amount\n2026-09-03,COSTCO WHSE #928,5.00\n"
    import_transactions(
        state,
        parse_csv(new),
        filename="new.csv",
        content=new,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert classifier.calls == ["costco whse"]
    assert state["transactions"][-1]["category"] == "Groceries"
    assert spending_summary(state)["net_spending"] == "35.00"


def test_json_input_and_store_round_trip(tmp_path):
    content = json.dumps(
        [
            {"date": "2026-09-01", "raw_description": "Cafe", "amount": "3.20"},
            {"date": "bad", "raw_description": "Cafe", "amount": "4.00"},
        ]
    ).encode()
    parsed = parse_json(content)
    assert parsed.rows_processed == 2
    assert len(parsed.rows) == 1
    assert len(parsed.errors) == 1
    state = empty_state()
    import_transactions(
        state,
        parsed,
        filename="sample.json",
        content=content,
        source="bank",
        currency="USD",
        classifier=UnconfiguredJevClassifier(),
    )
    store = JsonStore(tmp_path / "state.json")
    store.save(state)
    assert store.load() == state
    assert state["transactions"][0]["needs_review"] is True


def test_no_id_replay_skipped_but_similar_purchase_preserved():
    state = empty_state()
    first = b"date,description,amount\n2026-09-01,Cafe,4.00\n"
    another = b"date,description,amount\n2026-09-01,Cafe,4.00\n2026-09-02,Book,8.00\n"
    classifier = UnconfiguredJevClassifier()
    import_transactions(
        state,
        parse_csv(first),
        filename="a.csv",
        content=first,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    replay = import_transactions(
        state,
        parse_csv(first),
        filename="renamed.csv",
        content=first,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert replay["duplicates_skipped"] == 1
    later = import_transactions(
        state,
        parse_csv(another),
        filename="another.csv",
        content=another,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert later["rows_imported"] == 2
    assert later["possible_duplicates"] == 1


def test_invalid_jev_category_falls_back_without_mapping():
    state = empty_state()
    content = b"date,description,amount\n2026-09-01,Cafe,4.00\n"
    classifier = FakeClassifier(Prediction("MadeUp", 0.9))
    import_transactions(
        state,
        parse_csv(content),
        filename="a.csv",
        content=content,
        source="bank",
        currency="USD",
        classifier=classifier,
    )
    assert state["merchant_mappings"] == {}
    assert state["transactions"][0]["needs_review"] is True


def test_cli_import_and_summary(tmp_path, capsys):
    source = tmp_path / "sample.json"
    source.write_text('[{"date":"2026-09-01","raw_description":"Coffee","amount":"4.25"}]')
    store = tmp_path / "state.json"
    assert main(["--data", str(store), "import", str(source), "--source", "bank"]) == 0
    capsys.readouterr()
    assert main(["--data", str(store), "summary"]) == 0
    assert json.loads(capsys.readouterr().out)["net_spending"] == "4.25"


def test_jev_request_and_response(monkeypatch):
    captured = {}

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        return BytesIO(
            json.dumps(
                {
                    "answers": {
                        "category": {
                            "type": "choice",
                            "choice": "Food + coffee",
                            "confidence": 0.02,
                        }
                    }
                }
            ).encode()
        )

    monkeypatch.setattr("spend_tracker.classifiers.urllib.request.urlopen", open_request)
    result = JevClassifier("sk-test").classify("coffee shop", tuple(Category))
    assert result == Prediction(Category.FOOD_COFFEE, 0.02)
    assert captured["url"] == "https://jevmodel.org/v1/systemone"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"]["questions"]["category"]["type"] == "choice"
    assert set(captured["body"]["questions"]["category"]["criteria"]) == {
        category.value for category in Category
    }
    assert [category.value for category in Category] == [
        "Groceries",
        "Food + coffee",
        "Transportation",
        "Shopping",
        "Entertainment",
        "Travel",
        "Health + personal wellness",
        "Other",
    ]


def test_jev_retry_reuses_idempotency_key(monkeypatch):
    keys = []

    def open_request(request, timeout):
        keys.append(dict(request.header_items())["Idempotency-key"])
        if len(keys) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "rate limit", {}, None)
        return BytesIO(
            json.dumps({"answers": {"category": {"type": "choice", "choice": "Other"}}}).encode()
        )

    monkeypatch.setattr("spend_tracker.classifiers.urllib.request.urlopen", open_request)
    monkeypatch.setattr("spend_tracker.classifiers.time.sleep", lambda seconds: None)
    assert JevClassifier("sk-test").classify("unknown", tuple(Category)).category == Category.OTHER
    assert len(keys) == 2
    assert keys[0] == keys[1]


def test_bad_confidence_does_not_discard_valid_jev_category(monkeypatch):
    def open_request(request, timeout):
        return BytesIO(
            json.dumps(
                {
                    "answers": {
                        "category": {
                            "type": "choice",
                            "choice": "Food + coffee",
                            "confidence": "not-a-number",
                        }
                    }
                }
            ).encode()
        )

    monkeypatch.setattr("spend_tracker.classifiers.urllib.request.urlopen", open_request)
    prediction = JevClassifier("sk-test").classify("cafe", tuple(Category))
    assert prediction == Prediction(Category.FOOD_COFFEE, None)


def test_retired_jev_category_is_rejected(monkeypatch):
    def open_request(request, timeout):
        return BytesIO(
            json.dumps({"answers": {"category": {"type": "choice", "choice": "Dining"}}}).encode()
        )

    monkeypatch.setattr("spend_tracker.classifiers.urllib.request.urlopen", open_request)
    with pytest.raises(JevError, match="invalid classification"):
        JevClassifier("sk-test").classify("cafe", tuple(Category))
