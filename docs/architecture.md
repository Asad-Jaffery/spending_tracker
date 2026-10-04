# MVP Architecture

The [project spec](project-spec.md) describes the eventual product. This MVP runs as a local command and stores state in JSON.

```text
CSV / synthetic JSON
       ↓
parser → canonical transaction (date, description, Decimal amount, optional ID)
       ↓
merchant normalizer → stable merchant key
       ↓
classification engine → saved mapping? use it
                    ↘ unknown? call jevmodel.org once per merchant
                              ↘ valid choice: save mapping
                              ↘ failure: Other + review, no mapping
       ↓
JSON store → transactions, merchant mappings, import runs
       ↓
CLI corrections and deterministic spending summary
```

`parsing.py` owns input validation. `normalization.py` conservatively converts raw descriptions to merchant keys. `classifiers.py` owns the Jev HTTP request and response parsing. `services.py` applies the classification, duplicate, correction, and analytics rules. `storage.py` owns JSON loading and atomic replacement. `__main__.py` is the command interface.

The JSON file is a deliberate temporary store. When Supabase/Postgres is added, replace the persistence boundary with database reads and transactional writes. Keep the classification and correction policies unchanged. There is no active database connector or HTTP server.
