"""RED tests for Task 12 — Fate binding: zone state + zone-move legality + OTEL
(ADR-096 v2, Track C3).

The Fate binding consumes the C3 projection (``project_zones``) and brings the
inert ADR-144 slots live: ``StructuredEncounter.zones`` (encounter.py) and
``EncounterActor.per_actor_state['zone']``. Two new methods on
``FateRulesetModule``:

- ``project_conflict_zones(*, encounter, mask, ...)`` — populate the slots from
  each actor's seated ``cell``, return name->zone, emit ``tactical.zone.projected``.
- ``adjudicate_zone_move(*, from_zone, to_zone, projection, ...)`` — Fate Core
  RAW legality: same/adjacent zone is a FREE supplemental move; a non-adjacent
  (2+) zone move REQUIRES an Overcome. Emits ``tactical.zone.move``.

Grounding limit (plan Task 12 — stated, not invented): Fate has NO ``move``
action verb and this story adds none — the deliverable is projection + state +
legality classification + OTEL only. ``run_fate_exchange`` is untouched.

Spans must MIRROR into the turn_telemetry sink via ``publish_event`` (the GM
panel is the lie detector; a bare Jaeger span reads as DEAD in saves) — model:
``tests/telemetry/test_tactical_telemetry_sink.py``, including its recording-
tracer fixture (the mirror skips NonRecordingSpans).

Location note: the plan doc says ``tests/agents/ruleset/`` but that directory
does not exist — the WN sibling binding test lives here (``tests/game/ruleset/``),
so the Fate binding test follows the repo, not the doc (logged as a deviation).

Hand-verified DUMBBELL facts (see test_zones.py): cell (1,1) is in the TOP lobe,
cell (5,3) is in the BOTTOM lobe — two different zones separated by the pinch.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sidequest.game.tactical.zones import ZoneMoveAdjudication, ZoneProjection

import sidequest.telemetry.spans as spans_module
import sidequest.telemetry.spans.tactical as tac
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module

DUMBBELL = "#######\n#.....#\n#.#.#.#\n#.....#\n#######"


def _fate() -> FateRulesetModule:
    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    return module


def _fate_conflict() -> StructuredEncounter:
    """A seated Fate conflict: Hero on (1,1) (top lobe), Rival on (5,3) (bottom
    lobe) — cells as [x, y] lists, the seating.py persisted shape."""
    return StructuredEncounter(
        encounter_type="social",
        player_metric=EncounterMetric(name="advantage", threshold=3),
        opponent_metric=EncounterMetric(name="advantage", threshold=3),
        actors=[
            EncounterActor(
                name="Hero",
                role="combatant",
                side="player",
                per_actor_state={"cell": [1, 1]},
            ),
            EncounterActor(
                name="Rival",
                role="combatant",
                side="opponent",
                per_actor_state={"cell": [5, 3]},
            ),
        ],
    )


# A z0 - z1 - z2 line: z0 and z2 are NOT adjacent (2 zones apart).
_LINE_PROJECTION = ZoneProjection(
    zones={
        "z0": frozenset({(0, 0)}),
        "z1": frozenset({(1, 0)}),
        "z2": frozenset({(2, 0)}),
    },
    cell_to_zone={(0, 0): "z0", (1, 0): "z1", (2, 0): "z2"},
    adjacency={
        "z0": frozenset({"z1"}),
        "z1": frozenset({"z0", "z2"}),
        "z2": frozenset({"z1"}),
    },
)


@pytest.fixture
def capture_spans(monkeypatch):
    """Install a recording tracer so ``Span.open`` yields a span with
    ``attributes`` — the ``_mirror`` helper skips NonRecordingSpans, so without
    this the sink assertions can never fire (plan-doc bug class documented in
    test_tactical_telemetry_sink.py)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-fate-zone-binding")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


# --- project_conflict_zones: the inert slots go live -----------------------------------


def test_project_conflict_zones_populates_state():
    fate = _fate()
    enc = _fate_conflict()
    placed = fate.project_conflict_zones(encounter=enc, mask=DUMBBELL)

    assert enc.zones, "encounter.zones (the inert ADR-144 slot) must be populated"
    assert set(enc.zones) == {"z0", "z1"}, "DUMBBELL projects exactly two zones"
    for actor in enc.actors:
        assert actor.per_actor_state["zone"] in enc.zones
    assert placed["Hero"] == enc.actors[0].per_actor_state["zone"]
    assert placed["Rival"] == enc.actors[1].per_actor_state["zone"]
    # The pinch separates the lobes: the conflict opens with the Other one zone
    # away — the free-move/Overcome distinction is live from the first exchange.
    assert placed["Hero"] != placed["Rival"]


def test_project_conflict_zones_skips_unseated_actor():
    """An actor with no seated cell is SKIPPED — not crashed on, not fabricated
    a position (No Silent Fallbacks: absence stays visible as absence)."""
    fate = _fate()
    enc = _fate_conflict()
    enc.actors.append(EncounterActor(name="Bystander", role="bystander", side="neutral"))
    placed = fate.project_conflict_zones(encounter=enc, mask=DUMBBELL)

    assert "Bystander" not in placed
    assert "zone" not in enc.actors[2].per_actor_state
    # The seated actors are still projected — one unseated actor must not
    # abort the whole projection.
    assert placed["Hero"] and placed["Rival"]


# --- adjudicate_zone_move: Fate Core RAW legality ---------------------------------------


def test_adjudicate_zone_move_free_vs_overcome():
    fate = _fate()
    same = fate.adjudicate_zone_move(from_zone="z0", to_zone="z0", projection=_LINE_PROJECTION)
    assert same.free and not same.requires_overcome

    adjacent = fate.adjudicate_zone_move(from_zone="z0", to_zone="z1", projection=_LINE_PROJECTION)
    # Fate Core RAW: one zone is a free supplemental move.
    assert adjacent.free and not adjacent.requires_overcome

    far = fate.adjudicate_zone_move(from_zone="z0", to_zone="z2", projection=_LINE_PROJECTION)
    assert not far.free and far.requires_overcome


def test_adjudicate_zone_move_returns_the_task11_dataclass():
    """The verdict must BE ``zones.ZoneMoveAdjudication`` — imported from the
    C3 library, never a redefined lookalike (plan Task 12: 'import it, do not
    redefine'). A shadow class would fool duck-typed asserts; ``type() is``
    does not."""
    fate = _fate()
    verdict = fate.adjudicate_zone_move(from_zone="z0", to_zone="z1", projection=_LINE_PROJECTION)
    assert type(verdict) is ZoneMoveAdjudication
    assert verdict.from_zone == "z0" and verdict.to_zone == "z1"


def test_adjudicate_zone_move_unknown_zone_is_never_free():
    """A from_zone the projection has never heard of must classify as
    requires_overcome — a broken caller must never mint a free teleport."""
    fate = _fate()
    verdict = fate.adjudicate_zone_move(from_zone="z9", to_zone="z0", projection=_LINE_PROJECTION)
    assert not verdict.free
    assert verdict.requires_overcome


# --- OTEL: the GM panel is the lie detector --------------------------------------------


def test_zone_projected_span_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    fate = _fate()
    fate.project_conflict_zones(encounter=_fate_conflict(), mask=DUMBBELL)

    zone_events = [
        (et, fields, kw)
        for et, fields, kw in published
        if fields.get("op") == "tactical.zone.projected"
    ]
    assert zone_events, (
        "tactical.zone.projected did not mirror into the turn_telemetry sink — "
        "the projection would read as DEAD on the GM panel"
    )
    event_type, fields, kw = zone_events[0]
    assert event_type == "state_transition"
    assert kw["component"] == "tactical"
    assert fields["zone_count"] == 2


def test_zone_move_span_mirrors_to_sink(monkeypatch, capture_spans):
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    fate = _fate()
    fate.adjudicate_zone_move(
        from_zone="z0", to_zone="z2", projection=_LINE_PROJECTION, actor="Hero"
    )

    move_events = [
        (et, fields, kw) for et, fields, kw in published if fields.get("op") == "tactical.zone.move"
    ]
    assert move_events, "tactical.zone.move did not mirror into the turn_telemetry sink"
    _, fields, kw = move_events[0]
    assert kw["component"] == "tactical"
    assert fields["from_zone"] == "z0"
    assert fields["to_zone"] == "z2"
    assert fields["free"] is False


def test_zone_span_routes_registered():
    """Both new span names must carry SPAN_ROUTES entries (the sink mirror and
    the routing-completeness sweep both key on them) and be exported."""
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    assert tac.SPAN_TACTICAL_ZONE_PROJECTED == "tactical.zone.projected"
    assert tac.SPAN_TACTICAL_ZONE_MOVE == "tactical.zone.move"
    for name in (tac.SPAN_TACTICAL_ZONE_PROJECTED, tac.SPAN_TACTICAL_ZONE_MOVE):
        assert name in SPAN_ROUTES, f"{name} has no SPAN_ROUTES entry"
        assert SPAN_ROUTES[name].component == "tactical"
        assert SPAN_ROUTES[name].event_type == "state_transition"
    for exported in (
        "SPAN_TACTICAL_ZONE_PROJECTED",
        "SPAN_TACTICAL_ZONE_MOVE",
        "tactical_zone_projected_span",
        "tactical_zone_move_span",
    ):
        assert exported in tac.__all__, f"{exported} missing from spans.tactical.__all__"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
