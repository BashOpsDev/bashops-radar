"""add events table

Revision ID: b7c3d4e5f6a1
Revises: f4b9c2d1e8a7
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c3d4e5f6a1"
down_revision: Union[str, Sequence[str], None] = "f4b9c2d1e8a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("event_name", sa.String(length=100), nullable=False),
        sa.Column("page", sa.String(length=500), nullable=True),
        sa.Column("referrer", sa.String(length=500), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_events_created_at"), "events", ["created_at"], unique=False)
    op.create_index(op.f("ix_events_event_name"), "events", ["event_name"], unique=False)
    op.create_index(op.f("ix_events_user_id"), "events", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_events_user_id"), table_name="events")
    op.drop_index(op.f("ix_events_event_name"), table_name="events")
    op.drop_index(op.f("ix_events_created_at"), table_name="events")
    op.drop_table("events")
