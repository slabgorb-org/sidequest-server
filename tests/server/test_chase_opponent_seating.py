"""Story 59-13 — chase confrontation seats an Other (ADR-116 movement slice).

RED tests. These FAIL today by design and pass once Dev implements ADR-116's
movement slice.

Background (measured, not assumed): chase beat write-back is NOT broken — with
an opponent seated, ``apply_beat`` advances the opponent dial and the phase
transitions. The defect is that a chase instantiates with no opponent seated:
``_npc_fallback_at_location`` seats a found NPC as ``side="neutral"`` for
non-combat categories, and the empty-opponent guard exempts ``movement``
entirely. With no ``side="opponent"`` actor, opponent beats are skipped and the
dial freezes at 0.

ADR-116 ("A Confrontation Requires an Other"):
  AC1 — chase seats a room NPC / bestiary mob as ``side="opponent"`` (+ a
        ``participant.joined`` membership span).
  AC2 — chase with no sourceable opponent raises ``NoOpponentAvailableError``
        (the movement exemption is removed); the dispatch handler then renders
        prose. No one-sided chase is created.
  AC3 — end-to-end: a chase instantiated via the production seating path
        advances the opponent dial off 0 and transitions Setup→Opening.
  AC4 — end-on-no-Other: when the last opponent withdraws, the encounter
        resolves (+ a ``participant.left`` span). Mirror of the player-side
        ``yield_action`` rule.

Fixture convention (see test_encounter_actors_all_combatants.py): load the
frozen ``test_genre`` pack from disk (it defines a ``chase`` confrontation,
category=movement, beats scramble/shortcut/distraction/go_underground). Do NOT
use the conftest ``synthetic_two_dial_pack`` (combat-only) or the stale
``chase_*_goal10.json`` fixtures (rejected legacy single-``metric`` shape).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
)
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for
from tests._helpers.trigger_encounter import trigger_encounter

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"

_LOCATION = "Dust Flats"
_PC = "Sam"


def _load_pack():
    return load_genre_pack(_FIXTURE_PACK)


def _make_npc(
    name: str,
    *,
    role: str | None = None,
    last_seen_location: str | None = None,
    creature_id: str | None = None,
    threat_level: int | None = None,
) -> Npc:
    """Minimal stateful Npc for a fallback-source fixture.

    ``creature_id`` set + ``threat_level`` set ⇒ a bestiary mob (ADR-059
    Monster Manual shape). ``creature_id=None`` ⇒ a narrator-declared NPC.
    Both live in the same ``snapshot.npcs`` roster — ADR-116 §2 seats both.
    """
    return Npc(
        core=CreatureCore(
            name=name,
            description="A wasteland pursuer.",
            personality="Relentless.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id=role,
        last_seen_location=last_seen_location,
        last_seen_turn=0,
        creature_id=creature_id,
        threat_level=threat_level,
    )


def _snap_with_pc_at_location() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[_PC] = _LOCATION
    return snap


@pytest.fixture
def otel_capture():
    """In-memory OTEL exporter attached to the running TracerProvider.

    Mirrors test_encounter_actors_all_combatants.py::otel_capture.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent
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


def _opponent_actors(enc) -> list:
    return [a for a in enc.actors if a.side == "opponent"]


# ---------------------------------------------------------------------------
# AC1 — chase seats a room NPC / mob as side="opponent"
# ---------------------------------------------------------------------------


def test_chase_seats_room_npc_as_opponent():
    """A chase dispatched with no named opponent, but with an NPC already at
    the PC's location, must seat that NPC as ``side="opponent"`` — not the
    current ``neutral`` (which can never drive the opponent dial)."""
    snap = _snap_with_pc_at_location()
    snap.npcs.append(_make_npc("Road Raider", role="hostile", last_seen_location=_LOCATION))
    pack = _load_pack()

    trigger_encounter(snap, pack, "chase", _PC, npcs_present=[])

    enc = snap.encounter
    assert enc is not None, "chase encounter was not instantiated"
    opp_names = [a.name for a in _opponent_actors(enc)]
    assert "Road Raider" in opp_names, (
        "chase seated no opponent — the pursuer was dropped or filed as "
        f"neutral. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )


def test_chase_seats_bestiary_mob_as_opponent():
    """The pursuer can be a bestiary mob (``creature_id`` set), not just a
    named NPC — same ``snapshot.npcs`` roster, seated the same way."""
    snap = _snap_with_pc_at_location()
    snap.npcs.append(
        _make_npc(
            "Dust Wraith",
            role="hostile",
            last_seen_location=_LOCATION,
            creature_id="dust_wraith",
            threat_level=2,
        )
    )
    pack = _load_pack()

    trigger_encounter(snap, pack, "chase", _PC, npcs_present=[])

    enc = snap.encounter
    assert enc is not None
    opp_names = [a.name for a in _opponent_actors(enc)]
    assert "Dust Wraith" in opp_names, (
        "chase did not seat the bestiary mob as an opponent. "
        f"actors={[(a.name, a.side) for a in enc.actors]!r}"
    )


def test_chase_opponent_seating_emits_participant_joined_span(otel_capture):
    """Membership observability (ADR-116 §2 + OTEL Observability Principle):
    seating an opponent emits a ``participant.joined`` span carrying the
    side so the GM panel can answer 'why is this pursuer here?'."""
    snap = _snap_with_pc_at_location()
    snap.npcs.append(_make_npc("Road Raider", role="hostile", last_seen_location=_LOCATION))
    pack = _load_pack()

    trigger_encounter(snap, pack, "chase", _PC, npcs_present=[])

    joined = [s for s in otel_capture.get_finished_spans() if s.name == "participant.joined"]
    assert joined, (
        "no participant.joined span emitted on opponent seating — membership "
        "entry is not observable on the GM panel"
    )
    assert any(s.attributes.get("side") == "opponent" for s in joined), (
        "participant.joined span fired but no opponent-side entry recorded"
    )


# ---------------------------------------------------------------------------
# AC2 — no sourceable opponent ⇒ raise (movement exemption removed)
# ---------------------------------------------------------------------------


def test_chase_with_empty_room_raises_no_opponent():
    """A chase with no named opponent AND no NPC/mob at the location must
    raise ``NoOpponentAvailableError`` — the same guard combat has. The
    movement exemption (story 45-33) is removed: a one-sided chase is not a
    confrontation. The dispatch handler catches this and renders prose."""
    snap = _snap_with_pc_at_location()  # no npcs at location
    pack = _load_pack()

    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="chase",
            player_name=_PC,
            npcs_present=[],
            genre_slug="test_pack",
        )


def test_chase_with_empty_room_does_not_create_one_sided_encounter():
    """Defensive sibling of the above: even if instantiation is reworked, an
    empty-room chase must never leave a player-only encounter on the snapshot."""
    snap = _snap_with_pc_at_location()
    pack = _load_pack()

    with contextlib.suppress(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="chase",
            player_name=_PC,
            npcs_present=[],
            genre_slug="test_pack",
        )

    enc = snap.encounter
    if enc is not None:
        assert _opponent_actors(enc), (
            "a chase encounter was created with no opponent seat — frozen "
            f"one-sided dial. actors={[(a.name, a.side) for a in enc.actors]!r}"
        )


# ---------------------------------------------------------------------------
# AC3 — end-to-end: dial moves off 0 via the production seating path
# ---------------------------------------------------------------------------


def test_chase_dial_advances_after_production_seating():
    """The story's original goal, hung on the corrected seam: a chase
    instantiated via the production path (opponent sourced from the room)
    advances the opponent dial off 0 and transitions Setup→Opening when the
    opponent takes a chase beat. Fails today because the pursuer is seated
    neutral, so ``apply_beat`` skips it (skipped_reason=neutral_actor)."""
    snap = _snap_with_pc_at_location()
    snap.npcs.append(_make_npc("Road Raider", role="hostile", last_seen_location=_LOCATION))
    pack = _load_pack()

    trigger_encounter(snap, pack, "chase", _PC, npcs_present=[])

    result = NarrationTurnResult(
        narration="The raider closes the gap.",
        beat_selections=[
            BeatSelection(actor="Road Raider", beat_id="scramble", outcome=RollOutcome.Success)
        ],
        npcs_present=[NpcMention(name="Road Raider", side="opponent", role="hostile")],
    )
    _apply_narration_result_to_snapshot(snap, result, _PC, pack=pack, room=room_for(snap))

    enc = snap.encounter
    assert enc is not None
    assert enc.opponent_metric.current > 0, (
        "opponent dial still frozen at 0 — the pursuer's beat was skipped "
        "(pursuer not seated as an opponent)"
    )
    assert enc.structured_phase == EncounterPhase.Opening, (
        f"phase did not advance Setup→Opening (got {enc.structured_phase!r})"
    )


# ---------------------------------------------------------------------------
# AC4 — end-on-no-Other (mirror of player-side yield resolution)
# ---------------------------------------------------------------------------


def test_chase_resolves_when_last_opponent_withdraws(otel_capture):
    """When the last live opponent leaves the chase (withdrawn via flee /
    defeat), the encounter resolves — a confrontation ends because there is no
    longer an Other, not only because a dial hit threshold. Mirror of
    ``yield_action``'s player-side rule. Emits ``participant.left``.

    Isolation note: this AC is independent of the seating fix (AC1). We build
    the encounter with an opponent already seated (direct construction) so this
    test fails for ITS OWN reason — resolution-on-no-Other not wired — rather
    than for the seating precondition."""
    snap = _snap_with_pc_at_location()
    pack = _load_pack()
    snap.encounter = StructuredEncounter(
        encounter_type="chase",
        player_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=10),
        structured_phase=EncounterPhase.Opening,
        actors=[
            EncounterActor(name=_PC, role="runner", side="player"),
            EncounterActor(name="Road Raider", role="pursuer", side="opponent"),
        ],
    )
    enc = snap.encounter
    opponents = _opponent_actors(enc)
    assert opponents, "precondition: an opponent must be seated for this AC"

    # The pursuer breaks off (flee / defeat) — its actor is withdrawn.
    for a in opponents:
        a.withdrawn = True

    # Drive a production turn-apply; with no live Other remaining, the
    # encounter must resolve. (Seam is Dev's to wire per ADR-116 §4.)
    result = NarrationTurnResult(narration="The pursuer peels off into the dust.")
    _apply_narration_result_to_snapshot(snap, result, _PC, pack=pack, room=room_for(snap))

    assert snap.encounter.resolved is True, (
        "chase did not resolve after its last opponent withdrew — "
        "end-on-no-Other (ADR-116 §4) not wired"
    )
    left = [s for s in otel_capture.get_finished_spans() if s.name == "participant.left"]
    assert left, "no participant.left span emitted when the last opponent withdrew"
