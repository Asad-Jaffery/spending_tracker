# Spend Tracker

A single-user FastAPI backend for importing transaction CSVs, reusing merchant
classifications, correcting categories, and calculating spending summaries.

```text
CSV → validate → normalize merchant → known mapping / Jev / CSV fallback
    → database → manual corrections and deterministic analytics
```

**Works now:** local SQLite storage, CSV uploads, classification orchestration,
merchant mappings, both correction scopes, import history, and basic analytics.
**Ready to connect:** a real Jev adapter and Supabase PostgreSQL. No external
credentials are required to try the app. Jev is explicitly unconfigured by default.
There is no custom frontend; FastAPI's `/docs` is the interactive interface.

## Run locally

Requires Python 3.12 or newer. From this directory:

```sh
make setup
make run
```

Open [interactive API docs](http://127.0.0.1:8000/docs) or
[health status](http://127.0.0.1:8000/health).

`make setup` creates `.venv`, installs the project and development dependencies,
and applies database migrations. SQLite persists in `data/spend_tracker.db`.
Run with one worker in SQLite mode. The development server binds to localhost;
authentication and public deployment are outside this version's scope.

Optional configuration:

```sh
cp .env.example .env
```

Then edit `.env`. Environment variables override that file. After changing
`DATABASE_URL`, run `make migrate` and restart the server. Schema changes are
always applied through Alembic, not automatically during API startup.

## Try a complete flow

The included CSV contains fictional examples, including a refund, income,
a transfer, and an unknown merchant.

```sh
curl -s -X POST http://127.0.0.1:8000/imports \
  -F 'file=@examples/transactions.csv' \
  -F 'source=demo-checking'

curl -s http://127.0.0.1:8000/transactions
curl -s 'http://127.0.0.1:8000/transactions?needs_review=true'
curl -s http://127.0.0.1:8000/analytics/summary
```

The sample imports nine transactions with net spending of `255.09` USD. Income
and transfers are excluded from spending; the refund reduces it. Uploading the
same file again skips all nine transactions and records another import run.

Copy a transaction's `id` from `/transactions`:

```sh
curl -s -X PATCH http://127.0.0.1:8000/transactions/TRANSACTION_ID/category \
  -H 'Content-Type: application/json' \
  -d '{"category":"Groceries","scope":"transaction"}'
```

Use `"scope":"merchant"` to update **all existing transactions for that normalized
merchant**, including earlier transaction-only edits, and its mapping for future
imports. A transaction-only edit leaves the merchant mapping untouched.

## CSV contract

UTF-8 (with or without a BOM), comma separated, with a header row. Standard CSV
quoting works, including quoted commas. The parser preserves the raw description.

| Field | Accepted headers, in priority order |
| --- | --- |
| Date, required | `date`, `transaction date`, `posting date`, `posted date` |
| Description, required | `raw description`, `description`, `merchant`, `name`, `payee` |
| Amount, required | `amount`, `transaction amount` |
| Category, optional | `category`, `transaction category` |
| Bank transaction ID, optional | `transaction id`, `source transaction id`, `id` |

Header matching ignores case and treats underscores as spaces. If multiple
aliases for a field exist, the first in the table wins. Separate debit/credit
columns and institution-specific formats need another parser returning
`CanonicalTransaction`.

Upload form options:

| Field | Default | Meaning |
| --- | --- | --- |
| `source` | Required | Stable bank/account label, e.g. `chase-checking-1234`; normalized to lowercase |
| `date_format` | `iso` | `iso`: `YYYY-MM-DD`; `us`: `MM/DD/YYYY`; dates are never guessed |
| `amount_convention` | `expenses_positive` | Use `expenses_negative` to invert a bank export's signs |
| `currency` | `USD` | Three uppercase letters; summaries never mix currencies |

Canonical amounts are **positive for expenses, negative for refunds/income**.
Amounts accept at most two decimal places, optional `$`, thousands separators,
or parentheses for negatives. All currencies currently use two decimal places;
there is no exchange-rate conversion. Amounts are parsed with `Decimal`, stored
as integer cents, and returned as decimal strings to avoid float rounding.

Malformed files, missing headers, and exceeded limits return an error with no
writes. Within a well-formed file, invalid rows are skipped and reported by line
number; valid rows and the import report are committed together. Even an import
with zero valid rows returns its report: always inspect `rows_failed`.
Defaults: 5 MiB per file and 10,000 rows, configurable in `.env`.

## Classification rules

1. Reuse an existing merchant mapping without calling Jev.
2. Ask Jev for an unseen merchant. Validate the category and confidence; cache
   accepted predictions at or above `JEV_MIN_CONFIDENCE` (default `0.80`).
3. If Jev is unconfigured, unavailable, invalid, or below the threshold, use a
   recognized CSV category. Unsupported category labels are treated as missing.
4. Otherwise use `Other`, source `unclassified`, and `needs_review=true`.

An unseen merchant is attempted at most once per import, even if the provider
fails. Accepted classifications are persisted across imports. Failed or uncertain
results and CSV fallbacks are **not** saved as merchant mappings; a later import
can retry them. `jev_attempts` is zero when no adapter is configured. There is no
background retry or reclassification queue yet.

Normalization uses narrow, tested rules and aliases. It handles the Costco,
Spotify, and payment-prefix examples in the architecture while preserving embedded
merchant digits and distinct names such as `Costco Gas`. It does not attempt
fuzzy matching or strip arbitrary locations. Extend aliases using real examples
and tests; changing rules does not retroactively rename existing transactions.

## Duplicate policy

- If a bank transaction ID exists, deduplicate on `(source, transaction ID)`.
- Otherwise, deduplicate on `(source, exact file hash, row position)`. Renaming
  the same file is safe, and two identical purchases within it are both kept.
- Matching date, amount, currency, source, and raw description creates a
  `possible_duplicate` flag, but does not discard a transaction.

Without IDs, an overlapping or reformatted export may import transactions again.
Review `/transactions?possible_duplicate=true`. Flagged entries remain included
in analytics; this version has no duplicate-resolution UI or endpoint. Always
use the same source label for the same account. Reuploading an unchanged file
does not apply revised currency/sign options or overwrite existing bank IDs.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Verify database tables and report Jev adapter status |
| GET | `/categories` | Allowed categories |
| POST | `/imports` | Upload a CSV with source/account metadata |
| GET | `/imports`, `/imports/{id}` | Import history and row errors |
| GET | `/transactions` | Filter by category, merchant, source, import, dates, or review flags |
| PATCH | `/transactions/{id}/category` | Transaction-only or merchant-wide correction |
| GET | `/merchants` | Persisted classification mappings |
| GET | `/analytics/summary` | Net spending, category totals, monthly totals, top ten merchants |

List endpoints support `limit` (1–500, default 100) and `offset`. Date ranges are
inclusive. Summaries exclude `Income` and `Transfers`; refunds in spending
categories reduce their totals. Empty summaries return `0.00`. Unknown entries
remain included under `Other` until corrected.

## Project structure

```text
AGENTS.md                  Project direction and agent guidelines
src/spend_tracker/
  main.py                  App factory, dependency injection, and API routes
  config.py                Environment settings
  domain.py                Categories, canonical transaction, Jev prediction
  parsing.py               CSV adapter and validation
  normalization.py         Conservative merchant normalization
  classifiers.py           Replaceable Jev protocol and unconfigured adapter
  services.py              Classification, ingestion, corrections, analytics
  models.py / db.py         Portable SQLAlchemy persistence
  schemas.py               HTTP response and correction models
migrations/                Versioned SQLite/PostgreSQL schema
tests/                     Behavioral and migration tests
examples/transactions.csv   Fictional demo input
docs/INTEGRATIONS.md        Jev and Supabase hookup instructions
docs/architecture.md        Original project brief
```

## Checks

```sh
make test
make check
```

Tests use isolated databases, never the app's local financial data. GitHub Actions
runs the suite against SQLite and a PostgreSQL 17 service. To run PostgreSQL tests
locally as well, set `TEST_DATABASE_URL` to a **dedicated test database**; each test
creates and cleans up its own schema. Tests cover cache reuse, fallback behavior,
both correction scopes, future imports, conservative deduplication, concurrent
reuploads, partial row errors, rollback, exact analytics, and migration round trips.

See [integration instructions](docs/INTEGRATIONS.md) for the two remaining hookups.
Recurring-spend detection, anomaly detection, a labeled Jev evaluation dataset,
and classification-quality metrics remain follow-up work.
