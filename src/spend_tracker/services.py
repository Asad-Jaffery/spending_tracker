import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from spend_tracker.classifiers import MerchantClassifier, UnconfiguredJevClassifier
from spend_tracker.domain import Category, ClassificationSource
from spend_tracker.parsing import ParsedInput

logger = logging.getLogger(__name__)


def digest(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def import_transactions(
    state: dict,
    parsed: ParsedInput,
    *,
    filename: str,
    content: bytes,
    source: str,
    currency: str,
    classifier: MerchantClassifier,
) -> dict:
    source = source.strip().casefold()
    if not source or len(source) > 100:
        raise ValueError("Source must contain 1–100 characters")
    if len(currency) != 3 or not currency.isalpha() or currency != currency.upper():
        raise ValueError("Currency must be a three-letter uppercase code")
    run = {
        "id": str(uuid4()),
        "filename": filename,
        "source": source,
        "rows_processed": parsed.rows_processed,
        "rows_imported": 0,
        "duplicates_skipped": 0,
        "possible_duplicates": 0,
        "rows_failed": len(parsed.errors),
        "jev_attempts": 0,
        "jev_failures": 0,
        "errors": parsed.errors,
        "created_at": timestamp(),
    }
    file_hash = hashlib.sha256(content).hexdigest()
    known_keys = {item["dedupe_key"] for item in state["transactions"]}
    known_fingerprints = {item["fingerprint"] for item in state["transactions"]}
    attempted: set[str] = set()
    mappings = state["merchant_mappings"]
    for row in parsed.rows:
        item = row.transaction
        cents = int(item.amount * 100)
        key = (
            digest("id", source, item.source_transaction_id)
            if item.source_transaction_id
            else digest("file-row", source, file_hash, row.line)
        )
        if key in known_keys:
            run["duplicates_skipped"] += 1
            continue
        fingerprint = digest(
            source, item.date.isoformat(), cents, currency, item.raw_description.casefold().strip()
        )
        possible_duplicate = fingerprint in known_fingerprints
        merchant = row.merchant
        if merchant in mappings:
            category = Category(mappings[merchant]["category"])
            classification_source = ClassificationSource.MERCHANT_MAPPING
            needs_review = False
        else:
            category = Category.OTHER
            classification_source = ClassificationSource.UNCLASSIFIED
            needs_review = True
            if merchant not in attempted:
                attempted.add(merchant)
                if not isinstance(classifier, UnconfiguredJevClassifier):
                    run["jev_attempts"] += 1
                try:
                    prediction = classifier.classify(merchant, tuple(Category))
                    if prediction is not None:
                        predicted_category = Category(prediction.category)
                        confidence = prediction.confidence
                        if confidence is not None:
                            try:
                                confidence = float(confidence)
                            except (TypeError, ValueError):
                                confidence = None
                            if confidence is not None and (
                                not math.isfinite(confidence) or not 0 <= confidence <= 1
                            ):
                                confidence = None
                        category = predicted_category
                        classification_source = ClassificationSource.JEV
                        needs_review = False
                        now = timestamp()
                        mappings[merchant] = {
                            "normalized_merchant": merchant,
                            "display_name": merchant.title(),
                            "category": category.value,
                            "confidence": confidence,
                            "source": ClassificationSource.JEV.value,
                            "created_at": now,
                            "updated_at": now,
                        }
                    elif not isinstance(classifier, UnconfiguredJevClassifier):
                        run["jev_failures"] += 1
                except Exception as exc:
                    run["jev_failures"] += 1
                    logger.warning("Classifier failed (%s); using Other", type(exc).__name__)
        state["transactions"].append(
            {
                "id": str(uuid4()),
                "import_run_id": run["id"],
                "source": source,
                "source_transaction_id": item.source_transaction_id,
                "dedupe_key": key,
                "fingerprint": fingerprint,
                "date": item.date.isoformat(),
                "raw_description": item.raw_description,
                "normalized_merchant": merchant,
                "amount_cents": cents,
                "currency": currency,
                "category": category.value,
                "classification_source": classification_source.value,
                "needs_review": needs_review,
                "possible_duplicate": possible_duplicate,
                "created_at": timestamp(),
            }
        )
        known_keys.add(key)
        known_fingerprints.add(fingerprint)
        run["rows_imported"] += 1
        run["possible_duplicates"] += int(possible_duplicate)
    state["import_runs"].append(run)
    return run


def correct_category(
    state: dict, transaction_id: str, category: Category, scope: str = "transaction"
) -> int:
    transaction = next(
        (item for item in state["transactions"] if item["id"] == transaction_id), None
    )
    if transaction is None:
        raise ValueError("Transaction not found")
    if scope not in {"transaction", "merchant"}:
        raise ValueError("Scope must be transaction or merchant")
    if scope == "transaction":
        targets = [transaction]
    else:
        merchant = transaction["normalized_merchant"]
        targets = [
            item for item in state["transactions"] if item["normalized_merchant"] == merchant
        ]
        now = timestamp()
        previous = state["merchant_mappings"].get(merchant)
        state["merchant_mappings"][merchant] = {
            "normalized_merchant": merchant,
            "display_name": merchant.title(),
            "category": category.value,
            "confidence": None,
            "source": ClassificationSource.MANUAL.value,
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
        }
    for item in targets:
        item["category"] = category.value
        item["classification_source"] = ClassificationSource.MANUAL.value
        item["needs_review"] = False
    return len(targets)


def money(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def spending_summary(state: dict, *, currency: str = "USD") -> dict:
    categories: dict[str, int] = defaultdict(int)
    months: dict[str, int] = defaultdict(int)
    merchants: dict[str, int] = defaultdict(int)
    count = 0
    for item in state["transactions"]:
        if item["currency"] != currency or item["category"] in ("Income", "Transfers"):
            continue
        cents = item["amount_cents"]
        categories[item["category"]] += cents
        months[item["date"][:7]] += cents
        merchants[item["normalized_merchant"]] += cents
        count += 1
    return {
        "currency": currency,
        "net_spending": money(sum(categories.values())),
        "transaction_count": count,
        "by_category": {key: money(value) for key, value in sorted(categories.items())},
        "by_month": {key: money(value) for key, value in sorted(months.items())},
        "top_merchants": [
            {"merchant": key, "net_spending": money(value)}
            for key, value in sorted(merchants.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
        ],
    }
