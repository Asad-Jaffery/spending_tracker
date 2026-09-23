# Integration handoff

## Connect Supabase PostgreSQL

The backend already uses SQLAlchemy with the psycopg driver. No Supabase SDK,
project URL, or API key is needed for this direct database approach.

1. Create/select the Supabase project and get its PostgreSQL connection string
   from the **Connect** panel. For this persistent Python service, use a direct
   connection or the session pooler. The session pooler is useful on IPv4-only
   networks. Follow [Supabase's connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres).
2. Copy `.env.example` to `.env` and set the actual connection URL:

   ```dotenv
   DATABASE_URL=postgresql+psycopg://USER:URL_ENCODED_PASSWORD@HOST:5432/postgres?sslmode=require
   ```

   Keep the username/host from Supabase's connection panel. Percent-encode special
   characters in the password. The app also accepts `postgresql://` and converts
   it to the [psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg).

3. Run `make migrate`, restart `make run`, and check `/health`. It should report
   `database: postgresql`. Import the fictional CSV first.

The migration creates `transactions`, `merchant_mappings`, and `import_runs`, with
category constraints, indexes, and unique duplicate keys. PostgreSQL row-level
security is enabled without public policies to prevent anonymous access through
Supabase's Data API. Connect from the backend using the table owner or another
appropriately privileged role; browser-side database access is not part of this
design. Do not publish `.env` or real transaction files.

Changing `DATABASE_URL` starts with that database's contents; it does **not** copy
local SQLite data. Reimport original CSVs into the new database and reapply manual
corrections, or write an explicit migration/export tool if local data needs to be
preserved. Keep the same source/account labels.

Writes are serialized in the app for SQLite and with a transaction-scoped advisory
lock for PostgreSQL. This keeps imports and merchant-wide corrections consistent
through the API. External SQL writers do not participate in that lock. Imports
are synchronous and hold a transaction while classifying; background jobs and
larger-scale throughput are future work.

## Connect Jev

No transport, endpoint, authentication scheme, or SDK has been assumed. The seam
is `MerchantClassifier` in `src/spend_tracker/classifiers.py`:

```python
def classify(self, merchant: str, categories: tuple[Category, ...]) -> Prediction | None: ...
```

Implement an adapter around whichever Jev access method you choose. It receives
only the normalized merchant and allowed category set. Return a validated
`Prediction(category=..., confidence=...)`, or `None` when unavailable.

Adapter responsibilities:

- Keep credentials in environment settings and send them only to the selected provider.
- Ask Jev to choose exactly one supplied category and return confidence in `[0, 1]`.
- Set a finite network timeout and bounded retries in the actual transport.
- Validate the returned payload with `Prediction.model_validate(...)`.
- Return `None` or raise an exception on unavailable/invalid responses. The engine
  falls back safely and logs only the exception type, not merchant data or secrets.

Wire it into the existing app factory, for example in a module you create:

```python
# src/spend_tracker/connected_app.py (after implementing YourJevAdapter)
from spend_tracker.main import create_app
from your_jev_integration import YourJevAdapter

app = create_app(classifier=YourJevAdapter())
```

Start it with `.venv/bin/uvicorn spend_tracker.connected_app:app --host 127.0.0.1`.
Alternatively, change the default classifier construction in `create_app` once
the adapter is ready. Configuration and persistence remain unchanged. `/health`
reports `adapter_configured`, which means the adapter is installed, not that a
remote provider request has succeeded.

`tests/conftest.py` contains a fake classifier used only for tests. It is not a
production Jev implementation. The engine tests already exercise unknown-merchant
calls, cache reuse, invalid/low-confidence results, failures, and manual overrides.

Accepted predictions use the configured threshold (default 0.80). Failed attempts
are suppressed for the rest of the import but retried on later imports. Neither
CSV fallbacks nor low-confidence predictions become authoritative mappings.
Already imported `Other` rows are not automatically retried; correct them through
the API for now. A reclassification queue is a later addition.

## Before relying on classification quality

Build a small labeled dataset of normalized merchants, with separate tuning and
held-out evaluation examples. Measure accuracy and per-category confusion,
fallback rate, confidence calibration, and how often manual corrections are
needed. Test normalization separately so accidental merchant merges are visible.
Do not measure cached answers as new Jev predictions. The current test suite proves
the engine's behavior, not the real provider's classification accuracy.
