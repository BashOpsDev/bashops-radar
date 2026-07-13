"""add maintainer validation

Revision ID: d5f6a7b8c9d0
Revises: b7c3d4e5f6a1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "b7c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("maintainer_pilot_access", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "maintainer_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("repository_full_name", sa.String(length=255), nullable=False),
        sa.Column("repository_url", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("analyzed_issue_count", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("is_partial", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("plan_context", sa.String(length=50), nullable=False),
        sa.Column("analysis_version", sa.String(length=50), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_maintainer_analyses_user_id"), "maintainer_analyses", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_maintainer_analyses_repository_full_name"),
        "maintainer_analyses",
        ["repository_full_name"],
        unique=False,
    )
    op.create_index(op.f("ix_maintainer_analyses_ip_address"), "maintainer_analyses", ["ip_address"], unique=False)
    op.create_index(op.f("ix_maintainer_analyses_created_at"), "maintainer_analyses", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_maintainer_analyses_created_at"), table_name="maintainer_analyses")
    op.drop_index(op.f("ix_maintainer_analyses_ip_address"), table_name="maintainer_analyses")
    op.drop_index(op.f("ix_maintainer_analyses_repository_full_name"), table_name="maintainer_analyses")
    op.drop_index(op.f("ix_maintainer_analyses_user_id"), table_name="maintainer_analyses")
    op.drop_table("maintainer_analyses")
    op.drop_column("users", "maintainer_pilot_access")
