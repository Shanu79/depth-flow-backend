"""happy hour v2: subscription-period entitlements, per-session video cap, app settings

Replaces the v1 calendar-month counters on `users` and the v1 sessions table (v1 was never released;
any v1 rows were test data) with:
  * happy_hour_entitlements  (tokens tied to the subscription period)
  * happy_hour_sessions      (reshaped: video cap snapshot + counters, 4 statuses)
  * app_settings             (admin-editable key/value settings)

Revision ID: d3f8b2a61c47
Revises: a7c1e5d2f9b3
Create Date: 2026-11-15 15:00:00.000000

"""
from typing import Sequence, Union
from sqlalchemy.engine.reflection import Inspector
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision: str = 'd3f8b2a61c47'
down_revision: Union[str, Sequence[str], None] = 'a7c1e5d2f9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

V1_USER_COLUMNS = ['happy_hour_last_claim_at', 'happy_hour_claimed', 'happy_hour_tokens',
                   'happy_hour_allocation', 'happy_hour_period']


def _create_entitlements():
    op.create_table(
        'happy_hour_entitlements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('subscription_id', sa.String(), nullable=True),
        sa.Column('subscription_type', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False, server_default='dodo'),
        sa.Column('total_allocated_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('available_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_claimed_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_token_activation_month', sa.String(), nullable=True),
        sa.Column('subscription_start_date', sa.DateTime(), nullable=True),
        sa.Column('subscription_expiry_date', sa.DateTime(), nullable=True),
        sa.Column('entitlement_status', sa.String(), nullable=False, server_default='ACTIVE'),
        sa.Column('last_grant_ref', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint('available_tokens >= 0', name='ck_hh_ent_available_nonneg'),
        sa.CheckConstraint('available_tokens + total_claimed_tokens <= total_allocated_tokens',
                           name='ck_hh_ent_never_exceeds_allocation'),
    )
    op.create_index('ix_happy_hour_entitlements_id', 'happy_hour_entitlements', ['id'])
    op.create_index('ix_happy_hour_entitlements_user_id', 'happy_hour_entitlements', ['user_id'], unique=True)


def _create_sessions():
    op.create_table(
        'happy_hour_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entitlement_id', sa.Integer(),
                  sa.ForeignKey('happy_hour_entitlements.id', ondelete='SET NULL'), nullable=True),
        sa.Column('subscription_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='ACTIVE'),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('max_video_generations', sa.Integer(), nullable=False),
        sa.Column('successful_video_generations', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('in_flight_generations', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('idempotency_key', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_happy_hour_idempotency'),
        sa.CheckConstraint('successful_video_generations + in_flight_generations <= max_video_generations',
                           name='ck_hh_session_within_limit'),
    )
    op.create_index('ix_happy_hour_sessions_id', 'happy_hour_sessions', ['id'])
    op.create_index('ix_happy_hour_sessions_user_id', 'happy_hour_sessions', ['user_id'])
    op.create_index('ix_happy_hour_sessions_user_started', 'happy_hour_sessions', ['user_id', 'started_at'])
    op.create_index('uq_happy_hour_one_active', 'happy_hour_sessions', ['user_id'], unique=True,
                    sqlite_where=text("status = 'ACTIVE'"),
                    postgresql_where=text("status = 'ACTIVE'"))


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    # 1. v1 sessions table has a different shape -> drop and recreate (v1 data was test-only).
    if 'happy_hour_sessions' in tables:
        existing = [c['name'] for c in inspector.get_columns('happy_hour_sessions')]
        if 'max_video_generations' not in existing:
            op.drop_table('happy_hour_sessions')
            tables.remove('happy_hour_sessions')

    # 2. entitlements
    if 'happy_hour_entitlements' not in tables:
        _create_entitlements()

    # 3. sessions
    if 'happy_hour_sessions' not in tables:
        _create_sessions()

    # 4. app settings
    if 'app_settings' not in tables:
        op.create_table(
            'app_settings',
            sa.Column('key', sa.String(), primary_key=True),
            sa.Column('value', sa.String(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        )

    # 5. drop the v1 per-user counters
    user_cols = [c['name'] for c in inspector.get_columns('users')]
    with op.batch_alter_table('users', schema=None) as batch_op:
        for name in V1_USER_COLUMNS:
            if name in user_cols:
                batch_op.drop_column(name)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()
    for t in ('happy_hour_sessions', 'happy_hour_entitlements', 'app_settings'):
        if t in tables:
            op.drop_table(t)

    # restore the v1 shape (columns on users + v1 sessions table)
    user_cols = [c['name'] for c in inspector.get_columns('users')]
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in [
            sa.Column('happy_hour_period', sa.String(), nullable=True),
            sa.Column('happy_hour_allocation', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('happy_hour_tokens', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('happy_hour_claimed', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('happy_hour_last_claim_at', sa.DateTime(), nullable=True),
        ]:
            if col.name not in user_cols:
                batch_op.add_column(col)
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
