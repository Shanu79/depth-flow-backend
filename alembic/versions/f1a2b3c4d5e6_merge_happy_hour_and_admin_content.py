"""merge happy hour and admin content

Revision ID: f1a2b3c4d5e6
Revises: c5d7a1f3e902, e9b4c7a2d815
Create Date: 2026-11-23 01:15:09.337707

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = ('c5d7a1f3e902', 'e9b4c7a2d815')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
