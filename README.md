# Spend Tracker

Spend Tracker imports bank and credit-card CSVs, saves merchant categories, and shows spending in a local dashboard. The interface follows the [A4A presentation](https://docs.google.com/presentation/d/1BmEbczXE_wI-0FQ6TzVDe6nLjsJwr4uDtWyU7ZZbAa4/edit): overview metrics, weekly spending, category breakdown, top merchants, transactions, saved mappings, and review items. The [project spec](docs/project-spec.md) explains the classification decisions; [architecture](docs/architecture.md) maps them to the code.

## Run the prototype

Requires Python 3.12 or newer and Node.js 20.19+ or 22.12+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
npm install
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174). `npm run dev` starts the React/TypeScript Vite server and the Python FastAPI backend together. Vite forwards `/api` requests to FastAPI at `http://127.0.0.1:8765`; Jev credentials stay in Python. The API is documented at [http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs). Stop both servers with Ctrl+C.

The first launch seeds an empty store with **synthetic May and June 2026 transactions** so the month comparison is ready to explore. June contains 60 transactions, including ten needing review. The data is visibly labeled in the UI and lives in [`may-2026.json`](src/spend_tracker/demo/may-2026.json) and [`june-2026.json`](src/spend_tracker/demo/june-2026.json). An existing store with synthetic June data receives May demo data on its next launch if it has no May transactions. The default data file is `data/spend-tracker.json`, which Git ignores. Use `npm run dev:api -- --no-demo` with a fresh data file for an empty start.

For a single-server version, run `npm run build` and then `.venv/bin/spend-tracker serve`. FastAPI serves the built React app at [http://127.0.0.1:8765](http://127.0.0.1:8765). The frontend and backend source can also be started independently with `npm run dev:web` and `npm run dev:api`.

The dashboard has four views:

- **Overview:** spending, transaction and merchant counts, review count, spending by week and category, a category-by-category comparison with the previous calendar month, top merchants, and review items. The comparison says when the previous month has no data.
- **Transactions:** category columns with counts and totals, plus the amount, date, classification source, and correction action for every transaction in the selected month.
- **Merchants:** saved categories that new transactions will reuse.
- **Review:** transactions assigned `Other` after a missing or failed classification.

Select **Import CSV** to add a bank statement. The importer validates rows, normalizes merchants, ignores any CSV category label, and reports imports, duplicates, and rejected rows. Choose the source account and, if needed, the date format and sign convention. A JSON import is also supported for synthetic/test data. Imported data persists across restarts.

Click **Correct** on a transaction, choose a category, then choose either **This transaction only** or **All from this merchant**. The first changes only the selected row. The second changes all existing rows for that merchant and saves its category for future imports.

## Jev classification

Create a [jevmodel.org API key](https://jevmodel.org/docs/), then put it in a local `.env` file at the project root (this file is Git-ignored):

```bash
cp .env.example .env
# Edit .env and set JEVMODEL_API_KEY=sk-your-real-key
.venv/bin/spend-tracker jev-test 'blue bottle coffee'
.venv/bin/spend-tracker serve
```

You can instead export `JEVMODEL_API_KEY` in the terminal *before* starting the server. The import dialog now shows whether the key is available. If you imported a CSV without a key, go to **Review → Retry with Jev** after adding it; existing `Other` rows will be classified without importing the file again. The equivalent CLI command is `.venv/bin/spend-tracker retry-classification`. Retry only touches imported rows still awaiting review, not manual corrections or synthetic demo transactions. A missing key is reported in the import result; API failures display a safe status such as HTTP 401 (invalid key) or HTTP 402 (insufficient balance).

The key stays in the Python process. The browser calls the local app, which makes the authenticated Jev request; the key is never sent to browser code or stored with transactions. Jev receives the normalized merchant name and a choice question constrained to the project's eight categories: Groceries, Food + coffee, Transportation, Shopping, Entertainment, Travel, Health + personal wellness, and Other. Every valid choice is saved as that merchant's default regardless of confidence. Future transactions use the saved mapping without another model call. If Jev is unavailable, fails, or returns an invalid category, the transaction becomes `Other` with a review flag; no mapping is saved. The ten synthetic June review items are generated offline and do not count as Jev calls.

Older saved `Dining` and `Health` labels are renamed when loaded. Existing `Housing` and `Utilities` transactions become `Other` for review, and their saved merchant mappings are retired. Older `Income` and `Transfers` transactions also become `Other` for review but remain excluded from spending totals; their merchant mappings are retired. Correcting one of those transactions explicitly includes it in spending. New demo data omits income and transfer rows.

The request/response shape and retry behavior are tested with simulated API responses. A live authenticated Jev call was verified locally with a configured key; supply your own key in `.env` to repeat it. See the [provider's API reference](https://jevmodel.org/docs/) for the endpoint and key setup.

## PostgreSQL option

The default JSON store works with no external service. To run the same app with a PostgreSQL database, including Supabase Postgres:

```bash
.venv/bin/python -m pip install -e '.[postgres]'
export SPEND_TRACKER_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require'
.venv/bin/spend-tracker serve
```

The app creates separate transaction, merchant-mapping, and import-run tables on first connection. It stores each record as JSONB and writes the single-user snapshot in one database transaction. The connector has been tested against a temporary local PostgreSQL instance. Use a dedicated database or schema for this prototype; it is designed for one writer, not concurrent users.

## Command-line workflow

```bash
.venv/bin/spend-tracker demo
.venv/bin/spend-tracker import examples/transactions.csv --source checking
.venv/bin/spend-tracker transactions
.venv/bin/spend-tracker transactions --review
.venv/bin/spend-tracker merchants
.venv/bin/spend-tracker summary
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries --scope merchant
```

Use `--data path/to/file.json` before a subcommand to select another local store. The same CLI commands use PostgreSQL when `SPEND_TRACKER_DATABASE_URL` is set. CSV needs date, description, and amount columns; common header variants are accepted. `--date-format us` handles `MM/DD/YYYY`. `--amount-convention expenses_negative` flips the sign of source expenses. When a source transaction ID exists, repeat imports are skipped exactly. Without IDs, an exact replay of the same file is skipped; similar purchases are flagged for inspection.

## Verify

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
npm run build
```

Only synthetic data belongs in source control. Keep API keys and real financial files out of the repository.
