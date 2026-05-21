"""add_next_credit_drop_date

Revision ID: 4d89c5ce5bc8
Revises: 6fceaf143bbe
Create Date: 2026-05-21 20:56:29.588555

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d89c5ce5bc8'
down_revision: Union[str, Sequence[str], None] = '6fceaf143bbe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_credit_drop_date', sa.DateTime(), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('next_credit_drop_date')
