"""Create reminder subscriptions.

Revision ID: 20261016_01
Revises: 20260925_01
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_01"
down_revision: str | None = "20260925_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminder_subscriptions",
        sa.Column("teams_user_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("service_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reminder_time", sa.Time(), nullable=False),
        sa.Column("last_sent_on", sa.Date(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skip_on", sa.Date(), nullable=True),
        sa.Column("opt_out_pending", sa.Boolean(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("teams_user_id", name="pk_reminder_subscriptions"),
    )
    op.create_index(
        "ix_reminder_subscriptions_enabled_time",
        "reminder_subscriptions",
        ["enabled", "reminder_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reminder_subscriptions_enabled_time", table_name="reminder_subscriptions"
    )
    op.drop_table("reminder_subscriptions")
