"""admin managed pricing plans, popups and offer banner

Revision ID: c5d7a1f3e902
Revises: b045d750bfe0
Create Date: 2026-11-20

Idempotent: every table / index is created only if it does not exist yet (the app also runs
Base.metadata.create_all on startup, so a table may already be there).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine.reflection import Inspector

revision: str = 'c5d7a1f3e902'
down_revision: Union[str, None] = 'b045d750bfe0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _indexes(inspector, table):
    return {i['name'] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = Inspector.from_engine(conn)
    tables = inspector.get_table_names()

    if 'pricing_plans' not in tables:
        op.create_table(
            'pricing_plans',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('plan_key', sa.String(), nullable=False),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('billing_interval', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('currency', sa.String(), nullable=False, server_default='USD'),
            sa.Column('credits', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('features', sa.Text(), nullable=True),
            sa.Column('is_most_popular', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('badge_text', sa.String(), nullable=True),
            sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('is_visible', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('pricing_version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint('plan_key', 'billing_interval', name='uq_pricing_plan_key_interval'),
        )
        op.create_index('ix_pricing_plans_id', 'pricing_plans', ['id'])
    if 'uq_pricing_one_popular' not in _indexes(Inspector.from_engine(conn), 'pricing_plans'):
        op.create_index('uq_pricing_one_popular', 'pricing_plans', ['kind', 'billing_interval'], unique=True,
                        sqlite_where=text('is_most_popular = 1'),
                        postgresql_where=text('is_most_popular = true'))

    if 'plan_prices' not in tables:
        op.create_table(
            'plan_prices',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('plan_id', sa.Integer(), sa.ForeignKey('pricing_plans.id', ondelete='CASCADE'), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('price_minor', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(), nullable=False, server_default='USD'),
            sa.Column('credits', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('gateway_product_id', sa.String(), nullable=True, unique=True),
            sa.Column('status', sa.String(), nullable=False, server_default='PENDING'),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('activated_at', sa.DateTime(), nullable=True),
            sa.Column('retired_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('plan_id', 'version', name='uq_plan_price_version'),
        )
        op.create_index('ix_plan_prices_id', 'plan_prices', ['id'])
        op.create_index('ix_plan_prices_plan_id', 'plan_prices', ['plan_id'])
    existing = _indexes(Inspector.from_engine(conn), 'plan_prices')
    if 'uq_plan_price_one_active' not in existing:
        op.create_index('uq_plan_price_one_active', 'plan_prices', ['plan_id'], unique=True,
                        sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'"))
    if 'uq_plan_price_one_pending' not in existing:
        op.create_index('uq_plan_price_one_pending', 'plan_prices', ['plan_id'], unique=True,
                        sqlite_where=text("status = 'PENDING'"), postgresql_where=text("status = 'PENDING'"))

    if 'popups' not in tables:
        op.create_table(
            'popups',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('image_url', sa.String(), nullable=True),
            sa.Column('button_text', sa.String(), nullable=True),
            sa.Column('button_link', sa.String(), nullable=True),
            sa.Column('coupon_code', sa.String(), nullable=True),
            sa.Column('button_color', sa.String(), nullable=True),
            sa.Column('text_color', sa.String(), nullable=True),
            sa.Column('bg_color', sa.String(), nullable=True),
            sa.Column('accent_color', sa.String(), nullable=True),
            sa.Column('header_text', sa.String(), nullable=True),
            sa.Column('footnote', sa.String(), nullable=True),
            sa.Column('layout', sa.String(), nullable=False, server_default='split'),
            sa.Column('show_countdown', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('target_audience', sa.String(), nullable=False, server_default='ALL'),
            sa.Column('start_at', sa.DateTime(), nullable=True),
            sa.Column('end_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(), nullable=False, server_default='INACTIVE'),
            sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
        )
        op.create_index('ix_popups_id', 'popups', ['id'])
    have = {c['name'] for c in Inspector.from_engine(conn).get_columns('popups')}
    for name, col in (
        ('button_color', sa.Column('button_color', sa.String(), nullable=True)),
        ('text_color', sa.Column('text_color', sa.String(), nullable=True)),
        ('bg_color', sa.Column('bg_color', sa.String(), nullable=True)),
        ('accent_color', sa.Column('accent_color', sa.String(), nullable=True)),
        ('header_text', sa.Column('header_text', sa.String(), nullable=True)),
        ('footnote', sa.Column('footnote', sa.String(), nullable=True)),
        ('layout', sa.Column('layout', sa.String(), nullable=False, server_default='split')),
        ('show_countdown', sa.Column('show_countdown', sa.Boolean(), nullable=False, server_default=sa.false())),
    ):
        if name not in have:
            op.add_column('popups', col)
    if 'offer_banners' in tables:
        have_b = {c['name'] for c in Inspector.from_engine(conn).get_columns('offer_banners')}
        for name, col in (
            ('button_color', sa.Column('button_color', sa.String(), nullable=True)),
            ('text_color', sa.Column('text_color', sa.String(), nullable=True)),
            ('bg_color', sa.Column('bg_color', sa.String(), nullable=True)),
            ('accent_color', sa.Column('accent_color', sa.String(), nullable=True)),
            ('header_text', sa.Column('header_text', sa.String(), nullable=True)),
            ('badge_text', sa.Column('badge_text', sa.String(), nullable=True)),
            ('show_countdown', sa.Column('show_countdown', sa.Boolean(), nullable=False, server_default=sa.false())),
        ):
            if name not in have_b:
                op.add_column('offer_banners', col)
    if 'ix_popups_status_window' not in _indexes(Inspector.from_engine(conn), 'popups'):
        op.create_index('ix_popups_status_window', 'popups', ['status', 'start_at', 'end_at'])

    if 'popup_displays' not in tables:
        op.create_table(
            'popup_displays',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('popup_id', sa.Integer(), sa.ForeignKey('popups.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('displayed_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('display_date', sa.Date(), nullable=False),
            sa.UniqueConstraint('popup_id', 'user_id', 'display_date', name='uq_popup_user_day'),
        )
        op.create_index('ix_popup_displays_id', 'popup_displays', ['id'])
        op.create_index('ix_popup_displays_popup_id', 'popup_displays', ['popup_id'])
        op.create_index('ix_popup_displays_user_id', 'popup_displays', ['user_id'])

    if 'offer_banners' not in tables:
        op.create_table(
            'offer_banners',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('image_url', sa.String(), nullable=True),
            sa.Column('button_text', sa.String(), nullable=True),
            sa.Column('button_link', sa.String(), nullable=True),
            sa.Column('coupon_code', sa.String(), nullable=True),
            sa.Column('button_color', sa.String(), nullable=True),
            sa.Column('text_color', sa.String(), nullable=True),
            sa.Column('bg_color', sa.String(), nullable=True),
            sa.Column('accent_color', sa.String(), nullable=True),
            sa.Column('header_text', sa.String(), nullable=True),
            sa.Column('badge_text', sa.String(), nullable=True),
            sa.Column('show_countdown', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('start_at', sa.DateTime(), nullable=True),
            sa.Column('end_at', sa.DateTime(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
        )
        op.create_index('ix_offer_banners_id', 'offer_banners', ['id'])

    # festive styling columns (both tables), added whether the tables were just created or already existed
    for table in ('popups', 'offer_banners'):
        if table in Inspector.from_engine(conn).get_table_names():
            have_s = {c['name'] for c in Inspector.from_engine(conn).get_columns(table)}
            for name, col in (
                ('bg_color2', sa.Column('bg_color2', sa.String(), nullable=True)),
                ('theme', sa.Column('theme', sa.String(), nullable=False, server_default='none')),
                ('emoji', sa.Column('emoji', sa.String(), nullable=True)),
                ('font_style', sa.Column('font_style', sa.String(), nullable=False, server_default='modern')),
                ('bg_image_url', sa.Column('bg_image_url', sa.String(), nullable=True)),
                ('display_mode', sa.Column('display_mode', sa.String(), nullable=False, server_default='designed')),
            ):
                if name not in have_s:
                    op.add_column(table, col)
            if table == 'offer_banners' and 'mobile_image_url' not in have_s:
                op.add_column(table, sa.Column('mobile_image_url', sa.String(), nullable=True))

    if 'promo_templates' not in Inspector.from_engine(conn).get_table_names():
        op.create_table(
            'promo_templates',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('emoji', sa.String(), nullable=True),
            sa.Column('data', sa.Text(), nullable=False),
            sa.Column('end_in_days', sa.Integer(), nullable=True),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
        )
        op.create_index('ix_promo_templates_id', 'promo_templates', ['id'])
        op.create_index('ix_promo_templates_kind', 'promo_templates', ['kind'])

    if 'whats_new_releases' not in Inspector.from_engine(conn).get_table_names():
        op.create_table(
            'whats_new_releases',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('tab_label', sa.String(), nullable=False),
            sa.Column('badge_text', sa.String(), nullable=True),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('subtitle', sa.Text(), nullable=True),
            sa.Column('features', sa.Text(), nullable=False, server_default='[]'),
            sa.Column('button1_text', sa.String(), nullable=True),
            sa.Column('button1_link', sa.String(), nullable=True),
            sa.Column('button1_style', sa.String(), nullable=False, server_default='light'),
            sa.Column('button2_text', sa.String(), nullable=True),
            sa.Column('button2_link', sa.String(), nullable=True),
            sa.Column('auto_show', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('status', sa.String(), nullable=False, server_default='INACTIVE'),
            sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_by_user_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp()),
        )
        op.create_index('ix_whats_new_releases_id', 'whats_new_releases', ['id'])


def downgrade() -> None:
    conn = op.get_bind()
    tables = Inspector.from_engine(conn).get_table_names()
    for t in ('whats_new_releases', 'promo_templates', 'offer_banners', 'popup_displays', 'popups', 'plan_prices', 'pricing_plans'):
        if t in tables:
            op.drop_table(t)
