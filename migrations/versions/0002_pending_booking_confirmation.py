"""Persist an offer until the client confirms it.

Revision ID: 0002_pending_confirmation
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_pending_confirmation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "calendar_event_id" not in {c["name"] for c in inspector.get_columns("bookings")}:
        op.add_column("bookings", sa.Column("calendar_event_id", sa.String(length=200), nullable=True))
    if "pending_bookings" not in inspector.get_table_names():
        op.create_table(
            "pending_bookings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
            sa.Column("service_id", sa.String(length=80), nullable=False),
            sa.Column("specialist_id", sa.String(length=80), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("quoted_price", sa.Integer(), nullable=False),
            sa.Column("client_name", sa.String(length=200), nullable=False),
            sa.Column("client_phone", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_pending_bookings_conversation_id", "pending_bookings", ["conversation_id"])
        op.create_index("ix_pending_bookings_client_id", "pending_bookings", ["client_id"])
        op.create_index("ix_pending_bookings_status", "pending_bookings", ["status"])


def downgrade():
    op.drop_table("pending_bookings")
    op.drop_column("bookings", "calendar_event_id")
