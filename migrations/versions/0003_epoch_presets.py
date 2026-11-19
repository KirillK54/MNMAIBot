"""Add era rules and registration starting statistics.

Revision ID: 0003_epoch_presets
Revises: 0002_additive_schema
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "0003_epoch_presets"
down_revision = "0002_additive_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("epochs")}
    if "rules" not in columns:
        op.add_column(
            "epochs",
            sa.Column("rules", sa.Text(), nullable=False, server_default=text("''")),
        )
    if "starting_stats" not in columns:
        op.add_column(
            "epochs",
            sa.Column("starting_stats", sa.JSON(), nullable=False, server_default=text("'{}'")),
        )


def downgrade() -> None:
    # Keep epoch data and schema intact when reverting the migration marker.
    pass
