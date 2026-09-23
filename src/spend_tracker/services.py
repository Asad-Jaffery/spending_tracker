import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from spend_tracker.classifiers import MerchantClassifier, UnconfiguredJevClassifier
from spend_tracker.domain import Category, ClassificationSource, Prediction
from spend_tracker.models import ImportRun, MerchantMapping, Transaction
from spend_tracker.parsing import ParsedCsv

logger = logging.getLogger(__name__)


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def lock_writes(session: Session):
    # All API writes use this transaction-scoped lock on Postgres. SQLite writes
    # are serialized by the app's lock; local SQLite mode requires one worker.
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(763924510)"))


@dataclass(frozen=True)
class Decision:
    category: Category
    source: ClassificationSource
    needs_review: bool = False


class ClassificationEngine:
    def __init__(self, session: Session, classifier: MerchantClassifier, min_confidence: float):
        self.session = session
        self.classifier = classifier
        self.min_confidence = min_confidence
        self.attempted: set[str] = set()
        self.attempt_count = 0

    def classify(self, merchant: str, csv_category: Category | None) -> Decision:
        mapping = self.session.scalar(
            select(MerchantMapping).where(MerchantMapping.normalized_merchant == merchant)
        )
        if mapping:
            return Decision(mapping.category, ClassificationSource.MERCHANT_MAPPING)
        if merchant not in self.attempted:
            self.attempted.add(merchant)
            if not isinstance(self.classifier, UnconfiguredJevClassifier):
                self.attempt_count += 1
            try:
                raw_prediction = self.classifier.classify(merchant, tuple(Category))
                prediction = (
                    Prediction.model_validate(raw_prediction)
                    if raw_prediction is not None
                    else None
                )
            except Exception as exc:
                # Provider failures must not abort an import or leak descriptions/secrets.
                logger.warning("Classifier failed (%s); using fallback", type(exc).__name__)
                prediction = None
            if prediction and prediction.confidence >= self.min_confidence:
                self.session.add(
                    MerchantMapping(
                        normalized_merchant=merchant,
                        display_name=merchant.title(),
                        category=prediction.category,
                        confidence=prediction.confidence,
                        source=ClassificationSource.JEV,
                    )
                )
                self.session.flush()
                return Decision(prediction.category, ClassificationSource.JEV)
        if csv_category:
            return Decision(csv_category, ClassificationSource.CSV)
        return Decision(Category.OTHER, ClassificationSource.UNCLASSIFIED, needs_review=True)


def import_transactions(
    session: Session,
    *,
    parsed: ParsedCsv,
    filename: str,
    content: bytes,
    source: str,
    currency: str,
    classifier: MerchantClassifier,
    min_confidence: float,
) -> ImportRun:
    lock_writes(session)
    file_hash = hashlib.sha256(content).hexdigest()
    run = ImportRun(
        filename=filename,
        source=source,
        file_sha256=file_hash,
        rows_processed=parsed.rows_processed,
        rows_imported=0,
        duplicates_skipped=0,
        possible_duplicates=0,
        rows_failed=len(parsed.errors),
        errors=parsed.errors,
    )
    session.add(run)
    session.flush()
    engine = ClassificationEngine(session, classifier, min_confidence)
    for row in parsed.rows:
        item = row.transaction
        cents = int(item.amount * 100)
        # Include account/source, but not filename: renaming an identical file is safe.
        key = (
            digest("id", source, item.source_transaction_id)
            if item.source_transaction_id
            else digest("file-row", source, file_hash, row.line)
        )
        if session.scalar(select(Transaction.id).where(Transaction.dedupe_key == key)):
            run.duplicates_skipped += 1
            continue
        fingerprint = digest(
            source, item.date.isoformat(), cents, currency, item.raw_description.casefold().strip()
        )
        # A matching fingerprint alone is not proof of a duplicate purchase.
        possible_duplicate = bool(
            session.scalar(select(Transaction.id).where(Transaction.fingerprint == fingerprint))
        )
        decision = engine.classify(row.merchant, item.csv_category)
        session.add(
            Transaction(
                import_run_id=run.id,
                source=source,
                source_transaction_id=item.source_transaction_id,
                dedupe_key=key,
                fingerprint=fingerprint,
                date=item.date,
                raw_description=item.raw_description,
                normalized_merchant=row.merchant,
                amount_cents=cents,
                currency=currency,
                category=decision.category,
                classification_source=decision.source,
                needs_review=decision.needs_review,
                possible_duplicate=possible_duplicate,
            )
        )
        session.flush()
        run.rows_imported += 1
        run.possible_duplicates += int(possible_duplicate)
    run.jev_attempts = engine.attempt_count
    session.flush()
    return run


def correct_category(session: Session, transaction_id: str, category: Category, scope: str) -> int:
    lock_writes(session)
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise LookupError("Transaction not found")
    if scope == "transaction":
        transaction.category = category
        transaction.classification_source = ClassificationSource.MANUAL
        transaction.needs_review = False
        return 1
    merchant = transaction.normalized_merchant
    mapping = session.scalar(
        select(MerchantMapping).where(MerchantMapping.normalized_merchant == merchant)
    )
    if mapping is None:
        mapping = MerchantMapping(normalized_merchant=merchant, display_name=merchant.title())
        session.add(mapping)
    mapping.category = category
    mapping.source = ClassificationSource.MANUAL
    mapping.confidence = None
    result = session.execute(
        update(Transaction)
        .where(Transaction.normalized_merchant == merchant)
        .values(
            category=category, classification_source=ClassificationSource.MANUAL, needs_review=False
        )
    )
    return result.rowcount


def money(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def spending_summary(
    session: Session, currency: str, date_from: date | None, date_to: date | None
) -> dict:
    conditions = [
        Transaction.currency == currency,
        Transaction.category.notin_([Category.INCOME, Category.TRANSFERS]),
    ]
    if date_from:
        conditions.append(Transaction.date >= date_from)
    if date_to:
        conditions.append(Transaction.date <= date_to)
    # Integer aggregation is exact on SQLite and PostgreSQL. Refunds reduce spending.
    groups = session.execute(
        select(
            Transaction.date,
            Transaction.category,
            Transaction.normalized_merchant,
            func.sum(Transaction.amount_cents),
            func.count(),
        )
        .where(*conditions)
        .group_by(Transaction.date, Transaction.category, Transaction.normalized_merchant)
    ).all()
    categories, months, merchants = {}, {}, {}
    total, count = 0, 0
    for day, category, merchant, cents, number in groups:
        cents = int(cents)
        total += cents
        count += number
        categories[category.value] = categories.get(category.value, 0) + cents
        month = day.strftime("%Y-%m")
        months[month] = months.get(month, 0) + cents
        merchants[merchant] = merchants.get(merchant, 0) + cents
    return {
        "currency": currency,
        "net_spending": money(total),
        "transaction_count": count,
        "by_category": {key: money(value) for key, value in sorted(categories.items())},
        "by_month": {key: money(value) for key, value in sorted(months.items())},
        "top_merchants": [
            {"merchant": key, "net_spending": money(value)}
            for key, value in sorted(merchants.items(), key=lambda item: (-item[1], item[0]))[:10]
        ],
    }
