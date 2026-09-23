import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import ValidationError

from spend_tracker.domain import CanonicalTransaction, Category
from spend_tracker.normalization import normalize_merchant

DateFormat = Literal["iso", "us"]
AmountConvention = Literal["expenses_positive", "expenses_negative"]

# Add institution-specific parsers here, returning the same canonical model.
ALIASES = {
    "date": ("date", "transaction date", "posting date", "posted date"),
    "description": ("raw description", "description", "merchant", "name", "payee"),
    "amount": ("amount", "transaction amount"),
    "category": ("category", "transaction category"),
    "id": ("transaction id", "source transaction id", "id"),
}


class CsvError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedRow:
    line: int
    transaction: CanonicalTransaction
    merchant: str


@dataclass(frozen=True)
class ParsedCsv:
    rows: list[ParsedRow]
    errors: list[dict]
    rows_processed: int


def parse_amount(raw: str) -> Decimal:
    value = raw.strip()
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    if not re.fullmatch(r"[+-]?\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", value):
        raise ValueError("Amount must be a finite number with at most two decimal places")
    return Decimal(value.replace(",", "").replace("$", ""))


def parse_csv(
    content: bytes,
    *,
    max_rows: int,
    date_format: DateFormat = "iso",
    amount_convention: AmountConvention = "expenses_positive",
) -> ParsedCsv:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("CSV must use UTF-8 encoding") from exc
    if "\x00" in text:
        raise CsvError("CSV must not contain null characters")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = reader.fieldnames
        if not headers:
            raise CsvError("CSV is empty or missing headers")
        normalized = [re.sub(r"[_\s]+", " ", h.strip().lower()) for h in headers]
        if len(normalized) != len(set(normalized)):
            raise CsvError("CSV contains duplicate column names")
        lookup = dict(zip(normalized, headers, strict=True))
        columns = {
            field: next((lookup[alias] for alias in aliases if alias in lookup), None)
            for field, aliases in ALIASES.items()
        }
        if any(columns[field] is None for field in ("date", "description", "amount")):
            raise CsvError("Required columns: date, description, amount (see README for aliases)")
        rows, errors = [], []
        processed = 0
        for row in reader:
            processed += 1
            if processed > max_rows:
                raise CsvError(f"CSV exceeds the {max_rows} row limit")
            line = reader.line_num
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Row has a different number of columns than the header")
                description = row[columns["description"]].strip()
                merchant = normalize_merchant(description)
                amount = parse_amount(row[columns["amount"]])
                if amount_convention == "expenses_negative":
                    amount = -amount
                raw_category = row.get(columns["category"], "").strip().casefold()
                category = next((c for c in Category if c.value.casefold() == raw_category), None)
                raw_date = row[columns["date"]].strip()
                pattern = r"\d{4}-\d{2}-\d{2}" if date_format == "iso" else r"\d{2}/\d{2}/\d{4}"
                if not re.fullmatch(pattern, raw_date):
                    raise ValueError("Date does not match the selected date format (iso or us)")
                try:
                    parsed_date = datetime.strptime(
                        raw_date, "%Y-%m-%d" if date_format == "iso" else "%m/%d/%Y"
                    ).date()
                except ValueError as exc:
                    raise ValueError("Invalid calendar date") from exc
                transaction = CanonicalTransaction(
                    date=parsed_date,
                    raw_description=description,
                    amount=amount,
                    source_transaction_id=row.get(columns["id"], "").strip() or None,
                    csv_category=category,
                )
                rows.append(ParsedRow(line, transaction, merchant))
            except ValidationError:
                errors.append({"line": line, "message": "Invalid field length or amount magnitude"})
            except ValueError as exc:
                errors.append({"line": line, "message": str(exc)})
        if processed == 0:
            raise CsvError("CSV contains no transaction rows")
        return ParsedCsv(rows, errors, processed)
    except csv.Error as exc:
        raise CsvError("Malformed CSV quoting or field size") from exc
