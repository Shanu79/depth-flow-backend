"""force add play purchase token

Revision ID: b045d750bfe0
Revises: 4d89c5ce5bc8
Create Date: 2026-07-12 13:12:20.938553

"""
from typing import Sequence, Union
from sqlalchemy.engine.reflection import Inspector
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b045d750bfe0'
down_revision: Union[str, Sequence[str], None] = '4d89c5ce5bc8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    
    # Get a list of all existing columns in the 'users' table
    columns = [col['name'] for col in inspector.get_columns('users')]
    
    # Only add the column if it doesn't already exist (e.g., on Production)
    if 'play_purchase_token' not in columns:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('play_purchase_token', sa.String(), nullable=True))
            batch_op.create_index(batch_op.f('ix_users_play_purchase_token'), ['play_purchase_token'], unique=False)

def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    columns = [col['name'] for col in inspector.get_columns('users')]
    
    if 'play_purchase_token' in columns:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_users_play_purchase_token'))
            batch_op.drop_column('play_purchase_token')