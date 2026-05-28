"""Unit + wiring tests for sidequest/server/emitters.py.

Phase 1 of session_handler decomposition. These tests verify:
1. Each extracted function exists with the expected signature.
2. The thin delegate methods on WebSocketSessionHandler still call
   into emitters.py (wiring guard per CLAUDE.md).
3. Behavior is preserved (functional parity with the pre-extraction
   methods).
"""

from __future__ import annotations


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


def test_persist_scrapbook_entry_inserts_row() -> None:
    """Behavioral test — calling the function delegates to the repository's
    append_scrapbook_entry method with the correct arguments."""
    from unittest.mock import MagicMock

    from sidequest.game.event_log import EventLog
    from sidequest.protocol.messages import ScrapbookEntryNpcRef, ScrapbookEntryPayload
    from sidequest.server import emitters

    mock_repo = MagicMock()
    mock_event_log = MagicMock(spec=EventLog)
    mock_event_log.repository = mock_repo

    class _Handler:
        pass

    handler = _Handler()
    handler._event_log = mock_event_log

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

    mock_repo.append_scrapbook_entry.assert_called_once_with(
        turn_id=42,
        scene_title="A pondering",
        scene_type="character",
        location="test_loc",
        image_url=None,
        narrative_excerpt="The fighter pondered.",
        world_facts=["a fact"],
        npcs_present=[
            {
                "name": "Goblin",
                "role": "opponent",
                "disposition": "hostile",
                # Story 65-6: portrait_url persisted (None when unset).
                "portrait_url": None,
            }
        ],
        render_status="rendered",
    )


def test_update_scrapbook_image_url_backfills_most_recent_row() -> None:
    """Playtest 2026-05-02: when render.completed fires, update_scrapbook_image_url
    must delegate to the repository's update_scrapbook_image_url method and
    return the boolean the repository returns.

    The idempotency contract (NULL-only update, second call returns False) is
    enforced by the repository implementation (PgScrapbookStore / SqliteSaveRepository).
    This test verifies the emitter passes through the repository's return value
    faithfully for both calls.
    """
    from unittest.mock import MagicMock

    from sidequest.game.event_log import EventLog
    from sidequest.server import emitters

    mock_repo = MagicMock()
    mock_event_log = MagicMock(spec=EventLog)
    mock_event_log.repository = mock_repo

    class _Handler:
        pass

    handler = _Handler()
    handler._event_log = mock_event_log

    # First call: repository reports a row was updated.
    mock_repo.update_scrapbook_image_url.return_value = True
    updated = emitters.update_scrapbook_image_url(
        handler, turn_id=7, image_url="/renders/zimage/render_abc.png"
    )
    assert updated is True
    mock_repo.update_scrapbook_image_url.assert_called_once_with(
        turn_id=7, image_url="/renders/zimage/render_abc.png"
    )

    # Second call: repository reports no NULL-image row found (idempotency).
    mock_repo.update_scrapbook_image_url.reset_mock()
    mock_repo.update_scrapbook_image_url.return_value = False
    updated_again = emitters.update_scrapbook_image_url(
        handler, turn_id=7, image_url="/renders/zimage/render_xyz.png"
    )
    assert updated_again is False


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

    def _spy(h, kind, payload, *, author_player_id=None, per_recipient_payload=None):
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
