import argparse
import json
import sys
from pathlib import Path

from spend_tracker.classifiers import JevClassifier, JevError, UnconfiguredJevClassifier
from spend_tracker.config import database_url, jev_api_key, local_setting
from spend_tracker.domain import Category
from spend_tracker.import_profiles import resolve_import_profile
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
        help="Select a local JSON file instead of the configured PostgreSQL store",
    )
    command.add_argument(
        "--database-url",
        default=database_url(),
        help="PostgreSQL URL from .env or environment; otherwise use local JSON",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    importing = subcommands.add_parser("import", help="Import a CSV or synthetic JSON file")
    importing.add_argument("path", type=Path)
    importing.add_argument("--source", required=True, help="Bank/account identifier")
    importing.add_argument("--currency", default="USD")
    importing.add_argument("--import-preset", help="Built-in or saved card preset ID")
    importing.add_argument("--date-format", choices=("iso", "us"), default="iso")
    importing.add_argument(
        "--amount-convention",
        choices=("expenses_positive", "expenses_negative"),
        default="expenses_positive",
    )
    importing.add_argument("--date-column", help="Exact CSV header for the date")
    importing.add_argument("--description-column", help="Exact CSV header for the description")
    importing.add_argument("--amount-column", help="Exact CSV header for the amount")
    importing.add_argument("--id-column", help="Exact CSV header for an optional transaction ID")
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
    subcommands.add_parser(
        "migrate-to-postgres", help="Copy the local JSON data into an empty PostgreSQL store"
    )
    server = subcommands.add_parser("serve", help="Run the local dashboard")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-demo", action="store_true", help="Start with an empty dashboard")
    return command


def print_json(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    data_path = args.data or Path(local_setting("SPEND_TRACKER_DATA") or "data/spend-tracker.json")
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
        if args.command == "migrate-to-postgres":
            if not args.database_url:
                raise ValueError("Set SPEND_TRACKER_DATABASE_URL before migrating")
            if not data_path.is_file():
                raise ValueError(f"Local JSON data file not found: {data_path}")
            source_state = JsonStore(data_path).load()
            target = PostgresStore(args.database_url)
            target_state = target.load()
            if any(
                target_state[key]
                for key in (
                    "transactions",
                    "merchant_mappings",
                    "import_runs",
                    "csv_column_mappings",
                    "import_profiles",
                )
            ):
                raise ValueError("PostgreSQL store is not empty; migration did not overwrite it")
            target.save(source_state)
            migrated = target.load()
            for key in ("transactions", "import_runs"):
                if {item["id"]: item for item in source_state[key]} != {
                    item["id"]: item for item in migrated[key]
                }:
                    raise ValueError(f"Migration verification failed for {key}")
            for key in ("merchant_mappings", "csv_column_mappings", "import_profiles"):
                if source_state[key] != migrated[key]:
                    raise ValueError(f"Migration verification failed for {key}")
            print_json(
                {
                    "transactions": len(migrated["transactions"]),
                    "merchant_mappings": len(migrated["merchant_mappings"]),
                    "import_runs": len(migrated["import_runs"]),
                    "csv_column_mappings": len(migrated["csv_column_mappings"]),
                    "import_profiles": len(migrated["import_profiles"]),
                    "json_source_retained": str(data_path),
                }
            )
            return 0
        store = (
            PostgresStore(args.database_url)
            if args.database_url and args.data is None
            else JsonStore(data_path)
        )
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
            if args.import_preset and args.path.suffix.lower() != ".csv":
                raise ValueError("Card import presets require a CSV file")
            content = args.path.read_bytes()
            if args.path.suffix.lower() == ".csv":
                base_preset, settings = (
                    resolve_import_profile(state, args.import_preset, None)
                    if args.import_preset
                    else (None, None)
                )
                column_mapping = {
                    field: value
                    for field, value in (
                        ("date", args.date_column),
                        ("description", args.description_column),
                        ("amount", args.amount_column),
                        ("id", args.id_column),
                    )
                    if value is not None
                }
                parsed = parse_csv(
                    content,
                    date_format=args.date_format,
                    amount_convention=args.amount_convention,
                    column_mapping=column_mapping
                    or state["csv_column_mappings"].get(args.source.strip().casefold()),
                    import_preset=base_preset,
                    import_settings=settings,
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
            if args.path.suffix.lower() == ".csv" and column_mapping and not args.import_preset:
                state["csv_column_mappings"][args.source.strip().casefold()] = column_mapping
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
