# Spend Tracker — Core Architecture

This is the intended project design. The existing prototype may differ from it.
For instructions specific to the existing prototype, see
[the prototype guide](docs/prototype.md).

## Goal

Spend Tracker ingests transaction CSV files, normalizes merchant names, classifies transactions into spending categories, stores the results in Postgres, and supports corrections that improve future classification behavior.

The project is intentionally scoped around a local import pipeline and the
classification system. A Python command reads CSV files directly; no upload
endpoint or running web server is required.

Core flow:

```text
CSV
 ↓
Local Python import command
 ↓
Parse + validate
 ↓
Canonical transaction model
 ↓
Merchant normalization
 ↓
Classification engine
 ↓
Supabase Postgres
 ↓
Analytics
```

## Tech Stack

- Python
- Supabase-hosted PostgreSQL
- Jev for merchant classification
- CSV transaction files as input

No web server, Plaid, PDF ingestion, multi-user system, frontend, or
complicated authentication is needed for the initial version.

---

# Transaction Ingestion

A local Python command takes a CSV file path, parses it, and reports how many
rows were imported, skipped as duplicates, or rejected. It does not need a
long-running service.

Different institutions may have different column names or formats, but each parser should convert transactions into the same internal representation.

Example:

```json
{
  "date": "2026-09-21",
  "raw_description": "COSTCO WHSE #1029",
  "amount": 142.18
}
```

Once the transaction has been converted into the canonical format, the rest of the system should not care which CSV it came from.

---

# Merchant Normalization

Raw transaction descriptions are often inconsistent.

For example:

```text
COSTCO WHSE #1029
COSTCO 00129
COSTCO WHOLESALE
```

should ideally resolve to:

```text
costco
```

The transaction should preserve both values:

```text
raw_description = "COSTCO WHSE #1029"
normalized_merchant = "costco"
```

Normalization may initially include:

- lowercasing
- removing store numbers
- removing unnecessary punctuation
- stripping location-specific information
- removing known transaction prefixes
- normalizing whitespace

The normalized merchant is what the classification engine uses.

---

# Classification Engine

The classification engine answers:

> What category should this merchant belong to?

The important design principle is that **Jev should not classify every transaction**.

The system should remember previously classified merchants.

```text
Transaction
    ↓
Normalize Merchant
    ↓
Known merchant?
   /            \
 yes             no
 ↓                ↓
Use stored      Call Jev
category            ↓
                Valid category?
               /               \
             yes                no
              ↓                  ↓
       Store Jev mapping     Other + review flag
              ↓              (no mapping saved)
         Use category
```

Classification priority:

```text
1. Known merchant mapping
2. Jev classification for unseen merchants
3. `Other` with a review flag if Jev is unavailable, fails, or returns an
   invalid category
```

CSV category labels are ignored, whether present or absent. Every valid Jev
category is saved as the merchant mapping regardless of confidence. Confidence,
if provided, is metadata and does not change that decision. A failed or invalid
Jev response does not create a merchant mapping.

---

# Known Merchant Mapping

The database maintains a mapping like:

```text
costco        → Shopping
spotify       → Entertainment
chipotle      → Dining
shell         → Transportation
```

When a transaction arrives:

```text
SPOTIFY USA 1234
```

normalization produces:

```text
spotify
```

The classification engine queries the existing merchant mapping.

Because Spotify is already known, the system immediately returns:

```text
Entertainment
```

Jev is never called.

This gives us:

- faster imports
- fewer AI requests
- lower inference cost
- consistent classifications

---

# Jev Classification

Jev is used only when the system encounters an unknown merchant.

Example:

```text
SQ *BLUE BOTTLE COFFEE 0421
```

Normalize:

```text
blue bottle coffee
```

No existing merchant mapping is found.

The import pipeline asks Jev to choose from a constrained category set such as:

```text
Groceries
Dining
Transportation
Housing
Utilities
Shopping
Entertainment
Travel
Health
Income
Transfers
Other
```

Jev might return (the confidence field is optional):

```json
{
  "category": "Dining",
  "confidence": 0.96
}
```

The system then stores:

```text
blue bottle coffee → Dining
```

The next Blue Bottle transaction uses the stored mapping rather than calling Jev again.

Jev therefore acts primarily as a **cold-start classifier for unseen merchants**.

---

# Why Cache Classifications?

Suppose a CSV contains:

```text
10,000 transactions
```

but those transactions represent only:

```text
400 unique merchants
```

Calling Jev for every transaction would require:

```text
10,000 Jev calls
```

Instead:

```text
10,000 transactions
        ↓
400 unique merchants
        ↓
300 already known
        ↓
100 unknown merchants
        ↓
100 Jev calls
```

The classification cache reduces unnecessary AI calls while also making results more consistent.

---

# Manual Corrections

Jev and the merchant mappings will sometimes be wrong.

Suppose:

```text
Costco → Shopping
```

but the user changes a Costco transaction to:

```text
Groceries
```

A local correction command should let the user choose:

```text
Apply this change to:

[ Just this transaction ]

[ All transactions from Costco ]
```

These actions mean different things.

## Just This Transaction

Only update the individual transaction.

```text
transaction.category = Groceries
```

The merchant mapping remains:

```text
costco → Shopping
```

Future Costco transactions still use Shopping.

## All Transactions From This Merchant

Update the merchant mapping:

```text
costco → Groceries
```

and update existing Costco transactions to match.

Future Costco transactions will then automatically be classified as Groceries.

This lets the classification system improve through manual corrections without needing a separate user-override architecture.

---

# Classification Flow After Corrections

The resulting system is:

```text
CSV transaction
      ↓
Normalize merchant
      ↓
Check merchant_mappings
      ↓
     Found?
    /      \
 yes        no
 ↓           ↓
Use        Jev
mapping      ↓
        Valid category?
         /          \
       yes           no
        ↓             ↓
   Save mapping   Other + review flag
        ↓         (no mapping saved)
   Use category
        ↓
   Store transaction
```

Both outcomes are stored as transactions. Only the valid Jev result creates a
merchant mapping.

Later:

```text
User edits category
        ↓
One transaction?
        ↓
Update transaction only
```

or:

```text
User edits category
        ↓
All transactions from merchant?
        ↓
Update merchant mapping
        ↓
Update existing transactions
        ↓
Future imports use new category
```

---

# Database

The initial system only needs a few important tables.

## transactions

```text
id
date
raw_description
normalized_merchant
amount
category
classification_source
created_at
```

Possible `classification_source` values:

```text
merchant_mapping
jev
manual
unclassified
```

## merchant\_mappings

```text
id
normalized_merchant
display_name
category
confidence
source
created_at
updated_at
```

Example:

```text
costco
Costco
Groceries
0.92
jev
```

After a manual merchant-level correction, the category and source can be updated accordingly.

## import\_runs

Used to track CSV imports.

```text
id
filename
rows_processed
rows_imported
duplicates_skipped
rows_failed
created_at
```

---

# Duplicate Handling

Importing the same CSV twice should not create duplicate transactions.

Where possible, use a transaction identifier provided by the source.

Otherwise, create a transaction fingerprint from stable fields such as:

```text
date
amount
raw description
source
```

Duplicate detection needs to be conservative because two legitimate transactions could have the same merchant and amount on the same day.

---

# Analytics

Once transactions are stored, Python and SQL should calculate financial metrics deterministically.

Examples:

- spending by category
- spending by month
- top merchants
- category changes over time
- recurring spending
- unusual transactions

AI should not be responsible for calculating these values.

---

# Core Engineering Story

The central idea of the project is:

```text
CSV transactions
       ↓
canonical representation
       ↓
merchant normalization
       ↓
reuse knowledge about known merchants
       ↓
Jev only for unknown merchants
       ↓
persist classifications
       ↓
allow manual corrections
       ↓
learn better merchant mappings over time
       ↓
deterministic spending analytics
```

The interesting part of the project is not simply using an AI model.

It is designing a system around an imperfect classifier that:

- avoids unnecessary model calls
- remembers previous classifications
- handles messy merchant names
- allows humans to correct mistakes
- distinguishes transaction-level corrections from merchant-level corrections
- produces traceable classification decisions

---

# Follow-Up Work

These are design questions that still need to be worked through before the project is considered complete.

### How do we know Jev works?

We need an evaluation strategy rather than assuming Jev's classifications are correct.

Possible direction:

- create a labeled transaction dataset
- run unseen merchants through Jev
- compare predictions against expected categories
- measure classification accuracy
- inspect which categories are commonly confused
- measure how often manual correction is required

This should eventually give us concrete metrics for the classification engine.

### How should merchant normalization work?

We need to decide how aggressively different raw descriptions should be merged.

Example:

```text
COSTCO WHSE #1029
COSTCO WHSE #928
```

should almost certainly both become `costco`.

But aggressive normalization can accidentally merge unrelated merchants.

We need to define the initial normalization rules and test them.

### How should confidence scores be evaluated?

A valid Jev category is always accepted and stored for the merchant. If Jev
provides confidence, retain it for later analysis of classification quality; it
is not an acceptance threshold.

### How should duplicate detection work?

We need a strategy that prevents duplicate imports without incorrectly deleting legitimate repeated purchases.

### What happens if Jev is unavailable?

Assign `Other`, flag the transaction for review, and do not save a merchant
mapping for the failed attempt. A later import can try Jev again. An automatic
retry queue remains a possible follow-up feature.

### How do we handle merchant-level corrections?

When the user selects:

```text
Apply to all transactions from Costco
```

Update every existing Costco transaction and the merchant mapping. Future
imports then use the corrected mapping.

### How do we test the classification engine?

We should eventually have tests covering:

```text
known merchant → no Jev call

unknown merchant → Jev called once

same merchant again → cached mapping

single transaction correction → mapping unchanged

merchant-wide correction → mapping updated

future transactions → corrected mapping used
```
