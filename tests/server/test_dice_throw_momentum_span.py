"""OTEL span coverage for the new mid-turn CONFRONTATION broadcast.

Story 45-3 — Momentum readout state sync.

Per CLAUDE.md OTEL principle: every backend fix that touches a subsystem
MUST add OTEL watcher events so the GM panel can verify the fix is
working. The mid-turn CONFRONTATION broadcast is exactly such a fix —
without a span, Sebastien (mechanical-first player, watches the GM
panel) cannot tell whether the dial moved because the engine emitted a
fresh state, or because the narrator accidentally improvised matching
prose.

This test asserts the new ``encounter.momentum_broadcast`` span:

1. **AC2 positive:** fires on every non-deferred dice-throw beat with
   ``encounter_type``, ``player_metric_after``, ``opponent_metric_after``,
   ``source="dice_throw"``, and ``beat_id``.

2. **AC2 negative — opposed branch:** the opposed_check defers
   beat-apply, so no broadcast happens at the dice-throw site; the span
   MUST NOT fire on this branch. (The post-narration broadcast that
   eventually carries the opposed result is out of scope here — the
   span there fires with ``source="narration_apply"``.)

3. **AC2 negative — error branch:** if ``apply_beat`` raises
   ``DiceDispatchError`` (e.g., a beat skipped per ``apply_beat`` skip
   path), the span MUST NOT fire — there was no broadcast.

4. **SPAN_ROUTES wiring:** the new span name appears in
   ``SPAN_ROUTES`` so the GM-panel watcher feed picks it up.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
)
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.protocol.messages import DiceThrowMessage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115 F1: the AC2 narration-apply test persists/reads through PG)."""
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


@pytest.fixture
def otel_capture():
    """Live-singleton OTEL capture (matches ``tests/agents/conftest.py``).

    Inline so this test module doesn't need to relocate the fixture from
    the agents tree to ``tests/server/conftest.py``. The pattern is the
    same: install a SimpleSpanProcessor on the global TracerProvider so
    the tracer() helper inside our span context managers reaches it.

    **Provider reset (45-36 fix):** kept symmetric with
    ``conftest.py::otel_capture`` — see that fixture's docstring for
    full rationale. We clear ``_span_processors`` on the existing
    provider rather than swapping the provider itself, because
    production modules cache tracers at import time.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)

    # Drop accumulated processors from prior invocations — see
    # ``tests/server/conftest.py::otel_capture`` for full rationale.
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _install_combat_def(sd, *, resolution_mode: str = "beat_selection") -> None:
    cdef = ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Dungeon Combat",
            "category": "combat",
            "resolution_mode": resolution_mode,
            "opponent_default_stats": ({"STR": 12} if resolution_mode == "opposed_check" else {}),
            "player_metric": MetricDef(
                name="momentum",
                starting=0,
                threshold=10,
            ).model_dump(),
            "opponent_metric": MetricDef(
                name="momentum",
                starting=0,
                threshold=10,
            ).model_dump(),
            "beats": [
                BeatDef.model_validate(
                    {
                        "id": "attack",
                        "label": "Attack",
                        "kind": "strike",
                        "base": 3,
                        "stat_check": "STRENGTH",
                    }
                ).model_dump(),
            ],
        }
    )
    sd.genre_pack.rules.confrontations = [cdef]


def _install_active_encounter(sd) -> None:
    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="momentum",
            current=0,
            starting=0,
            threshold=10,
        ),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Rux", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    sd.snapshot.encounter = enc


def _throw(face: int = 15, beat_id: str = "attack") -> DiceThrowMessage:
    return DiceThrowMessage(
        payload=DiceThrowPayload(
            request_id="span-req-1",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[face],
            beat_id=beat_id,
        ),
        player_id="player-1",
    )


class _StubRoom:
    slug = "span-test"

    def __init__(self) -> None:
        self.broadcasts: list[tuple[object, str | None]] = []
        from sidequest.game.session import GameSnapshot
        from sidequest.server.session import Session

        self.session = Session(GameSnapshot(genre_slug="caverns_and_claudes"))

    def broadcast(
        self,
        msg: object,
        *,
        exclude_socket_id: str | None = None,
    ) -> None:
        self.broadcasts.append((msg, exclude_socket_id))

    def is_paused(self) -> bool:
        return False


SPAN_NAME = "encounter.momentum_broadcast"


# ---------------------------------------------------------------------------
# AC2 positive: span fires on dice-throw beat-apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_momentum_broadcast_span_fires_on_dice_throw(
    session_handler_factory,
    otel_capture,
):
    """``encounter.momentum_broadcast`` fires with the post-apply momentum.

    Required attributes (per AC2 + context-story-45-3.md):
      - ``encounter_type`` — the active encounter slug
      - ``player_metric_after`` — encounter.player_metric.current after apply_beat
      - ``opponent_metric_after`` — encounter.opponent_metric.current after apply_beat
      - ``source`` = "dice_throw"
      - ``beat_id`` — the resolved beat id
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14  # +2 mod

    handler._room = _StubRoom()  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Strike lands."),
    )

    await handler.handle_message(_throw(face=15))

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_NAME]
    assert len(spans) >= 1, (
        f"expected at least one '{SPAN_NAME}' span on a successful "
        f"dice-throw beat; got "
        f"{[s.name for s in otel_capture.get_finished_spans()]!r}"
    )

    # Find the dice_throw-sourced span (post-narration emit will fire a
    # second one with source='narration_apply'; we only check the dice
    # path here).
    dice_spans = [s for s in spans if (s.attributes or {}).get("source") == "dice_throw"]
    assert len(dice_spans) == 1, (
        f"exactly one momentum_broadcast span should fire from the "
        f"dice path; got {len(dice_spans)} (sources: "
        f"{[s.attributes.get('source') for s in spans]!r})"
    )
    attrs = dict(dice_spans[0].attributes or {})

    assert attrs.get("encounter_type") == "combat"
    # Success on attack: own_delta=base=3, encounter.player_metric goes 0 → 3.
    assert attrs.get("player_metric_after") == 3, (
        f"player_metric_after must reflect post-apply momentum (=3); "
        f"got {attrs.get('player_metric_after')}"
    )
    # Opponent metric untouched on a player-side strike.
    assert attrs.get("opponent_metric_after") == 0
    assert attrs.get("beat_id") == "attack"


def test_momentum_broadcast_span_is_in_span_routes():
    """The new span MUST be registered in SPAN_ROUTES.

    Without this entry the watcher dispatcher drops the span on the
    floor — the GM panel never sees it. Any audit script that lists
    routed spans (and the watcher integration tests) read this dict;
    a missing entry is invisible at runtime until someone notices the
    GM panel is silent.
    """
    from sidequest.telemetry.spans import SPAN_ROUTES

    assert SPAN_NAME in SPAN_ROUTES, (
        f"'{SPAN_NAME}' must be registered in SPAN_ROUTES so the GM "
        f"watcher feed picks it up; current keys with 'momentum': "
        f"{[k for k in SPAN_ROUTES if 'momentum' in k]!r}"
    )

    route = SPAN_ROUTES[SPAN_NAME]

    # The route must extract attributes the GM panel needs to render
    # the dial event. We don't pin the exact field names beyond the
    # core five — the test verifies the contract, not the schema.
    class _FakeSpan:
        attributes = {
            "encounter_type": "combat",
            "player_metric_after": 3,
            "opponent_metric_after": 0,
            "source": "dice_throw",
            "beat_id": "attack",
        }

    extracted = route.extract(_FakeSpan())
    assert isinstance(extracted, dict)
    # Core attributes must round-trip into the watcher event payload.
    assert extracted.get("encounter_type") == "combat"
    assert extracted.get("player_metric_after") == 3
    assert extracted.get("source") == "dice_throw"


# ---------------------------------------------------------------------------
# AC2 negative: opposed branch defers — no span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_momentum_broadcast_span_does_not_fire_on_opposed_check(
    session_handler_factory,
    otel_capture,
):
    """Opposed-check defers beat-apply; no broadcast → no span.

    A fix that fires the span unconditionally on every dice path entry
    would mislead the watcher: the GM panel would show a momentum-
    advance event on a turn where the engine deferred and didn't move
    a metric.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd, resolution_mode="opposed_check")
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    handler._room = _StubRoom()  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You square off."),
    )

    await handler.handle_message(_throw(face=15))

    dice_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_NAME and (s.attributes or {}).get("source") == "dice_throw"
    ]
    assert dice_spans == [], (
        f"opposed_check defers beat-apply; '{SPAN_NAME}' MUST NOT fire "
        f"with source='dice_throw' on this branch. Got: "
        f"{[(s.name, dict(s.attributes or {})) for s in dice_spans]!r}"
    )


# ---------------------------------------------------------------------------
# AC2 negative: dispatch error path → no span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_momentum_broadcast_span_does_not_fire_on_dispatch_error(
    session_handler_factory,
    otel_capture,
):
    """When dispatch raises (e.g., unknown beat_id), no broadcast → no span.

    The handler returns an ErrorMessage on ``DiceDispatchError`` BEFORE
    any room broadcast. A regression that emitted the span before the
    raise (or in a finally block) would falsely tell the GM panel the
    dial advanced on a turn that errored out.
    """
    from sidequest.server.session_handler import _State

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14

    handler._room = _StubRoom()  # type: ignore[assignment]

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="never reached"),
    )

    # Unknown beat_id → DiceDispatchError before apply_beat runs.
    await handler.handle_message(_throw(face=15, beat_id="nonexistent_beat"))

    dice_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_NAME and (s.attributes or {}).get("source") == "dice_throw"
    ]
    assert dice_spans == [], (
        f"DiceDispatchError before apply_beat → no broadcast → no "
        f"'{SPAN_NAME}' span. Got: "
        f"{[(s.name, dict(s.attributes or {})) for s in dice_spans]!r}"
    )


# ---------------------------------------------------------------------------
# AC2 second site: narration_apply emit must also fire the new span
# ---------------------------------------------------------------------------
#
# context-story-45-3.md AC2 names TWO sources for the new
# encounter.momentum_broadcast span: "dice_throw" (the dice-dispatch
# site) and "narration_apply" (the post-narration CONFRONTATION emit
# inside _execute_narration_turn). The lie-detector contract requires
# the span to fire from EVERY site that broadcasts a CONFRONTATION
# carrying post-mutation momentum — without it, the GM panel cannot
# audit "the dial moved because the engine moved a metric, not because
# the narrator improvised matching prose" on narrator-driven dial moves.
#
# This test exercises the post-narration emit path with a real
# EventLog + ProjectionFilter (matching test_confrontation_mp_broadcast.py)
# so _emit_event takes the production branch. It asserts the new span
# fires with source="narration_apply" — currently failing; will go
# GREEN once Dev wraps the _emit_event("CONFRONTATION", ...) call site
# at session_handler.py with encounter_momentum_broadcast_span(
# source="narration_apply", ...).


@pytest.mark.asyncio
async def test_narration_apply_emits_momentum_broadcast_span(
    session_handler_factory,
    otel_capture,
    tmp_path,
):
    """Post-narration CONFRONTATION emit must fire ``encounter.momentum_broadcast``.

    Drives ``_execute_narration_turn`` through a full session setup
    (real EventLog, ProjectionCache, ComposedFilter, SessionRoom). The
    orchestrator mock returns a narration that opens a fresh combat
    confrontation (``confrontation="combat"``) — which is the branch
    that triggers the post-narration CONFRONTATION emit at the
    ``_emit_event("CONFRONTATION", ...)`` site.

    AC2 requires this site fire the new span with:
      - ``source="narration_apply"``
      - ``encounter_type`` from the now-live encounter
      - ``player_metric_after`` / ``opponent_metric_after`` from the
        encounter's current state

    Currently failing: the span helper is wired only at the dice-throw
    site. The fix is to wrap the post-narration emit call site with
    ``encounter_momentum_broadcast_span(source="narration_apply", ...)``.
    """
    import asyncio as _asyncio

    from sidequest.game import db_pool
    from sidequest.game.event_log import EventLog
    from sidequest.game.persistence import (
        GameMode,
    )
    from sidequest.game.projection.cache import ProjectionCache
    from sidequest.game.projection.composed import ComposedFilter
    from sidequest.server.session_handler import (
        _build_turn_context,
        _State,
    )
    from sidequest.server.session_room import RoomRegistry
    from sidequest.server.session_state import _build_pg_repos_for_slug

    slug = "ac2-narration-apply-span-test"

    # ADR-115 F1: register the session in Postgres; events/projection persist
    # and read through the PG repository.
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode=str(GameMode.MULTIPLAYER),
        genre_slug="caverns_and_claudes",
        world_slug="",
    )

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    sd.player_id = "actor"
    sd.player_name = "Rux"
    sd.mode = GameMode.MULTIPLAYER
    sd.game_slug = slug
    # Ensure the pack has a "combat" confrontation def the narrator can
    # open (matches the orchestrator mock's confrontation="combat").
    _install_combat_def(sd)
    # Story 59-4: encounter creation is now router-driven, not narrator-
    # initiated. Pre-install an active encounter so _execute_narration_turn
    # sees now_live=True and emits the CONFRONTATION + momentum_broadcast span.
    _install_active_encounter(sd)

    handler._event_log = EventLog(repo)
    handler._projection_filter = ComposedFilter.with_no_genre_rules()
    handler._projection_cache = ProjectionCache(repo)

    # Two-socket room — the actor plus a peer. We don't inspect the
    # peer queue here (that's AC5's regression test), but the projection
    # filter requires at least one connected non-emitter to take the
    # production fan-out branch.
    registry = RoomRegistry()
    room = registry.get_or_create(slug=slug, mode=GameMode.MULTIPLAYER)
    socket_ids = {"actor": "sock-actor", "peer": "sock-peer"}
    for pid, sid in socket_ids.items():
        room.connect(pid, socket_id=sid)
        room.attach_outbound(sid, _asyncio.Queue())
    handler._room = room
    handler._socket_id = socket_ids["actor"]

    # Orchestrator mock: returns narration prose. The encounter already
    # exists (pre-installed above) so _execute_narration_turn detects
    # now_live=True and emits the post-narration CONFRONTATION +
    # momentum_broadcast span.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="Rux squares off — combat begins.",
        ),
    )

    await handler._execute_narration_turn(
        sd,
        "I attack the goblin.",
        _build_turn_context(sd),
    )

    # Find the narration_apply-sourced span. Filter strictly by source
    # — the dice path (which we're NOT exercising here) would emit
    # source="dice_throw"; only narration_apply is in scope.
    narration_apply_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_NAME and (s.attributes or {}).get("source") == "narration_apply"
    ]
    assert len(narration_apply_spans) == 1, (
        f"expected exactly ONE '{SPAN_NAME}' span with "
        f"source='narration_apply' from the post-narration emit; got "
        f"{len(narration_apply_spans)}. AC2 (context-story-45-3.md) "
        f"requires the span fire from EVERY site that broadcasts a "
        f"CONFRONTATION post-mutation; the post-narration emit at "
        f'_emit_event("CONFRONTATION", ...) inside _execute_narration_turn '
        f"is one such site. All {SPAN_NAME} spans seen: "
        f"{[(s.name, dict(s.attributes or {}).get('source', '?')) for s in otel_capture.get_finished_spans() if s.name == SPAN_NAME]!r}"
    )

    attrs = dict(narration_apply_spans[0].attributes or {})
    # encounter_type must reflect the freshly-opened confrontation.
    assert attrs.get("encounter_type") == "combat", (
        f"narration_apply span must carry encounter_type from the live "
        f"encounter; got {attrs.get('encounter_type')!r}"
    )
    # The metric values reflect the encounter state at emit time. The
    # narrator-opened encounter starts with current=0 / threshold=10
    # (per the cdef defaults); player_metric_after must match.
    assert attrs.get("player_metric_after") == 0, (
        f"narration_apply span must carry post-emit player_metric "
        f"(=0 on a freshly-opened encounter); got "
        f"{attrs.get('player_metric_after')!r}"
    )
    assert attrs.get("opponent_metric_after") == 0, (
        f"narration_apply span must carry post-emit opponent_metric "
        f"(=0 on a freshly-opened encounter); got "
        f"{attrs.get('opponent_metric_after')!r}"
    )
    # source pinned exactly — no fallback or empty-string regression.
    assert attrs.get("source") == "narration_apply"


# asyncio marker for the test module
_ = asyncio
