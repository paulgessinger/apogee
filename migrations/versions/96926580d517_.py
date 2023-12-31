"""empty message

Revision ID: 96926580d517
Revises: 85865beb3543
Create Date: 2023-10-25 16:35:29.626365

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "96926580d517"
down_revision = "85865beb3543"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("patch", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("pull_request_number", sa.Integer(), nullable=True)
        )
        batch_op.alter_column(
            "commit_sha", existing_type=sa.VARCHAR(length=40), nullable=True
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_patch_pull_request_number_pull_request"),
            "pull_request",
            ["pull_request_number"],
            ["number"],
        )

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("patch", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_patch_pull_request_number_pull_request"), type_="foreignkey"
        )
        batch_op.alter_column(
            "commit_sha", existing_type=sa.VARCHAR(length=40), nullable=False
        )
        batch_op.drop_column("pull_request_number")

    # ### end Alembic commands ###
