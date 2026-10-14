# Prototype Architecture

```text
CSV / synthetic JSON
       ↓
parser and validation → canonical transaction (date, description, Decimal amount, optional ID)
       ↓
merchant normalizer → conservative merchant key
       ↓
classification engine → saved mapping? use it
                    ↘ unknown? call Jev once per merchant
                              ↘ valid choice: save mapping
                              ↘ failure: Other + review, no mapping
       ↓
JSON store or PostgreSQL → transactions, merchant mappings, import runs
       ↓
FastAPI / CLI → analytics and manual corrections
       ↑
React + TypeScript dashboard (Vite in development)
```

`parsing.py` validates CSV and JSON inputs. `normalization.py` produces merchant keys without hardcoded aliases. `classifiers.py` owns Jev's authenticated HTTP request and response validation. `services.py` applies classification, duplicate, correction, and summary rules. `analytics.py` calculates the dashboard's month-specific metrics. `storage.py` is the default local JSON store; `postgres.py` provides an optional PostgreSQL store. `webapp.py` defines the FastAPI endpoints and serves a built React app when available. `frontend/` contains the React/TypeScript dashboard; Vite proxies API calls during development. `synthetic.py` loads the explicitly labeled May and June demo data.

A saved merchant mapping is a default for **new** transactions. The category displayed on a stored transaction is its own value. Correcting one transaction leaves the mapping alone; a merchant-wide correction updates the mapping and all existing matching transactions. A valid Jev category always becomes a mapping regardless of confidence. Failed classifications create a review item without a mapping.

Both stores expose `load()` and `save()` to the service layer. JSON saves via atomic file replacement. PostgreSQL uses three JSONB-backed tables and saves a single-user snapshot in one transaction. The local server serializes writes with a process lock. This prototype does not provide multi-user concurrency or a production database migration system.
