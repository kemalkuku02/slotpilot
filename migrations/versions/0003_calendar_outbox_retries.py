"""Schedule retries for failed calendar outbox items.

Revision ID: 0003_calendar_outbox_retries
Revises: 0002_pending_confirmation
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_calendar_outbox_retries"
down_revision = "0002_pending_confirmation"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns("calendar_outbox")}

    if "next_attempt_at" not in columns:
        op.add_column("calendar_outbox", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    if "last_error" not in columns:
        op.add_column("calendar_outbox", sa.Column("last_error", sa.String(length=100), nullable=True))

    op.execute(sa.text(
        "UPDATE calendar_outbox SET next_attempt_at = COALESCE(next_attempt_at, created_at, CURRENT_TIMESTAMP) "
        "WHERE next_attempt_at IS NULL"
    ))

    inspector = sa.inspect(bind)
    next_attempt = next(column for column in inspector.get_columns("calendar_outbox")
                        if column["name"] == "next_attempt_at")
    if next_attempt["nullable"] or next_attempt["default"] is None:
        with op.batch_alter_table("calendar_outbox") as batch:
            batch.alter_column(
                "next_attempt_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("calendar_outbox")}
    if "ix_calendar_outbox_due" not in indexes:
        op.create_index("ix_calendar_outbox_due", "calendar_outbox",
                        ["processed_at", "next_attempt_at", "id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ix_calendar_outbox_due" in {index["name"] for index in inspector.get_indexes("calendar_outbox")}:
        op.drop_index("ix_calendar_outbox_due", table_name="calendar_outbox")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("calendar_outbox")}
    if "last_error" in columns or "next_attempt_at" in columns:
        with op.batch_alter_table("calendar_outbox") as batch:
            if "last_error" in columns:
                batch.drop_column("last_error")
            if "next_attempt_at" in columns:
                batch.drop_column("next_attempt_at")
