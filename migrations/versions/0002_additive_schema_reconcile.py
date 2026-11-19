"""Additive schema update for databases that already applied dynamic 0001.

Revision ID: 0002_additive_schema
Revises: 0001_initial
"""

from decimal import Decimal

from alembic import op
from sqlalchemy import JSON, DateTime, inspect, text
from sqlalchemy.schema import DefaultClause, UniqueConstraint

from vpi_bot.models import Base

revision = "0002_additive_schema"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _default_sql(column) -> str | None:
    if column.server_default is not None:
        return str(column.server_default.arg)
    default = column.default
    if default is None:
        return None
    value = default.arg
    if value is dict:
        return "'{}'"
    if value is list:
        return "'[]'"
    if callable(value):
        return "CURRENT_TIMESTAMP" if isinstance(column.type, DateTime) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(column.type, JSON):
        return "'{}'"
    return None


def _add_missing_columns(bind, table) -> None:
    inspector = inspect(bind)
    existing = {column["name"] for column in inspector.get_columns(table.name)}
    for column in table.columns:
        if column.name in existing:
            continue
        if column.primary_key:
            raise RuntimeError(f"Cannot safely add missing primary key {table.name}.{column.name}")
        addition = column._copy()
        # Unique/index constraints are reconciled separately after the column exists.
        addition.unique = False
        addition.index = False
        default = _default_sql(column)
        sqlite_timestamp_backfill = (
            bind.dialect.name == "sqlite"
            and default == "CURRENT_TIMESTAMP"
            and not addition.nullable
        )
        if not addition.nullable and addition.server_default is None:
            if default is None:
                raise RuntimeError(
                    f"Missing non-null column {table.name}.{column.name} has no safe backfill default"
                )
            if sqlite_timestamp_backfill:
                addition.nullable = True
            else:
                addition.server_default = DefaultClause(text(default))
        op.add_column(table.name, addition)
        if sqlite_timestamp_backfill:
            preparer = bind.dialect.identifier_preparer
            quoted_table = preparer.quote(table.name)
            quoted_column = preparer.quote(column.name)
            op.execute(
                text(
                    f"UPDATE {quoted_table} SET {quoted_column} = CURRENT_TIMESTAMP "
                    f"WHERE {quoted_column} IS NULL"
                )
            )
            with op.batch_alter_table(table.name) as batch:
                batch.alter_column(column.name, existing_type=column.type, nullable=False)
        existing.add(column.name)


def _add_missing_unique_constraints(bind, table) -> None:
    inspector = inspect(bind)
    existing = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(table.name)
    }
    existing.update(
        tuple(index.get("column_names") or ())
        for index in inspector.get_indexes(table.name)
        if index.get("unique")
    )
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = tuple(column.name for column in constraint.columns)
        if columns in existing:
            continue
        name = constraint.name or f"uq_{table.name}_{'_'.join(columns)}"
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table.name) as batch:
                batch.create_unique_constraint(name, list(columns))
        else:
            op.create_unique_constraint(name, table.name, list(columns))
        existing.add(columns)


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 used live metadata; this creates newly introduced tables on old installations.
    for table in Base.metadata.sorted_tables:
        table.create(bind=bind, checkfirst=True)
    for table in Base.metadata.sorted_tables:
        _add_missing_columns(bind, table)
    for table in Base.metadata.sorted_tables:
        _add_missing_unique_constraints(bind, table)
        for index in table.indexes:
            index.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Additive reconciliation intentionally preserves data when moving the revision marker back.
    pass
