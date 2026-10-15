"""Optional PostgreSQL storage using five durable tables.

The service layer still owns classification rules. Each save replaces the
single-user snapshot inside one database transaction.
"""

import json

from spend_tracker.storage import empty_state, migrate_categories


class PostgresStore:
    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install the Postgres extra: pip install -e '.[postgres]'") from exc
        self.url = url
        self.psycopg = psycopg
        self._initialize()

    def _connect(self):
        return self.psycopg.connect(self.url)

    def _initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS spend_tracker_transactions "
                "(id uuid PRIMARY KEY, payload jsonb NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS spend_tracker_merchant_mappings "
                "(normalized_merchant text PRIMARY KEY, payload jsonb NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS spend_tracker_import_runs "
                "(id uuid PRIMARY KEY, payload jsonb NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS spend_tracker_csv_column_mappings "
                "(source text PRIMARY KEY, payload jsonb NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS spend_tracker_import_profiles "
                "(id text PRIMARY KEY, payload jsonb NOT NULL)"
            )

    def load(self) -> dict:
        state = empty_state()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM spend_tracker_transactions "
                "ORDER BY (payload->>'created_at'), id"
            )
            state["transactions"] = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT normalized_merchant, payload FROM spend_tracker_merchant_mappings"
            )
            state["merchant_mappings"] = {key: payload for key, payload in cursor.fetchall()}
            cursor.execute(
                "SELECT payload FROM spend_tracker_import_runs "
                "ORDER BY (payload->>'created_at'), id"
            )
            state["import_runs"] = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT source, payload FROM spend_tracker_csv_column_mappings")
            state["csv_column_mappings"] = {
                source: payload for source, payload in cursor.fetchall()
            }
            cursor.execute("SELECT id, payload FROM spend_tracker_import_profiles ORDER BY id")
            state["import_profiles"] = dict(cursor.fetchall())
        return migrate_categories(state)

    def save(self, state: dict) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(832917422)")
            cursor.execute("DELETE FROM spend_tracker_transactions")
            cursor.execute("DELETE FROM spend_tracker_merchant_mappings")
            cursor.execute("DELETE FROM spend_tracker_import_runs")
            cursor.execute("DELETE FROM spend_tracker_csv_column_mappings")
            cursor.execute("DELETE FROM spend_tracker_import_profiles")
            cursor.executemany(
                "INSERT INTO spend_tracker_transactions (id, payload) VALUES (%s, %s::jsonb)",
                [(item["id"], json.dumps(item)) for item in state["transactions"]],
            )
            cursor.executemany(
                "INSERT INTO spend_tracker_merchant_mappings (normalized_merchant, payload) "
                "VALUES (%s, %s::jsonb)",
                [(key, json.dumps(item)) for key, item in state["merchant_mappings"].items()],
            )
            cursor.executemany(
                "INSERT INTO spend_tracker_import_runs (id, payload) VALUES (%s, %s::jsonb)",
                [(item["id"], json.dumps(item)) for item in state["import_runs"]],
            )
            cursor.executemany(
                "INSERT INTO spend_tracker_csv_column_mappings (source, payload) "
                "VALUES (%s, %s::jsonb)",
                [
                    (source, json.dumps(mapping))
                    for source, mapping in state["csv_column_mappings"].items()
                ],
            )
            cursor.executemany(
                "INSERT INTO spend_tracker_import_profiles (id, payload) VALUES (%s, %s::jsonb)",
                [
                    (key, json.dumps(profile))
                    for key, profile in state.get("import_profiles", {}).items()
                ],
            )
