from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Category(StrEnum):
    GROCERIES = "Groceries"
    DINING = "Dining"
    TRANSPORTATION = "Transportation"
    HOUSING = "Housing"
    UTILITIES = "Utilities"
    SHOPPING = "Shopping"
    ENTERTAINMENT = "Entertainment"
    TRAVEL = "Travel"
    HEALTH = "Health"
    INCOME = "Income"
    TRANSFERS = "Transfers"
    OTHER = "Other"


class ClassificationSource(StrEnum):
    MERCHANT_MAPPING = "merchant_mapping"
    JEV = "jev"
    MANUAL = "manual"
    CSV = "csv"
    UNCLASSIFIED = "unclassified"


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class CanonicalTransaction(BaseModel):
    date: date
    raw_description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(max_digits=12, decimal_places=2, allow_inf_nan=False)
    source_transaction_id: str | None = Field(default=None, max_length=200)
    csv_category: Category | None = None
