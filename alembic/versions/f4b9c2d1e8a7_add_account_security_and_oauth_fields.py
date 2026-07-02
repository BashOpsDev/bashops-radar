"""add account security and oauth fields

Revision ID: f4b9c2d1e8a7
Revises: 8a2c5d6e9f10
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4b9c2d1e8a7"
down_revision: Union[str, Sequence[str], None] = "8a2c5d6e9f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("email_verification_token", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("email_verification_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("password_reset_token", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("password_reset_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("marketing_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("marketing_opt_in_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("github_id", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("github_username", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("auth_provider", sa.String(length=50), nullable=True, server_default="email"))
    op.create_index(op.f("ix_users_email_verification_token"), "users", ["email_verification_token"], unique=False)
    op.create_index(op.f("ix_users_password_reset_token"), "users", ["password_reset_token"], unique=False)
    op.create_index(op.f("ix_users_github_id"), "users", ["github_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_github_id"), table_name="users")
    op.drop_index(op.f("ix_users_password_reset_token"), table_name="users")
    op.drop_index(op.f("ix_users_email_verification_token"), table_name="users")
    op.drop_column("users", "auth_provider")
    op.drop_column("users", "github_username")
    op.drop_column("users", "github_id")
    op.drop_column("users", "marketing_opt_in_at")
    op.drop_column("users", "marketing_opt_in")
    op.drop_column("users", "password_reset_sent_at")
    op.drop_column("users", "password_reset_token")
    op.drop_column("users", "email_verification_sent_at")
    op.drop_column("users", "email_verification_token")
    op.drop_column("users", "email_verified")
