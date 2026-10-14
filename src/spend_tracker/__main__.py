import argparse
import json
import os
import sys
from pathlib import Path

from spend_tracker.classifiers import JevClassifier, JevError, UnconfiguredJevClassifier
from spend_tracker.config import jev_api_key
from spend_tracker.domain import Category
from spend_tracker.parsing import InputError, parse_csv, parse_json
from spend_tracker.postgres import PostgresStore
from spend_tracker.services import (
    correct_category,
    import_transactions,
    money,
    retry_unclassified,
    spending_summary,
)
from spend_tracker.storage import JsonStore
from spend_tracker.synthetic import seed_june, seed_may
from spend_tracker.webapp import serve


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="spend-tracker", description="Local spending tracker")
    command.add_argument(
        "--data",
        type=Path,
        default=Path(os.getenv("SPEND_TRACKER_DATA", "data/spend-tracker.json")),
        help="Local JSON data file (default: data/spend-tracker.json)",
    )
    command.add_argument(
        "--database-url",
        default=os.getenv("SPEND_TRACKER_DATABASE_URL"),
        help="Optional PostgreSQL URL; otherwise use the local JSON file",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    importing = subcommands.add_parser("import", help="Import a CSV or synthetic JSON file")
    importing.add_argument("path", type=Path)
    importing.add_argument("--source", required=True, help="Bank/account identifier")
    importing.add_argument("--currency", default="USD")
    importing.add_argument("--date-format", choices=("iso", "us"), default="iso")
    importing.add_argument(
        "--amount-convention",
        choices=("expenses_positive", "expenses_negative"),
        default="expenses_positive",
    )
    transactions = subcommands.add_parser("transactions", help="Show imported transactions and IDs")
    transactions.add_argument(
        "--review", action="store_true", help="Only show items needing review"
    )
    subcommands.add_parser("merchants", help="Show saved merchant mappings")
    correction = subcommands.add_parser(
        "correct", help="Correct one transaction or a whole merchant"
    )
    correction.add_argument("transaction_id")
    correction.add_argument("category", choices=[category.value for category in Category])
    correction.add_argument("--scope", choices=("transaction", "merchant"), default="transaction")
    summary = subcommands.add_parser("summary", help="Show deterministic spending totals")
    summary.add_argument("--currency", default="USD")
    jev = subcommands.add_parser("jev-test", help="Make one live Jev call without saving anything")
    jev.add_argument("merchant", help="A merchant name, such as 'blue bottle coffee'")
    subcommands.add_parser(
        "retry-classification", help="Classify previously imported rows needing review"
    )
    subcommands.add_parser("demo", help="Import the synthetic May and June 2026 datasets")
    server = subcommands.add_parser("serve", help="Run the local dashboard")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-demo", action="store_true", help="Start with an empty dashboard")
    return command


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "jev-test":
            key = jev_api_key()
            if not key:
                raise ValueError("Set JEVMODEL_API_KEY before running jev-test")
            prediction = JevClassifier(key).classify(args.merchant, tuple(Category))
            print_json(
                {
                    "merchant": args.merchant,
                    "category": prediction.category.value,
                    "confidence": prediction.confidence,
                }
            )
            return 0
        store = PostgresStore(args.database_url) if args.database_url else JsonStore(args.data)
        if args.command == "serve":
            serve(store, port=args.port, seed_demo=not args.no_demo)
            return 0
        state = store.load()
        if args.command == "demo":
            june = seed_june(state)
            may = seed_may(state)
            store.save(state)
            print_json({"june": june, "may": may})
        elif args.command == "import":
            content = args.path.read_bytes()
            if args.path.suffix.lower() == ".csv":
                parsed = parse_csv(
                    content, date_format=args.date_format, amount_convention=args.amount_convention
                )
            elif args.path.suffix.lower() == ".json":
                parsed = parse_json(content)
            else:
                raise ValueError("Input file must end in .csv or .json")
            key = jev_api_key()
            classifier = JevClassifier(key) if key else UnconfiguredJevClassifier()
            run = import_transactions(
                state,
                parsed,
                filename=args.path.name,
                content=content,
                source=args.source,
                currency=args.currency,
                classifier=classifier,
            )
            store.save(state)
            print_json(run)
        elif args.command == "retry-classification":
            key = jev_api_key()
            if not key:
                raise ValueError("Set JEVMODEL_API_KEY in .env or your terminal before retrying")
            report = retry_unclassified(state, JevClassifier(key))
            store.save(state)
            print_json(report)
        elif args.command == "transactions":
            print_json(
                [
                    {
                        "id": item["id"],
                        "date": item["date"],
                        "raw_description": item["raw_description"],
                        "normalized_merchant": item["normalized_merchant"],
                        "amount": money(item["amount_cents"]),
                        "currency": item["currency"],
                        "category": item["category"],
                        "classification_source": item["classification_source"],
                        "needs_review": item["needs_review"],
                        "possible_duplicate": item["possible_duplicate"],
                    }
                    for item in state["transactions"]
                    if not args.review or item["needs_review"]
                ]
            )
        elif args.command == "merchants":
            print_json(list(state["merchant_mappings"].values()))
        elif args.command == "correct":
            count = correct_category(
                state, args.transaction_id, Category(args.category), args.scope
            )
            store.save(state)
            print_json({"updated_transactions": count, "scope": args.scope})
        elif args.command == "summary":
            print_json(spending_summary(state, currency=args.currency))
        return 0
    except (OSError, ValueError, InputError, JevError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
