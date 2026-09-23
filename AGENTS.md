# Spend Tracker

## What we are building

Spend Tracker is a single-user Python/FastAPI backend that imports transaction
CSVs, normalizes merchant names, classifies spending, and produces deterministic
analytics. Supabase-hosted PostgreSQL is the intended production database. Jev
will classify previously unseen merchants once its integration is chosen.

Core flow: CSV → validation → canonical transactions → merchant normalization →
stored merchant mapping or Jev → persistence → analytics and manual corrections.

## General plan

1. Establish a small, runnable backend with configuration, documentation, and tests.
2. Build CSV ingestion, conservative duplicate detection, and merchant normalization.
3. Reuse known merchant classifications; isolate Jev behind a replaceable interface.
4. Support transaction-only and merchant-wide category corrections.
5. Add deterministic summaries and prepare the PostgreSQL schema and connection.
6. Connect the real Jev provider and Supabase, then evaluate classification quality
   with labeled examples before trusting predictions at scale.

## Design guidelines

- Keep CSV parsing, normalization, classification, persistence, and HTTP handlers separate.
- Preserve raw descriptions, use Decimal for money, and record classification sources.
- Prefer known merchant mappings over Jev; use CSV categories as a fallback.
- Do not pretend Jev is connected. The app must work without external credentials.
- A transaction-only correction must not change merchant mappings. A merchant-wide
  correction updates the mapping and all existing transactions for that merchant.
- Be conservative when detecting duplicates and merging merchant names.
- Keep credentials and real financial data out of source control and logs.
- No Plaid, PDF ingestion, multiple users, complicated authentication, or frontend
  is required for this initial backend. Keep the development server local.
- Test the classification, duplicate-handling, and correction behaviors when changing them.
- Keep README setup instructions and integration notes consistent with the code.
- Use conventional commit messages if asked to commit; inherit parent repository rules.
