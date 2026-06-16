"""PgForensicReader — MVCC read-side forensics (ADR-115 C2).

Ports ``forensic_query.py``'s three public functions to Postgres pooled reads:

  - list_saves()          — all sessions with telemetry counts
  - build_timeline()      — per-round narrative/event boundaries
  - build_turn_bundle()   — full drill-down bundle for one round

All reads are lock-free MVCC: plain ``with pool.connection() as conn``, no
``session_tx``, no ``FOR UPDATE``.  The ``?mode=ro`` SQLite open path is
retired; tables always exist under Postgres.

_NORM_EV_TS decision
---------------------
The SQLite forensic_query normalises event timestamps from Python isoformat
('YYYY-MM-DDThh:mm:ss.ffffff+00:00') to SQLite datetime('now') format
('YYYY-MM-DD HH:MM:SS') so that lexical comparisons with narrative_log
timestamps are correct.

Under Postgres BOTH narrative_log.created_at and events.created_at are
written by ``datetime.now(tz=UTC).isoformat()`` — the same format, same
function, same precision.  Lexical ordering of two identically-formatted
ISO-8601 strings is always correct (the format sorts as wall-clock time),
so _NORM_EV_TS is NOT applied here.  The fixture-verified boundary tests
prove this directly.

Return shapes
-------------
The return shapes of list_saves / build_timeline / build_turn_bundle are
identical to forensic_query.py so that the D7 REST endpoints swap only the
data source.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from sidequest.game.event_log import EventRow
from sidequest.game.forensic_fold import (
    fold_known_facts,
    fold_mechanical_census,
    fold_turn_telemetry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers — timestamp-safe JSON decode
# ---------------------------------------------------------------------------


def _safe_json(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"__unparseable__": raw}


def _safe_json_logged(raw: str | None, *, context: str):
    """``_safe_json`` that logs loudly when the column is unparseable.

    The bare ``_safe_json`` returns the ``{"__unparseable__": raw}`` sentinel
    silently — fine for the bulk event/projection display path where the
    sentinel is surfaced verbatim to the GM panel. But for the snapshot and
    encounter-events reads a corrupt column should be *observable* in the
    server log (No-Silent-Fallbacks), matching ``_safe_json_list``. Same
    return contract as ``_safe_json``.
    """
    parsed = _safe_json(raw)
    if isinstance(parsed, dict) and "__unparseable__" in parsed:
        logger.warning("pg.forensic.%s unparseable raw=%r", context, raw)
    return parsed


def _safe_json_list(raw: str | None) -> list:
    """Read-only display decode for list-typed stored columns.

    Logs loudly on null/parse-failure/non-list (No-Silent-Fallbacks) and
    returns [] — identical contract to forensic_query._safe_json_list.
    """
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("pg.forensic._safe_json_list unparseable raw=%r", raw)
        return []
    if not isinstance(parsed, list):
        logger.warning("pg.forensic._safe_json_list non_list raw=%r", raw)
        return []
    return parsed


# ---------------------------------------------------------------------------
# Empty-bundle factories (same contract as forensic_query)
# ---------------------------------------------------------------------------


def _empty_telemetry() -> dict:
    return {"rows": [], "by_component": {}, "total": 0, "unparseable_seqs": []}


def _empty_mechanical() -> dict:
    return {"state": "absent", "pcs": [], "trope": None, "unparseable_seqs": []}


# ---------------------------------------------------------------------------
# Round boundary helpers (ported from forensic_query._round_boundaries /
# _events_for_round, WITHOUT _NORM_EV_TS — see module docstring)
# ---------------------------------------------------------------------------


def _round_boundaries(conn: psycopg.Connection, session_id: int) -> list[tuple[int, str]]:
    """Ordered (round_number, min_created_at) per round in narrative_log."""
    rows = conn.execute(
        "SELECT round_number, MIN(created_at) AS first_ts "
        "FROM narrative_log "
        "WHERE session_id = %s "
        "GROUP BY round_number "
        "ORDER BY round_number",
        (session_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _events_for_round(
    conn: psycopg.Connection,
    session_id: int,
    lo_ts: str,
    hi_ts: str | None,
    *,
    first_round: bool,
):
    """Events whose created_at is in [lo_ts, hi_ts).

    The first round also sweeps any events that predate the first narrative
    row.  No timestamp normalisation needed — both tables use the same
    Python isoformat format (see module docstring).
    """
    if first_round and hi_ts is not None:
        return conn.execute(
            "SELECT seq, kind, created_at FROM events "
            "WHERE session_id = %s AND created_at < %s "
            "ORDER BY seq",
            (session_id, hi_ts),
        ).fetchall()
    if first_round and hi_ts is None:
        return conn.execute(
            "SELECT seq, kind, created_at FROM events WHERE session_id = %s ORDER BY seq",
            (session_id,),
        ).fetchall()
    if hi_ts is None:
        return conn.execute(
            "SELECT seq, kind, created_at FROM events "
            "WHERE session_id = %s AND created_at >= %s "
            "ORDER BY seq",
            (session_id, lo_ts),
        ).fetchall()
    return conn.execute(
        "SELECT seq, kind, created_at FROM events "
        "WHERE session_id = %s AND created_at >= %s AND created_at < %s "
        "ORDER BY seq",
        (session_id, lo_ts, hi_ts),
    ).fetchall()


# ---------------------------------------------------------------------------
# Telemetry + mechanical fold helpers (ported from forensic_query)
# ---------------------------------------------------------------------------


def _telemetry_for_round(
    conn: psycopg.Connection, session_id: int, seq_start: int, seq_end: int, round_number: int
) -> dict:
    """Read this round's turn_telemetry rows and fold them.

    Bucketing: rows whose event_seq is within [seq_start, seq_end] OR whose
    round column equals round_number (covers rows emitted with NULL event_seq,
    e.g. beat-selection telemetry).

    Under Postgres the table always exists — no sqlite_master probe needed.
    Unexpected query errors propagate to the caller.
    """
    rows = conn.execute(
        "SELECT seq, event_seq, round, ts, component, event_type, payload_json "
        "FROM turn_telemetry "
        "WHERE session_id = %s "
        "  AND ((event_seq IS NOT NULL AND event_seq >= %s AND event_seq <= %s) "
        "       OR round = %s) "
        "ORDER BY seq",
        (session_id, seq_start, seq_end, round_number),
    ).fetchall()
    # Convert psycopg Row objects to dicts for forensic_fold (same as SQLite dict(r))
    raw = [
        {
            "seq": r[0],
            "event_seq": r[1],
            "round": r[2],
            "ts": r[3],
            "component": r[4],
            "event_type": r[5],
            "payload_json": r[6],
        }
        for r in rows
    ]
    fold = fold_turn_telemetry(raw)
    return {
        "rows": [
            {
                "seq": tr.seq,
                "component": tr.component,
                "event_type": tr.event_type,
                "ts": tr.ts,
                "fields": tr.fields,
            }
            for tr in fold.rows
        ],
        "by_component": fold.by_component,
        "total": fold.total,
        "unparseable_seqs": list(fold.unparseable_seqs),
    }


def _mechanical_for_round(
    conn: psycopg.Connection, session_id: int, seq_start: int, seq_end: int, round_number: int
) -> dict:
    """Read this round's component='mechanical' rows + the previous census
    round's rows; fold into a per-PC diff.

    Under Postgres the table always exists — no sqlite_master probe needed.
    """
    cur_rows = conn.execute(
        "SELECT seq, event_seq, round, ts, component, event_type, payload_json "
        "FROM turn_telemetry "
        "WHERE session_id = %s "
        "  AND component = 'mechanical' "
        "  AND ((event_seq IS NOT NULL AND event_seq >= %s AND event_seq <= %s) "
        "       OR round = %s) "
        "ORDER BY seq",
        (session_id, seq_start, seq_end, round_number),
    ).fetchall()

    prev_round_row = conn.execute(
        "SELECT MAX(round) FROM turn_telemetry "
        "WHERE session_id = %s "
        "  AND component = 'mechanical' "
        "  AND round IS NOT NULL "
        "  AND round < %s",
        (session_id, round_number),
    ).fetchone()

    prev_rows = []
    if prev_round_row and prev_round_row[0] is not None:
        prev_rows = conn.execute(
            "SELECT seq, event_seq, round, ts, component, event_type, payload_json "
            "FROM turn_telemetry "
            "WHERE session_id = %s "
            "  AND component = 'mechanical' "
            "  AND round = %s "
            "ORDER BY seq",
            (session_id, prev_round_row[0]),
        ).fetchall()

    def _to_dict(r) -> dict:
        return {
            "seq": r[0],
            "event_seq": r[1],
            "round": r[2],
            "ts": r[3],
            "component": r[4],
            "event_type": r[5],
            "payload_json": r[6],
        }

    fold = fold_mechanical_census(
        [_to_dict(r) for r in cur_rows],
        [_to_dict(r) for r in prev_rows],
    )
    return {
        "state": fold.state,
        "pcs": [
            {
                "player_id": pc.player_id,
                "character_name": pc.character_name,
                "seat": pc.seat,
                "kind": pc.kind,
                "deltas": list(pc.deltas),
                "absolute": pc.absolute,
            }
            for pc in fold.pcs
        ],
        "trope": fold.trope,
        "unparseable_seqs": list(fold.unparseable_seqs),
    }


# ---------------------------------------------------------------------------
# PgForensicReader
# ---------------------------------------------------------------------------


class PgForensicReader:
    """MVCC read-side forensics over Postgres (ADR-115 C2).

    All methods are read-only: plain ``with pool.connection() as conn``
    pooled reads under MVCC — no session_tx, no FOR UPDATE, no locking.

    Return shapes are identical to forensic_query.py so that D7 REST
    endpoints swap only the data source, not the response contract.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # list_saves
    # ------------------------------------------------------------------

    def list_saves(self) -> list[dict]:
        """Return all sessions with genre/world/timestamps and telemetry counts.

        Replaces the per-file ``session_meta WHERE id=1`` walk in
        forensic_query.list_saves.  No sqlite_master probes — tables always
        exist under Postgres.

        Return shape per row (identical to forensic_query.list_saves):
            slug          — session_slug
            genre         — genre_slug
            world         — world_slug
            created_at    — ISO-8601 TEXT
            last_played   — ISO-8601 TEXT
            last_activity_ts — int ms; derived from last_played
                               (datetime.fromisoformat(last_played).timestamp()*1000).
                               A faithful — arguably better — equivalent of SQLite's
                               save-file mtime: it is the session's true last-activity
                               instant rather than a filesystem proxy.
            telemetry_rows   — COUNT(*) from turn_telemetry
            mechanical_rows  — COUNT(*) FILTER (WHERE component='mechanical')

        Sorted newest-first by last_activity_ts DESC (matches the SQLite sort
        by file mtime).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.session_slug,
                    s.genre_slug,
                    s.world_slug,
                    s.created_at,
                    s.last_played,
                    COUNT(tt.seq)                                              AS telemetry_rows,
                    COUNT(tt.seq) FILTER (WHERE tt.component = 'mechanical')   AS mechanical_rows
                FROM sessions s
                LEFT JOIN turn_telemetry tt
                       ON tt.session_id = s.session_id
                GROUP BY s.session_id, s.session_slug, s.genre_slug, s.world_slug,
                         s.created_at, s.last_played
                ORDER BY s.last_played DESC NULLS LAST
                """,
            ).fetchall()

        out = [
            {
                "slug": r[0],
                "genre": r[1],
                "world": r[2],
                "created_at": r[3],
                "last_played": r[4],
                "last_activity_ts": int(datetime.fromisoformat(r[4]).timestamp() * 1000),
                "telemetry_rows": int(r[5]),
                "mechanical_rows": int(r[6]),
            }
            for r in rows
        ]
        out.sort(key=lambda row: row["last_activity_ts"], reverse=True)
        return out

    # ------------------------------------------------------------------
    # snapshot_json — raw stored snapshot for the forensics panel
    # ------------------------------------------------------------------

    def snapshot_json(self, session_id: int) -> dict:
        """Raw persisted ``game_state.snapshot_json`` for one session.

        Read-only decode for the forensics 'final stored snapshot' panel —
        returns the stored dict verbatim, never deserializes through the
        domain model (that's PgSaveRepository.load's job). ``{}`` when the
        session has no game_state row or the stored value is not a JSON
        object. PG port of the SQLite snapshot-endpoint raw read.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM game_state WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return {}
        parsed = _safe_json_logged(row[0], context="snapshot_json")
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    # encounter_events
    # ------------------------------------------------------------------

    def encounter_events(self, session_id: int) -> list[dict]:
        """Ordered ENCOUNTER_* event rows for one session.

        PG port of persistence.query_encounter_events — the GM panel's
        EncounterTab timeline read. Return shape is identical
        (seq, kind, payload, created_at), so D7's REST endpoint swaps only
        the data source.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE session_id = %s AND kind LIKE 'ENCOUNTER_%%' "
                "ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "seq": r[0],
                "kind": r[1],
                "payload": _safe_json_logged(r[2], context="encounter_events"),
                "created_at": r[3],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # build_timeline
    # ------------------------------------------------------------------

    def build_timeline(self, session_id: int) -> list[dict]:
        """One entry per narrative round, with its event seq-range + summary.

        Ported from forensic_query.build_timeline.  Timestamp bucketing uses
        direct ISO-8601 string comparison — no _NORM_EV_TS normalisation
        needed under Postgres (see module docstring).

        Return shape per entry (identical to forensic_query.build_timeline):
            round               — round_number
            seq_start           — first event seq in this round (or None)
            seq_end             — last event seq in this round (or None)
            event_kind_counts   — {kind: count}
            narrative_authors   — sorted list of distinct authors
            ts                  — min(narrative_log.created_at) for the round
        """
        with self._pool.connection() as conn:
            bounds = _round_boundaries(conn, session_id)
            if not bounds:
                return []

            timeline: list[dict] = []
            for idx, (rnd, lo_ts) in enumerate(bounds):
                hi_ts = bounds[idx + 1][1] if idx + 1 < len(bounds) else None
                evs = _events_for_round(conn, session_id, lo_ts, hi_ts, first_round=(idx == 0))
                kind_counts: dict[str, int] = {}
                for e in evs:
                    kind = e[1]  # positional: seq, kind, created_at
                    kind_counts[kind] = kind_counts.get(kind, 0) + 1

                author_rows = conn.execute(
                    "SELECT DISTINCT author FROM narrative_log "
                    "WHERE session_id = %s AND round_number = %s "
                    "ORDER BY author",
                    (session_id, rnd),
                ).fetchall()
                authors = [r[0] for r in author_rows]

                timeline.append(
                    {
                        "round": rnd,
                        "seq_start": evs[0][0] if evs else None,
                        "seq_end": evs[-1][0] if evs else None,
                        "event_kind_counts": kind_counts,
                        "narrative_authors": authors,
                        "ts": lo_ts,
                    }
                )
        return timeline

    # ------------------------------------------------------------------
    # build_turn_bundle
    # ------------------------------------------------------------------

    def build_turn_bundle(self, session_id: int, round_number: int) -> dict:
        """Assemble every drill-down panel for one round.

        Ported from forensic_query.build_turn_bundle.  Truth tiers stay
        separate: narrative / events / projection / scrapbook are verbatim DB
        rows; derived is the KnownFacts ledger folded from every event's
        footnotes up to and including this round's last seq.  forensic_fold
        helpers are reused unchanged (they fold Python dicts, engine-agnostic).

        Unknown round → empty bundle (lossy/best-effort, never raises).
        Read-only: plain pooled connection under MVCC.

        Return shape (identical to forensic_query.build_turn_bundle):
            round           — int
            narrative       — list[{round, author, content, tags, created_at}]
            events          — list[{seq, kind, payload, created_at}]
            derived         — {fact_id: {value, source_seqs}}
            projection      — list[{event_seq, player_id, include, payload}]
            scrapbook       — list[{scene_title, scene_type, location, image_url,
                                    narrative_excerpt, world_facts, npcs_present,
                                    render_status}]
            unparseable_seqs — list[int]
            telemetry       — {rows, by_component, total, unparseable_seqs}
            mechanical      — {state, pcs, trope, unparseable_seqs}
        """
        with self._pool.connection() as conn:
            # -- narrative for this round ----------------------------------
            narr_rows = conn.execute(
                "SELECT round_number, author, content, tags, created_at "
                "FROM narrative_log "
                "WHERE session_id = %s AND round_number = %s "
                "ORDER BY id",
                (session_id, round_number),
            ).fetchall()
            narrative = [
                {
                    "round": r[0],
                    "author": r[1],
                    "content": r[2],
                    "tags": _safe_json_list(r[3]),
                    "created_at": r[4],
                }
                for r in narr_rows
            ]

            # -- seq boundaries for this round ----------------------------
            # We only need seq_start/seq_end here, so compute exactly that —
            # not the full timeline entry (build_timeline owns event_kind_counts
            # and narrative_authors; computing them here would be a wasted SQL
            # round-trip per call).
            bounds = _round_boundaries(conn, session_id)
            seq_start: int | None = None
            seq_end: int | None = None
            for idx, (rnd, lo_ts) in enumerate(bounds):
                if rnd == round_number:
                    hi_ts = bounds[idx + 1][1] if idx + 1 < len(bounds) else None
                    evs = _events_for_round(conn, session_id, lo_ts, hi_ts, first_round=(idx == 0))
                    if evs:
                        seq_start = evs[0][0]
                        seq_end = evs[-1][0]
                    break

            # -- empty bundle when round is unknown or has no events ------
            if seq_start is None:
                return {
                    "round": round_number,
                    "narrative": narrative,
                    "events": [],
                    "derived": {},
                    "projection": [],
                    "scrapbook": [],
                    "unparseable_seqs": [],
                    "telemetry": _empty_telemetry(),
                    "mechanical": _empty_mechanical(),
                }

            # seq_start/seq_end are set together; the guard above proves both
            # are non-None here.
            assert seq_end is not None

            # -- telemetry + mechanical fold ------------------------------
            telemetry = _telemetry_for_round(conn, session_id, seq_start, seq_end, round_number)
            mechanical = _mechanical_for_round(conn, session_id, seq_start, seq_end, round_number)

            # -- events in [seq_start, seq_end] ---------------------------
            raw_events = conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE session_id = %s AND seq >= %s AND seq <= %s "
                "ORDER BY seq",
                (session_id, seq_start, seq_end),
            ).fetchall()
            events = [
                {
                    "seq": e[0],
                    "kind": e[1],
                    "payload": _safe_json(e[2]),
                    "created_at": e[3],
                }
                for e in raw_events
            ]

            # -- KnownFacts fold: all events <= seq_end -------------------
            fold_rows = conn.execute(
                "SELECT seq, kind, payload_json, created_at FROM events "
                "WHERE session_id = %s AND seq <= %s "
                "ORDER BY seq",
                (session_id, seq_end),
            ).fetchall()
            fold = fold_known_facts(
                [
                    EventRow(
                        seq=r[0],
                        kind=r[1],
                        payload_json=r[2],
                        created_at=r[3],
                    )
                    for r in fold_rows
                ]
            )
            derived = {
                k: {"value": v.value, "source_seqs": list(v.source_seqs)}
                for k, v in fold.derived.items()
            }

            # -- projection_cache [seq_start, seq_end] --------------------
            proj_rows = conn.execute(
                "SELECT event_seq, player_id, include, payload_json "
                "FROM projection_cache "
                "WHERE session_id = %s AND event_seq >= %s AND event_seq <= %s "
                "ORDER BY event_seq, player_id",
                (session_id, seq_start, seq_end),
            ).fetchall()
            projection = [
                {
                    "event_seq": p[0],
                    "player_id": p[1],
                    "include": p[2],
                    "payload": _safe_json(p[3]),
                }
                for p in proj_rows
            ]

            # -- scrapbook entries for this round -------------------------
            scrb_rows = conn.execute(
                "SELECT scene_title, scene_type, location, image_url, "
                "narrative_excerpt, world_facts, npcs_present, render_status "
                "FROM scrapbook_entries "
                "WHERE session_id = %s AND turn_id = %s "
                "ORDER BY id",
                (session_id, round_number),
            ).fetchall()
            scrapbook = [
                {
                    "scene_title": s[0],
                    "scene_type": s[1],
                    "location": s[2],
                    "image_url": s[3],
                    "narrative_excerpt": s[4],
                    "world_facts": _safe_json_list(s[5]),
                    "npcs_present": _safe_json_list(s[6]),
                    "render_status": s[7],
                }
                for s in scrb_rows
            ]

        return {
            "round": round_number,
            "narrative": narrative,
            "events": events,
            "derived": derived,
            "projection": projection,
            "scrapbook": scrapbook,
            "unparseable_seqs": list(fold.unparseable_seqs),
            "telemetry": telemetry,
            "mechanical": mechanical,
        }
