"""Unit + wiring tests for sidequest/server/emitters.py.

Phase 1 of session_handler decomposition. These tests verify:
1. Each extracted function exists with the expected signature.
2. The thin delegate methods on WebSocketSessionHandler still call
   into emitters.py (wiring guard per CLAUDE.md).
3. Behavior is preserved (functional parity with the pre-extraction
   methods).
"""

from __future__ import annotations

from sidequest.game.sqlite_repository import SqliteSaveRepository


def test_emitters_module_exposes_required_functions() -> None:
    """Wiring guard — the required emitter functions must be importable
    from sidequest.server.emitters by their canonical names."""
    from sidequest.server import emitters

    assert hasattr(emitters, "persist_scrapbook_entry")
    assert hasattr(emitters, "emit_event")
    assert hasattr(emitters, "emit_scrapbook_entry")


def test_persist_scrapbook_entry_delegate_calls_module_function(
    monkeypatch, session_handler_factory
) -> None:
    """Wiring guard — WebSocketSessionHandler._persist_scrapbook_entry
    must delegate to emitters.persist_scrapbook_entry."""
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server import emitters

    sd, handler = session_handler_factory()
    captured: list[tuple] = []

    def _spy(h, payload):
        captured.append((h, payload))

    monkeypatch.setattr(emitters, "persist_scrapbook_entry", _spy)

    payload = ScrapbookEntryPayload(
        turn_id=1,
        location="test_loc",
        narrative_excerpt="hello",
        scene_title=None,
        scene_type=None,
        image_url=None,
        world_facts=[],
        npcs_present=[],
    )
    handler._persist_scrapbook_entry(payload)

    assert captured == [(handler, payload)]


def test_persist_scrapbook_entry_inserts_row(session_handler_factory) -> None:
    """Behavioral test — calling the function inserts a row into the
    scrapbook_entries table that can be read back."""
    from sidequest.game.event_log import EventLog
    from sidequest.protocol.messages import ScrapbookEntryNpcRef, ScrapbookEntryPayload
    from sidequest.server import emitters

    sd, handler = session_handler_factory()
    # The factory does not seed an EventLog by default (legacy path);
    # attach one so the function has a store to write to.
    handler._event_log = EventLog(SqliteSaveRepository(sd.store))

    payload = ScrapbookEntryPayload(
        turn_id=42,
        location="test_loc",
        narrative_excerpt="The fighter pondered.",
        scene_title="A pondering",
        scene_type="character",
        image_url=None,
        world_facts=["a fact"],
        npcs_present=[
            ScrapbookEntryNpcRef(name="Goblin", role="opponent", disposition="hostile"),
        ],
    )

    emitters.persist_scrapbook_entry(handler, payload)

    rows = sd.store._conn.execute(
        "SELECT turn_id, location, narrative_excerpt FROM scrapbook_entries"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 42
    assert rows[0][1] == "test_loc"
    assert rows[0][2] == "The fighter pondered."


def test_update_scrapbook_image_url_backfills_most_recent_row(tmp_path) -> None:
    """Playtest 2026-05-02: when render.completed fires, the new helper
    must UPDATE the scrapbook_entries row for the matching turn_id from
    image_url=NULL to the served URL. On reconnect/replay, this is the
    only persisted record of which turn produced which image — the live
    IMAGE broadcast is ephemeral and missed entirely on browser reload.

    Uses a minimal stub instead of `session_handler_factory` because the
    factory loads a real genre pack from `tests/fixtures/packs/` and that
    fixture is missing the world-tier `openings.yaml` required since the
    canned-openings story (pre-existing baseline failure unrelated to
    this change). The helper only touches `handler._event_log.store`, so
    a minimal duck-typed stub is sufficient.
    """
    from sidequest.game.event_log import EventLog
    from sidequest.game.persistence import SqliteStore
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server import emitters

    store = SqliteStore(tmp_path / "test.db")

    class _Handler:
        pass

    handler = _Handler()
    handler._event_log = EventLog(SqliteSaveRepository(store))

    payload = ScrapbookEntryPayload(
        turn_id=7,
        location="The Kestrel — Galley, Mid-Coast",
        narrative_excerpt="The mug jitters once, exactly once.",
        scene_title="A clan-blue omen",
        scene_type="scene_illustration",
        image_url=None,
    )
    emitters.persist_scrapbook_entry(handler, payload)

    updated = emitters.update_scrapbook_image_url(
        handler, turn_id=7, image_url="/renders/zimage/render_abc.png"
    )
    assert updated is True

    rows = store._conn.execute(
        "SELECT turn_id, image_url FROM scrapbook_entries WHERE turn_id = 7"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "/renders/zimage/render_abc.png"

    # Idempotency: a second backfill for the same turn must NOT clobber —
    # we only update rows where image_url IS NULL, so the second call
    # finds nothing matching and returns False.
    updated_again = emitters.update_scrapbook_image_url(
        handler, turn_id=7, image_url="/renders/zimage/render_xyz.png"
    )
    assert updated_again is False
    final_url = store._conn.execute(
        "SELECT image_url FROM scrapbook_entries WHERE turn_id = 7"
    ).fetchone()[0]
    assert final_url == "/renders/zimage/render_abc.png", (
        "second update must not overwrite — first render wins per turn"
    )


def test_update_scrapbook_image_url_legacy_path_no_event_log_is_noop() -> None:
    """When handler has no event log, the helper returns False without
    raising — same shape as `persist_scrapbook_entry`."""
    from sidequest.server import emitters

    class _Handler:
        pass

    handler = _Handler()
    handler._event_log = None
    assert emitters.update_scrapbook_image_url(handler, 1, "/x.png") is False


def test_persist_scrapbook_entry_legacy_path_no_event_log_is_noop(
    session_handler_factory,
) -> None:
    """Behavioral test — when handler._event_log is None (legacy path),
    the function returns cleanly without writing or raising."""
    from sidequest.protocol.messages import ScrapbookEntryPayload
    from sidequest.server import emitters

    sd, handler = session_handler_factory()
    handler._event_log = None  # legacy path

    payload = ScrapbookEntryPayload(
        turn_id=1,
        location="test_loc",
        narrative_excerpt="nope",
        scene_title=None,
        scene_type=None,
        image_url=None,
        world_facts=[],
        npcs_present=[],
    )

    # Must not raise.
    emitters.persist_scrapbook_entry(handler, payload)


def test_emit_event_delegate_calls_module_function(monkeypatch, session_handler_factory) -> None:
    """Wiring guard — WebSocketSessionHandler._emit_event must delegate
    to emitters.emit_event."""
    from sidequest.server import emitters

    sd, handler = session_handler_factory()
    sentinel = object()
    captured: list[tuple] = []

    def _spy(h, kind, payload):
        captured.append((h, kind, payload))
        return sentinel

    monkeypatch.setattr(emitters, "emit_event", _spy)

    result = handler._emit_event("NARRATION", object())

    assert result is sentinel
    assert len(captured) == 1
    assert captured[0][0] is handler
    assert captured[0][1] == "NARRATION"


def test_emit_scrapbook_entry_delegate_calls_module_function(
    monkeypatch, session_handler_factory
) -> None:
    """Wiring guard — WebSocketSessionHandler._emit_scrapbook_entry
    must delegate to emitters.emit_scrapbook_entry."""
    from sidequest.game.session import GameSnapshot
    from sidequest.server import emitters

    sd, handler = session_handler_factory()
    captured: list[tuple] = []

    def _spy(h, *, sd, snapshot, result, render_status="rendered"):
        captured.append((h, sd, snapshot, result, render_status))

    monkeypatch.setattr(emitters, "emit_scrapbook_entry", _spy)

    snap = GameSnapshot(genre_slug=sd.genre_slug)
    sentinel_result = object()
    handler._emit_scrapbook_entry(sd=sd, snapshot=snap, result=sentinel_result)

    # Default render_status="rendered" — the delegate threads the kwarg
    # through unmodified (Story 45-30).
    assert captured == [(handler, sd, snap, sentinel_result, "rendered")]
