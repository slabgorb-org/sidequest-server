"""initial unified Postgres schema (ADR-115 direct port)

Revision ID: 0001
Revises:
Create Date: 2026-05-26

Ports the per-session SQLite schema (game/persistence.py SCHEMA_SQL +
dungeon/persistence.py DUNGEON_SCHEMA_SQL) to one unified Postgres database:
a `sessions` table (integer surrogate PK + unique slug natural key,
absorbing session_meta + games) and a session_id FK on every per-session
table. Raw SQL via op.execute — no ORM models. See the plan's "Schema
translation decisions" for every type/key choice.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sessions (
            session_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_slug      TEXT NOT NULL UNIQUE,
            mode              TEXT NOT NULL CHECK (mode IN ('solo', 'multiplayer')),
            genre_slug        TEXT NOT NULL,
            world_slug        TEXT NOT NULL,
            claude_session_id TEXT,
            schema_version    INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT NOT NULL,
            last_played       TEXT NOT NULL
        );

        CREATE TABLE game_state (
            session_id    BIGINT NOT NULL PRIMARY KEY
                          REFERENCES sessions(session_id) ON DELETE CASCADE,
            snapshot_json TEXT NOT NULL,
            saved_at      TEXT NOT NULL
        );

        CREATE TABLE world_save (
            session_id   BIGINT NOT NULL PRIMARY KEY
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL,
            saved_at     TEXT NOT NULL
        );

        CREATE TABLE narrative_log (
            id           BIGINT GENERATED ALWAYS AS IDENTITY,
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL,
            author       TEXT NOT NULL,
            content      TEXT NOT NULL,
            tags         TEXT,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (session_id, id)
        );
        CREATE INDEX idx_narrative_round ON narrative_log (session_id, round_number);
        CREATE INDEX idx_narrative_author ON narrative_log (session_id, author);

        CREATE TABLE lore_fragments (
            session_id    BIGINT NOT NULL
                          REFERENCES sessions(session_id) ON DELETE CASCADE,
            id            TEXT NOT NULL,
            category      TEXT NOT NULL,
            content       TEXT NOT NULL,
            source        TEXT NOT NULL,
            turn_created  INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            PRIMARY KEY (session_id, id)
        );
        CREATE INDEX idx_lore_category ON lore_fragments (session_id, category);

        CREATE TABLE scenario_archive (
            session_id          BIGINT NOT NULL PRIMARY KEY
                                REFERENCES sessions(session_id) ON DELETE CASCADE,
            scenario_session_id TEXT,
            scenario_json       TEXT NOT NULL,
            saved_at            TEXT NOT NULL
        );

        CREATE TABLE scrapbook_entries (
            id                BIGINT GENERATED ALWAYS AS IDENTITY,
            session_id        BIGINT NOT NULL
                              REFERENCES sessions(session_id) ON DELETE CASCADE,
            turn_id           INTEGER NOT NULL,
            scene_title       TEXT,
            scene_type        TEXT,
            location          TEXT NOT NULL,
            image_url         TEXT,
            narrative_excerpt TEXT NOT NULL,
            world_facts       TEXT NOT NULL DEFAULT '[]',
            npcs_present      TEXT NOT NULL DEFAULT '[]',
            render_status     TEXT NOT NULL DEFAULT 'rendered',
            created_at        TEXT NOT NULL,
            PRIMARY KEY (session_id, id)
        );
        CREATE INDEX idx_scrapbook_turn ON scrapbook_entries (session_id, turn_id);

        CREATE TABLE events (
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            seq          BIGINT NOT NULL,
            kind         TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );

        CREATE TABLE projection_cache (
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            event_seq    BIGINT NOT NULL,
            player_id    TEXT NOT NULL,
            include      INTEGER NOT NULL,
            payload_json TEXT,
            PRIMARY KEY (session_id, event_seq, player_id),
            FOREIGN KEY (session_id, event_seq)
                REFERENCES events(session_id, seq) ON DELETE CASCADE
        );
        CREATE INDEX idx_projection_cache_player
            ON projection_cache (session_id, player_id, event_seq);

        CREATE TABLE turn_telemetry (
            seq          BIGINT GENERATED ALWAYS AS IDENTITY,
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            event_seq    BIGINT,
            round        INTEGER,
            ts           TEXT NOT NULL,
            component    TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        CREATE INDEX idx_turn_telemetry_round ON turn_telemetry (session_id, round);
        CREATE INDEX idx_turn_telemetry_event_seq ON turn_telemetry (session_id, event_seq);

        CREATE TABLE location_promotions (
            session_id        BIGINT NOT NULL
                              REFERENCES sessions(session_id) ON DELETE CASCADE,
            region_id         TEXT NOT NULL,
            entity_id         TEXT NOT NULL,
            provenance        TEXT NOT NULL,
            label             TEXT NOT NULL,
            promoted_at_turn  INTEGER NOT NULL,
            promoted_canon    TEXT NOT NULL,
            new_tier          TEXT NOT NULL DEFAULT 'yes_and',
            new_binding_kind  TEXT,
            new_binding_ref   TEXT,
            PRIMARY KEY (session_id, region_id, entity_id)
        );
        CREATE INDEX idx_location_promotions_region
            ON location_promotions (session_id, region_id);

        CREATE TABLE dungeon_map (
            session_id        BIGINT NOT NULL
                              REFERENCES sessions(session_id) ON DELETE CASCADE,
            region_id         TEXT NOT NULL,
            expansion_id      INTEGER NOT NULL,
            depth_score       DOUBLE PRECISION,
            generator_version TEXT NOT NULL,
            payload           TEXT NOT NULL,
            mask              BYTEA,
            created_at        TEXT NOT NULL,
            PRIMARY KEY (session_id, region_id)
        );
        CREATE INDEX idx_dungeon_map_expansion ON dungeon_map (session_id, expansion_id);
        CREATE INDEX idx_dungeon_map_depth ON dungeon_map (session_id, depth_score);

        CREATE TABLE dungeon_edge (
            edge_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id   BIGINT NOT NULL
                         REFERENCES sessions(session_id) ON DELETE CASCADE,
            expansion_id INTEGER NOT NULL,
            a            TEXT NOT NULL,
            b            TEXT NOT NULL,
            kind         TEXT NOT NULL,
            hidden       INTEGER NOT NULL,
            shortcut     INTEGER NOT NULL,
            payload      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX idx_dungeon_edge_a ON dungeon_edge (session_id, a);
        CREATE INDEX idx_dungeon_edge_b ON dungeon_edge (session_id, b);
        CREATE INDEX idx_dungeon_edge_expansion ON dungeon_edge (session_id, expansion_id);

        CREATE TABLE dungeon_frontier (
            session_id        BIGINT NOT NULL
                              REFERENCES sessions(session_id) ON DELETE CASCADE,
            frontier_edge_id  TEXT NOT NULL,
            from_region_id    TEXT NOT NULL,
            heading           TEXT NOT NULL,
            spawn_depth_score DOUBLE PRECISION NOT NULL,
            payload           TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            PRIMARY KEY (session_id, frontier_edge_id)
        );
        CREATE INDEX idx_dungeon_frontier_from
            ON dungeon_frontier (session_id, from_region_id);

        CREATE TABLE dungeon_mutation_overlay (
            mutation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id  BIGINT NOT NULL
                        REFERENCES sessions(session_id) ON DELETE CASCADE,
            region_id   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            payload     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX idx_dungeon_mutation_region
            ON dungeon_mutation_overlay (session_id, region_id);

        CREATE TABLE dungeon_complication_ledger (
            session_id            BIGINT NOT NULL
                                  REFERENCES sessions(session_id) ON DELETE CASCADE,
            thread_id             TEXT NOT NULL,
            origin_region_id      TEXT NOT NULL,
            kind                  TEXT NOT NULL,
            status                TEXT NOT NULL,
            started_at_depth_score DOUBLE PRECISION NOT NULL,
            payload               TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            resolved_at           TEXT,
            PRIMARY KEY (session_id, thread_id)
        );
        CREATE INDEX idx_dungeon_ledger_status
            ON dungeon_complication_ledger (session_id, status);
        CREATE INDEX idx_dungeon_ledger_origin
            ON dungeon_complication_ledger (session_id, origin_region_id);

        CREATE TABLE dungeon_meta (
            session_id    BIGINT NOT NULL PRIMARY KEY
                          REFERENCES sessions(session_id) ON DELETE CASCADE,
            campaign_seed BIGINT NOT NULL,
            created_at    TEXT NOT NULL
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS dungeon_meta;
        DROP TABLE IF EXISTS dungeon_complication_ledger;
        DROP TABLE IF EXISTS dungeon_mutation_overlay;
        DROP TABLE IF EXISTS dungeon_frontier;
        DROP TABLE IF EXISTS dungeon_edge;
        DROP TABLE IF EXISTS dungeon_map;
        DROP TABLE IF EXISTS location_promotions;
        DROP TABLE IF EXISTS turn_telemetry;
        DROP TABLE IF EXISTS projection_cache;
        DROP TABLE IF EXISTS events;
        DROP TABLE IF EXISTS scrapbook_entries;
        DROP TABLE IF EXISTS scenario_archive;
        DROP TABLE IF EXISTS lore_fragments;
        DROP TABLE IF EXISTS narrative_log;
        DROP TABLE IF EXISTS world_save;
        DROP TABLE IF EXISTS game_state;
        DROP TABLE IF EXISTS sessions;
        """
    )
