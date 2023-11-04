"""Add session table

Revision ID: 76d7ab19c1bf
Revises: 96926580d517
Create Date: 2023-11-04 14:22:42.134571

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


# revision identifiers, used by Alembic.
revision = "76d7ab19c1bf"
down_revision = "96926580d517"
branch_labels = None
depends_on = None


def upgrade():

    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if "session" not in tables:
        op.create_table(
            "sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.String(length=255)),
            sa.Column("data", sa.LargeBinary),
            sa.Column("expiry", sa.DateTime(timezone=False), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        )


def downgrade():
    op.drop_table("sessions")
