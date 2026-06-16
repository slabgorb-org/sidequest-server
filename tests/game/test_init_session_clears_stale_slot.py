"""Story 45-5 — Stale-slot reuse on session reinit blocks turn 1.

Unit-level RED tests on ``SqliteStore.init_session()``. Covers AC1, AC4, AC5
at the persistence seam:

- **AC1**: When ``init_session()`` is called against a slot that already
  carries rows in any per-slot table (``narrative_log``, ``game_state``,
  ``scrapbook_entries``, ``lore_fragments``, ``events``,
  ``projection_cache``), every per-slot table is left empty after the call.
- **AC1 (regression guard)**: A fresh slot reinits cleanly — clear is a
  no-op, ``session_meta`` row 1 is still installed.
- **AC1 (scope guard)**: The slug-keyed ``games`` table is **not** cleared.
  ``init_session()`` is per-slot lifecycle; the games table is global.
- **AC4**: Every ``init_session()`` call emits a single
  ``session.slot_reinitialized`` watcher event with accurate
  ``prior_narrative_count``, ``prior_event_count``, ``cleared_tables``,
  and ``mode="clear"``. This fires even on a fresh slot — Sebastien's
  GM panel needs the *negative* confirmation that reinit ran cleanly.
- **AC5 (post-reinit precondition)**: After ``init_session()``, the
  narrative log is empty, so ``max(round_number)`` cannot wedge a fresh
  ``TurnManager(round=1, interaction=1)`` at session start. The broader
  round-vs-max sync invariant belongs to story 45-11.
- **SPAN_ROUTES registration**: ``SPAN_SESSION_SLOT_REINITIALIZED`` exists,
  is registered in ``SPAN_ROUTES``, and matches the constant value
  ``"session.slot_reinitialized"`` so the routing-completeness lint does
  not silently drop it.

Playtest 3 evidence (2026-04-19, evropi/prot_thokk and evropi/hant): the
``narrative_log`` carried a row dated 2026-04-18 in a save created at
2026-04-19T16:31 UTC; ``init_session()`` overwrote ``session_meta`` row 1
but left every other per-slot table untouched. The first
``_execute_narration_turn()`` call saw a populated ``narrative_log`` and
a ``turn_manager`` whose ``round=0`` disagreed with the log's
``max(round_number)``; turn 1 wedged.

These tests are **expected to fail** under the current implementation —
they spec the clear-on-reinit fix (option 1 from the story context) that
Dev will land in GREEN. Refuse-on-populated (option 2) is explicitly out
of scope for this story (TEA deviation log).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.session import NarrativeEntry

# Per-slot tables that ``init_session()`` MUST clear. The ``games`` table
# is slug-keyed (not per-slot) and the ``scenario_archive`` is keyed by
# session_id (also global). They MUST NOT clear with a per-slot reinit.
PER_SLOT_TABLES = (
    "game_state",
    "narrative_log",
    "scrapbook_entries",
    "lore_fragments",
    "events",
    "projection_cache",
)

GLOBAL_TABLES = ("games",)


def _populate_slot_with_stale_rows(store: SqliteStore) -> None:
    """Insert one row into every per-slot table to simulate a slot that
    was used by a prior session (Playtest 3 evidence shape).

    The first ``init_session(genre, world)`` call has already installed
    ``session_meta`` row 1, so we don't re-insert it here.
    """
    # narrative_log — the playtest 3 smoking gun
    store.append_narrative(
        NarrativeEntry(
            timestamp=0,
            round=5,
            author="narrator",
            content="Prior-day narration that must not survive reinit.",
            tags=["stale"],
        )
    )

    # game_state — the snapshot table the load() path reads
    store._conn.execute(
        """INSERT OR REPLACE INTO game_state (id, snapshot_json, saved_at)
           VALUES (1, ?, ?)""",
        ('{"prior":"snapshot"}', "2026-04-18T12:00:00+00:00"),
    )

    # scrapbook_entries
    store._conn.execute(
        """INSERT INTO scrapbook_entries
           (turn_id, scene_title, scene_type, location, image_url,
            narrative_excerpt, world_facts, npcs_present)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            5,
            "Stale Scrapbook",
            "exploration",
            "old-room",
            None,
            "Prior session scrapbook entry.",
            "[]",
            "[]",
        ),
    )

    # lore_fragments
    store._conn.execute(
        """INSERT INTO lore_fragments (id, category, content, source, turn_created)
           VALUES (?, ?, ?, ?, ?)""",
        ("stale-lore-1", "history", "Stale lore.", "prior_session", 5),
    )

    # events
    store._conn.execute(
        """INSERT INTO events (kind, payload_json, created_at)
           VALUES (?, ?, ?)""",
        ("stale_event", json.dumps({"foo": "bar"}), "2026-04-18T12:00:00+00:00"),
    )

    # projection_cache (FK to events, so insert after the event row above)
    event_seq_row = store._conn.execute(
        "SELECT seq FROM events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert event_seq_row is not None, "events insert above did not produce a row"
    event_seq = event_seq_row[0]
    store._conn.execute(
        """INSERT INTO projection_cache
           (event_seq, player_id, include, payload_json)
           VALUES (?, ?, ?, ?)""",
        (event_seq, "stale-player", 1, "{}"),
    )

    store._conn.commit()


def _row_count(store: SqliteStore, table: str) -> int:
    row = store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# AC1 — clear-on-reinit covers every per-slot table.
# ---------------------------------------------------------------------------


class TestInitSessionClearsStalePerSlotTables:
    """Every per-slot table is empty after ``init_session()`` on a populated slot."""

    def test_narrative_log_cleared_on_reinit(self, tmp_path: Path) -> None:
        """The Playtest 3 smoking gun: a stale ``narrative_log`` row dated
        2026-04-18 survived an ``init_session()`` call on a save created
        2026-04-19. After the fix, the log is empty."""
        db = db_path_for_slug(tmp_path, "test-stale-narrative")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        # First init seeds session_meta; populate all per-slot tables; then reinit.
        store.init_session("caverns_and_claudes", "grimvault")
        _populate_slot_with_stale_rows(store)
        assert _row_count(store, "narrative_log") == 1, "fixture sanity check"

        store.init_session("caverns_and_claudes", "grimvault")

        assert _row_count(store, "narrative_log") == 0, (
            "AC1 violation: stale narrative_log entries survived init_session(). "
            "The Playtest 3 prot_thokk/hant bug is back."
        )

    def test_every_per_slot_table_cleared_on_reinit(self, tmp_path: Path) -> None:
        """All six per-slot tables must clear together — a half-clear is the
        coal that the original bug taught us to refuse. ``session_meta`` is
        the exception: it must remain populated (row 1 is the new metadata)."""
        db = db_path_for_slug(tmp_path, "test-stale-all-tables")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("caverns_and_claudes", "grimvault")
        _populate_slot_with_stale_rows(store)
        for tbl in PER_SLOT_TABLES:
            assert _row_count(store, tbl) >= 1, (
                f"fixture sanity: {tbl} must be populated before reinit"
            )

        store.init_session("caverns_and_claudes", "grimvault")

        for tbl in PER_SLOT_TABLES:
            assert _row_count(store, tbl) == 0, (
                f"AC1 violation: per-slot table {tbl!r} not cleared on reinit. "
                "Half-clear is the original coal — every per-slot table must "
                "clear together or none does (single transaction)."
            )

    def test_session_meta_replaced_not_cleared_on_reinit(self, tmp_path: Path) -> None:
        """``session_meta`` is per-slot but the reinit's contract is
        *replace* (not clear-then-no-row). The single row 1 must carry the
        new genre/world/created_at after reinit."""
        db = db_path_for_slug(tmp_path, "test-meta-replaced")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("old_genre", "old_world")
        _populate_slot_with_stale_rows(store)

        store.init_session("caverns_and_claudes", "grimvault")

        meta = store._conn.execute(
            "SELECT genre_slug, world_slug FROM session_meta WHERE id = 1"
        ).fetchone()
        assert meta is not None, (
            "session_meta row 1 must exist after init_session — reinit "
            "REPLACES, it does not blank out"
        )
        assert meta[0] == "caverns_and_claudes"
        assert meta[1] == "grimvault"

    def test_games_table_not_cleared_on_reinit(self, tmp_path: Path) -> None:
        """The slug-keyed ``games`` table is global, not per-slot. A
        per-slot reinit must not nuke the slug binding — that would lose
        the slug→genre/world mapping every other connect path depends on."""
        db = db_path_for_slug(tmp_path, "test-games-survives")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("caverns_and_claudes", "grimvault")
        upsert_game(
            store,
            slug="2026-04-19-grimvault",
            mode=GameMode.MULTIPLAYER,
            genre_slug="caverns_and_claudes",
            world_slug="grimvault",
        )
        _populate_slot_with_stale_rows(store)
        assert _row_count(store, "games") == 1, "fixture: games row inserted"

        store.init_session("caverns_and_claudes", "grimvault")

        assert _row_count(store, "games") == 1, (
            "Scope violation: per-slot reinit must not clear the global "
            "games (slug) table — slug→genre/world binding would be lost "
            "and every reconnect would 404."
        )


# ---------------------------------------------------------------------------
# AC1 (negative) — fresh slot reinits cleanly.
# ---------------------------------------------------------------------------


class TestInitSessionFreshSlotIsCleanNoOp:
    """A fresh slot must reinit without error: clear is a no-op, every
    per-slot table is already empty, ``session_meta`` row 1 is created
    by the call. Regression guard against a refuse-on-populated path
    being added by mistake."""

    def test_fresh_slot_per_slot_tables_remain_empty(self, tmp_path: Path) -> None:
        db = db_path_for_slug(tmp_path, "test-fresh-empty")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)

        store.init_session("caverns_and_claudes", "grimvault")

        for tbl in PER_SLOT_TABLES:
            assert _row_count(store, tbl) == 0, (
                f"fresh-slot reinit unexpectedly seeded {tbl}; clear must "
                "be a no-op on a slot that has no rows"
            )

    def test_fresh_slot_session_meta_installed(self, tmp_path: Path) -> None:
        """Fresh-slot reinit's actual contract: install ``session_meta``
        row 1. This is the existing pre-fix behavior — must keep working."""
        db = db_path_for_slug(tmp_path, "test-fresh-meta")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)

        store.init_session("caverns_and_claudes", "grimvault")

        meta = store._conn.execute(
            "SELECT genre_slug, world_slug FROM session_meta WHERE id = 1"
        ).fetchone()
        assert meta is not None
        assert meta[0] == "caverns_and_claudes"
        assert meta[1] == "grimvault"


# ---------------------------------------------------------------------------
# AC4 — OTEL ``session.slot_reinitialized`` watcher event.
# ---------------------------------------------------------------------------


class TestInitSessionEmitsWatcherEvent:
    """``init_session()`` MUST publish a ``session.slot_reinitialized``
    watcher event on every call so Sebastien's GM panel can verify the
    reinit fired (CLAUDE.md OTEL principle — without the event, a silent
    half-clear regression is invisible)."""

    def test_watcher_event_fires_on_populated_slot(self, tmp_path: Path) -> None:
        db = db_path_for_slug(tmp_path, "test-otel-populated")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("caverns_and_claudes", "grimvault")
        _populate_slot_with_stale_rows(store)

        with patch(
            "sidequest.game.persistence._watcher_publish",
        ) as wp:
            store.init_session("caverns_and_claudes", "grimvault")

        event_calls = [
            call
            for call in wp.call_args_list
            if call.args and call.args[0] == "session.slot_reinitialized"
        ]
        assert len(event_calls) == 1, (
            "AC4 violation: init_session() must emit exactly one "
            "session.slot_reinitialized watcher event per call. Got "
            f"{len(event_calls)} call(s) named 'session.slot_reinitialized' "
            f"out of {len(wp.call_args_list)} total publishes. The GM panel "
            "(Sebastien's lie-detector) cannot confirm reinit ran without it."
        )

    def test_watcher_event_attributes_on_populated_slot(self, tmp_path: Path) -> None:
        """Span attributes must be machine-readable: count of stale rows,
        list of cleared tables, mode='clear', and the slot identity
        (genre_slug, world_slug). The dashboard uses these to render
        the reinit row."""
        db = db_path_for_slug(tmp_path, "test-otel-attrs")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("caverns_and_claudes", "grimvault")
        _populate_slot_with_stale_rows(store)

        with patch(
            "sidequest.game.persistence._watcher_publish",
        ) as wp:
            store.init_session("caverns_and_claudes", "grimvault")

        event_calls = [
            call
            for call in wp.call_args_list
            if call.args and call.args[0] == "session.slot_reinitialized"
        ]
        assert event_calls, "no session.slot_reinitialized event emitted"
        attrs = event_calls[0].args[1] if len(event_calls[0].args) > 1 else {}

        for required in (
            "genre_slug",
            "world_slug",
            "cleared_tables",
            "prior_narrative_count",
            "prior_event_count",
            "mode",
        ):
            assert required in attrs, (
                f"AC4 violation: required attribute {required!r} missing "
                f"from session.slot_reinitialized event; got attrs={attrs!r}"
            )

        assert attrs["mode"] == "clear", (
            f"This story commits to the clear-on-reinit path; mode must be "
            f"'clear', got {attrs['mode']!r}"
        )
        assert attrs["genre_slug"] == "caverns_and_claudes"
        assert attrs["world_slug"] == "grimvault"
        assert attrs["prior_narrative_count"] == 1, (
            "fixture inserted exactly one narrative_log row before reinit; "
            f"event reported {attrs['prior_narrative_count']}"
        )
        assert attrs["prior_event_count"] == 1, (
            "fixture inserted exactly one events row before reinit; "
            f"event reported {attrs['prior_event_count']}"
        )
        cleared = attrs["cleared_tables"]
        for tbl in PER_SLOT_TABLES:
            assert tbl in cleared, (
                f"cleared_tables must list every per-slot table that was "
                f"cleared; missing {tbl!r}, got {cleared!r}"
            )

    def test_watcher_event_fires_on_fresh_slot_with_zero_priors(self, tmp_path: Path) -> None:
        """**Negative confirmation** — the GM panel needs to see that
        reinit ran cleanly even when there were no stale rows. Without
        this event, Sebastien cannot tell whether a quiet reinit happened
        or whether the codepath silently bypassed the clear."""
        db = db_path_for_slug(tmp_path, "test-otel-fresh")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)

        with patch(
            "sidequest.game.persistence._watcher_publish",
        ) as wp:
            store.init_session("caverns_and_claudes", "grimvault")

        event_calls = [
            call
            for call in wp.call_args_list
            if call.args and call.args[0] == "session.slot_reinitialized"
        ]
        assert len(event_calls) == 1, (
            "AC4 negative confirmation violation: init_session() on a "
            "fresh slot must STILL emit session.slot_reinitialized so "
            "the GM panel knows reinit ran. Silent skip = invisible "
            "regression."
        )
        attrs = event_calls[0].args[1] if len(event_calls[0].args) > 1 else {}
        assert attrs.get("prior_narrative_count") == 0
        assert attrs.get("prior_event_count") == 0


# ---------------------------------------------------------------------------
# AC5 — post-reinit precondition for the round-vs-max invariant.
# ---------------------------------------------------------------------------


class TestPostReinitInvariantPrecondition:
    """After ``init_session()``, ``max(narrative_log.round_number)`` must
    not wedge a fresh ``TurnManager(round=1, interaction=1)`` at session
    start. The full sync invariant (round vs. max across the session
    lifetime) is story 45-11; this test guarantees the **post-reinit
    precondition** that story can rely on."""

    def test_max_round_is_null_after_reinit_on_populated_slot(self, tmp_path: Path) -> None:
        db = db_path_for_slug(tmp_path, "test-invariant")
        db.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteStore(db)
        store.init_session("caverns_and_claudes", "grimvault")
        _populate_slot_with_stale_rows(store)

        store.init_session("caverns_and_claudes", "grimvault")

        max_round_row = store._conn.execute(
            "SELECT MAX(round_number) FROM narrative_log"
        ).fetchone()
        max_round = max_round_row[0] if max_round_row else None
        assert max_round is None, (
            "AC5 violation: stale narrative_log row's round_number "
            f"survived reinit (max_round={max_round!r}). A fresh "
            "TurnManager(round=1) would be wedged at session start "
            "because max_round > round. This is exactly the Playtest 3 "
            "prot_thokk/hant freeze."
        )


# ---------------------------------------------------------------------------
# SPAN_ROUTES registration — completeness lint must not silently drop the
# new span constant.
# ---------------------------------------------------------------------------


def test_span_constant_session_slot_reinitialized_exists() -> None:
    """The story registers a new OTEL span name. The constant must live
    in spans.py with the exact value ``session.slot_reinitialized`` so
    callers have a single import surface."""
    from sidequest.telemetry import spans

    assert hasattr(spans, "SPAN_SESSION_SLOT_REINITIALIZED"), (
        "missing SPAN_SESSION_SLOT_REINITIALIZED constant in "
        "sidequest/telemetry/spans.py — Story 45-5 needs this for the "
        "GM panel watcher event"
    )
    assert spans.SPAN_SESSION_SLOT_REINITIALIZED == "session.slot_reinitialized", (
        "constant value must equal 'session.slot_reinitialized' (the "
        "watcher event name); got "
        f"{spans.SPAN_SESSION_SLOT_REINITIALIZED!r}"
    )


def test_span_route_registered_for_session_slot_reinitialized() -> None:
    """Every SPAN_* constant must have an explicit routing decision —
    membership in ``SPAN_ROUTES`` or ``FLAT_ONLY_SPANS``. Without it,
    ``test_routing_completeness.py`` fails the lint."""
    from sidequest.telemetry import spans

    span_name = "session.slot_reinitialized"
    assert span_name in spans.SPAN_ROUTES or span_name in spans.FLAT_ONLY_SPANS, (
        f"{span_name!r} must be registered in SPAN_ROUTES (preferred — "
        "the dashboard renders the typed event) or FLAT_ONLY_SPANS (only "
        "if the dashboard truly does not need a typed row). Story 45-5 "
        "needs SPAN_ROUTES so Sebastien's GM panel renders the reinit row."
    )

    # Strong preference: SPAN_ROUTES, not FLAT_ONLY. The reinit event is
    # exactly the kind of subsystem decision the GM panel must surface.
    assert span_name in spans.SPAN_ROUTES, (
        "session.slot_reinitialized belongs in SPAN_ROUTES (state_transition "
        "on the session component) so the dashboard's Subsystems tab renders "
        "it. Putting it in FLAT_ONLY_SPANS would silently demote it to a "
        "timing-only span — Sebastien's lie-detector goes blind."
    )
    route = spans.SPAN_ROUTES[span_name]
    assert route.component == "session", (
        f"route.component should be 'session' (matches other session.* "
        f"events), got {route.component!r}"
    )


# ---------------------------------------------------------------------------
# Wiring sanity — ``init_session`` is the single seam every connect path
# uses; this test pins the import surface so a refactor can't silently
# move the seam without updating these tests.
# ---------------------------------------------------------------------------


def test_init_session_is_callable_on_sqlite_store() -> None:
    """Architectural pin: ``SqliteStore.init_session`` is the seam both
    ``ConnectHandler.handle()`` branches drive into. If this method ever
    moves or is renamed, every test in this file must follow."""
    assert hasattr(SqliteStore, "init_session"), (
        "SqliteStore.init_session() is the canonical reinit seam — "
        "removing or renaming it without coordinating with handlers/connect.py "
        "is a wire-test failure"
    )
    assert callable(SqliteStore.init_session)


@pytest.fixture(autouse=True)
def _reset_otel_tracer():
    """Each test gets a clean tracer state. Without this, span-export
    ordering in surrounding suites can leak attributes into our
    assertions."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    yield
