"""Create encrypted Orbit sessions.

Revision ID: 20260925_01
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orbit_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("teams_user_id", sa.String(length=255), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("provider_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_orbit_sessions"),
        sa.UniqueConstraint("teams_user_id", name="uq_orbit_sessions_teams_user_id"),
    )
    op.create_index(
        "ix_orbit_sessions_session_expires_at",
        "orbit_sessions",
        ["session_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_orbit_sessions_session_expires_at", table_name="orbit_sessions")
    op.drop_table("orbit_sessions")
