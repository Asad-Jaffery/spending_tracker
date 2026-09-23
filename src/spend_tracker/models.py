from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from spend_tracker.domain import Category, ClassificationSource


def now():
    return datetime.now(UTC)


def identifier():
    return str(uuid4())


def enum_type(enum, name):
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda values: [value.value for value in values],
    )


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Keep API timestamps consistent when SQLite discards timezone information."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    filename: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(100), index=True)
    file_sha256: Mapped[str] = mapped_column(String(64))
    rows_processed: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)
    possible_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    jev_attempts: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)


class MerchantMapping(Base):
    __tablename__ = "merchant_mappings"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="mapping_confidence_range"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    normalized_merchant: Mapped[str] = mapped_column(String(500), unique=True)
    display_name: Mapped[str] = mapped_column(String(500))
    category: Mapped[Category] = mapped_column(enum_type(Category, "mapping_category"))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[ClassificationSource] = mapped_column(
        enum_type(ClassificationSource, "mapping_source")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now, onupdate=now)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "amount_cents >= -999999999999 AND amount_cents <= 999999999999",
            name="transaction_amount_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    import_run_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(100))
    source_transaction_id: Mapped[str | None] = mapped_column(String(200))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    raw_description: Mapped[str] = mapped_column(String(500))
    normalized_merchant: Mapped[str] = mapped_column(String(500), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    category: Mapped[Category] = mapped_column(
        enum_type(Category, "transaction_category"), index=True
    )
    classification_source: Mapped[ClassificationSource] = mapped_column(
        enum_type(ClassificationSource, "transaction_classification_source")
    )
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    possible_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)

    @property
    def amount(self) -> Decimal:
        return (Decimal(self.amount_cents) / 100).quantize(Decimal("0.01"))
