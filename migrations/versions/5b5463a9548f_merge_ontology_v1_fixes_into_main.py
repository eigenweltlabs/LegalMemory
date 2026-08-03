"""merge ontology-v1-fixes into main

Revision ID: 5b5463a9548f
Revises: 9b1f4c7e6a02, 5b1f7c2e8a41
Create Date: 2026-07-31 13:36:46.223460

"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '5b5463a9548f'
down_revision: Union[str, Sequence[str], None] = ('9b1f4c7e6a02', '5b1f7c2e8a41')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
