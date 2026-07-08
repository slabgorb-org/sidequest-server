"""dungeon site_id — per-site dungeon storage keying (Track B, Story 164-1)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08

The dungeon tables were keyed by ``session_id`` alone, so a session held ONE
dungeon. Sites need ``(session_id, site_id)`` so a bounded tavern and the
frontier deep coexist in one session without colliding. This migration adds a
``site_id TEXT NOT NULL DEFAULT 'frontier'`` column to every dungeon table and
re-keys the composite primary keys to include it. Additive — every existing row
defaults to ``'frontier'`` (``DEFAULT_SITE_ID``), so the legacy single-dungeon
path is unchanged. Raw SQL via op.execute, mirroring 0001/0002.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Legacy key: pre-site dungeons (Sünden's existing deep) live under this id.
# Must match sidequest.game.pg.dungeon.DEFAULT_SITE_ID.
_SITE = "frontier"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE dungeon_map ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';
        ALTER TABLE dungeon_map DROP CONSTRAINT dungeon_map_pkey;
        ALTER TABLE dungeon_map ADD PRIMARY KEY (session_id, site_id, region_id);

        ALTER TABLE dungeon_edge ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';

        ALTER TABLE dungeon_frontier ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';
        ALTER TABLE dungeon_frontier DROP CONSTRAINT dungeon_frontier_pkey;
        ALTER TABLE dungeon_frontier ADD PRIMARY KEY (session_id, site_id, frontier_edge_id);

        ALTER TABLE dungeon_mutation_overlay ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';

        ALTER TABLE dungeon_complication_ledger ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';
        ALTER TABLE dungeon_complication_ledger DROP CONSTRAINT dungeon_complication_ledger_pkey;
        ALTER TABLE dungeon_complication_ledger ADD PRIMARY KEY (session_id, site_id, thread_id);

        ALTER TABLE dungeon_meta ADD COLUMN site_id TEXT NOT NULL DEFAULT '{_SITE}';
        ALTER TABLE dungeon_meta DROP CONSTRAINT dungeon_meta_pkey;
        ALTER TABLE dungeon_meta ADD PRIMARY KEY (session_id, site_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE dungeon_meta DROP CONSTRAINT dungeon_meta_pkey;
        ALTER TABLE dungeon_meta DROP COLUMN site_id;
        ALTER TABLE dungeon_meta ADD PRIMARY KEY (session_id);

        ALTER TABLE dungeon_complication_ledger DROP CONSTRAINT dungeon_complication_ledger_pkey;
        ALTER TABLE dungeon_complication_ledger DROP COLUMN site_id;
        ALTER TABLE dungeon_complication_ledger ADD PRIMARY KEY (session_id, thread_id);

        ALTER TABLE dungeon_mutation_overlay DROP COLUMN site_id;

        ALTER TABLE dungeon_frontier DROP CONSTRAINT dungeon_frontier_pkey;
        ALTER TABLE dungeon_frontier DROP COLUMN site_id;
        ALTER TABLE dungeon_frontier ADD PRIMARY KEY (session_id, frontier_edge_id);

        ALTER TABLE dungeon_edge DROP COLUMN site_id;

        ALTER TABLE dungeon_map DROP CONSTRAINT dungeon_map_pkey;
        ALTER TABLE dungeon_map DROP COLUMN site_id;
        ALTER TABLE dungeon_map ADD PRIMARY KEY (session_id, region_id);
        """
    )
