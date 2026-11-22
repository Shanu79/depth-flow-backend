"""notifications: inbox, device tokens, broadcasts (+ sweep index on happy hour sessions)

Revision ID: e9b4c7a2d815
Revises: d3f8b2a61c47
Create Date: 2026-11-15 18:00:00.000000

"""
from typing import Sequence, Union
from sqlalchemy.engine.reflection import Inspector
from alembic import op
import sqlalchemy as sa


revision: str = 'e9b4c7a2d815'
down_revision: Union[str, Sequence[str], None] = 'd3f8b2a61c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'notifications' not in tables:
        op.create_table(
            'notifications',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('type', sa.String(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('body', sa.String(), nullable=False),
            sa.Column('data', sa.Text(), nullable=True),
            sa.Column('dedupe_key', sa.String(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('read_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('user_id', 'dedupe_key', name='uq_notification_dedupe'),
        )
        op.create_index('ix_notifications_id', 'notifications', ['id'])
        op.create_index('ix_notifications_user_created', 'notifications', ['user_id', 'created_at'])
        op.create_index('ix_notifications_user_read', 'notifications', ['user_id', 'read_at'])

    if 'device_tokens' not in tables:
        op.create_table(
            'device_tokens',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('token', sa.String(), nullable=False, unique=True),
            sa.Column('platform', sa.String(), nullable=False, server_default='android'),
            sa.Column('app_version', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('last_seen_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('disabled_at', sa.DateTime(), nullable=True),
        )
        op.create_index('ix_device_tokens_id', 'device_tokens', ['id'])
        op.create_index('ix_device_tokens_user_id', 'device_tokens', ['user_id'])

    if 'notification_broadcasts' not in tables:
        op.create_table(
            'notification_broadcasts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('body', sa.String(), nullable=False),
            sa.Column('audience', sa.String(), nullable=False),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('recipient_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('device_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        )
        op.create_index('ix_notification_broadcasts_id', 'notification_broadcasts', ['id'])

    if 'happy_hour_sessions' in tables:
        existing = [i['name'] for i in inspector.get_indexes('happy_hour_sessions')]
        if 'ix_happy_hour_sessions_status_expires' not in existing:
            op.create_index('ix_happy_hour_sessions_status_expires', 'happy_hour_sessions', ['status', 'expires_at'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()
    if 'happy_hour_sessions' in tables:
        existing = [i['name'] for i in inspector.get_indexes('happy_hour_sessions')]
        if 'ix_happy_hour_sessions_status_expires' in existing:
            op.drop_index('ix_happy_hour_sessions_status_expires', table_name='happy_hour_sessions')
    for t in ('notification_broadcasts', 'device_tokens', 'notifications'):
        if t in tables:
            op.drop_table(t)
