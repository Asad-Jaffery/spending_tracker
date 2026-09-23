from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from spend_tracker.db import build_engine


def test_migrations_upgrade_downgrade_and_match_models(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = build_engine(database_url)
    assert {"transactions", "merchant_mappings", "import_runs"}.issubset(
        inspect(engine).get_table_names()
    )
    command.check(config)
    command.downgrade(config, "base")
    assert "transactions" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    engine.dispose()
