"""empty message

Revision ID: 8c4988c0836b
Revises: 6b47df420867
Create Date: 2023-10-17 10:09:52.715699

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8c4988c0836b"
down_revision = "6b47df420867"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "pull_request",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("html_url", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("merge_commit_sha", sa.String(), nullable=True),
        sa.Column("head_label", sa.String(), nullable=False),
        sa.Column("head_ref", sa.String(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("head_user_id", sa.Integer(), nullable=False),
        sa.Column("head_repo_full_name", sa.String(), nullable=False),
        sa.Column("base_label", sa.String(), nullable=False),
        sa.Column("base_ref", sa.String(), nullable=False),
        sa.Column("base_sha", sa.String(), nullable=False),
        sa.Column("base_user_id", sa.Integer(), nullable=False),
        sa.Column("base_repo_full_name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["base_user_id"],
            ["user.id"],
            name=op.f("fk_pull_request_base_user_id_user"),
        ),
        sa.ForeignKeyConstraint(
            ["head_user_id"],
            ["user.id"],
            name=op.f("fk_pull_request_head_user_id_user"),
        ),
        sa.PrimaryKeyConstraint("number", name=op.f("pk_pull_request")),
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("pull_request")
    # ### end Alembic commands ###
