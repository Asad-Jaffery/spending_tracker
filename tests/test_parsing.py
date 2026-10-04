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
