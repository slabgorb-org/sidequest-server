"""RED-phase tests for JournalRequestHandler (story 50-14, ADR-100 Seam C).

The handler responds to JOURNAL_REQUEST with the requesting player's
character.known_facts, packaged as a JOURNAL_RESPONSE that satisfies the
UI contract defined in ``sidequest-ui/src/types/payloads.ts``::

    JournalResponsePayload {
      entries: Array<{
        fact_id: string;
        content: string;
        category: string;
        source: string;
        confidence: string;
        learned_turn: number;
      }>;
    }

Player→character lookup goes through ``snapshot.player_seats[player_id]``
per ADR-036 (the only character a player can introspect is their own
seat). No cross-player lookups are supported.

The suite exercises:

* protocol shape — ``JournalRequestMessage`` / ``JournalResponseMessage``
  wrappers participate in the ``_Phase1Variant`` discriminated union and
  round-trip through ``GameMessage.model_validate``;
* dispatch wiring — ``JournalRequestHandler`` is registered in
  ``WebSocketSessionHandler._message_handler_for`` and reachable through
  ``handle_message``;
* response correctness — every ``KnownFact`` on the seated character
  surfaces with its ``fact_id`` preserved (UI dedups by this) and every
  field of the UI contract populated;
* ADR-036 isolation — players see only their own seat's journal, and
  every error branch returns an ``ErrorMessage`` with a stable, distinct
  ``code`` (``session_unbound``, ``invalid_player_id``,
  ``player_unseated``, ``seat_broken``) so the UI can branch reliably and
  so a regression that swaps codes will fail a test;
* OTEL — ``SPAN_JOURNAL_REPLAY`` fires with ``character_name`` and
  ``entry_count`` attributes and is registered in the span catalog.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.persistence import GameMode, SqliteStore
from sidequest.game.session import GameSnapshot
from sidequest.protocol.enums import MessageType
from sidequest.protocol.messages import (
    ErrorMessage,
    JournalRequestMessage,
    JournalResponseMessage,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.session_room import RoomRegistry, SessionRoom

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _attach(handler: WebSocketSessionHandler) -> None:
    handler.attach_room_context(
        registry=RoomRegistry(),
        socket_id="sock-1",
        out_queue=asyncio.Queue(),
    )


def _character_with_facts(name: str, facts: list[KnownFact]) -> Character:
    """Build a Character with the supplied known_facts.

    Uses CreatureCore.name as the seat key (matches player_seats).
    """
    return Character(
        core=CreatureCore(
            name=name,
            description=f"{name}, journal-test seat",
            personality="curious",
            inventory=Inventory(),
        ),
        char_class="Sleuth",
        race="Human",
        backstory=f"{name} carries a small notebook everywhere.",
        known_facts=facts,
    )


def _bind_seated_room(
    handler: WebSocketSessionHandler,
    tmp_path: Path,
    seats: dict[str, Character],
) -> SessionRoom:
    """Bind a room with a snapshot whose player_seats map player_id → char.

    Returns the bound SessionRoom so tests can introspect it.
    """
    snapshot = GameSnapshot()
    for char in seats.values():
        snapshot.characters.append(char)
    snapshot.player_seats = {pid: ch.core.name for pid, ch in seats.items()}

    store = SqliteStore(tmp_path / "journal.db")
    room = SessionRoom(slug="journal-test", mode=GameMode.SOLO)
    room.bind_world(snapshot=snapshot, store=store)
    handler._room = room
    return room


def _three_facts() -> list[KnownFact]:
    """Three facts with mixed sources/confidences/turns — exercises full payload."""
    return [
        KnownFact(
            content="The bell tower chimes at midnight.",
            confidence="Certain",
            source="Observation",
            learned_turn=2,
        ),
        KnownFact(
            content="Lady Ashworth was seen near the conservatory.",
            confidence="Suspected",
            source="Gossip",
            learned_turn=5,
        ),
        KnownFact(
            content="The vicar keeps a second journal.",
            confidence="Rumored",
            source="ScenarioClue",
            learned_turn=7,
        ),
    ]


# ---------------------------------------------------------------------------
# AC1 — Handler receives JOURNAL_REQUEST and responds with JOURNAL_RESPONSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_journal_response_message(tmp_path: Path) -> None:
    """RED: dispatch returns a JOURNAL_RESPONSE wrapper for the requesting player."""
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", _three_facts())},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1, "handler must emit exactly one outbound message on success"
    response = outbound[0]
    assert isinstance(response, JournalResponseMessage), (
        f"expected JournalResponseMessage, got {type(response).__name__}"
    )
    assert response.type == MessageType.JOURNAL_RESPONSE
    assert response.player_id == "P1", (
        "response must be addressed to the requesting player (ADR-036)"
    )


@pytest.mark.asyncio
async def test_response_entry_count_matches_known_facts(tmp_path: Path) -> None:
    """RED: every KnownFact on the seated character appears in the response."""
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    facts = _three_facts()
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", facts)},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)
    response = outbound[0]

    assert isinstance(response, JournalResponseMessage)
    assert len(response.payload.entries) == len(facts), (
        "response.entries must include every KnownFact on the character — "
        "missing entries silently drops player knowledge"
    )


@pytest.mark.asyncio
async def test_response_entries_carry_full_ui_contract(tmp_path: Path) -> None:
    """Each entry carries the six fields the UI consumes and preserves identity.

    UI contract (sidequest-ui/src/types/payloads.ts JournalResponsePayload):
        fact_id, content, category, source, confidence, learned_turn.

    The fact_id assertion verifies pass-through identity (not just
    truthiness) so a regression that swaps fact_ids between entries — which
    would silently break ``useStateMirror`` dedup — fails this test.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    facts = _three_facts()
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", facts)},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)
    response = outbound[0]
    assert isinstance(response, JournalResponseMessage)

    by_content = {e.content: e for e in response.payload.entries}
    for fact in facts:
        entry = by_content.get(fact.content)
        assert entry is not None, (
            f"fact {fact.content!r} missing from response — handler must not drop facts"
        )
        assert entry.fact_id == fact.fact_id, (
            f"fact_id pass-through mismatch for {fact.content!r}: "
            f"response={entry.fact_id!r} source={fact.fact_id!r} — "
            "UI dedups by fact_id, so a swap would silently duplicate journal entries"
        )
        assert entry.confidence == fact.confidence, (
            f"confidence mismatch: response={entry.confidence!r} fact={fact.confidence!r}"
        )
        assert entry.source == fact.source, (
            f"source mismatch: response={entry.source!r} fact={fact.source!r}"
        )
        assert entry.learned_turn == fact.learned_turn, (
            f"learned_turn mismatch: response={entry.learned_turn} fact={fact.learned_turn}"
        )
        assert entry.category == fact.category, (
            f"category mismatch: response={entry.category!r} fact={fact.category!r} — "
            "UI passes category through validateCategory; do NOT silently default"
        )


@pytest.mark.asyncio
async def test_fact_id_stable_across_requests(tmp_path: Path) -> None:
    """RED: same character, two requests, same fact_ids — UI relies on this for dedup.

    ``useStateMirror`` deduplicates entries by fact_id across messages. If
    the handler regenerates fact_ids per call (e.g. enumerate index that
    shifts when facts are added), the UI will accumulate duplicates.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", _three_facts())},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    first = await handler.handle_message(msg)
    second = await handler.handle_message(msg)

    assert isinstance(first[0], JournalResponseMessage)
    assert isinstance(second[0], JournalResponseMessage)

    first_ids = sorted(e.fact_id for e in first[0].payload.entries)
    second_ids = sorted(e.fact_id for e in second[0].payload.entries)
    assert first_ids == second_ids, (
        "fact_ids must be stable across requests for the same character/snapshot — "
        "unstable IDs will cause UI to accumulate duplicate journal entries"
    )


@pytest.mark.asyncio
async def test_empty_known_facts_returns_empty_entries(tmp_path: Path) -> None:
    """RED: character with no facts returns an empty list — NOT an error.

    A character may legitimately have no journal entries (early game,
    nothing learned yet). This is distinct from a missing seat (error).
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", [])},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    assert isinstance(outbound[0], JournalResponseMessage)
    assert outbound[0].payload.entries == [], (
        "empty known_facts must produce an empty list, not an error"
    )


# ---------------------------------------------------------------------------
# AC2 — Multiplayer validation via ADR-036 (player→character via player_seats)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbound_room_returns_session_unbound_error(tmp_path: Path) -> None:
    """RED: JOURNAL_REQUEST without a bound room → ErrorMessage(session_unbound).

    Mirrors the OrbitalIntent precedent — UI catches ``session_unbound``
    and fires SESSION_EVENT{connect} to recover. We MUST NOT crash or
    silently return empty entries.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    # No room bound.

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    err = outbound[0]
    assert isinstance(err, ErrorMessage), (
        f"expected ErrorMessage on unbound room, got {type(err).__name__}"
    )
    assert err.payload.code == "session_unbound", (
        f"error code must be 'session_unbound' for UI auto-recovery, got {err.payload.code!r}"
    )


@pytest.mark.asyncio
async def test_unseated_player_returns_error_not_empty_list(tmp_path: Path) -> None:
    """RED: player_id with no seat → ErrorMessage, NOT an empty JournalResponse.

    The "no silent fallbacks" rule (CLAUDE.md): returning an empty list
    for an unseated player would mask a misconfigured client (wrong
    player_id, lobby/play state confusion) and the table would be
    debugging "why is the journal blank?" for an hour.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", _three_facts())},
    )

    msg = JournalRequestMessage(payload={}, player_id="P_GHOST")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    err = outbound[0]
    assert isinstance(err, ErrorMessage), (
        f"unseated player must get ErrorMessage, got {type(err).__name__} — "
        "silent empty list would mask the bug"
    )
    assert err.payload.code == "player_unseated", (
        f"unseated-player branch must emit code='player_unseated' for UI branching, "
        f"got {err.payload.code!r}"
    )


@pytest.mark.asyncio
async def test_player_only_sees_own_journal_not_peers(tmp_path: Path) -> None:
    """RED: two seated players; each request returns only the requester's facts.

    ADR-036 doctrine: a player can introspect only their own character's
    state. Cross-player journal access is forbidden.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)

    p1_fact = KnownFact(
        content="P1-ONLY: gold cufflink in the study",
        confidence="Certain",
        source="Observation",
        learned_turn=1,
    )
    p2_fact = KnownFact(
        content="P2-ONLY: cipher under the rug",
        confidence="Certain",
        source="Observation",
        learned_turn=1,
    )
    _bind_seated_room(
        handler,
        tmp_path,
        {
            "P1": _character_with_facts("Rux", [p1_fact]),
            "P2": _character_with_facts("Nim", [p2_fact]),
        },
    )

    p1_out = await handler.handle_message(
        JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    )
    p2_out = await handler.handle_message(
        JournalRequestMessage(payload={}, player_id="P2")  # type: ignore[arg-type]
    )

    assert isinstance(p1_out[0], JournalResponseMessage)
    assert isinstance(p2_out[0], JournalResponseMessage)
    p1_contents = {e.content for e in p1_out[0].payload.entries}
    p2_contents = {e.content for e in p2_out[0].payload.entries}

    assert p1_fact.content in p1_contents
    assert p2_fact.content not in p1_contents, "P1 request leaked P2's journal — ADR-036 violation"
    assert p2_fact.content in p2_contents
    assert p1_fact.content not in p2_contents, "P2 request leaked P1's journal — ADR-036 violation"


@pytest.mark.asyncio
async def test_seat_points_to_missing_character_returns_error(tmp_path: Path) -> None:
    """RED: player_seats[P1]='Rux' but no Character named 'Rux' → error, not empty.

    This is a state-consistency violation (seat without character). The
    handler must surface it, not silently return empty entries.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)

    snapshot = GameSnapshot()
    # Seat referencing a nonexistent character.
    snapshot.player_seats = {"P1": "Rux"}
    # No characters in the list — broken state.

    store = SqliteStore(tmp_path / "broken.db")
    room = SessionRoom(slug="broken", mode=GameMode.SOLO)
    room.bind_world(snapshot=snapshot, store=store)
    handler._room = room

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    err = outbound[0]
    assert isinstance(err, ErrorMessage), (
        "broken seat (no matching character) must raise an error, not return empty"
    )
    assert err.payload.code == "seat_broken", (
        f"broken-seat branch must emit code='seat_broken' so the GM panel can "
        f"distinguish state-corruption from missing-seat, got {err.payload.code!r}"
    )


@pytest.mark.asyncio
async def test_filter_field_is_currently_ignored(tmp_path: Path) -> None:
    """``JournalRequestPayload.filter`` is documented as a future-use placeholder.

    Until a future story wires up filter dispatch, the handler must
    return every known_fact regardless of the filter value. This test
    pins that ignore-behavior so the day someone implements filtering
    they will have a baseline regression signal (this test will fail
    when filtering becomes selective).
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    facts = _three_facts()
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", facts)},
    )

    msg = JournalRequestMessage(
        payload={"filter": "Lore"},  # type: ignore[arg-type]
        player_id="P1",
    )
    outbound = await handler.handle_message(msg)
    response = outbound[0]
    assert isinstance(response, JournalResponseMessage)
    assert len(response.payload.entries) == len(facts), (
        "filter field is documented as future-use; the handler must currently "
        "return all known_facts regardless of filter value"
    )


@pytest.mark.asyncio
async def test_empty_player_id_returns_error(tmp_path: Path) -> None:
    """RED: missing/empty player_id → error.

    Defensive boundary check (python.md rule #11: input validation at API
    handlers). An empty player_id cannot match any seat — but if the
    handler treats "" as a sentinel for "first seat", multiple bugs
    follow.
    """
    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", _three_facts())},
    )

    msg = JournalRequestMessage(payload={}, player_id="")  # type: ignore[arg-type]
    outbound = await handler.handle_message(msg)

    assert len(outbound) == 1
    err = outbound[0]
    assert isinstance(err, ErrorMessage), (
        "empty player_id must produce an error, not fall through to a default seat"
    )
    assert err.payload.code == "invalid_player_id", (
        f"empty-player_id branch must emit code='invalid_player_id', got {err.payload.code!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — OTEL observability (SPAN_JOURNAL_REPLAY)
# ---------------------------------------------------------------------------


@pytest.fixture
def otel_capture():
    """SDK provider + in-memory exporter — mirrors test_npc_edge_publish_wiring.py."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.mark.asyncio
async def test_handler_emits_journal_replay_span(
    tmp_path: Path,
    otel_capture: InMemorySpanExporter,
) -> None:
    """RED: SPAN_JOURNAL_REPLAY fires with character_name + entry_count.

    Per OTEL observability principle (CLAUDE.md): every backend fix that
    touches a subsystem MUST add OTEL watcher events. The GM panel is
    the lie detector — without this span, Sebastien has no way to verify
    the handler actually engaged.
    """
    # Constant must exist — RED on missing constant.
    from sidequest.telemetry.spans import SPAN_JOURNAL_REPLAY  # noqa: F401

    handler = WebSocketSessionHandler(save_dir=tmp_path / "saves", genre_pack_search_paths=[])
    _attach(handler)
    facts = _three_facts()
    _bind_seated_room(
        handler,
        tmp_path,
        {"P1": _character_with_facts("Rux", facts)},
    )

    msg = JournalRequestMessage(payload={}, player_id="P1")  # type: ignore[arg-type]
    await handler.handle_message(msg)

    finished = otel_capture.get_finished_spans()
    journal_spans = [s for s in finished if s.name == "journal.replay"]
    assert len(journal_spans) == 1, (
        f"expected exactly one journal.replay span, got {len(journal_spans)} "
        f"(span names: {[s.name for s in finished]})"
    )
    span = journal_spans[0]
    assert span.attributes is not None
    assert span.attributes.get("character_name") == "Rux", (
        f"character_name attribute missing or wrong: {span.attributes.get('character_name')!r}"
    )
    assert span.attributes.get("entry_count") == len(facts), (
        f"entry_count attribute missing or wrong: "
        f"{span.attributes.get('entry_count')!r}, expected {len(facts)}"
    )


@pytest.mark.asyncio
async def test_journal_replay_span_registered_in_catalog() -> None:
    """RED: SPAN_JOURNAL_REPLAY must be in FLAT_ONLY_SPANS or SPAN_ROUTES.

    ``tests/telemetry/test_routing_completeness.py`` enforces this for
    every span constant, but we duplicate the check here so the failure
    points at this story rather than a global catalog test.
    """
    from sidequest.telemetry.spans import (
        FLAT_ONLY_SPANS,
        SPAN_JOURNAL_REPLAY,
        SPAN_ROUTES,
    )

    registered = SPAN_JOURNAL_REPLAY in FLAT_ONLY_SPANS or SPAN_JOURNAL_REPLAY in SPAN_ROUTES
    assert registered, (
        f"SPAN_JOURNAL_REPLAY={SPAN_JOURNAL_REPLAY!r} not registered in "
        "FLAT_ONLY_SPANS or SPAN_ROUTES — add to spans/journal.py"
    )


# ---------------------------------------------------------------------------
# AC4 / AC5 — Wiring: handler is reachable from real dispatch
# ---------------------------------------------------------------------------


def test_handler_is_registered() -> None:
    """RED: WebSocketSessionHandler routes JOURNAL_REQUEST to a real handler.

    Wiring test per CLAUDE.md "Every Test Suite Needs a Wiring Test". A
    handler module that exists but is not imported into the registry is
    dead code; this guards against that failure mode.
    """
    registered = WebSocketSessionHandler._message_handler_for("JOURNAL_REQUEST")
    assert registered is not None, (
        "JOURNAL_REQUEST not wired into WebSocketSessionHandler._message_handler_for — "
        "add `from sidequest.handlers.journal_request import HANDLER as JOURNAL_REQUEST_HANDLER` "
        "and register it in the dict"
    )


def test_journal_request_payload_in_phase1_variant() -> None:
    """RED: JournalRequestMessage + JournalResponseMessage participate in the union.

    The discriminated union must accept JOURNAL_REQUEST / JOURNAL_RESPONSE
    or wire-format messages will fail to deserialize at the WebSocket
    boundary, even if the handler is wired.
    """
    from sidequest.protocol.messages import GameMessage

    # Deserialize a wire-format JOURNAL_REQUEST and confirm the discriminator
    # routed it to the JournalRequestPayload shape (with default filter=None).
    msg = GameMessage.model_validate({"type": "JOURNAL_REQUEST", "payload": {}, "player_id": "P1"})
    assert msg.player_id == "P1"
    assert msg.payload.filter is None

    # Deserialize a wire-format JOURNAL_RESPONSE with one entry and confirm
    # the discriminator routed it to the JournalResponsePayload shape with
    # the entry's fields populated (not just that the type tag matched).
    response = GameMessage.model_validate(
        {
            "type": "JOURNAL_RESPONSE",
            "payload": {
                "entries": [
                    {
                        "fact_id": "f-1",
                        "content": "test",
                        "category": "Lore",
                        "source": "Observation",
                        "confidence": "Certain",
                        "learned_turn": 1,
                    }
                ]
            },
            "player_id": "P1",
        }
    )
    assert response.player_id == "P1"
    assert len(response.payload.entries) == 1
    assert response.payload.entries[0].fact_id == "f-1"
    assert response.payload.entries[0].content == "test"
    assert response.payload.entries[0].learned_turn == 1
