"""Enable row security for Supabase's public Data API.

Revision ID: 398c7da801ab
Revises: 259ae3b10874
"""

from alembic import op

revision = "398c7da801ab"
down_revision = "259ae3b10874"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        # No public policies: the backend connects directly as a privileged owner.
        for table in ("transactions", "merchant_mappings", "import_runs", "alembic_version"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in ("transactions", "merchant_mappings", "import_runs", "alembic_version"):
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
