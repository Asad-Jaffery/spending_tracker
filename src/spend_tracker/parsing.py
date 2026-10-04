import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from spend_tracker.domain import CanonicalTransaction
from spend_tracker.normalization import normalize_merchant

DateFormat = Literal["iso", "us"]
AmountConvention = Literal["expenses_positive", "expenses_negative"]
ALIASES = {
    "date": ("date", "transaction date", "posting date", "posted date"),
    "description": ("raw description", "description", "merchant", "name", "payee"),
    "amount": ("amount", "transaction amount"),
    "id": ("transaction id", "source transaction id", "id"),
}


class InputError(ValueError):
    pass


CsvError = InputError


@dataclass(frozen=True)
class ParsedRow:
    line: int
    transaction: CanonicalTransaction
    merchant: str


@dataclass(frozen=True)
class ParsedInput:
    rows: list[ParsedRow]
    errors: list[dict]
    rows_processed: int


def parse_amount(raw: str) -> Decimal:
    value = raw.strip()
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    if not re.fullmatch(r"[+-]?\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", value):
        raise ValueError("Amount must be a finite number with at most two decimal places")
    amount = Decimal(value.replace(",", "").replace("$", ""))
    if abs(amount) > Decimal("9999999999.99"):
        raise ValueError("Amount is too large")
    return amount


def _parse_row(values: dict, line: int, date_format: DateFormat, convention: AmountConvention):
    if not isinstance(values["description"], str):
        raise ValueError("Description must be text")
    description = values["description"].strip()
    if not description or len(description) > 500:
        raise ValueError("Description must contain 1–500 characters")
    merchant = normalize_merchant(description)
    raw_date = str(values["date"]).strip()
    pattern = r"\d{4}-\d{2}-\d{2}" if date_format == "iso" else r"\d{2}/\d{2}/\d{4}"
    if not re.fullmatch(pattern, raw_date):
        raise ValueError("Date does not match the selected date format (iso or us)")
    try:
        day = datetime.strptime(raw_date, "%Y-%m-%d" if date_format == "iso" else "%m/%d/%Y").date()
    except ValueError as exc:
        raise ValueError("Invalid calendar date") from exc
    amount = parse_amount(str(values["amount"]))
    if convention == "expenses_negative":
        amount = -amount
    source_id = str(values.get("id") or "").strip() or None
    if source_id is not None and len(source_id) > 200:
        raise ValueError("Transaction ID is too long")
    return ParsedRow(line, CanonicalTransaction(day, description, amount, source_id), merchant)


def parse_csv(
    content: bytes,
    *,
    max_rows: int = 10_000,
    date_format: DateFormat = "iso",
    amount_convention: AmountConvention = "expenses_positive",
) -> ParsedInput:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError("CSV must use UTF-8 encoding") from exc
    if "\x00" in text:
        raise InputError("CSV must not contain null characters")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        headers = reader.fieldnames
        if not headers:
            raise InputError("CSV is empty or missing headers")
        normalized = [re.sub(r"[_\s]+", " ", h.strip().lower()) for h in headers]
        if len(normalized) != len(set(normalized)):
            raise InputError("CSV contains duplicate column names")
        lookup = dict(zip(normalized, headers, strict=True))
        columns = {
            field: next((lookup[alias] for alias in aliases if alias in lookup), None)
            for field, aliases in ALIASES.items()
        }
        if any(columns[field] is None for field in ("date", "description", "amount")):
            raise InputError("Required columns: date, description, amount")
        rows, errors, processed = [], [], 0
        for row in reader:
            processed += 1
            if processed > max_rows:
                raise InputError(f"CSV exceeds the {max_rows} row limit")
            line = reader.line_num
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Row has a different number of columns than the header")
                values = {
                    field: row[column] if column else None for field, column in columns.items()
                }
                rows.append(_parse_row(values, line, date_format, amount_convention))
            except ValueError as exc:
                errors.append({"line": line, "message": str(exc)})
        if not processed:
            raise InputError("CSV contains no transaction rows")
        return ParsedInput(rows, errors, processed)
    except csv.Error as exc:
        raise InputError("Malformed CSV quoting or field size") from exc


def parse_json(content: bytes, *, max_rows: int = 10_000) -> ParsedInput:
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError("JSON must be valid UTF-8 JSON") from exc
    if not isinstance(data, list) or not data:
        raise InputError("JSON input must be a nonempty array of transactions")
    if len(data) > max_rows:
        raise InputError(f"JSON exceeds the {max_rows} row limit")
    rows, errors = [], []
    for number, item in enumerate(data, 1):
        try:
            if not isinstance(item, dict):
                raise ValueError("Transaction must be an object")
            values = {
                "date": item["date"],
                "description": item["raw_description"],
                "amount": item["amount"],
                "id": item.get("source_transaction_id"),
            }
            rows.append(_parse_row(values, number, "iso", "expenses_positive"))
        except KeyError as exc:
            errors.append({"line": number, "message": f"Missing field: {exc.args[0]}"})
        except ValueError as exc:
            errors.append({"line": number, "message": str(exc)})
    return ParsedInput(rows, errors, len(data))
