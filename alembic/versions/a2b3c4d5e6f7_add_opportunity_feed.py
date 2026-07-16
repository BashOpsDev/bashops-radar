"""add opportunity feed

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_feed_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_full_name", sa.String(length=255), nullable=False),
        sa.Column("repository_url", sa.String(length=500), nullable=False),
        sa.Column("repository_owner", sa.String(length=100), nullable=False),
        sa.Column("repository_name", sa.String(length=155), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("primary_language", sa.String(length=100), nullable=True),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("radar_score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=255), nullable=False),
        sa.Column("best_issue_number", sa.Integer(), nullable=True),
        sa.Column("best_issue_title", sa.String(length=500), nullable=True),
        sa.Column("best_issue_url", sa.String(length=500), nullable=True),
        sa.Column("difficulty", sa.String(length=100), nullable=True),
        sa.Column("merge_probability", sa.String(length=100), nullable=True),
        sa.Column("maintainer_activity_signal", sa.String(length=255), nullable=True),
        sa.Column("recent_activity_signal", sa.String(length=255), nullable=True),
        sa.Column("commercial_signal", sa.String(length=255), nullable=True),
        sa.Column("paid_sprint_signal", sa.String(length=255), nullable=True),
        sa.Column("public_reason", sa.String(length=500), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_opportunity_feed_items_repository_full_name"),
        "opportunity_feed_items",
        ["repository_full_name"],
        unique=True,
    )
    op.create_index(op.f("ix_opportunity_feed_items_expires_at"), "opportunity_feed_items", ["expires_at"])
    op.create_index(op.f("ix_opportunity_feed_items_is_active"), "opportunity_feed_items", ["is_active"])

    op.create_table(
        "user_opportunity_interactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("feed_item_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["feed_item_id"], ["opportunity_feed_items.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "feed_item_id", "action", name="uq_user_feed_item_action"),
    )
    op.create_index(
        op.f("ix_user_opportunity_interactions_user_id"),
        "user_opportunity_interactions",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_user_opportunity_interactions_feed_item_id"),
        "user_opportunity_interactions",
        ["feed_item_id"],
    )
    op.create_index(
        op.f("ix_user_opportunity_interactions_action"),
        "user_opportunity_interactions",
        ["action"],
    )
    op.create_index(
        op.f("ix_user_opportunity_interactions_created_at"),
        "user_opportunity_interactions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_opportunity_interactions_created_at"), table_name="user_opportunity_interactions")
    op.drop_index(op.f("ix_user_opportunity_interactions_action"), table_name="user_opportunity_interactions")
    op.drop_index(op.f("ix_user_opportunity_interactions_feed_item_id"), table_name="user_opportunity_interactions")
    op.drop_index(op.f("ix_user_opportunity_interactions_user_id"), table_name="user_opportunity_interactions")
    op.drop_table("user_opportunity_interactions")
    op.drop_index(op.f("ix_opportunity_feed_items_is_active"), table_name="opportunity_feed_items")
    op.drop_index(op.f("ix_opportunity_feed_items_expires_at"), table_name="opportunity_feed_items")
    op.drop_index(op.f("ix_opportunity_feed_items_repository_full_name"), table_name="opportunity_feed_items")
    op.drop_table("opportunity_feed_items")
