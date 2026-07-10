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

import sidequest.telemetry.spans as spans_module
import sidequest.telemetry.spans.tactical as tac
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.tactical.zones import ZoneMoveAdjudication, ZoneProjection

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
    # abort the whole projection. Exact zones, not truthiness (review round 1, [LOW] O).
    assert placed == {"Hero": "z0", "Rival": "z1"}


def test_project_conflict_zones_skips_actor_on_unzoned_cell():
    """Review round 1 [MEDIUM] C: an actor that HAS a seated cell which resolves
    to no zone (a wall / off-projection coordinate — a coordinate bug or stale
    room cell) is skipped exactly like an unseated actor: excluded from
    ``placed``, no ``zone`` key. Regression pin on shipped behavior — this is
    the SECOND distinct skip path (``zid is None``), separate from the tested
    ``cell is None`` path; without this pin an off-by-one/x-y-swap bug is
    indistinguishable from 'correctly unseated'."""
    fate = _fate()
    enc = _fate_conflict()
    enc.actors.append(
        EncounterActor(
            name="Ghost",
            role="combatant",
            side="opponent",
            per_actor_state={"cell": [0, 0]},  # (0,0) is a wall in DUMBBELL
        )
    )
    placed = fate.project_conflict_zones(encounter=enc, mask=DUMBBELL)

    assert "Ghost" not in placed
    assert "zone" not in enc.actors[2].per_actor_state
    assert placed == {"Hero": "z0", "Rival": "z1"}


def test_project_conflict_zones_zones_a_full_table_of_pcs():
    """Review round 1 [MEDIUM] E: a table of PCs is the common case, not an edge
    (seating.py doctrine, 165-3 BLOCKER 3 — Keith's playgroup is multiplayer).
    Two player-side actors plus the Other, all cell-seated, must ALL carry
    valid zones. Regression pin on shipped behavior."""
    fate = _fate()
    enc = StructuredEncounter(
        encounter_type="social",
        player_metric=EncounterMetric(name="advantage", threshold=3),
        opponent_metric=EncounterMetric(name="advantage", threshold=3),
        actors=[
            EncounterActor(
                name="Sam", role="combatant", side="player", per_actor_state={"cell": [1, 1]}
            ),
            EncounterActor(
                name="Miller", role="combatant", side="player", per_actor_state={"cell": [3, 1]}
            ),
            EncounterActor(
                name="Silas", role="combatant", side="opponent", per_actor_state={"cell": [5, 3]}
            ),
        ],
    )
    placed = fate.project_conflict_zones(encounter=enc, mask=DUMBBELL)

    # Both PCs are in the top lobe (z0), the Other across the pinch (z1).
    assert placed == {"Sam": "z0", "Miller": "z0", "Silas": "z1"}
    for actor in enc.actors:
        assert actor.per_actor_state["zone"] in enc.zones


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


def test_adjudicate_zone_move_unknown_same_zone_is_not_free():
    """Review round 1 [MEDIUM] H (RED): the same-zone shortcut must not
    short-circuit ahead of membership validation — ``z99 -> z99`` for a zone the
    projection has never heard of returned ``free=True`` (a free verdict about a
    position that does not exist), contradicting the docstring's own 'never a
    free teleport'. An unknown zone id yields requires_overcome even when
    from_zone == to_zone."""
    fate = _fate()
    verdict = fate.adjudicate_zone_move(from_zone="z99", to_zone="z99", projection=_LINE_PROJECTION)
    assert not verdict.free, "an unknown zone id must never produce a free verdict"
    assert verdict.requires_overcome


def test_adjudicate_zone_move_known_same_zone_is_free():
    """The legitimate stay-put: a KNOWN zone moving to itself stays free — the
    unknown-zone fix must not break the real same-zone supplemental move."""
    fate = _fate()
    verdict = fate.adjudicate_zone_move(from_zone="z1", to_zone="z1", projection=_LINE_PROJECTION)
    assert verdict.free
    assert not verdict.requires_overcome


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
    # Review round 1 [MEDIUM] B: the outcome count must be pinned, not just the
    # input count — placed_count is how the GM panel tells "2 zones, both actors
    # placed" from "2 zones, none placed".
    assert fields["placed_count"] == 2


def test_zone_projected_span_counts_only_placed_actors(monkeypatch, capture_spans):
    """Review round 1 [MEDIUM] B: with one unseated actor at the table, the span
    reports zone_count=2 but placed_count=2 (not 3) — the discrepancy between
    roster size and placed_count is exactly the lie-detector signal the field
    exists to carry. Regression pin on shipped behavior."""
    published: list[tuple] = []
    monkeypatch.setattr(
        tac, "publish_event", lambda et, fields, **kw: published.append((et, fields, kw))
    )
    fate = _fate()
    enc = _fate_conflict()
    enc.actors.append(EncounterActor(name="Bystander", role="bystander", side="neutral"))
    fate.project_conflict_zones(encounter=enc, mask=DUMBBELL)

    zone_events = [
        (et, fields, kw)
        for et, fields, kw in published
        if fields.get("op") == "tactical.zone.projected"
    ]
    assert zone_events
    _, fields, _ = zone_events[0]
    assert fields["zone_count"] == 2
    assert fields["placed_count"] == 2, (
        "placed_count must count zone-seated actors only — an unseated actor "
        f"must not inflate it; got {fields['placed_count']} for 3 actors, 2 seated"
    )


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
