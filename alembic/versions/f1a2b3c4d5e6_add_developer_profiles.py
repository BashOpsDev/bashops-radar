"""add developer profiles

Revision ID: f1a2b3c4d5e6
Revises: e8f1a2b3c4d5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e8f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "developer_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("github_username", sa.String(length=39), nullable=False),
        sa.Column("github_user_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("public_location", sa.String(length=255), nullable=True),
        sa.Column("profile_url", sa.String(length=500), nullable=False),
        sa.Column("profile_data", sa.JSON(), nullable=False),
        sa.Column("strength_data", sa.JSON(), nullable=False),
        sa.Column("contribution_data", sa.JSON(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_claimed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("public_slug", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_developer_profiles_user_id"), "developer_profiles", ["user_id"], unique=True)
    op.create_index(
        op.f("ix_developer_profiles_github_username"),
        "developer_profiles",
        ["github_username"],
        unique=True,
    )
    op.create_index(
        op.f("ix_developer_profiles_github_user_id"),
        "developer_profiles",
        ["github_user_id"],
        unique=True,
    )
    op.create_index(op.f("ix_developer_profiles_expires_at"), "developer_profiles", ["expires_at"], unique=False)
    op.create_index(
        op.f("ix_developer_profiles_public_slug"),
        "developer_profiles",
        ["public_slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_developer_profiles_public_slug"), table_name="developer_profiles")
    op.drop_index(op.f("ix_developer_profiles_expires_at"), table_name="developer_profiles")
    op.drop_index(op.f("ix_developer_profiles_github_user_id"), table_name="developer_profiles")
    op.drop_index(op.f("ix_developer_profiles_github_username"), table_name="developer_profiles")
    op.drop_index(op.f("ix_developer_profiles_user_id"), table_name="developer_profiles")
    op.drop_table("developer_profiles")
