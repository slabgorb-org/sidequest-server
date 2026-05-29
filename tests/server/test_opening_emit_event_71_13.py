"""Story 71-13 — failing tests (RED phase).

The MP opening is the LAST narration path still on the raw transport fan-out
(``room.broadcast``).  These tests assert the CORRECT post-71-13 behaviour
using the same ``emit_event(author_player_id=<driver>)`` Track-A pattern that
every other narration turn already uses.

All tests FAIL NOW (RED) because the current code uses ``room.broadcast``
which does NOT:
  • persist events (no seq assignment)
  • run per-recipient projection / visible_to filter
  • apply per-recipient POV swap live
  • emit ``emit.author_resolved`` / ``projection.filter.decide`` OTEL spans

They will pass (GREEN) once Dev routes the opening through ``emit_event``,
deletes the 71-5 helper + broadcast block (AC4), and imports ``emit_event``
at the top of ``chargen_mixin``.

AC coverage
-----------
AC1  (wiring)    — test_opening_pov_swap_71_5.test_opening_does_not_use_room_broadcast
AC2  (anchor-peer live-swap)  — test_anchor_peer_gets_live_pov_swap_not_only_on_reconnect
AC3  (visible_to exclusion)   — test_visible_to_private_opening_card_excluded_from_non_recipients
AC4  (seq / persistence)      — test_opening_events_persisted_with_seq_assigned
AC5  (OTEL author_resolved)   — test_emit_author_resolved_fires_with_project_emitter_true
AC6  (OTEL per-player decide) — test_projection_filter_decide_fires_per_connected_player
AC7  (OTEL retired spans)     — test_opening_pov_swap_71_5.test_broadcast_to_peers_watcher_event_retired
                                test_opening_pov_swap_71_5.test_pov_swap_helper_watcher_event_retired
AC8  (solo Invariant-3)       — test_solo_opening_also_persisted_with_seq

Requires Postgres (opening events persist per ADR-115). Run with::

    SIDEQUEST_TEST_DATABASE_URL=postgresql://$USER@localhost:5432/sidequest_test \\
    uv run pytest -n0 tests/server/test_opening_emit_event_71_13.py

SKIP ≠ RED — tests must run and FAIL, not skip.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.persistence import GameMode
from sidequest.game.projection.composed import ComposedFilter
from sidequest.game.projection.rules import load_rules_from_yaml_str
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    NarrationMessage,
    NarrationPayload,
)
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import WebSocketSessionHandler
from sidequest.server.websocket_handlers import chargen_mixin
from tests.server.test_opening_turn_bootstrap import _connect, claude_mock, handler  # noqa: F401

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRIVER_PID = "p_driver"
PEER_PID = "p_peer"
DRIVER_SOCK = "sock-driver"
PEER_SOCK = "sock-peer"

SEED_TEXT = "The galley hatch yawns open; cool recycled air drifts out."
PROSE_TEXT_DRIVER_ANCHOR = "Rux steps into the galley as the hatch seals behind Rux."
PROSE_TEXT_PEER_ANCHOR = "Donut steps into the galley as the hatch seals behind Donut."
PRIVATE_TEXT = "Only the driver can read this private opening note."

# Rules YAML that wires VisibilityTagRule for NARRATION — ensures the
# projection filter honours visible_to in tests regardless of content-pack
# projection.yaml state.
_NARRATION_VISIBILITY_RULES_YAML = """
rules:
  - kind: NARRATION
    visibility_tag: {}
"""

# ---------------------------------------------------------------------------
# Helpers: isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db (ADR-115 F1)."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Helpers: OTEL
# ---------------------------------------------------------------------------


def _setup_tracing() -> InMemorySpanExporter:
    """Attach an in-memory span exporter to the active OTEL provider.

    Mirrors test_merged_mp_emitter_projection._setup_tracing — adds a
    SimpleSpanProcessor to whichever TracerProvider is already active so we
    capture spans without resetting global state.
    """
    exporter = InMemorySpanExporter()
    current = trace.get_tracer_provider()
    if hasattr(current, "add_span_processor"):
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    return exporter


def _narration_decide_player_ids(exporter: InMemorySpanExporter) -> set[str]:
    """Return the set of player_ids that received a projection.filter.decide
    span for a NARRATION event.  Mirrors _decide_player_ids from Track-A
    reference tests but filtered to NARRATION so ancillary spans (e.g.
    chargen CONFRONTATION) don't pollute the assertion."""
    return {
        str((s.attributes or {}).get("player_id", ""))
        for s in exporter.get_finished_spans()
        if s.name == "projection.filter.decide"
        and (s.attributes or {}).get("event.kind") == "NARRATION"
    }


# ---------------------------------------------------------------------------
# Helpers: chargen bootstrap
# ---------------------------------------------------------------------------


async def _walk_to_confirmation(h: WebSocketSessionHandler) -> None:
    """Drive chargen up to (not through) the confirmation commit."""
    sd = h._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None
    while not builder.is_confirmation():
        scene = builder.current_scene()
        eff = scene.mechanical_effects
        if eff is not None and eff.assignment_required:
            pool = builder.arrangement_pool() or []
            sorted_pool = sorted(pool, reverse=True)
            stat_order = list(builder._ability_score_names)  # type: ignore[attr-defined]
            for stat, value in zip(stat_order, sorted_pool, strict=True):
                out = await h.handle_message(
                    CharacterCreationMessage(
                        payload=CharacterCreationPayload(
                            phase="arrange_assign", stat=stat, value=value
                        ),
                        player_id="pid",
                    )
                )
                if out and isinstance(out[0], ErrorMessage):
                    raise AssertionError(f"walk error: {out[0].payload.message}")
            payload = CharacterCreationPayload(phase="arrange_confirm")
        elif eff is not None and eff.identity_capture is not None:
            payload = CharacterCreationPayload(
                phase="story_confirm",
                pronouns="they/them",
                background="A wanderer's past.",
                description="Watchful eyes, quiet hands.",
            )
        elif scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Rux")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await h.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


def _make_mp(
    h: WebSocketSessionHandler,
    *,
    driver_pc: str = "Rux",
    peer_pc: str = "Donut",
    peer_pronouns: str = "she/her",
    slug: str = "opening-71-13",
) -> tuple[asyncio.Queue, asyncio.Queue]:
    """Rebind handler onto a fresh MULTIPLAYER room.

    Returns ``(q_driver, q_peer)`` — both outbound queues.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory
    from sidequest.server.session_room import SessionRoom

    sd = h._session_data  # type: ignore[attr-defined]
    sd.mode = GameMode.MULTIPLAYER
    driver_pid = sd.player_id or DRIVER_PID
    sd.player_id = driver_pid
    sd.player_name = driver_pc
    snap = sd.snapshot

    # Ensure peer character exists with pronouns for POV-swap tests.
    if not any(c.core.name == peer_pc for c in snap.characters):
        snap.characters.append(
            Character(
                core=CreatureCore(
                    name=peer_pc,
                    description="A peer PC.",
                    personality="bold",
                    inventory=Inventory(),
                ),
                char_class="Fighter",
                race="Human",
                backstory="A wandering adventurer.",
                pronouns=peer_pronouns,
            )
        )
    else:
        # Set pronouns on the existing character so swap can fire.
        for c in snap.characters:
            if c.core.name == peer_pc:
                c.pronouns = peer_pronouns

    snap.player_seats[driver_pid] = driver_pc
    snap.player_seats[PEER_PID] = peer_pc

    room = SessionRoom(slug=slug, mode=GameMode.MULTIPLAYER)
    room.bind_world(snapshot=snap, store=sd.repository)
    room.connect(driver_pid, socket_id=DRIVER_SOCK)
    room.seat(driver_pid, character_slot=driver_pc)
    room.transition_to_playing(driver_pid)
    room.connect(PEER_PID, socket_id=PEER_SOCK)
    room.seat(PEER_PID, character_slot=peer_pc)
    room.transition_to_playing(PEER_PID)

    q_driver: asyncio.Queue = asyncio.Queue()
    q_peer: asyncio.Queue = asyncio.Queue()
    room.attach_outbound(DRIVER_SOCK, q_driver)
    room.attach_outbound(PEER_SOCK, q_peer)
    h._room = room
    sd._room = room
    h._socket_id = DRIVER_SOCK
    return q_driver, q_peer


async def _fire_opening(
    h: WebSocketSessionHandler,
    monkeypatch: pytest.MonkeyPatch,
    *,
    opening_factory,
) -> list[object]:
    """Drive the confirmation commit with a canned opening; return local out.

    Post sq-playtest 2026-05-28 #G1, the real ``_run_opening_turn_narration``
    EMITS its frames through ``emit_event`` (cold-open) / ``_execute_narration_turn``
    (narrator prose) and the chargen caller only ``out.extend(...)`` the already-
    emitted frames — it no longer re-emits (the old loop blind-persisted
    RENDER_QUEUED / NARRATION_END / AUDIO_CUE frames under kind="NARRATION" and
    bricked reconnect). This fake mirrors that contract: it routes each canned
    opening frame through the REAL ``_emit_event`` so the event-sourcing /
    projection / POV / OTEL outcomes under test actually fire, using the same
    author rule (driver pid when >1 connected, else None) the method uses.
    """
    monkeypatch.setattr(chargen_mixin, "_should_fire_opening_narration", lambda _sd, _room: True)

    async def _emitting_opening(sd: object, _player_id: str, _span: object) -> list[object]:
        room = h._room
        connected = (
            room.connected_player_ids()
            if room is not None and callable(getattr(room, "connected_player_ids", None))
            else []
        )
        author = sd.player_id if len(connected) > 1 else None  # type: ignore[attr-defined]
        return [
            h._emit_event("NARRATION", m.payload, author_player_id=author)
            for m in opening_factory()
        ]

    monkeypatch.setattr(h, "_run_opening_turn_narration", _emitting_opening)
    out = await h.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )
    return list(out)


def _narration_texts_from_queue(q: asyncio.Queue) -> list[str]:
    """Drain a queue and return text strings from NarrationMessage payloads."""
    texts: list[str] = []
    while not q.empty():
        msg = q.get_nowait()
        payload = getattr(msg, "payload", None)
        raw = getattr(payload, "text", None)
        root = getattr(raw, "root", None)
        texts.append(root if isinstance(root, str) else str(raw))
    return texts


def _narration_texts_from_list(messages: list[object]) -> list[str]:
    """Extract text from NarrationMessage objects in a list."""
    texts: list[str] = []
    for m in messages:
        payload = getattr(m, "payload", None)
        raw = getattr(payload, "text", None)
        if raw is None:
            continue
        root = getattr(raw, "root", None)
        texts.append(root if isinstance(root, str) else str(raw))
    return texts


# ---------------------------------------------------------------------------
# RED tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anchor_peer_gets_live_pov_swap_not_only_on_reconnect(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC2 — RED: anchor-peer currently gets raw 3rd-person via room.broadcast.

    When a PEER's PC == the opening's ``anchor_pc``, they must receive their
    card in 2nd person ("You step…") LIVE — not only on reconnect via
    event-log projection.

    Setup: driver seats "Rux" (non-anchor); peer seats "Donut" (THE anchor).
    Canned opening has ``anchor_pc="Donut"``.

    Current behaviour (RED): ``room.broadcast`` sends the raw "Donut steps…"
    to the peer → assertion on "You step…" FAILS.

    After fix (GREEN): ``emit_event(author_player_id=driver_pid)`` calls
    ``_apply_pov_swap`` for the peer (Donut == anchor_pc) → peer receives
    "You step…".
    """
    await _connect(handler)
    await _walk_to_confirmation(handler)

    _q_driver, q_peer = _make_mp(
        handler,
        driver_pc="Rux",
        peer_pc="Donut",
        peer_pronouns="she/her",
        slug="opening-71-13-anchor-peer",
    )

    # Override projection filter to ensure VisibilityTagRule fires.
    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str(_NARRATION_VISIBILITY_RULES_YAML),
        pack_slug="caverns_and_claudes",
    )

    def _peer_anchored_opening():
        anchored = {"visible_to": "all", "anchor_pc": "Donut", "pov_strategy": "pc_anchored"}
        return [
            NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PROSE_TEXT_PEER_ANCHOR),
                    visibility_sidecar=anchored,
                )
            ),
        ]

    await _fire_opening(handler, monkeypatch, opening_factory=_peer_anchored_opening)

    peer_texts = _narration_texts_from_queue(q_peer)
    assert any("You step into the galley" in t for t in peer_texts), (
        "Anchor-peer (Donut, anchor_pc=='Donut') must receive their opening card in "
        "2nd person ('You step…') LIVE via emit_event POV swap — not only on "
        f"reconnect.  Peer received: {peer_texts!r}"
    )
    assert not any("Donut steps into the galley" in t for t in peer_texts), (
        "Anchor-peer must NOT see their own name in 3rd person after the fix. "
        f"Peer received: {peer_texts!r}"
    )


@pytest.mark.asyncio
async def test_visible_to_private_opening_card_excluded_from_non_recipients(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC3 — RED: visible_to private card currently leaks to ALL players via broadcast.

    An opening NARRATION card with ``visible_to=[driver_pid]`` must be
    delivered ONLY to the driver; the peer must NOT receive it.

    Current behaviour (RED): ``room.broadcast`` sends it to the peer →
    assertion fails.

    After fix (GREEN): ``emit_event`` → ``ComposedFilter.project()`` reads
    ``visible_to`` → excludes the peer → peer queue has no private card.
    """
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _q_driver, q_peer = _make_mp(
        handler, slug="opening-71-13-visible-to"
    )

    # Explicit VisibilityTagRule for NARRATION — projection filter must honour
    # visible_to regardless of content-pack state.
    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str(_NARRATION_VISIBILITY_RULES_YAML),
        pack_slug="caverns_and_claudes",
    )

    driver_pid = handler._session_data.player_id  # type: ignore[union-attr]

    def _private_opening():
        # One card private to driver only.
        return [
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PRIVATE_TEXT),
                    visibility_sidecar={
                        "visible_to": [driver_pid],
                        "anchor_pc": None,
                        "pov_strategy": None,
                    },
                )
            ),
        ]

    await _fire_opening(handler, monkeypatch, opening_factory=_private_opening)

    peer_texts = _narration_texts_from_queue(q_peer)
    assert PRIVATE_TEXT not in peer_texts, (
        "A private opening card (visible_to=[driver_pid]) must NOT reach the peer. "
        "room.broadcast leaks it to everyone — emit_event fixes this via "
        f"ComposedFilter.project(). Peer received: {peer_texts!r}"
    )


@pytest.mark.asyncio
async def test_opening_events_persisted_with_seq_assigned(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC5 — RED: opening messages currently have seq=0 (no EventLog persistence).

    After 71-13, ``emit_event`` runs the C2 transaction (``EventLog.append``)
    for each opening message, assigning a monotonic ``seq`` from the DB.

    Current behaviour (RED): ``out.extend(driver_copies)`` returns copies from
    the helper with ``seq=0`` → assertion fails.

    After fix (GREEN): ``emit_event`` returns messages with ``seq > 0``.
    """
    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler, slug="opening-71-13-seq")

    def _standard_opening():
        anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
        return [
            NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PROSE_TEXT_DRIVER_ANCHOR),
                    visibility_sidecar=anchored,
                )
            ),
        ]

    out = await _fire_opening(handler, monkeypatch, opening_factory=_standard_opening)

    narration_msgs = [m for m in out if isinstance(m, NarrationMessage)]
    assert narration_msgs, "No NarrationMessage found in opening output"
    seqs = [m.payload.seq for m in narration_msgs]
    assert any(s > 0 for s in seqs), (
        "Opening events must be persisted (EventLog.append assigns seq > 0). "
        "The current room.broadcast path skips EventLog — seq stays 0. "
        f"Got seq values: {seqs!r}"
    )


@pytest.mark.asyncio
async def test_emit_author_resolved_fires_with_project_emitter_true(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC7 (OTEL) — RED: emit.author_resolved does NOT fire for the opening today.

    After 71-13, ``emit_event(author_player_id=<driver_pid>)`` fires the
    ``emit.author_resolved`` watcher event with ``project_emitter: True`` for
    each opening message — confirming Track A engaged for the opening.

    Current behaviour (RED): ``emit_event`` is never called for the opening →
    no ``emit.author_resolved`` event → assertion fails.
    """
    watcher_events: list[dict] = []

    def _spy_session_watcher(event_type: str, fields: object, **_: object) -> None:
        if isinstance(fields, dict) and fields.get("field") == "emit.author_resolved":
            watcher_events.append(dict(fields))

    # emitters.py does `from sidequest.server.session_handler import _watcher_publish`
    # inside the emit_event function body — patching the re-export here ensures
    # the local import picks up the spy.
    monkeypatch.setattr("sidequest.server.session_handler._watcher_publish", _spy_session_watcher)

    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler, slug="opening-71-13-author-resolved")

    def _std_opening():
        anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
        return [
            NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PROSE_TEXT_DRIVER_ANCHOR),
                    visibility_sidecar=anchored,
                )
            ),
        ]

    await _fire_opening(handler, monkeypatch, opening_factory=_std_opening)

    narration_author_resolved = [
        e for e in watcher_events
        if e.get("kind") == "NARRATION" and e.get("project_emitter") is True
    ]
    assert narration_author_resolved, (
        "emit.author_resolved must fire with project_emitter=True for each "
        "NARRATION opening message — confirming Track A engaged.  "
        "Currently emit_event is not called for the opening at all.  "
        f"All emit.author_resolved events seen: {watcher_events!r}"
    )


@pytest.mark.asyncio
async def test_projection_filter_decide_fires_per_connected_player(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC7 (OTEL) — RED: projection.filter.decide does NOT fire for opening today.

    After 71-13, ``emit_event`` calls ``projection_filter.project()`` once per
    connected player per opening event, emitting a ``projection.filter.decide``
    OTEL span for each.  For a 2-player opening with seed + prose, this fires
    at least twice (once per player for at least one NARRATION event).

    Current behaviour (RED): ``room.broadcast`` bypasses the projection filter →
    zero ``projection.filter.decide`` spans for NARRATION → assertion fails.
    """
    exporter = _setup_tracing()
    exporter.clear()

    handler._projection_filter = ComposedFilter(
        rules=load_rules_from_yaml_str(_NARRATION_VISIBILITY_RULES_YAML),
        pack_slug="caverns_and_claudes",
    )

    await _connect(handler)
    await _walk_to_confirmation(handler)
    _make_mp(handler, slug="opening-71-13-decide")

    def _std_opening():
        anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
        return [
            NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PROSE_TEXT_DRIVER_ANCHOR),
                    visibility_sidecar=anchored,
                )
            ),
        ]

    await _fire_opening(handler, monkeypatch, opening_factory=_std_opening)

    decided = _narration_decide_player_ids(exporter)
    assert decided, (
        "projection.filter.decide must fire for each connected player for the "
        "opening NARRATION events.  Currently room.broadcast bypasses the "
        "projection filter entirely — zero decide spans.  "
        f"Players with decide span: {decided!r}"
    )
    # Both driver and peer must each get a projection decision for the opening.
    driver_pid = handler._session_data.player_id  # type: ignore[union-attr]
    assert driver_pid in decided, (
        f"Driver ({driver_pid!r}) must receive a projection.filter.decide span "
        f"(Track A — driver projected like a peer).  Got: {decided!r}"
    )
    assert PEER_PID in decided, (
        f"Peer ({PEER_PID!r}) must receive a projection.filter.decide span. "
        f"Got: {decided!r}"
    )


@pytest.mark.asyncio
async def test_solo_opening_also_persisted_with_seq(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """AC8/Q3 — RED: solo opening currently has seq=0 (no EventLog persistence).

    In SOLO mode, ``author_player_id=None`` preserves Invariant-3 (raw bypass).
    Even so, ``emit_event`` still runs the C2 EventLog transaction and assigns
    ``seq``.  The driver's opening messages must have ``seq > 0`` after the fix.

    Current behaviour (RED): solo path uses ``out.extend(driver_copies)`` from
    the helper — no EventLog call, ``seq=0`` → assertion fails.

    After fix (GREEN): ``emit_event(author_player_id=None)`` → C2 txn →
    ``seq > 0`` on the returned message.
    """
    # Stay in SOLO mode — do NOT call _make_mp.
    await _connect(handler)
    await _walk_to_confirmation(handler)

    def _std_opening():
        anchored = {"visible_to": "all", "anchor_pc": "Rux", "pov_strategy": "pc_anchored"}
        return [
            NarrationMessage(payload=NarrationPayload(text=NonBlankString(SEED_TEXT))),
            NarrationMessage(
                payload=NarrationPayload(
                    text=NonBlankString(PROSE_TEXT_DRIVER_ANCHOR),
                    visibility_sidecar=anchored,
                )
            ),
        ]

    out = await _fire_opening(handler, monkeypatch, opening_factory=_std_opening)

    narration_msgs = [m for m in out if isinstance(m, NarrationMessage)]
    assert narration_msgs, "No NarrationMessage in solo opening output"
    seqs = [m.payload.seq for m in narration_msgs]
    assert any(s > 0 for s in seqs), (
        "Solo opening events must be persisted (seq > 0) after 71-13 — "
        "emit_event(author_player_id=None) runs the EventLog transaction even "
        "on the Invariant-3 raw-bypass path.  Currently seq=0 (no EventLog "
        f"call in the solo helper path).  Got: {seqs!r}"
    )


@pytest.mark.asyncio
async def test_render_queued_frame_not_persisted_as_narration(
    handler: WebSocketSessionHandler, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """sq-playtest 2026-05-28 #G1 regression — a RENDER_QUEUED frame in the
    opening list must NOT be persisted as a NARRATION event.

    The opening turn ends by dispatching a landscape render, so
    ``_run_opening_turn_narration`` returns a ``RenderQueuedMessage`` (payload
    ``{render_id}``) alongside the narration. The old chargen caller looped
    ``emit_event(self, "NARRATION", msg.payload)`` over EVERY frame, persisting
    the render-cue under ``kind="NARRATION"``. On reconnect, replay rebuilt it
    via ``NarrationPayload(**{render_id, seq})`` → ValidationError → the whole
    ws_endpoint aborted → an infinite reconnect brick-loop (160× in the log).

    This test reproduces the producer end-to-end: it drives the real chargen
    confirmation with an opening list containing an (already-emitted) NARRATION
    plus a RenderQueuedMessage, then replays EVERY persisted event row through
    ``_build_message_for_kind`` — the exact operation reconnect performs. After
    the fix the render frame is passed through (never event-sourced), so replay
    completes without raising and no NARRATION row carries a render-cue shape.
    """
    import json

    import psycopg

    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import RenderQueuedMessage, RenderQueuedPayload
    from sidequest.server.session_handler import _build_message_for_kind

    await _connect(handler)
    await _walk_to_confirmation(handler)

    sd = handler._session_data  # type: ignore[attr-defined]
    poison_render_id = "5386571aacad"

    monkeypatch.setattr(chargen_mixin, "_should_fire_opening_narration", lambda _sd, _room: True)

    async def _opening_with_render(_sd: object, _player_id: str, _span: object) -> list[object]:
        # Mirror the real method: the NARRATION is already emitted via
        # emit_event; the RENDER_QUEUED frame is appended un-persisted (the
        # daemon round-trip is fire-and-forget).
        narration = handler._emit_event(
            "NARRATION", NarrationPayload(text=NonBlankString(SEED_TEXT))
        )
        render = RenderQueuedMessage(
            type=MessageType.RENDER_QUEUED,  # type: ignore[arg-type]
            payload=RenderQueuedPayload(render_id=poison_render_id),
        )
        return [narration, render]

    monkeypatch.setattr(handler, "_run_opening_turn_narration", _opening_with_render)
    await handler.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )

    # Read every persisted event row for this session and replay it exactly as
    # reconnect does. This is the assertion that the brick-loop is gone.
    session_id = sd.repository.session_id
    plain = __import__("os").environ["SIDEQUEST_DATABASE_URL"]
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT seq, kind, payload_json FROM events "
            "WHERE session_id = %s ORDER BY seq",
            (session_id,),
        ).fetchall()

    assert rows, "Expected at least one persisted event after the opening turn"

    # No NARRATION row may carry a render-cue shape (the exact poison).
    for seq, kind, payload_json in rows:
        if kind == "NARRATION":
            data = json.loads(payload_json)
            assert "render_id" not in data, (
                f"NARRATION event seq={seq} carries a render-cue payload "
                f"{data!r} — the #G1 producer regressed (render frame persisted "
                "as NARRATION)."
            )
            assert "text" in data, (
                f"NARRATION event seq={seq} is missing 'text' (payload={data!r}) "
                "— a malformed NARRATION row would crash reconnect replay."
            )

    # Replay every row the way connect.py does. Must not raise.
    for seq, kind, payload_json in rows:
        _build_message_for_kind(kind=kind, payload_json=payload_json, seq=int(seq))

    # And the poison render_id must not be event-sourced at all.
    assert not any(poison_render_id in (pj or "") for _s, _k, pj in rows), (
        f"render_id {poison_render_id!r} must never be persisted to the events "
        "table — render cues are not event-sourced (fire-and-forget)."
    )
