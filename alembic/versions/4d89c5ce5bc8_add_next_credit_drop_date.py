"""add_next_credit_drop_date and play_purchase_token

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
        # 1. Commented out because this column already exists in your DB!
        # batch_op.add_column(sa.Column('next_credit_drop_date', sa.DateTime(), nullable=True))
        
        # 2. Manually add the missing Google Play token column
        batch_op.add_column(sa.Column('play_purchase_token', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_play_purchase_token'), ['play_purchase_token'], unique=False)

def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        # Handle the downgrade cleanly
        batch_op.drop_index(batch_op.f('ix_users_play_purchase_token'))
        batch_op.drop_column('play_purchase_token')
        # batch_op.drop_column('next_credit_drop_date')