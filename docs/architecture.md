# Project Architecture

This describes the intended design in the [project spec](project-spec.md). The existing prototype may differ from it.

## System boundary

A local Python command takes a CSV path and its source/account information. It runs the import pipeline, writes the results to Supabase-hosted PostgreSQL, and prints an import report. The initial design does not require an HTTP server or frontend.

```text
CSV file → parser → canonical transactions → merchant normalization
                                                 ↓
                                   mapping lookup → Jev if unknown
                                                 ↓
                              transactions + merchant mappings
                                                 ↓
                              corrections + deterministic analytics
```

## Components

| Component | Responsibility |
| --- | --- |
| Command interface | Accept the file path and import options; report counts and row errors. |
| CSV adapters | Read institution-specific columns and validate each row. |
| Canonical transaction | Represent dates, raw descriptions, amounts, and source IDs consistently. |
| Merchant normalizer | Produce a conservative merchant key while preserving the raw description. |
| Classification engine | Reuse stored mappings or request one category from Jev. |
| Jev adapter | Hide the provider URL, credentials, request format, and response parsing. |
| Persistence | Store transactions, merchant mappings, and import runs in PostgreSQL. |
| Analytics | Calculate summaries from stored amounts and categories without AI. |

The command should orchestrate these components rather than contain their logic. Jev and the database connection remain replaceable integrations.

## Import sequence

1. The command opens a CSV file and records its source/account label and import options.
2. A CSV adapter validates rows and converts them into canonical transactions. Bad rows are reported without losing valid rows.
3. Duplicate checks skip known transaction IDs or exact file replays. Similar-looking purchases are flagged for review rather than silently discarded.
4. The normalizer produces a merchant key, such as `costco`, while the transaction retains `COSTCO WHSE #1029`.
5. The classification engine chooses a category using the decision below.
6. The importer persists transactions and an import report. Database failures roll back the affected import.

## Classification decision

```text
Normalized merchant
        ↓
Stored mapping? ── yes → use saved category
        │
        no
        ↓
      Call Jev
        ↓
Valid allowed category? ── yes → save Jev category as merchant mapping
        │                         and use it for this transaction
        no
        ↓
Other + needs review; do not save a merchant mapping
```

The engine ignores CSV category labels. Every valid category Jev returns becomes the merchant mapping, regardless of confidence. If Jev explicitly chooses `Other`, that is still a valid Jev classification and is saved. An unavailable provider, failed call, or invalid response produces the separate `Other` review fallback without a mapping. A later import can retry that merchant.

Known merchants never call Jev. Repeated rows for an unknown merchant should cause at most one Jev attempt within an import. Confidence may be stored as metadata, but it is not an acceptance threshold.

## Data records

- **Transaction:** date, raw description, normalized merchant, amount, currency, category, classification source, review flag, and source transaction ID when available.
- **Merchant mapping:** normalized merchant, display name, category, source (`jev` or `manual`), optional confidence, and timestamps.
- **Import run:** source file, processed/imported/failed/skipped counts, row errors, and timestamp.

Parse money with `Decimal` and store exact minor units, such as integer cents. Keep import identity separate from a looser fingerprint used only to flag possible duplicates.

## Corrections

A transaction-only correction changes one stored transaction and leaves the merchant mapping alone. A merchant-wide correction changes the mapping and all existing transactions for that normalized merchant. Future imports then reuse the corrected mapping without calling Jev.

## Integration and failure boundaries

The Jev adapter receives the normalized merchant and allowed categories. It returns a validated category or signals failure; the classification engine owns the `Other` review fallback. Keep provider credentials in local configuration, and do not log secrets or real financial data.

The database layer owns transactions and schema migrations. It must prevent duplicate writes and keep merchant-wide corrections consistent with imports. No web server, multi-user authentication, Plaid, or PDF ingestion is required for this phase.
