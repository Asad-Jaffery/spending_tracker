from alembic import context

from spend_tracker.config import Settings
from spend_tracker.db import build_engine
from spend_tracker.models import Base

settings = Settings()
engine = build_engine(settings.database_url.get_secret_value())

if context.is_offline_mode():
    context.configure(
        url=engine.url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
engine.dispose()
