"""Initial SlotPilot schema.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute("""ALTER TABLE bookings ADD CONSTRAINT ex_booking_no_overlap
          EXCLUDE USING gist (specialist_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&)
          WHERE (status = 'confirmed')""")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ex_booking_no_overlap")
    Base.metadata.drop_all(bind=bind)
