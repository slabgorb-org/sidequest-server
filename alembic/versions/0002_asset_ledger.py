"""asset_ledger — per-session runtime asset ledger (Story 65-2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27

Links each save (sessions row) to the runtime-generated R2 artifacts it
produced (portraits, scene illustrations, POI renders), so the UI can
rehydrate prior-turn imagery on resume without re-rendering.

The content sha256 is already embedded in ``r2_key``
(``artifacts/<world>/<session>/<kind>/<sha256>.<ext>``), so there is no
``md5``/``size_bytes`` column — see context-story-65-2.md "Content hash / size".
Raw SQL via op.execute, mirroring 0001.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE asset_ledger (
            r2_key       TEXT PRIMARY KEY,
            asset_type   TEXT NOT NULL,
            entity_ref   TEXT NOT NULL,
            created_turn INTEGER NOT NULL,
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX idx_asset_ledger_session ON asset_ledger (session_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_ledger;")
