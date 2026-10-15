import base64
import copy
import json

import pytest
from fastapi.testclient import TestClient

from spend_tracker.__main__ import main
from spend_tracker.import_profiles import save_import_profile
from spend_tracker.parsing import InputError, parse_csv, preset_settings
from spend_tracker.storage import JsonStore, empty_state
from spend_tracker.webapp import create_app


def client_for(store):
    return TestClient(create_app(store, seed_demo=False))


def save_payload(**overrides):
    return {"label": "Example card", "source": "example-card", "settings": {}, **overrides}


def import_payload(content, preset):
    return {
        "filename": "example.csv",
        "content_base64": base64.b64encode(content).decode(),
        "source": "example-card",
        "import_preset": preset,
    }


def test_save_without_file_persists_across_restart_and_in_synthetic_view(tmp_path):
    store = JsonStore(tmp_path / "state.json")
    client = client_for(store)
    original = store.load()
    result = client.post(
        "/api/import-presets",
        json=save_payload(
            id="wells_fargo",
            label="My Wells Fargo",
            settings={"payment_value": "CUSTOM PAYMENT"},
        ),
    )
    assert result.status_code == 200
    for collection in ("transactions", "merchant_mappings", "import_runs", "csv_column_mappings"):
        assert store.load()[collection] == original[collection]
    restarted = client_for(JsonStore(store.path))
    for path in ("/api/state", "/api/state?month=synthetic"):
        preset = restarted.get(path).json()["import_presets"][0]
        assert preset["label"] == "My Wells Fargo"
        assert preset["settings"]["payment_value"] == "CUSTOM PAYMENT"
        assert preset["default_settings"]["payment_value"] == "ONLINE PAYMENT THANK YOU"


def test_future_card_saved_rules_skip_before_parsing_or_jev_keep_refunds(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "state.json")
    client = client_for(store)
    result = client.post(
        "/api/import-presets",
        json=save_payload(
            settings={
                "column_mapping": {"date": "Posted On", "description": "Memo", "amount": "Debit"},
                "amount_convention": "expenses_negative",
                "ignore_rules": [
                    {"column": "Memo", "operator": "equals", "value": "CARD PAYMENT"},
                    {"column": "Memo", "operator": "starts_with", "value": "TRANSFER:"},
                ],
            }
        ),
    )
    assert result.status_code == 200
    preset_id = result.json()["id"]
    content = (
        b"Posted On,Memo,Debit\n2026-09-01,Example shop,-20\n"
        b"2026-09-02,Example refund,5\ninvalid, card payment ,bad\n"
        b"invalid,TRANSFER: savings,bad\n"
    )
    result = client.post("/api/import", json=import_payload(content, preset_id))
    assert result.status_code == 200
    report = result.json()
    assert report["rows_imported"] == 2
    assert report["rules_skipped"] == 2
    assert report["rows_failed"] == 0
    assert report["jev_skipped_no_key"] == 2
    assert sum(t["amount_cents"] for t in store.load()["transactions"]) == 1500
    assert len(store.load()["import_profiles"]) == 1
    assert (
        client.post("/api/import", json=import_payload(content, preset_id)).json()[
            "duplicates_skipped"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "operator, value",
    [
        ("equals", "ONLINE PAYMENT THANK YOU"),
        ("contains", "PAYMENT"),
        ("starts_with", "ONLINE PAYMENT"),
    ],
)
def test_generic_rules_ignore_case_spaces_and_do_not_skip_refunds(operator, value):
    parsed = parse_csv(
        b"Date,Description,Amount\n2026-09-01, online payment thank you ,100\n"
        b"2026-09-02,Refund,-5\n2026-09-03,Example,20\n",
        import_preset="custom",
        import_settings={
            "ignore_rules": [{"column": " description ", "operator": operator, "value": value}]
        },
    )
    assert parsed.rules_skipped == 1
    assert [row.transaction.amount for row in parsed.rows] == [-5, 20]


def test_rule_missing_column_stops_import_and_all_ignored_file_is_valid():
    settings = {"ignore_rules": [{"column": "Type", "operator": "equals", "value": "Payment"}]}
    with pytest.raises(InputError, match="Ignore rule column not found: Type"):
        parse_csv(
            b"Date,Description,Amount\n2026-09-01,Example,20\n",
            import_preset="custom",
            import_settings=settings,
        )
    parsed = parse_csv(
        b"Date,Description,Amount,Type\nbad,Payment,bad,Payment\n",
        import_preset="custom",
        import_settings=settings,
    )
    assert parsed.rules_skipped == parsed.rows_processed == 1
    assert parsed.rows == parsed.errors == []


@pytest.mark.parametrize(
    "settings",
    [
        {"ignore_rules": "Payment"},
        {"ignore_rules": [{"column": "Memo", "operator": "equals", "value": ""}]},
        {"ignore_rules": [{"column": "", "operator": "equals", "value": "Payment"}]},
        {"ignore_rules": [{"column": "Memo", "operator": "regex", "value": ".*"}]},
        {"ignore_rules": [{"column": "Memo", "operator": "equals"}]},
        {"ignore_rules": [{"column": "Memo", "operator": "equals", "value": "Payment"}] * 31},
        {"column_mapping": {"date": "Date"}},
        {"column_mapping": {"date": "Date", "description": "date", "amount": "Amount"}},
        {"column_mapping": {"date": "Date", "description": "Memo", "amount": "Amount", "x": "X"}},
    ],
)
def test_invalid_save_rejected_without_writing(tmp_path, settings):
    store = JsonStore(tmp_path / "state.json")
    result = client_for(store).post("/api/import-presets", json=save_payload(settings=settings))
    assert result.status_code == 400
    assert not store.path.exists()


def test_saved_robinhood_settings_keep_existing_overlap_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_tracker.webapp.jev_api_key", lambda: None)
    client = client_for(JsonStore(tmp_path / "state.json"))
    first = (
        b"Date,Time,Merchant,Description,Amount,Type\n2026-09-01,1:00 PM,Shop,Shop,20,Purchase\n"
    )
    assert (
        client.post("/api/import", json=import_payload(first, "robinhood")).json()["rows_imported"]
        == 1
    )
    assert (
        client.post(
            "/api/import-presets",
            json=save_payload(
                id="robinhood", label="My Robinhood", settings={"skip_non_posted": False}
            ),
        ).status_code
        == 200
    )
    overlapping = first + b"2026-09-02,1:00 PM,Shop,Shop,30,Purchase\n"
    report = client.post("/api/import", json=import_payload(overlapping, "robinhood")).json()
    assert report["duplicates_skipped"] == report["rows_imported"] == 1


def test_cli_uses_saved_custom_preset(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("spend_tracker.__main__.jev_api_key", lambda: None)
    store = JsonStore(tmp_path / "state.json")
    state = empty_state()
    preset = save_import_profile(
        state,
        save_payload(
            settings={
                "ignore_rules": [
                    {"column": "Description", "operator": "equals", "value": "Payment"}
                ]
            }
        ),
    )
    store.save(state)
    path = tmp_path / "example.csv"
    path.write_text("Date,Description,Amount\n2026-09-01,Shop,20\n2026-09-02,Payment,-100\n")
    assert (
        main(
            [
                "--data",
                str(store.path),
                "import",
                str(path),
                "--source",
                "example-card",
                "--import-preset",
                preset["id"],
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["rows_imported"] == report["rules_skipped"] == 1


def test_profile_only_migration_is_preserved_and_blocks_overwrite(tmp_path, monkeypatch, capsys):
    source = JsonStore(tmp_path / "source.json")
    target = JsonStore(tmp_path / "target.json")
    state = empty_state()
    save_import_profile(state, save_payload())
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
    assert target.load() == state
    assert main(command) == 1
    assert "did not overwrite" in capsys.readouterr().err


def test_old_json_store_loads_without_import_profiles(tmp_path):
    state = empty_state()
    del state["import_profiles"]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(state))
    assert JsonStore(path).load()["import_profiles"] == {}


def test_settings_validation_does_not_mutate_caller_defaults():
    settings = {"ignore_rules": [{"column": " Memo ", "operator": "equals", "value": " Payment "}]}
    original = copy.deepcopy(settings)
    result = preset_settings("custom", settings)
    assert settings == original
    assert result["ignore_rules"][0]["column"] == "Memo"
