from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    GROCERIES = "Groceries"
    FOOD_COFFEE = "Food + coffee"
    TRANSPORTATION = "Transportation"
    SHOPPING = "Shopping"
    ENTERTAINMENT = "Entertainment"
    TRAVEL = "Travel"
    HEALTH_PERSONAL_WELLNESS = "Health + personal wellness"
    OTHER = "Other"


class ClassificationSource(StrEnum):
    MERCHANT_MAPPING = "merchant_mapping"
    JEV = "jev"
    MANUAL = "manual"
    UNCLASSIFIED = "unclassified"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class Prediction:
    category: Category
    confidence: float | None = None


@dataclass(frozen=True)
class CanonicalTransaction:
    date: date
    raw_description: str
    amount: Decimal
    source_transaction_id: str | None = None
