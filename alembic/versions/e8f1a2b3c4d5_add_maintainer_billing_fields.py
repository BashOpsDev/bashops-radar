"""add maintainer billing fields

Revision ID: e8f1a2b3c4d5
Revises: d5f6a7b8c9d0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("maintainer_paddle_subscription_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("maintainer_subscription_status", sa.String(length=50), nullable=True),
    )
    op.create_index(
        op.f("ix_users_maintainer_paddle_subscription_id"),
        "users",
        ["maintainer_paddle_subscription_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_maintainer_paddle_subscription_id"), table_name="users")
    op.drop_column("users", "maintainer_subscription_status")
    op.drop_column("users", "maintainer_paddle_subscription_id")
