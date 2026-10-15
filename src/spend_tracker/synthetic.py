"""Clearly marked offline demo data; these are not Jev predictions."""

import calendar
import json
from decimal import ROUND_HALF_UP, Decimal
from importlib.resources import files

from spend_tracker.domain import Category, ClassificationSource, Prediction
from spend_tracker.normalization import normalize_merchant
from spend_tracker.parsing import parse_json
from spend_tracker.services import import_transactions


class SyntheticClassifier:
    source = ClassificationSource.SYNTHETIC
    counts_as_jev = False

    def __init__(self, categories: dict[str, Category | None]):
        self.categories = categories

    def classify(self, merchant: str, categories: tuple[Category, ...]) -> Prediction | None:
        category = self.categories.get(merchant)
        return Prediction(category) if category in categories else None


def _seed_month(state: dict, filename: str, source: str) -> dict:
    content = files("spend_tracker").joinpath(f"demo/{filename}").read_bytes()
    return _import_demo(state, content, filename, source)


def _import_demo(state: dict, content: bytes, filename: str, source: str) -> dict:
    rows = json.loads(content)
    categories = {
        normalize_merchant(row["raw_description"]): (
            Category(row["synthetic_category"]) if row["synthetic_category"] else None
        )
        for row in rows
    }
    return import_transactions(
        state,
        parse_json(content),
        filename=filename,
        content=content,
        source=source,
        currency="USD",
        classifier=SyntheticClassifier(categories),
    )


def seed_june(state: dict) -> dict:
    return _seed_month(state, "june-2026.json", "synthetic-june")


def seed_may(state: dict) -> dict:
    return _seed_month(state, "may-2026.json", "synthetic-may")


def seed_presentation_history(state: dict) -> list[dict]:
    """Six offline demo months; June's presentation dashboard stays unchanged."""
    reports = [seed_june(state), seed_may(state)]
    template = json.loads(files("spend_tracker").joinpath("demo/june-2026.json").read_bytes())
    # Deliberately varied examples: rising groceries, a spring trip, and a
    # shopping spike make the category lines useful to explore in a demo.
    profiles = {
        "Groceries": ("0.55", "0.58", "0.63", "0.68"),
        "Food + coffee": ("1.35", "1.22", "1.10", "0.95"),
        "Transportation": ("0.85", "0.90", "1.10", "0.95"),
        "Shopping": ("0.85", "1.25", "2.85", "1.15"),
        "Entertainment": ("0.75", "1.60", "1.05", "1.30"),
        "Travel": ("0.20", "0.50", "3.90", "1.80"),
        "Health + personal wellness": ("0.85", "1.05", "1.20", "1.55"),
        "Other": ("0.95", "0.97", "1.02", "1.00"),
    }
    for month in range(1, 5):
        rows = []
        for index, original in enumerate(template):
            day = min(int(original["date"][-2:]), calendar.monthrange(2026, month)[1])
            category = original["synthetic_category"] or "Other"
            factor = Decimal(profiles[category][month - 1])
            amount = (Decimal(original["amount"]) * factor).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            rows.append(
                {
                    **original,
                    "date": f"2026-{month:02d}-{day:02d}",
                    "amount": str(amount),
                    "source_transaction_id": f"history-{month:02d}-{index:03d}",
                }
            )
        reports.append(
            _import_demo(
                state,
                json.dumps(rows).encode(),
                f"synthetic-history-2026-{month:02d}.json",
                f"synthetic-history-2026-{month:02d}",
            )
        )
    return reports
