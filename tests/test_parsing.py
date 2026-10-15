from decimal import Decimal

import pytest

from spend_tracker.normalization import normalize_merchant
from spend_tracker.parsing import CsvError, parse_amount, parse_csv


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("COSTCO WHSE #1029", "costco whse"),
        ("COSTCO 00129", "costco"),
        ("COSTCO WHOLESALE", "costco wholesale"),
        ("SPOTIFY USA 1234", "spotify usa"),
        ("SQ *BLUE BOTTLE COFFEE 0421", "blue bottle coffee"),
        ("7-ELEVEN #321", "7 eleven"),
        ("STUDIO 54", "studio 54"),
        ("CAFÉ DE PARIS", "café de paris"),
        ("COSTCO GAS", "costco gas"),
    ],
)
def test_normalization(description, expected):
    assert normalize_merchant(description) == expected


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1.234", "1,23", "1e3", "--3", "1$2", ""])
def test_reject_invalid_money(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_money_and_formats():
    assert parse_amount("($1,234.50)") == Decimal("-1234.50")
    parsed = parse_csv(
        b"\xef\xbb\xbfTransaction Date,Merchant,Transaction Amount\n09/21/2026,Shop,-12.34\n",
        max_rows=10,
        date_format="us",
        amount_convention="expenses_negative",
    )
    assert parsed.rows[0].transaction.amount == Decimal("12.34")
    assert parsed.rows[0].transaction.date.isoformat() == "2026-09-21"


@pytest.mark.parametrize(
    "content",
    [b"", b"wrong,headers\n1,2\n", b"date,date,amount\n1,2,3\n", b"\xff"],
)
def test_invalid_files(content):
    with pytest.raises(CsvError):
        parse_csv(content, max_rows=10)


def test_limit_rejects_entire_file():
    with pytest.raises(CsvError, match="row limit"):
        parse_csv(b"date,description,amount\n2026-09-01,A,1\n2026-09-02,B,2\n", max_rows=1)


def test_custom_column_mapping_uses_exact_headers_and_canonicalizes_rows():
    content = (
        b"Posted On,Transaction Details,Charge,Reference Number,Category\n"
        b"09/21/2026,SQ *BLUE BOTTLE COFFEE 0421,($12.50),ref-1,Groceries\n"
    )
    with pytest.raises(CsvError, match="Enter a custom column mapping"):
        parse_csv(content, date_format="us")
    parsed = parse_csv(
        content,
        date_format="us",
        column_mapping={
            "date": "Posted On",
            "description": "Transaction Details",
            "amount": "Charge",
            "id": "Reference Number",
        },
    )
    row = parsed.rows[0]
    assert row.transaction.date.isoformat() == "2026-09-21"
    assert row.transaction.amount == Decimal("-12.50")
    assert row.transaction.source_transaction_id == "ref-1"
    assert row.transaction.raw_description == "SQ *BLUE BOTTLE COFFEE 0421"
    assert row.merchant == "blue bottle coffee"


def test_custom_column_mapping_rejects_missing_or_ambiguous_fields():
    content = b"Posted On,Transaction Details,Charge\n2026-09-21,Coffee,12.50\n"
    with pytest.raises(CsvError, match="needs date, description, and amount"):
        parse_csv(content, column_mapping={"date": "Posted On"})
    with pytest.raises(CsvError, match="Mapped column\\(s\\) not found: Total"):
        parse_csv(
            content,
            column_mapping={
                "date": "Posted On",
                "description": "Transaction Details",
                "amount": "Total",
            },
        )
    with pytest.raises(CsvError, match="different CSV column"):
        parse_csv(
            content,
            column_mapping={"date": "Posted On", "description": "Posted On", "amount": "Charge"},
        )


def test_wrong_row_format_rejects_file_when_nothing_can_import():
    content = b"date,description,amount\n09/21/2026,Coffee,4.50\n"
    with pytest.raises(CsvError, match="Check the selected date/amount format"):
        parse_csv(content)
    assert parse_csv(content, date_format="us").rows_processed == 1


def test_valid_rows_import_while_bad_rows_are_reported():
    parsed = parse_csv(b"date,description,amount\n2026-09-21,Coffee,4.50\n2026-09-22,Shop,bad\n")
    assert len(parsed.rows) == 1
    assert parsed.rows_processed == 2
    assert parsed.errors == [
        {"line": 3, "message": "Amount must be a finite number with at most two decimal places"}
    ]


def test_wells_fargo_preset_flips_charges_and_refunds_and_skips_payments():
    parsed = parse_csv(
        b"DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS\n"
        b"46302,Example subscription,-10.25,,Posted\n"
        b"09/21/2026,Example refund,3.00,,Posted\n"
        b"2026-09-22, ONLINE PAYMENT THANK YOU ,100.00,,Posted\n"
        b"2026-09-23,Example fee,-0.02,,Posted\n",
        import_preset="wells_fargo",
    )
    assert parsed.rows_processed == 4
    assert parsed.payments_skipped == 1
    assert not parsed.errors
    assert [row.transaction.amount for row in parsed.rows] == [
        Decimal("10.25"),
        Decimal("-3.00"),
        Decimal("0.02"),
    ]
    assert parsed.rows[0].transaction.date.isoformat() == "2026-10-07"


def test_robinhood_preset_uses_description_and_preserves_refunds():
    parsed = parse_csv(
        b"Date,Time,Cardholder,Amount,Points,Balance,Status,Type,Merchant,Description\n"
        b"46293.708333333336,2:00 PM,Example,12.50,0,0,Posted,Purchase,Short name,Example cafe\n"
        b"46293.708333333336,12:00 PM,Example,-5.00,0,0,Posted,Refund,Refund label,Example cafe\n"
        b"46270.708333333336,1:00 PM,Example,-100,0,0,Posted, Payment ,Payment,THANK YOU\n",
        import_preset="robinhood",
    )
    assert parsed.rows_processed == 3
    assert parsed.payments_skipped == 1
    assert not parsed.errors
    assert [row.transaction.amount for row in parsed.rows] == [
        Decimal("12.50"),
        Decimal("-5.00"),
    ]
    assert parsed.rows[0].transaction.raw_description == "Example cafe"
    assert parsed.rows[0].merchant == "short name"
    assert parsed.rows[0].transaction.source_transaction_id is not None
    assert parsed.rows[0].transaction.date.isoformat() == "2026-09-28"


def test_card_preset_skips_non_posted_rows():
    parsed = parse_csv(
        b"DATE,DESCRIPTION,AMOUNT,CHECK #,STATUS\n"
        b"09/01/2026,Posted purchase,-20,,Posted\n"
        b"09/02/2026,Pending purchase,-30,,Pending\n",
        import_preset="wells_fargo",
    )
    assert parsed.rows_processed == 2
    assert len(parsed.rows) == 1
    assert parsed.non_posted_skipped == 1


@pytest.mark.parametrize("preset", ["wells_fargo", "robinhood"])
def test_card_presets_accept_calendar_dates_and_payment_only_files(preset):
    header = (
        b"Date,Time,Merchant,Description,Amount,Type\n"
        if preset == "robinhood"
        else b"Date,Description,Amount,Type\n"
    )
    purchase_rows = (
        b"9/1/2026,1:00 PM,Example,Example purchase,5.00,Purchase\n"
        b"2026-09-02,2:00 PM,Example,Example purchase,6.00,Purchase\n"
        if preset == "robinhood"
        else b"9/1/2026,Example purchase,5.00,Purchase\n2026-09-02,Example purchase,6.00,Purchase\n"
    )
    parsed = parse_csv(
        header + purchase_rows,
        import_preset=preset,
    )
    assert [row.transaction.date.isoformat() for row in parsed.rows] == [
        "2026-09-01",
        "2026-09-02",
    ]
    payments = parse_csv(
        header
        + (
            b"2026-09-01,1:00 PM,Payment,ONLINE PAYMENT THANK YOU,100,Payment\n"
            if preset == "robinhood"
            else b"2026-09-01,ONLINE PAYMENT THANK YOU,100,Payment\n"
        ),
        import_preset=preset,
    )
    assert payments.rows == []
    assert payments.errors == []
    assert payments.payments_skipped == 1


def test_card_presets_validate_headers_and_dates_and_override_custom_settings():
    with pytest.raises(CsvError, match="Unknown card"):
        parse_csv(b"", import_preset="unknown")
    with pytest.raises(CsvError, match="missing configured columns"):
        parse_csv(b"Date,Description,Amount\n2026-09-01,Example,10\n", import_preset="robinhood")
    parsed = parse_csv(
        b"Date,Description,Amount\n"
        b"2026-09-01,Example,-10\n"
        b"2958466,Example,-10\n"
        b"60,Example,-10\n"
        b"NaN,Example,-10\n",
        import_preset="wells_fargo",
        column_mapping={"date": "wrong"},
        amount_convention="expenses_positive",
    )
    assert parsed.rows[0].transaction.amount == Decimal("10")
    assert len(parsed.errors) == 3


def test_custom_import_does_not_silently_filter_payments():
    parsed = parse_csv(b"date,description,amount\n2026-09-01,ONLINE PAYMENT THANK YOU,100\n")
    assert len(parsed.rows) == 1
    assert parsed.payments_skipped == 0


def test_excel_date_epoch_and_fraction_do_not_shift_the_calendar_day():
    parsed = parse_csv(
        b"Date,Description,Amount\n25569.999999999,Example,-1\n",
        import_preset="wells_fargo",
    )
    assert parsed.rows[0].transaction.date.isoformat() == "1970-01-01"
