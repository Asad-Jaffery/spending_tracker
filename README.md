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

Select **Dashboard** in the sidebar, then use the tabs at the top to switch between two analysis views. The sidebar also has three workflow screens:

For presentations, select **Synthetic** in the month selector. This isolated demo
uses June 2026 sample transactions for the monthly dashboard and six months of
fake history (January–June 2026, 347 transactions) for the over-time view.
The history includes changing category totals, a spring travel spike, and a
shopping spike so the chart is useful to explore during a presentation.
Transactions, merchants, and review also stay within the demo. Demo corrections stay in
memory and reset when the server restarts; they never update the saved financial
data. Open `http://127.0.0.1:5174/?month=synthetic` to start directly in the demo.
The selected month is kept in the URL so refreshing preserves that selection.

- **Monthly view:** spending, transaction and merchant counts, review count, spending by week and category with Chart.js hover tooltips for exact amounts and category shares, a category-by-category comparison with the previous calendar month, top merchants, and review items. The month selector chooses which month to inspect. The comparison says when the previous month has no data.
- **Over time:** all imported months in one view, with a line for each spending category, hover details for the nearest month, category visibility controls, all-time summary metrics, and a table of exact monthly amounts. Months without transactions appear as zero points so gaps in the timeline are visible. Transactions excluded from spending totals remain excluded here.
- **Transactions:** category columns with counts and totals, plus the amount, date, classification source, and correction action for every transaction in the selected month.
- **Merchants:** saved categories that new transactions will reuse.
- **Review:** transactions assigned `Other` after a missing or failed classification.

Select **Import CSV** to add a bank statement. The importer validates rows, normalizes merchants, ignores any CSV category label, and reports imports, duplicates, and rejected rows. Choose the source account and, if needed, the date format and sign convention. It first uses a column mapping previously saved for that account; otherwise it matches common header names: `date`/`transaction date`/`posting date`/`posted date`, `description`/`merchant`/`name`/`payee`/`raw description`, and `amount`/`transaction amount`; a transaction ID is optional. These defaults are common header names, not claims of support for specific banks. If your export uses different headers, expand **Custom column mapping** and enter the exact date, description, and amount column names from the file, plus an optional transaction ID column. A successful import saves the mapping for that account so later imports can omit it. A structural mismatch rejects the file and shows its available headers so you can correct the mapping and retry. Invalid individual rows are reported while valid rows still import; if no rows are valid, the file is rejected. Files with separate debit and credit columns need conversion to one amount column before import. A JSON import is also supported for synthetic/test data. Imported data persists across restarts.

Click **Correct** on a transaction, choose a category, then choose either **This transaction only** or **All from this merchant**. The first changes only the selected row. The second changes all existing rows for that merchant and saves its category for future imports.

### Credit-card import presets

In **Import CSV → Card / import format**, select **Wells Fargo Active Cash** or
**Robinhood** instead of entering settings by hand:

Selecting a preset fills in an editable **Import settings** panel. You can change
the date format, amount signs, CSV column names, merchant column, payment filter,
and posted-status filter. Expand **Transaction IDs and matching** to edit an ID
column or Robinhood's overlap-matching settings. **Save settings** saves the preset
name, account name, and settings without needing a file. Saved settings are prefilled
next time, including after restarting the app. **Reset defaults** restores the
original template in the editor; choose Save settings to keep that reset.
Import file uses the current editor settings without automatically saving changes.

Choose **＋ New card preset** to configure another card. Under **Additional rows
to ignore**, add CSV column/value rules using **Equals**, **Contains**, or **Starts
with**. A row is skipped if any rule matches, ignoring case and surrounding spaces.
For example, `Description` equals `ONLINE PAYMENT THANK YOU` ignores a payment
without discarding refunds. Use a specific value when choosing Contains.
Configured rule columns must exist in the CSV; a missing column stops the import.
Ignored rows are counted separately in the report and never sent to Jev or stored
as transactions. Presets persist in both JSON and PostgreSQL storage.

| Preset | Columns | Amounts | Payments skipped |
| --- | --- | --- | --- |
| Wells Fargo Active Cash | `DATE`, `DESCRIPTION`, `AMOUNT` | Negative charges become positive spending; positive refunds reduce spending | Description exactly matches `ONLINE PAYMENT THANK YOU` (ignoring case and surrounding spaces) |
| Robinhood | `Date`, `Time`, `Merchant`, `Description`, `Amount`, plus `Type` | Positive purchases are spending; negative refunds reduce spending | `Type` is `Payment` (ignoring case and surrounding spaces) |

Both presets accept ISO dates, US dates, and Excel date numbers, including
fractional days. Robinhood preserves the full `Description` on each transaction
and uses its `Merchant` field for classification, falling back to the description
when that field is blank. Refunds stay in the spending history. Payments and,
when a `Status` column exists, rows not marked `Posted` are skipped before
classification or storage and counted separately in the import report.
Payment-only files produce a report with zero imported transactions.

The preset supplies initial settings even if the account has an older custom
mapping. Your edits to the preset settings take precedence for that import.
It does not overwrite that custom mapping. Keep the same
account name on later imports, or edit it to distinguish multiple cards.
**Custom / other account** retains the original manual settings and JSON import.
Presets expect actual CSV files, not Markdown tables. Robinhood exports have no
bank transaction ID, so the importer derives an identity from each posted row's
date, time, cardholder, amount, type, merchant, and description. Points and
balance are excluded from that identity. Repeated rows in overlapping exports
are skipped when those stable fields match;
identical purchases in one file are preserved. If Robinhood changes a row's
exported fields between downloads, that match may fail and the row is flagged
as a possible duplicate instead. Wells Fargo's export lacks enough detail to
identify two same-day, same-amount purchases reliably. A different export with
matching Wells Fargo rows therefore imports them, flags them as possible
duplicates, and shows that count in the import report. On the Transactions
screen, use **Exclude from totals** on a confirmed duplicate; **Include in
totals** reverses that choice. This keeps the row for audit while removing its
amount from spending analytics. Reimporting the exact same file skips its rows.

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

The default JSON store works with no external service. Each transaction has an app-generated UUID as its PostgreSQL primary key; a bank-provided transaction ID, when present, is stored separately for duplicate detection. To set up a local database on macOS with Homebrew:

```bash
brew install postgresql@14
brew services start postgresql@14
createdb spend_tracker_local
.venv/bin/python -m pip install -e '.[postgres]'
# Add SPEND_TRACKER_DATABASE_URL=postgresql:///spend_tracker_local to .env
.venv/bin/spend-tracker migrate-to-postgres
npm run dev
```

Create the database only once. `migrate-to-postgres` copies the existing `data/spend-tracker.json` into an **empty** database, verifies the records, and leaves the JSON file intact. Skip migration if you are starting with an empty database. The app reads `SPEND_TRACKER_DATABASE_URL` from your shell or `.env` and uses PostgreSQL automatically; an explicit `--data path/to/file.json` selects JSON for a CLI command. For a hosted database such as Supabase, use its connection URL instead. Keep that URL in the ignored `.env` file and use TLS as required by your provider.

The app creates separate transaction, merchant-mapping, import-run, CSV-column-mapping, and import-preset tables on first connection. It stores each record as JSONB and writes the single-user snapshot in one database transaction. Use a dedicated database or schema for this prototype; it is designed for one writer, not concurrent users.

## Command-line workflow

```bash
.venv/bin/spend-tracker demo
.venv/bin/spend-tracker import examples/transactions.csv --source checking
.venv/bin/spend-tracker import bank.csv --source checking --date-column 'Posted On' --description-column 'Transaction Details' --amount-column 'Charge'
.venv/bin/spend-tracker import wells-fargo.csv --source wells-fargo-active-cash --import-preset wells_fargo
.venv/bin/spend-tracker import robinhood.csv --source robinhood --import-preset robinhood
.venv/bin/spend-tracker transactions
.venv/bin/spend-tracker transactions --review
.venv/bin/spend-tracker merchants
.venv/bin/spend-tracker summary
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries
.venv/bin/spend-tracker correct TRANSACTION_ID Groceries --scope merchant
```

Use `--data path/to/file.json` before a subcommand to select a local JSON store even when PostgreSQL is configured. Otherwise the CLI uses PostgreSQL when `SPEND_TRACKER_DATABASE_URL` is set. CSV needs date, description, and amount columns; the importer uses a saved mapping for the selected `--source`, then common header variants. To define or replace an account mapping, use `--date-column`, `--description-column`, `--amount-column`, and optional `--id-column`. Supply all three required fields when defining a mapping. `--date-format us` handles `MM/DD/YYYY`. `--amount-convention expenses_negative` flips the sign of source expenses. When a source transaction ID exists, repeat imports are skipped exactly. Without IDs, an exact replay of the same file is skipped; similar purchases are flagged for inspection.

## Verify

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
npm run build
```

Only synthetic data belongs in source control. Keep API keys and real financial files out of the repository.
