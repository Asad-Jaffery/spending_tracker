import json

import pytest

from spend_tracker.__main__ import main
from spend_tracker.parsing import InputError, parse_csv, preset_settings
from spend_tracker.storage import JsonStore


def test_edited_preset_changes_columns_signs_dates_and_filters():
    parsed = parse_csv(
        b"Posted On,Memo,Debit,Vendor,State\n"
        b"09/01/2026,Example location,20,Example shop,Settled\n"
        b"09/02/2026,CARD PAYMENT,100,Payment,Settled\n"
        b"09/03/2026,Pending shop,30,Shop,Pending\n"
        b"09/04/2026,ONLINE PAYMENT THANK YOU,5,Example shop,Settled\n",
        import_preset="wells_fargo",
        import_settings={
            "date_format": "us",
            "amount_convention": "expenses_positive",
            "column_mapping": {"date": "Posted On", "description": "Memo", "amount": "Debit"},
            "merchant_column": "Vendor",
            "payment_column": "Memo",
            "payment_value": "CARD PAYMENT",
            "status_column": "State",
            "posted_value": "Settled",
        },
    )
    assert parsed.payments_skipped == 1
    assert parsed.non_posted_skipped == 1
    assert not parsed.errors
    assert [row.transaction.amount for row in parsed.rows] == [20, 5]
    assert parsed.rows[0].merchant == "example shop"
    assert parsed.rows[0].transaction.raw_description == "Example location"
    assert parsed.rows[0].transaction.date.isoformat() == "2026-09-01"
    assert preset_settings("wells_fargo")["payment_value"] == "ONLINE PAYMENT THANK YOU"


def test_disabling_preset_rules_removes_their_column_requirements():
    parsed = parse_csv(
        b"Date,Description,Amount\n2026-09-01,Example shop,20\n",
        import_preset="robinhood",
        import_settings={
            "merchant_column": "",
            "skip_payments": False,
            "skip_non_posted": False,
            "match_rows": False,
        },
    )
    assert parsed.rows[0].merchant == "example shop"
    assert parsed.rows[0].transaction.source_transaction_id is None


def test_edited_date_format_is_enforced_instead_of_auto_detection():
    with pytest.raises(InputError, match="No valid transaction rows"):
        parse_csv(
            b"DATE,DESCRIPTION,AMOUNT\n09/01/2026,Example,-20\n",
            import_preset="wells_fargo",
            import_settings={"date_format": "iso"},
        )


def test_supplied_transaction_id_is_kept_instead_of_derived_matching_id():
    parsed = parse_csv(
        b"Date,Time,Merchant,Description,Amount,Type,Reference\n"
        b"2026-09-01,1:00 PM,Example,Example location,20,Purchase,bank-one\n",
        import_preset="robinhood",
        import_settings={
            "column_mapping": {
                "date": "Date",
                "description": "Description",
                "amount": "Amount",
                "id": "Reference",
            },
        },
    )
    assert parsed.rows[0].transaction.source_transaction_id == "bank-one"


@pytest.mark.parametrize(
    "settings",
    [
        {"skip_payments": "false"},
        {"date_format": "guess"},
        {"amount_convention": "zero"},
        {"payment_value": ""},
        {"unknown_field": True},
    ],
)
def test_invalid_preset_settings_are_rejected(settings):
    with pytest.raises(InputError):
        preset_settings("wells_fargo", settings)


@pytest.mark.parametrize(
    ("preset", "content"),
    [
        (
            "wells_fargo",
            "DATE,DESCRIPTION,AMOUNT\n"
            "2026-09-01,Example shop,-20\n"
            "2026-09-02,Example shop,5\n"
            "2026-09-03,ONLINE PAYMENT THANK YOU,100\n",
        ),
        (
            "robinhood",
            "Date,Time,Merchant,Description,Amount,Type\n"
            "2026-09-01,1:00 PM,Example shop,Example shop,20,Purchase\n"
            "2026-09-02,1:00 PM,Example shop,Example shop,-5,Refund\n"
            "2026-09-03,1:00 PM,Payment,PAYMENT - THANK YOU,-100,Payment\n",
        ),
    ],
)
def test_cli_card_presets(tmp_path, monkeypatch, capsys, preset, content):
    monkeypatch.setattr("spend_tracker.__main__.jev_api_key", lambda: None)
    csv_path = tmp_path / "card.csv"
    csv_path.write_text(content)
    state_path = tmp_path / "state.json"
    assert (
        main(
            [
                "--data",
                str(state_path),
                "import",
                str(csv_path),
                "--source",
                "example-card",
                "--import-preset",
                preset,
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["rows_imported"] == 2
    assert report["payments_skipped"] == 1
    assert (
        sum(item["amount_cents"] for item in JsonStore(state_path).load()["transactions"]) == 1500
    )


def test_robinhood_overlapping_exports_reuse_row_identity():
    from spend_tracker.classifiers import UnconfiguredJevClassifier
    from spend_tracker.parsing import parse_csv
    from spend_tracker.services import import_transactions
    from spend_tracker.storage import empty_state

    header = "Date,Time,Cardholder,Amount,Balance,Status,Type,Merchant,Description\n"
    first_row = (
        "2026-09-01,1:00 PM,Example,20,100,Posted,Purchase,Example shop,Example shop location\n"
    )
    second_row = (
        "2026-09-02,2:00 PM,Example,30,200,Posted,Purchase,Another shop,Another shop location\n"
    )
    second_row_updated_balance = second_row.replace(",200,", ",999,")
    third_row = "2026-09-03,3:00 PM,Example,40,300,Posted,Purchase,Third shop,Third shop location\n"
    state = empty_state()

    for content, expected_imported, expected_skipped in (
        (header + first_row + second_row, 2, 0),
        (header + second_row_updated_balance + third_row, 1, 1),
    ):
        encoded = content.encode()
        report = import_transactions(
            state,
            parse_csv(encoded, import_preset="robinhood"),
            filename="robinhood.csv",
            content=encoded,
            source="robinhood",
            currency="USD",
            classifier=UnconfiguredJevClassifier(),
        )
        assert report["rows_imported"] == expected_imported
        assert report["duplicates_skipped"] == expected_skipped

    assert len(state["transactions"]) == 3
    assert state["transactions"][0]["raw_description"] == "Example shop location"
    assert state["transactions"][0]["normalized_merchant"] == "example shop"


def test_robinhood_keeps_two_identical_rows_in_one_export():
    from spend_tracker.parsing import parse_csv

    row = b"2026-09-01,1:00 PM,Example,20,Posted,Purchase,Example shop,Example shop location\n"
    parsed = parse_csv(
        b"Date,Time,Cardholder,Amount,Status,Type,Merchant,Description\n" + row + row,
        import_preset="robinhood",
    )
    assert len(parsed.rows) == 2
    assert (
        parsed.rows[0].transaction.source_transaction_id
        != parsed.rows[1].transaction.source_transaction_id
    )


def test_wells_fargo_overlapping_exports_flag_ambiguous_matches():
    from spend_tracker.classifiers import UnconfiguredJevClassifier
    from spend_tracker.parsing import parse_csv
    from spend_tracker.services import import_transactions
    from spend_tracker.storage import empty_state

    header = "DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS\n"
    first_row = "09/01/2026,Example shop,-20,,Posted\n"
    second_row = "09/02/2026,Another shop,-30,,Posted\n"
    third_row = "09/03/2026,Third shop,-40,,Posted\n"
    state = empty_state()
    for content in (header + first_row + second_row, header + second_row + third_row):
        encoded = content.encode()
        report = import_transactions(
            state,
            parse_csv(encoded, import_preset="wells_fargo"),
            filename="wells.csv",
            content=encoded,
            source="wells-fargo-active-cash",
            currency="USD",
            classifier=UnconfiguredJevClassifier(),
        )
    assert report["rows_imported"] == 2
    assert report["possible_duplicates"] == 1
    assert len(state["transactions"]) == 4
    assert sum(item["possible_duplicate"] for item in state["transactions"]) == 1
