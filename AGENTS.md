# Spend Tracker

## What we are building

Spend Tracker is a single-user Python tool that imports transaction CSVs through
a local command, normalizes merchant names, classifies spending, and produces
deterministic analytics. Supabase-hosted PostgreSQL is the intended database.
Jev will classify previously unseen merchants through jevmodel.org, the selected
gateway for the initial integration. Its API behavior and current limits still
need to be verified with a live request.
The current prototype may still include an API; the revised spec does not
require a web server.

Core flow: CSV → validation → canonical transactions → merchant normalization →
stored merchant mapping or Jev → persistence → analytics and manual corrections.

## General plan

1. Establish a small, runnable local import workflow with configuration,
   documentation, and tests.
2. Build CSV ingestion, conservative duplicate detection, and merchant normalization.
3. Reuse known merchant classifications; isolate Jev behind a replaceable interface.
4. Support transaction-only and merchant-wide category corrections.
5. Add deterministic summaries and prepare the PostgreSQL schema and connection.
6. Connect Jev through jevmodel.org and connect Supabase, then evaluate
   classification quality with labeled examples before trusting predictions at scale.

## Design guidelines

- Keep CSV parsing, normalization, classification, persistence, and command
  handling separate.
- Preserve raw descriptions, use Decimal for money, and record classification sources.
- Reuse known merchant mappings. For an unseen merchant, save every valid Jev
  category as its mapping regardless of confidence. Ignore CSV labels. If Jev
  fails or returns an invalid category, use `Other` with a review flag and do
  not save a mapping for that failed attempt.
- Do not pretend Jev is connected. The app must work without external credentials.
- Keep the Jev integration behind a replaceable interface so the gateway can
  change without changing classification rules. Do not commit API keys.
- A transaction-only correction must not change merchant mappings. A merchant-wide
  correction updates the mapping and all existing transactions for that merchant.
- Be conservative when detecting duplicates and merging merchant names.
- Keep credentials and real financial data out of source control and logs.
- No web server, Plaid, PDF ingestion, multiple users, complicated
  authentication, or frontend is required for the initial workflow.
- Test the classification, duplicate-handling, and correction behaviors when changing them.
- Keep README setup instructions and integration notes consistent with the code.
- Use conventional commit messages if asked to commit; inherit parent repository rules.
