import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from spend_tracker.domain import CanonicalTransaction
from spend_tracker.normalization import normalize_merchant

DateFormat = Literal["iso", "us"]
AmountConvention = Literal["expenses_positive", "expenses_negative"]
KNOWN_COLUMN_NAMES = {
    "date": ("date", "transaction date", "posting date", "posted date"),
    "description": ("raw description", "description", "merchant", "name", "payee"),
    "amount": ("amount", "transaction amount"),
    "id": ("transaction id", "source transaction id", "id"),
}
REQUIRED_COLUMNS = ("date", "description", "amount")
IMPORT_PRESETS = {
    "wells_fargo": {
        "label": "Wells Fargo Active Cash",
        "source": "wells-fargo-active-cash",
        "description": "Negative charges, positive refunds. Skips payments and non-posted rows.",
        "amount_convention": "expenses_negative",
        "column_mapping": {"date": "DATE", "description": "DESCRIPTION", "amount": "AMOUNT"},
    },
    "robinhood": {
        "label": "Robinhood",
        "source": "robinhood",
        "description": (
            "Positive purchases, negative refunds. Uses Merchant for classification; "
            "skips payments and non-posted rows."
        ),
        "amount_convention": "expenses_positive",
        "column_mapping": {"date": "Date", "description": "Description", "amount": "Amount"},
    },
}


class InputError(ValueError):
    pass


CsvError = InputError


def preset_settings(preset_id: str, overrides: dict | None = None) -> dict:
    """Return editable defaults, applying only explicitly supplied settings."""
    if preset_id not in (*IMPORT_PRESETS, "custom"):
        raise InputError("Unknown card import preset")
    preset = IMPORT_PRESETS.get(
        preset_id,
        {
            "amount_convention": "expenses_positive",
            "column_mapping": {"date": "Date", "description": "Description", "amount": "Amount"},
        },
    )
    robinhood = preset_id == "robinhood"
    settings = {
        "date_format": "auto",
        "amount_convention": preset["amount_convention"],
        "column_mapping": deepcopy(preset["column_mapping"]),
        "merchant_column": "Merchant" if robinhood else "",
        "time_column": "Time" if robinhood else "",
        "type_column": "Type" if robinhood else "",
        "cardholder_column": "Cardholder" if robinhood else "",
        "skip_payments": preset_id != "custom",
        "payment_column": "Type" if robinhood else "DESCRIPTION",
        "payment_value": "Payment" if robinhood else "ONLINE PAYMENT THANK YOU",
        "skip_non_posted": preset_id != "custom",
        "status_column": "Status" if robinhood else "STATUS",
        "posted_value": "Posted",
        "match_rows": robinhood,
        "ignore_rules": [],
    }
    if preset_id == "custom":
        settings.update(payment_column="Description", payment_value="", status_column="Status")
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - set(settings):
            raise InputError("Unknown import setting")
        settings.update(deepcopy(overrides))
    if settings["date_format"] not in ("auto", "iso", "us"):
        raise InputError("Date format must be auto, iso, or us")
    if settings["amount_convention"] not in ("expenses_positive", "expenses_negative"):
        raise InputError("Invalid amount convention")
    for name in ("skip_payments", "skip_non_posted", "match_rows"):
        if not isinstance(settings[name], bool):
            raise InputError(f"{name} must be true or false")
    for name in (
        "merchant_column",
        "time_column",
        "type_column",
        "cardholder_column",
        "payment_column",
        "payment_value",
        "status_column",
        "posted_value",
    ):
        if not isinstance(settings[name], str) or len(settings[name]) > 500:
            raise InputError(f"Invalid {name}")
        settings[name] = settings[name].strip()
    for enabled, fields in (
        ("skip_payments", ("payment_column", "payment_value")),
        ("skip_non_posted", ("status_column", "posted_value")),
        ("match_rows", ("time_column", "type_column")),
    ):
        if settings[enabled] and any(not settings[field] for field in fields):
            raise InputError(f"Fill in {', '.join(fields)} or disable {enabled}")
    mapping = settings["column_mapping"]
    if (
        not isinstance(mapping, dict)
        or set(mapping) - set(KNOWN_COLUMN_NAMES)
        or any(not isinstance(v, str) or len(v) > 500 for v in mapping.values())
    ):
        raise InputError("Column mapping must contain CSV header names")
    settings["column_mapping"] = {
        key: value.strip() for key, value in mapping.items() if value.strip()
    }
    if any(key not in settings["column_mapping"] for key in REQUIRED_COLUMNS):
        raise InputError("Column mapping needs date, description, and amount headers")
    headers = [
        re.sub(r"[_\s]+", " ", name.casefold()) for name in settings["column_mapping"].values()
    ]
    if len(headers) != len(set(headers)):
        raise InputError("Each mapped field must use a different CSV column")
    rules = settings["ignore_rules"]
    if not isinstance(rules, list) or len(rules) > 30:
        raise InputError("Use at most 30 ignore rules")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"column", "operator", "value"}:
            raise InputError("Each ignore rule needs a column, operator, and value")
        if rule["operator"] not in ("equals", "contains", "starts_with"):
            raise InputError("Ignore rules must use equals, contains, or starts_with")
        for field in ("column", "value"):
            if (
                not isinstance(rule[field], str)
                or not rule[field].strip()
                or len(rule[field]) > 500
            ):
                raise InputError("Ignore rule column and value must contain 1–500 characters")
            rule[field] = rule[field].strip()
    return settings


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
    payments_skipped: int = 0
    non_posted_skipped: int = 0
    rules_skipped: int = 0


def _card_date(raw: str) -> date:
    """Accept exported calendar dates and Excel serial dates without shifting time zones."""
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        serial = Decimal(raw)
        if not 1 <= serial < 2_958_466 or int(serial) == 60:
            raise ValueError("Invalid Excel date")
        days = int(serial)
        # Excel includes a nonexistent 1900-02-29 at serial 60.
        return date(1899, 12, 31) + timedelta(days=days if days < 60 else days - 1)
    for pattern, fmt in (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{1,2}/\d{1,2}/\d{4}", "%m/%d/%Y"),
    ):
        if re.fullmatch(pattern, raw):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError as exc:
                raise ValueError("Invalid calendar date") from exc
    raise ValueError("Card date must be YYYY-MM-DD, MM/DD/YYYY, or an Excel date number")


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


def _parse_row(
    values: dict,
    line: int,
    date_format: DateFormat,
    convention: AmountConvention,
    *,
    card_dates: bool = False,
):
    if not isinstance(values["description"], str):
        raise ValueError("Description must be text")
    description = values["description"].strip()
    if not description or len(description) > 500:
        raise ValueError("Description must contain 1–500 characters")
    merchant = normalize_merchant(description)
    raw_date = str(values["date"]).strip()
    if card_dates:
        day = _card_date(raw_date)
    else:
        pattern = r"\d{4}-\d{2}-\d{2}" if date_format == "iso" else r"\d{2}/\d{2}/\d{4}"
        if not re.fullmatch(pattern, raw_date):
            raise ValueError("Date does not match the selected date format (iso or us)")
        try:
            day = datetime.strptime(
                raw_date, "%Y-%m-%d" if date_format == "iso" else "%m/%d/%Y"
            ).date()
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
    column_mapping: dict[str, str] | None = None,
    import_preset: str | None = None,
    import_settings: dict | None = None,
) -> ParsedInput:
    settings = None
    if import_preset is not None:
        settings = preset_settings(import_preset, import_settings)
        column_mapping = settings["column_mapping"]
        amount_convention = settings["amount_convention"]
    elif import_settings is not None:
        raise InputError("Choose a card preset before supplying preset settings")
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
        normalized = [re.sub(r"[_\s]+", " ", h.strip().casefold()) for h in headers]
        if len(normalized) != len(set(normalized)):
            raise InputError("CSV contains duplicate column names")
        lookup = dict(zip(normalized, headers, strict=True))
        extra_columns = {}
        ignore_rules = []
        if settings is not None:
            for rule in settings["ignore_rules"]:
                key = re.sub(r"[_\s]+", " ", rule["column"].casefold())
                if key not in lookup:
                    raise InputError(f"Ignore rule column not found: {rule['column']}")
                ignore_rules.append((lookup[key], rule["operator"], rule["value"].casefold()))
            for field in (
                "merchant_column",
                "time_column",
                "type_column",
                "cardholder_column",
                "payment_column",
                "status_column",
            ):
                name = settings[field]
                key = re.sub(r"[_\s]+", " ", name.casefold())
                extra_columns[field] = lookup.get(key)
            required = ["merchant_column"] if settings["merchant_column"] else []
            if settings["skip_payments"]:
                required.append("payment_column")
            if settings["match_rows"]:
                required.extend(("time_column", "type_column"))
            missing = [settings[field] for field in required if not extra_columns[field]]
            if missing:
                raise InputError(
                    "Preset CSV is missing configured columns: " + ", ".join(dict.fromkeys(missing))
                )
        if column_mapping is not None:
            if not isinstance(column_mapping, dict) or set(column_mapping) - set(
                KNOWN_COLUMN_NAMES
            ):
                raise InputError(
                    "Column mapping must name date, description, amount, and optional id"
                )
            if any(
                field not in column_mapping
                or not isinstance(column_mapping[field], str)
                or not column_mapping[field].strip()
                for field in REQUIRED_COLUMNS
            ) or any(
                not isinstance(value, str) or not value.strip() for value in column_mapping.values()
            ):
                raise InputError(
                    "Custom column mapping needs date, description, and amount headers"
                )
            selected = {
                field: re.sub(r"[_\s]+", " ", name.strip().casefold())
                for field, name in column_mapping.items()
            }
            if len(set(selected.values())) != len(selected):
                raise InputError("Each mapped field must use a different CSV column")
            missing = [
                column_mapping[field] for field, name in selected.items() if name not in lookup
            ]
            if missing:
                raise InputError(
                    f"Mapped column(s) not found: {', '.join(missing)}. "
                    f"Available headers: {', '.join(headers)}. "
                    "Edit the column mapping and try again."
                )
            columns = {field: lookup[name] for field, name in selected.items()}
        else:
            columns = {
                field: next((lookup[alias] for alias in aliases if alias in lookup), None)
                for field, aliases in KNOWN_COLUMN_NAMES.items()
            }
            missing = [field for field in REQUIRED_COLUMNS if columns[field] is None]
            if missing:
                raise InputError(
                    f"Could not match required CSV columns: {', '.join(missing)}. "
                    f"Available headers: {', '.join(headers)}. "
                    "Enter a custom column mapping and try again."
                )
        rows, errors, processed, payments_skipped, non_posted_skipped = [], [], 0, 0, 0
        rules_skipped = 0
        robinhood_occurrences: dict[str, int] = {}
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
                is_payment = (
                    settings is not None
                    and settings["skip_payments"]
                    and (
                        row[extra_columns["payment_column"]].strip().casefold()
                        == settings["payment_value"].casefold()
                    )
                )
                if is_payment:
                    payments_skipped += 1
                    continue
                if (
                    settings is not None
                    and settings["skip_non_posted"]
                    and extra_columns["status_column"]
                    and row[extra_columns["status_column"]].strip().casefold()
                    != settings["posted_value"].casefold()
                ):
                    non_posted_skipped += 1
                    continue
                if any(
                    (
                        row[column].strip().casefold() == value
                        if operator == "equals"
                        else value in row[column].strip().casefold()
                        if operator == "contains"
                        else row[column].strip().casefold().startswith(value)
                    )
                    for column, operator, value in ignore_rules
                ):
                    rules_skipped += 1
                    continue
                parsed_row = _parse_row(
                    values,
                    line,
                    settings["date_format"] if settings is not None else date_format,
                    amount_convention,
                    card_dates=settings is not None and settings["date_format"] == "auto",
                )
                merchant_name = ""
                if settings is not None and extra_columns["merchant_column"]:
                    merchant_name = row[extra_columns["merchant_column"]].strip()
                    if merchant_name:
                        parsed_row = replace(parsed_row, merchant=normalize_merchant(merchant_name))
                if (
                    settings is not None
                    and settings["match_rows"]
                    and not parsed_row.transaction.source_transaction_id
                ):
                    # Robinhood provides no transaction ID. Match stable purchase
                    # fields across exports; points, balance, and status can change.
                    # Count identical rows within a file to preserve repeated purchases.
                    identity_fields = {
                        "date": parsed_row.transaction.date.isoformat(),
                        "time": row[extra_columns["time_column"]].strip().casefold(),
                        "cardholder": (
                            row[extra_columns["cardholder_column"]].strip().casefold()
                            if extra_columns["cardholder_column"]
                            else ""
                        ),
                        "amount": str(parsed_row.transaction.amount),
                        "type": row[extra_columns["type_column"]].strip().casefold(),
                        "merchant": merchant_name.casefold(),
                        "description": parsed_row.transaction.raw_description.casefold(),
                    }
                    signature = hashlib.sha256(
                        json.dumps(identity_fields, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
                    occurrence = robinhood_occurrences.get(signature, 0) + 1
                    robinhood_occurrences[signature] = occurrence
                    parsed_row = replace(
                        parsed_row,
                        transaction=replace(
                            parsed_row.transaction,
                            source_transaction_id=f"{import_preset}-row-v1:{signature}:{occurrence}",
                        ),
                    )
                rows.append(parsed_row)
            except ValueError as exc:
                errors.append({"line": line, "message": str(exc)})
        if not processed:
            raise InputError("CSV contains no transaction rows")
        if not rows and errors:
            raise InputError(
                f"No valid transaction rows. Row {errors[0]['line']}: "
                f"{errors[0]['message']}. Check the selected date/amount format "
                "or custom column mapping and try again."
            )
        return ParsedInput(
            rows, errors, processed, payments_skipped, non_posted_skipped, rules_skipped
        )
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
