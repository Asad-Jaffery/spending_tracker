from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from spend_tracker.domain import Category, ClassificationSource


class OrmSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TransactionOut(OrmSchema):
    id: str
    import_run_id: str
    source: str
    source_transaction_id: str | None
    date: date
    raw_description: str
    normalized_merchant: str
    amount: Decimal
    currency: str
    category: Category
    classification_source: ClassificationSource
    needs_review: bool
    possible_duplicate: bool
    created_at: datetime


class ImportOut(OrmSchema):
    id: str
    filename: str
    source: str
    rows_processed: int
    rows_imported: int
    duplicates_skipped: int
    possible_duplicates: int
    rows_failed: int
    jev_attempts: int
    errors: list[dict]
    created_at: datetime


class MerchantOut(OrmSchema):
    id: str
    normalized_merchant: str
    display_name: str
    category: Category
    confidence: float | None
    source: ClassificationSource
    created_at: datetime
    updated_at: datetime


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Category
    scope: Literal["transaction", "merchant"] = "transaction"
