"""Clearly marked offline demo data; these are not Jev predictions."""

import json
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
