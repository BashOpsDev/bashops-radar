"""add paddle billing fields

Revision ID: 8a2c5d6e9f10
Revises: c73ed6e7d90b
Create Date: 2026-07-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a2c5d6e9f10"
down_revision: Union[str, Sequence[str], None] = "c73ed6e7d90b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("paddle_customer_id", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("paddle_subscription_id", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_users_paddle_customer_id"), "users", ["paddle_customer_id"], unique=False)
    op.create_index(op.f("ix_users_paddle_subscription_id"), "users", ["paddle_subscription_id"], unique=False)
    op.drop_index(op.f("ix_users_stripe_subscription_id"), table_name="users")
    op.drop_index(op.f("ix_users_stripe_customer_id"), table_name="users")
    op.drop_column("users", "stripe_subscription_id")
    op.drop_column("users", "stripe_customer_id")


def downgrade() -> None:
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("stripe_subscription_id", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_users_stripe_customer_id"), "users", ["stripe_customer_id"], unique=False)
    op.create_index(op.f("ix_users_stripe_subscription_id"), "users", ["stripe_subscription_id"], unique=False)
    op.drop_index(op.f("ix_users_paddle_subscription_id"), table_name="users")
    op.drop_index(op.f("ix_users_paddle_customer_id"), table_name="users")
    op.drop_column("users", "paddle_subscription_id")
    op.drop_column("users", "paddle_customer_id")
