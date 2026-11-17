"""add happy hour tokens and sessions

Revision ID: a7c1e5d2f9b3
Revises: b045d750bfe0
Create Date: 2026-11-15 12:00:00.000000

"""
from typing import Sequence, Union
from sqlalchemy.engine.reflection import Inspector
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision: str = 'a7c1e5d2f9b3'
down_revision: Union[str, Sequence[str], None] = 'b045d750bfe0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)

    # --- users columns (idempotent: create_all / earlier partial runs may have added them) ---
    columns = [c['name'] for c in inspector.get_columns('users')]
    new_cols = [
        sa.Column('happy_hour_period', sa.String(), nullable=True),
        sa.Column('happy_hour_allocation', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('happy_hour_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('happy_hour_claimed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('happy_hour_last_claim_at', sa.DateTime(), nullable=True),
    ]
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in new_cols:
            if col.name not in columns:
                batch_op.add_column(col)

    # --- sessions table ---
    if 'happy_hour_sessions' not in inspector.get_table_names():
        op.create_table(
            'happy_hour_sessions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('status', sa.String(), nullable=False),
            sa.Column('token_period', sa.String(), nullable=False),
            sa.Column('claimed_at', sa.DateTime(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('idempotency_key', sa.String(), nullable=True),
            sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_happy_hour_idempotency'),
        )
        op.create_index('ix_happy_hour_sessions_id', 'happy_hour_sessions', ['id'])
        op.create_index('ix_happy_hour_sessions_user_id', 'happy_hour_sessions', ['user_id'])
        op.create_index('uq_happy_hour_one_active', 'happy_hour_sessions', ['user_id'], unique=True,
                        sqlite_where=text("status = 'ACTIVE'"),
                        postgresql_where=text("status = 'ACTIVE'"))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    if 'happy_hour_sessions' in inspector.get_table_names():
        op.drop_table('happy_hour_sessions')
    columns = [c['name'] for c in inspector.get_columns('users')]
    with op.batch_alter_table('users', schema=None) as batch_op:
        for name in ['happy_hour_last_claim_at', 'happy_hour_claimed', 'happy_hour_tokens',
                     'happy_hour_allocation', 'happy_hour_period']:
            if name in columns:
                batch_op.drop_column(name)
