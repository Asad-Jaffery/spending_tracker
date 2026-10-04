# Spend Tracker

A small, local Python tool for importing transactions, remembering merchant categories, correcting mistakes, and calculating spending totals. The [project spec](docs/project-spec.md) describes the longer-term design; [architecture](docs/architecture.md) explains this MVP.

## What works now

```text
CSV or synthetic JSON → parse and validate → normalize merchant
    → saved mapping, or Jev for an unknown merchant
    → local JSON store → corrections and spending summary
```

The MVP uses a JSON file in `data/`, which Git ignores. No server or database is needed. Supabase/Postgres is a future storage replacement, not an active connection. The code uses only Python's standard library at runtime.

## Setup and demo

Requires Python 3.12 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/spend-tracker import examples/synthetic-transactions.json --source demo-checking
.venv/bin/spend-tracker transactions
.venv/bin/spend-tracker summary
```

The synthetic input is in [examples/synthetic-transactions.json](examples/synthetic-transactions.json). Imports and corrections persist in `data/spend-tracker.json`. With no API key, unknown merchants become `Other` and appear in `.venv/bin/spend-tracker transactions --review`. Importing the same file and source again skips those rows as duplicates.

You can also import a CSV:

```bash
.venv/bin/spend-tracker import examples/transactions.csv --source sample-checking
```

CSV needs date, description, and amount columns. Common header aliases are accepted. An optional transaction ID enables exact deduplication across files. Without IDs, an exact replay of the same file is skipped; similar purchases are flagged as possible duplicates rather than removed. CSV category columns are ignored. Use `--date-format us` for `MM/DD/YYYY` or `--amount-convention expenses_negative` if expenses are negative in the source file. Inspect the import report for rejected rows.

## Make a real Jev call

1. Create an account and API key at [jevmodel.org](https://jevmodel.org/). Its [API docs](https://jevmodel.org/docs/) specify a Bearer key and `POST /v1/systemone`.
2. In your terminal, set the key for that terminal session. Do not put it in source control:

   ```bash
   export JEVMODEL_API_KEY='paste-your-key-here'
   ```

3. Test one request without changing stored transactions:

   ```bash
   .venv/bin/spend-tracker jev-test 'blue bottle coffee'
   ```

   To call the provider directly, this is the same API shape with a smaller illustrative category set:

   ```bash
   curl https://jevmodel.org/v1/systemone \
     -H "Authorization: Bearer $JEVMODEL_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"model":"jev-latest","state":"Merchant name: blue bottle coffee","questions":{"category":{"type":"choice","instructions":"Classify this merchant.","criteria":{"Dining":"Cafe or restaurant","Other":"Anything else"}}}}'
   ```

   Look for `answers.category.choice` in the JSON response. The Spend Tracker client sends all 12 project categories instead of the two shown here.

4. Test the entire import flow using a **fresh** data file so the demo rows are not skipped as duplicates:

   ```bash
   .venv/bin/spend-tracker --data data/live-demo.json import examples/synthetic-transactions.json --source demo-checking
   .venv/bin/spend-tracker --data data/live-demo.json merchants
   .venv/bin/spend-tracker --data data/live-demo.json transactions
   ```

The client sends a normalized merchant name, the `jev-latest` model, and one `choice` question constrained to the 12 categories in the spec. It reads `answers.category.choice` and optional `confidence`. It retries transient HTTP 429/502 responses with the same idempotency key; other failures yield `Other` plus a review flag during import and increment `jev_failures` in the import report. A valid Jev choice, including `Other`, is saved as the merchant mapping regardless of confidence. The key is only read from `JEVMODEL_API_KEY`; it is never written to the JSON store.

The request format is implemented from the [jevmodel.org reference](https://jevmodel.org/docs/). The client has been tested against mocked responses, but a real authenticated call cannot be verified until you provide your own key.

## Corrections

Find a transaction ID with `transactions`, then correct one row:

```bash
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries
```

To change the saved merchant mapping and all existing rows for that merchant:

```bash
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries --scope merchant
```

Valid categories: `Groceries`, `Dining`, `Transportation`, `Housing`, `Utilities`, `Shopping`, `Entertainment`, `Travel`, `Health`, `Income`, `Transfers`, `Other`. A transaction-only correction leaves the merchant mapping unchanged. A merchant correction determines future classifications without another Jev call.

## Development

```bash
python3 -m pip install -e '.[dev]'
pytest -q
ruff check .
ruff format --check .
```

The JSON persistence boundary lives in [storage.py](src/spend_tracker/storage.py). A future Supabase/Postgres implementation should keep the same import, correction, and classification rules while adding database transactions, concurrency control, and migrations. There is no database connector in the MVP.
