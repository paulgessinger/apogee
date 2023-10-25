"""empty message

Revision ID: 85865beb3543
Revises: 68f1549ad9aa
Create Date: 2023-10-25 15:35:36.745011

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '85865beb3543'
down_revision = '68f1549ad9aa'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('pull_request', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mergeable', sa.Boolean(), server_default='t', nullable=False))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('pull_request', schema=None) as batch_op:
        batch_op.drop_column('mergeable')

    # ### end Alembic commands ###
