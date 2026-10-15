import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from spend_tracker.classifiers import JevError, MerchantClassifier, UnconfiguredJevClassifier
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
        "payments_skipped": parsed.payments_skipped,
        "non_posted_skipped": parsed.non_posted_skipped,
        "rules_skipped": parsed.rules_skipped,
        "jev_attempts": 0,
        "jev_failures": 0,
        "jev_skipped_no_key": 0,
        "jev_errors": [],
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
                if isinstance(classifier, UnconfiguredJevClassifier):
                    run["jev_skipped_no_key"] += 1
                if not isinstance(classifier, UnconfiguredJevClassifier) and getattr(
                    classifier, "counts_as_jev", True
                ):
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
                        classification_source = getattr(
                            classifier, "source", ClassificationSource.JEV
                        )
                        needs_review = False
                        now = timestamp()
                        mappings[merchant] = {
                            "normalized_merchant": merchant,
                            "display_name": merchant.title(),
                            "category": category.value,
                            "confidence": confidence,
                            "source": classification_source.value,
                            "created_at": now,
                            "updated_at": now,
                        }
                    elif not isinstance(classifier, UnconfiguredJevClassifier) and getattr(
                        classifier, "counts_as_jev", True
                    ):
                        run["jev_failures"] += 1
                except Exception as exc:
                    if getattr(classifier, "counts_as_jev", True):
                        run["jev_failures"] += 1
                    if isinstance(exc, JevError) and str(exc) not in run["jev_errors"]:
                        run["jev_errors"].append(str(exc))
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
                "excluded_from_spending": False,
                "created_at": timestamp(),
            }
        )
        known_keys.add(key)
        known_fingerprints.add(fingerprint)
        run["rows_imported"] += 1
        run["possible_duplicates"] += int(possible_duplicate)
    state["import_runs"].append(run)
    return run


def retry_unclassified(state: dict, classifier: MerchantClassifier) -> dict:
    """Classify existing non-demo review rows after Jev becomes available."""
    if isinstance(classifier, UnconfiguredJevClassifier):
        raise ValueError("Set JEVMODEL_API_KEY in .env or your terminal before retrying")
    pending: dict[str, list[dict]] = defaultdict(list)
    for item in state["transactions"]:
        if (
            item["needs_review"]
            and item["classification_source"] == ClassificationSource.UNCLASSIFIED.value
            and not item["source"].startswith("synthetic-")
        ):
            pending[item["normalized_merchant"]].append(item)
    report = {
        "merchants_attempted": 0,
        "jev_failures": 0,
        "jev_errors": [],
        "transactions_updated": 0,
    }
    mappings = state["merchant_mappings"]
    for merchant, rows in pending.items():
        mapping = mappings.get(merchant)
        created_mapping = mapping is None
        if mapping is None:
            report["merchants_attempted"] += 1
            try:
                prediction = classifier.classify(merchant, tuple(Category))
                if prediction is None:
                    raise ValueError("Jev returned no category")
                category = Category(prediction.category)
            except Exception as exc:
                report["jev_failures"] += 1
                if isinstance(exc, JevError) and str(exc) not in report["jev_errors"]:
                    report["jev_errors"].append(str(exc))
                logger.warning("Classifier retry failed (%s)", type(exc).__name__)
                continue
            now = timestamp()
            mapping = {
                "normalized_merchant": merchant,
                "display_name": merchant.title(),
                "category": category.value,
                "confidence": prediction.confidence,
                "source": ClassificationSource.JEV.value,
                "created_at": now,
                "updated_at": now,
            }
            mappings[merchant] = mapping
        for item in rows:
            item["category"] = mapping["category"]
            item["classification_source"] = (
                ClassificationSource.JEV.value
                if created_mapping
                else ClassificationSource.MERCHANT_MAPPING.value
            )
            item["needs_review"] = False
            report["transactions_updated"] += 1
    report["transactions_remaining"] = sum(
        len(rows) for merchant, rows in pending.items() if any(row["needs_review"] for row in rows)
    )
    return report


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
        if not item.get("possible_duplicate", False):
            item["excluded_from_spending"] = False
    return len(targets)


def set_duplicate_exclusion(state: dict, transaction_id: str, excluded: bool) -> None:
    transaction = next(
        (item for item in state["transactions"] if item["id"] == transaction_id), None
    )
    if transaction is None:
        raise ValueError("Transaction not found")
    if not transaction.get("possible_duplicate", False):
        raise ValueError("Only possible duplicates can be excluded here")
    transaction["excluded_from_spending"] = excluded


def money(cents: int) -> str:
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))


def spending_summary(state: dict, *, currency: str = "USD") -> dict:
    categories: dict[str, int] = defaultdict(int)
    months: dict[str, int] = defaultdict(int)
    merchants: dict[str, int] = defaultdict(int)
    count = 0
    for item in state["transactions"]:
        if item["currency"] != currency or item.get("excluded_from_spending", False):
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
