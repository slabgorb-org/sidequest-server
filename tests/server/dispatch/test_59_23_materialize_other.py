"""Story 59-23 #C3 — ship_combat must seat the NAMED THREAT as the Other,
never conscript the player's own crew.

RED tests (these FAIL at HEAD by design; they pass once Dev threads the
narrative-named threat into the ship_combat seating seam per ADR-116).

Background (verified at HEAD, 2026-05-29):
  ``ship_combat`` is ``category: combat`` + ``resolution_mode: beat_selection``
  (NOT sealed_letter), so the empty-``npcs_present`` location fallback
  (``_npc_fallback_at_location``) runs with ``adversary_only=False`` and seats
  EVERY NPC at the player's location — including the player's own crew — as
  ``side="opponent"`` (encounter_lifecycle.py default_side="opponent" for
  adversarial categories). The live router dispatches ship_combat with a
  hardcoded ``npcs_present=[]`` and never threads the narrative-named threat
  ("three unregistered hulls running dark"), so the crew are the only bodies in
  the room and become the enemy ship. This is playtest 2026-05-28 finding #C3.

  The MECHANICAL surface for materialization already exists: when an opponent
  actor is seated by name with no backing ``Npc``, ``_seed_combat_hp_depletion_to_npcs``
  (Task 9) CREATES one seeded from ``opponent_default_stats`` (hp 30 / AC 14).
  The gap is purely THREADING the named threat into the dispatch so it is
  seated as the opponent instead of falling through to the crew-conscripting
  fallback.

The correct fix (ADR-116 "a confrontation requires an Other"):
  - A ship_combat trigger that names a threat materializes that threat as the
    adversary and seats it ``side="opponent"``; the player's crew are never
    seated as opponents.
  - A ship_combat with no sourceable/ nameable Other raises
    ``NoOpponentAvailableError`` (fail loud, No Silent Fallbacks) rather than
    conscripting friendly bodies.

#C4 (hull-depletion resolution) is already delivered by the SWN port and pinned
by ``tests/server/test_space_opera_swn_combat_e2e.py`` — NOT re-tested here.
``test_ship_combat_materialized_threat_resolves_on_hull`` below is the AC7
coupling proof that ties #C3 (right Other) to #C4 (its hull resolves) through
the production path.

THREADING CONTRACT (TEA-defined for Dev): the named threat arrives on the
confrontation ``SubsystemDispatch.params`` under the key ``"threat"`` — a dict
carrying at least ``{"name": <str>}`` (optionally ``"description"``). The
docstring of ``run_confrontation_dispatch`` already sanctions params as the
router→engine channel for explicit actor info. If Dev threads the threat by a
different mechanism, update the ``_threat_dispatch`` helper here accordingly and
log it as a test-design deviation.

Skips gracefully when sidequest-content is not on disk.
"""

from __future__ import annotations

import contextlib

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.asyncio

# Content-authored ship_combat opponent stats (rules.yaml opponent_default_stats).
_SHIP_HP = 30
_SHIP_AC = 14

# Full SWN stat block so the player's initiative roll + attack params never
# KeyError on a missing stat (the SWN module fails loud on absent stats).
_STATS = {
    "Physique": 12,
    "Reflex": 12,
    "Will": 10,
    "Intellect": 12,
    "Resolve": 12,
    "Cunning": 12,
}

_PC = "Vela"
_LOCATION = "Bridge"
# The player's own crew — friendly bodies that share the bridge. They must NEVER
# be seated as the enemy ship.
_CREW = ("Wainu", "Kanga", "Cmdr Yarya")
_THREAT_NAME = "Raider Frigate"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_space_opera():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _player_character(name: str):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    return Character(
        core=CreatureCore(
            name=name,
            description="Freighter captain.",
            personality="steady",
            inventory=Inventory(items=[{"id": "blaster_sidearm", "name": "Sidearm Blaster"}]),
            hp={"current": 10, "max": 10, "base_max": 10},
        ),
        char_class="Soldier",
        race="Coreworlder",
        backstory="Long-haul spacer.",
        stats=dict(_STATS),
    )


def _friendly_crew_npc(name: str):
    """A crew member: NEUTRAL disposition, non-adversarial role, co-located with
    the PC. This is a friendly body, not the enemy ship."""
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.session import Npc

    return Npc(
        core=CreatureCore(
            name=name,
            description="A member of the player's own crew.",
            personality="Loyal.",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id="crew",  # explicitly NOT an adversarial role token
        last_seen_location=_LOCATION,
        last_seen_turn=0,
    )


def _snapshot_with_crew():
    """A bridge scene: the PC plus the PC's own friendly crew, no enemy."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[_PC] = _LOCATION
    snap.characters.append(_player_character(_PC))
    for crew_name in _CREW:
        snap.npcs.append(_friendly_crew_npc(crew_name))
    return snap


def _threat_dispatch(threat_name: str = _THREAT_NAME):
    """Confrontation dispatch naming a threat that is NOT an existing NPC entity.

    The threat rides on ``params["threat"]`` — the TEA-defined threading contract
    (see module docstring). The Dev fix reads this and materializes the threat as
    the Other rather than falling back to the crew."""
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    return SubsystemDispatch(
        subsystem="confrontation",
        params={
            "type": "ship_combat",
            "threat": {
                "name": threat_name,
                "description": "three unregistered hulls running dark",
            },
        },
        idempotency_key="vela_ship_combat",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


@pytest.fixture
def otel_capture():
    """In-memory OTEL exporter on the running TracerProvider (self-contained;
    mirrors tests/server/test_chase_opponent_seating.py)."""
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


def _opponent_names(enc) -> list[str]:
    return [a.name for a in enc.actors if a.side == "opponent"]


# ---------------------------------------------------------------------------
# AC1 / AC2 core — the player's own crew must NEVER be the enemy ship.
# This is the heart of #C3 and is robust to the fix's design (whether the fix
# materializes a threat or fails loud, the crew are never opponents).
# ---------------------------------------------------------------------------


async def test_ship_combat_does_not_conscript_friendly_crew(otel_capture):
    """RED at HEAD: a ship_combat dispatched into a bridge scene where the only
    NPCs are the player's own crew must NOT seat the crew as the enemy ship.

    At HEAD the empty-``npcs_present`` fallback seats every co-located NPC as
    ``side=opponent`` (adversary_only=False for beat_selection ship_combat), so
    the crew become the opponent — the #C3 bug. After the fix, the crew are
    never opponents (the named threat is materialized, or — absent a nameable
    threat — the engine fails loud)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    pack = _load_space_opera()
    snap = _snapshot_with_crew()

    # The live router dispatches ship_combat with NO explicit actors. The threat
    # is named in the narrative but not (yet) threaded — so the dispatch carries
    # only the type, exactly as the playtest captured.
    dispatch = SubsystemDispatch(
        subsystem="confrontation",
        params={"type": "ship_combat"},
        idempotency_key="vela_ship_combat",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )

    # The fix may legitimately fail loud (no nameable Other in a friendly scene).
    # Either way, the invariant under test is: NO crew member is an opponent.
    with contextlib.suppress(Exception):
        await run_confrontation_dispatch(
            dispatch,
            snapshot=snap,
            pack=pack,
            player_name=_PC,
            npcs_present=[],
        )

    enc = snap.encounter
    if enc is not None:
        seated_opponents = _opponent_names(enc)
        conscripted = [c for c in _CREW if c in seated_opponents]
        assert not conscripted, (
            f"ship_combat conscripted the player's own crew as the enemy ship "
            f"(#C3): crew seated as opponents = {conscripted}; "
            f"all opponents = {seated_opponents}"
        )


# ---------------------------------------------------------------------------
# AC1 / AC6 — a NAMED threat is materialized and seated as the Other.
# ---------------------------------------------------------------------------


async def test_ship_combat_threads_named_threat_as_other(otel_capture):
    """RED at HEAD: when the ship_combat trigger names a threat (params["threat"]),
    that threat is materialized as the adversary and seated ``side=opponent`` —
    and the crew are not. A ``participant.joined`` span records the Other with a
    materialization source so the GM panel can see why the enemy is here (AC6).

    At HEAD params["threat"] is ignored: the fallback conscripts the crew (no
    named opponent reaches seating), so no threat-named opponent is seated."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch

    pack = _load_space_opera()
    snap = _snapshot_with_crew()

    await run_confrontation_dispatch(
        _threat_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name=_PC,
        npcs_present=[],
    )

    enc = snap.encounter
    assert enc is not None, (
        "ship_combat naming a threat must instantiate an encounter with that "
        "threat as the Other (it raised/no-op'd instead — threat not threaded)"
    )
    opponents = _opponent_names(enc)
    assert _THREAT_NAME in opponents, (
        f"the named threat {_THREAT_NAME!r} must be seated as the opponent; "
        f"got opponents={opponents}"
    )
    conscripted = [c for c in _CREW if c in opponents]
    assert not conscripted, (
        f"crew were conscripted instead of / alongside the named threat: {conscripted}"
    )

    # AC6: the materialized Other is observable, distinguished from a
    # location-fallback seat by its source.
    joined = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "participant.joined"
        and (s.attributes or {}).get("name") == _THREAT_NAME
    ]
    assert joined, (
        f"no participant.joined span for the materialized Other {_THREAT_NAME!r} "
        "— the membership entry is not observable on the GM panel"
    )
    assert any((s.attributes or {}).get("source") == "materialized" for s in joined), (
        "the threat's participant.joined span must carry source='materialized' so "
        "the GM panel distinguishes a materialized Other from a location-fallback "
        f"seat; got sources="
        f"{[(s.attributes or {}).get('source') for s in joined]}"
    )


# ---------------------------------------------------------------------------
# AC7 — end-to-end coupling: the materialized Other's HULL resolves the fight.
# Ties #C3 (right Other) to #C4 (hp_depletion). #C4's resolution machinery is
# already delivered/tested; this proves it engages on the MATERIALIZED threat,
# not the crew.
# ---------------------------------------------------------------------------


async def test_ship_combat_materialized_threat_resolves_on_hull(otel_capture):
    """RED at HEAD: a ship_combat named-threat trigger (with friendly crew in the
    scene) seats the threat as the Other (#C3) AND its hull is seeded so driving
    strikes depletes the THREAT's hull to resolution (#C4). At HEAD this fails at
    the #C3 step — the threat is never seated (crew conscripted), so there is no
    materialized hull to deplete."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    pack = _load_space_opera()
    snap = _snapshot_with_crew()

    await run_confrontation_dispatch(
        _threat_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name=_PC,
        npcs_present=[],
    )

    enc = snap.encounter
    assert enc is not None and _THREAT_NAME in _opponent_names(enc), (
        "precondition (#C3): the named threat must be seated as the Other before "
        "its hull can resolve the fight"
    )

    # #C4 surface: the materialized threat carries a real, non-sentinel hull.
    hull_core = snap.find_creature_core(_THREAT_NAME)
    assert hull_core is not None, "materialized threat must have a backing core (Task 9)"
    assert hull_core.hp.max == _SHIP_HP, (
        f"threat hull must be seeded from opponent_default_stats (hp {_SHIP_HP}), "
        f"not a sentinel; got max={hull_core.hp.max}"
    )

    # Seed the hull low so one killing salvo resolves deterministically, then
    # drive a winning broadside through the real dispatch path.
    hull_core.hp.current = 1
    outcome = dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="59-23-ship-kill",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id="close_range",
        ),
        rolling_player_id=f"player-{_PC.lower()}",
        character_name=_PC,
        character_stats=_STATS,
        encounter=enc,
        pack=pack,
        genre_slug="space_opera",
        session_id="59-23-e2e",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )

    assert hull_core.hp.current <= 0, "the threat's hull must reach 0 on the killing salvo"
    assert outcome.encounter_resolved is True, "0 hull on the Other must resolve the fight"
    assert enc.outcome == "player_victory", (
        f"hp_depletion on the materialized threat must resolve player_victory; "
        f"got {enc.outcome!r}"
    )


# ---------------------------------------------------------------------------
# AC2 fail-loud PIN (passes at HEAD) — guards that the fix does not regress the
# existing No-Silent-Fallbacks behavior for a genuinely empty scene.
# ---------------------------------------------------------------------------


async def test_ship_combat_empty_scene_fails_loud(otel_capture):
    """PIN (green at HEAD): a ship_combat with NO co-located NPCs and no nameable
    threat already fails loud via NoOpponentAvailableError (the dispatch handler
    catches it and returns an error output rather than seating a phantom Other).
    The #C3 fix must not regress this — an empty scene with no Other is prose,
    never a one-sided encounter."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    pack = _load_space_opera()
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[_PC] = _LOCATION
    snap.characters.append(_player_character(_PC))
    # No NPCs at all — empty scene.

    dispatch = SubsystemDispatch(
        subsystem="confrontation",
        params={"type": "ship_combat"},
        idempotency_key="vela_empty",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )

    out = await run_confrontation_dispatch(
        dispatch,
        snapshot=snap,
        pack=pack,
        player_name=_PC,
        npcs_present=[],
    )

    assert snap.encounter is None, (
        "an empty-scene ship_combat must not leave a one-sided encounter on the "
        "snapshot (No Silent Fallbacks)"
    )
    assert out.data.get("error") == "no_opponent_available", (
        f"the handler must surface the no-opponent fail-loud signal; got {out.data!r}"
    )
