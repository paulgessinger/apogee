"""empty message

Revision ID: dd528d904ea8
Revises: 478cd764b542
Create Date: 2023-10-25 15:01:56.671974

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'dd528d904ea8'
down_revision = '478cd764b542'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('pipeline', schema=None) as batch_op:
        batch_op.add_column(sa.Column('refreshed_at', sa.DateTime(), nullable=False))

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('pipeline', schema=None) as batch_op:
        batch_op.drop_column('refreshed_at')

    # ### end Alembic commands ###
