"""Story 59-35 — seat present FRIENDLY companions as side="player" combatants.

Tests for Story 59-35 — written failing (red) before implementation and
passing (green) since the friendly-seater (``_friendly_fallback_at_location``)
was added in the same story. They cover ADR-116's friendly half + the SOUL
"Guitar Solo" principle: an allied NPC at the player's side FIGHTS, it is never
a silent spectator.

Background (Explore + Architect, 2026-06-04, develop): the opponent half of
ADR-116 seating already exists — ``_npc_fallback_at_location`` sources
opponents, and the generic actor loop seats the PC(s) as ``side="player"``.
The MISSING half: nothing scans ``snapshot.npcs`` for a scene-present,
FRIENDLY-disposition NPC and seats it as ``side="player"``. This is the FIRST
non-PC ``side="player"`` actor in the system.

Design seam (as implemented): in ``instantiate_encounter_from_trigger``, AFTER
the no-opponent guard, a symmetric ``_friendly_fallback_at_location`` seats
scene-present FRIENDLY NPCs as ``side="player"`` — additive, independent of
whether ``npcs_present`` was empty. Allies are deliberately kept OUT of
``npcs_present`` so they never satisfy the "requires an Other" guard (ADR-116
invariant — see ``test_friendly_ally_alone_still_raises_no_opponent``). Each
friendly seat emits ``participant.joined`` with ``source="friendly_fallback"``
plus ``disposition_attitude`` + ``last_seen_turn`` (the GM-panel lie-detector
proving the engine seated the ally because it read FRIENDLY disposition, not
the narrator inventing one).

AC mapping:
  AC1 — symmetric seater: present friendly → side="player"; present hostile →
        NOT friendly-seated; absent friendly → not seated; neutral → not seated.
        Plus the collision guard: a friendly ally co-located with a hostile in
        an empty-``npcs_present`` combat must NOT be conscripted as the opponent.
  AC2 — armed via EXISTING channels: a friendly-seated ally in an hp_depletion
        combat keeps a real ADR-114 stat block (its own ``Npc.core`` HP, not a
        placeholder, not the opponent's block).
  AC4 — OTEL: each friendly seat emits ``participant.joined`` with
        ``source="friendly_fallback"``, ``side="player"``.
  AC5 — Guitar Solo delivery (wiring): the seated ally appears in the
        per-recipient CONFRONTATION payload's ``actors`` roster with
        ``side="player"`` via the existing ``build_confrontation_payload``.

Fixture convention (see test_chase_opponent_seating.py): load the frozen
``test_genre`` pack from disk — it defines a ``combat`` confrontation
(category=combat, dial_threshold). The hp_depletion AC2 leg loads the real
``space_opera`` pack (sanctioned content exception, mirroring
test_72_8_presence_last_seen_stamp.py) because test_genre has no hp_depletion
confrontation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    find_confrontation_def,
)
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path
from tests._helpers.trigger_encounter import trigger_encounter

_FIXTURE_PACK = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "test_genre"

_PC = "Vesh"
_LOC = "Docking Ring"
# disposition > friendly_at (default 10) ⇒ Attitude.FRIENDLY; 0 ⇒ NEUTRAL;
# < hostile_at (default -10) ⇒ HOSTILE (see game/disposition.py).
_FRIENDLY = 25
_NEUTRAL = 0
_HOSTILE = -30


def _load_pack():
    return load_genre_pack(_FIXTURE_PACK)


def _make_npc(
    name: str,
    *,
    disposition: int = 0,
    last_seen_location: str | None = None,
    role: str | None = None,
    hp: HpPool | None = None,
) -> Npc:
    """Minimal stateful ``Npc`` carrying a disposition + a real ADR-114 HP pool.

    ``disposition`` is coerced to a ``Disposition`` wrapper by the Npc model
    (see disposition.py ``__get_pydantic_core_schema__``). The HP pool is the
    NPC's own stat block — AC2 asserts the friendly-seater does NOT replace it
    with a placeholder or the opponent's block.
    """
    return Npc(
        core=CreatureCore(
            name=name,
            description="A station-side companion.",
            personality="Loyal.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=hp or HpPool(current=10, max=10, base_max=10),
        ),
        npc_role_id=role,
        disposition=disposition,
        last_seen_location=last_seen_location,
        last_seen_turn=0,
    )


def _snap(*, genre_slug: str = "test_pack", world_slug: str = "test_world") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(interaction=2),
    )
    snap.character_locations[_PC] = _LOC
    return snap


def _explicit_opponent() -> list[NpcMention]:
    """An explicit opponent so the combat instantiates with an Other AND the
    opponent location-fallback is skipped (``npcs_present`` non-empty) — this
    isolates the friendly-seater from the opponent-sourcing path."""
    return [NpcMention(name="Raider", side="opponent", role="hostile")]


def _player_side_names(enc) -> set[str]:
    return {a.name for a in enc.actors if a.side == "player"}


def _actor(enc, name: str):
    return next((a for a in enc.actors if a.name == name), None)


@pytest.fixture
def otel_capture():
    """In-memory OTEL exporter attached to the running TracerProvider.

    Mirrors test_chase_opponent_seating.py::otel_capture.
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


# ---------------------------------------------------------------------------
# AC1 — symmetric friendly-seater
# ---------------------------------------------------------------------------


def test_present_friendly_npc_seated_as_player_side():
    """A scene-present FRIENDLY NPC is seated as ``side="player"`` — the core
    behaviour. The ally is in ``snapshot.npcs`` at the PC's location but is NOT
    named in ``npcs_present`` (an explicit opponent is). Today the seating loop
    only iterates ``npcs_present`` + the PC, so the ally is never seated; the
    friendly-seater must source it from ``snapshot.npcs``."""
    snap = _snap()
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=_explicit_opponent())

    enc = snap.encounter
    assert enc is not None, "combat encounter was not instantiated"
    mara = _actor(enc, "Mara")
    assert mara is not None, (
        "the FRIENDLY ally was not seated at all — the friendly-seater did not "
        f"source it from snapshot.npcs. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )
    assert mara.side == "player", (
        "a FRIENDLY ally must fight on side='player' (Guitar Solo), not as an "
        f"opponent/neutral spectator. got side={mara.side!r}"
    )


def test_present_hostile_npc_not_friendly_seated():
    """A scene-present HOSTILE NPC must NEVER be seated as ``side="player"``.
    With ``npcs_present=[]`` the opponent fallback seats the hostile as an
    opponent; the friendly-seater must not also pull it onto the player side."""
    snap = _snap()
    snap.npcs.append(
        _make_npc("Raider", disposition=_HOSTILE, role="hostile", last_seen_location=_LOC)
    )
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=[])

    enc = snap.encounter
    assert enc is not None
    raider = _actor(enc, "Raider")
    assert raider is not None, "the hostile NPC should have been sourced as the opponent"
    assert raider.side == "opponent", (
        f"a HOSTILE NPC must seat as opponent, never player. got side={raider.side!r}"
    )
    # Independent check (not entailed by the side assertion above): the ONLY
    # player-side actor is the PC — the friendly-seater added no phantom ally
    # from a hostile-only scene.
    assert _player_side_names(enc) == {_PC}, (
        "the only player-side actor should be the PC; the friendly-seater seated "
        f"an unexpected ally from a hostile-only scene. got {_player_side_names(enc)!r}"
    )


def test_absent_friendly_npc_not_seated():
    """A FRIENDLY NPC last seen at a DIFFERENT location is NOT pulled into the
    encounter — the friendly-seater is scene-scoped, mirroring the opponent
    fallback's location filter.

    Paired with a same-location FRIENDLY control ("Nessa" at ``_LOC``) so the
    test proves LOCATION is the discriminator: the control IS seated and the
    off-site ally is NOT. Without the control this assertion would pass against
    a no-op seater (the off-site ally is never seated either way)."""
    snap = _snap()
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location="Cargo Bay"))
    snap.npcs.append(_make_npc("Nessa", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=_explicit_opponent())

    enc = snap.encounter
    assert enc is not None
    nessa = _actor(enc, "Nessa")
    assert nessa is not None and nessa.side == "player", (
        "control failed: a same-location FRIENDLY ally must be seated side='player' "
        f"— the seater is not engaging. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )
    assert _actor(enc, "Mara") is None, (
        "a FRIENDLY NPC last seen elsewhere was incorrectly seated into the "
        f"encounter. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )


def test_neutral_npc_not_seated_as_player():
    """A scene-present NEUTRAL NPC is NOT auto-seated to the player side — only
    FRIENDLY disposition qualifies (story context DECISIONS: hostile/neutral are
    NOT auto-seated to player).

    Paired with a same-location FRIENDLY control ("Nessa") so the test proves
    DISPOSITION is the discriminator: the FRIENDLY ally IS seated player while
    the co-located NEUTRAL bystander is NOT. Without the control this would pass
    against a no-op seater."""
    snap = _snap()
    snap.npcs.append(_make_npc("Bystander", disposition=_NEUTRAL, last_seen_location=_LOC))
    snap.npcs.append(_make_npc("Nessa", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=_explicit_opponent())

    enc = snap.encounter
    assert enc is not None
    nessa = _actor(enc, "Nessa")
    assert nessa is not None and nessa.side == "player", (
        "control failed: a same-location FRIENDLY ally must be seated side='player' "
        f"— the seater is not engaging. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )
    assert "Bystander" not in _player_side_names(enc), (
        "a NEUTRAL bystander was auto-seated on the player side — only FRIENDLY "
        f"disposition should qualify. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )


def test_friendly_ally_not_conscripted_as_opponent_when_room_sourced():
    """Collision guard (Architect 2026-06-04): when ``npcs_present`` is empty,
    the opponent fallback (``_npc_fallback_at_location``, adversarial=True)
    currently conscripts EVERY same-location NPC as ``side="opponent"`` with no
    disposition filter — so a FRIENDLY ally co-located with a hostile would be
    seated as the enemy. The end-state contract: the hostile is the opponent,
    the FRIENDLY ally is on the player side. (Implementation is Dev's choice —
    teach the opponent fallback to skip FRIENDLY, or reclassify in the
    friendly-seater — this test pins the behaviour, not the mechanism.)"""
    snap = _snap()
    snap.npcs.append(
        _make_npc("Raider", disposition=_HOSTILE, role="hostile", last_seen_location=_LOC)
    )
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    # Empty npcs_present ⇒ the location fallback sources opponents from the room.
    trigger_encounter(snap, pack, "combat", _PC, npcs_present=[])

    enc = snap.encounter
    assert enc is not None
    raider = _actor(enc, "Raider")
    mara = _actor(enc, "Mara")
    assert raider is not None and raider.side == "opponent", (
        f"the hostile must remain the opponent. raider={raider!r}"
    )
    assert mara is not None and mara.side == "player", (
        "a FRIENDLY ally sourced from the room was conscripted as the enemy "
        f"instead of fighting at the player's side. mara={mara!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — invariant: a friendly ally does NOT satisfy "requires an Other"
# ---------------------------------------------------------------------------


def test_friendly_ally_alone_still_raises_no_opponent():
    """ADR-116 invariant (story AC3): a PC + a present FRIENDLY ally + ZERO
    opponents still raises ``NoOpponentAvailableError`` — a friendly side="player"
    seat must NEVER satisfy "a confrontation requires an Other".

    This is the load-bearing guard for the whole story: the friendly-seater adds
    the first non-PC player-side actor, and the danger is that a future refactor
    counts that ally toward the no-opponent guard (e.g. by appending allies into
    ``npcs_present``). The correct design keeps allies OUT of ``npcs_present`` so
    the guard fires on the empty opponent list BEFORE any ally is seated. Without
    this test that regression would pass every other test in the suite."""
    snap = _snap()
    # A FRIENDLY ally at the PC's location, and NO hostile/opponent anywhere.
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    with pytest.raises(NoOpponentAvailableError):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=pack,
            encounter_type="combat",
            player_name=_PC,
            npcs_present=[],
            genre_slug="test_pack",
        )

    # And the guard must refuse cleanly — no one-sided encounter left behind with
    # the ally mis-seated as the Other.
    if snap.encounter is not None:
        opp = [a for a in snap.encounter.actors if a.side == "opponent"]
        assert opp, (
            "a combat encounter was created with no opponent — the friendly ally "
            f"was wrongly treated as the Other. actors={[(a.name, a.side) for a in snap.encounter.actors]!r}"
        )


# ---------------------------------------------------------------------------
# AC4 — OTEL lie-detector: participant.joined source="friendly_fallback"
# ---------------------------------------------------------------------------


def test_friendly_seat_emits_participant_joined_span(otel_capture):
    """Each friendly seat emits a ``participant.joined`` span carrying
    ``side="player"`` and ``source="friendly_fallback"`` — the GM-panel
    lie-detector proving the ENGINE seated the ally (not the narrator inventing
    one). Today every ``side="player"`` participant.joined span hardcodes
    ``source="seat"`` (encounter_lifecycle.py), so no friendly_fallback-tagged
    span exists; the friendly-seater must distinguish its seats from PC seats."""
    snap = _snap()  # TurnManager(interaction=2) — the seat turn the stamp records
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=_explicit_opponent())

    joined = [s for s in otel_capture.get_finished_spans() if s.name == "participant.joined"]
    assert joined, "no participant.joined span emitted at all"
    friendly = [
        s
        for s in joined
        if (s.attributes or {}).get("name") == "Mara"
        and (s.attributes or {}).get("source") == "friendly_fallback"
    ]
    assert friendly, (
        "no participant.joined span for the friendly ally tagged "
        "source='friendly_fallback' — the GM panel cannot distinguish an "
        "engine-seated ally from the narrator improvising. "
        f"spans={[(dict(s.attributes or {}).get('name'), dict(s.attributes or {}).get('side'), dict(s.attributes or {}).get('source')) for s in joined]!r}"
    )
    attrs = dict(friendly[0].attributes or {})
    assert attrs.get("side") == "player", (
        f"the friendly seat's span must record side='player'; got {attrs.get('side')!r}"
    )
    # AC4: the span must carry BOTH disposition_attitude AND last_seen_turn so
    # the GM panel can prove the engine seated the ally *because it read FRIENDLY
    # disposition* — not because the narrator invented an ally. disposition_attitude
    # is the load-bearing "why" attribute; last_seen_turn is the recency stamp.
    assert attrs.get("disposition_attitude") == "friendly", (
        "the friendly seat's participant.joined span must carry "
        "disposition_attitude='friendly' (the GM-panel lie-detector proving the "
        f"seat was disposition-driven). got {attrs.get('disposition_attitude')!r}; "
        f"attrs={sorted(attrs)!r}"
    )
    assert attrs.get("last_seen_turn") == 2, (
        "the friendly seat's span must carry the ally's last_seen_turn recency "
        "stamp — the presence stamp records the seat turn (interaction=2). "
        f"got {attrs.get('last_seen_turn')!r}"
    )


# ---------------------------------------------------------------------------
# AC5 — Guitar Solo delivery: ally in the per-recipient CONFRONTATION payload
# ---------------------------------------------------------------------------


def test_friendly_ally_appears_in_confrontation_payload_actors():
    """End-to-end wiring (CLAUDE.md "Every Test Suite Needs a Wiring Test"):
    the seated ally must reach the per-recipient CONFRONTATION payload's
    ``actors`` roster with ``side="player"`` via the existing
    ``build_confrontation_payload`` — no silent audience. The payload projection
    is already seat-agnostic, so the load-bearing link is the seating: today the
    ally is never seated, so it never reaches the roster."""
    snap = _snap()
    snap.npcs.append(_make_npc("Mara", disposition=_FRIENDLY, last_seen_location=_LOC))
    pack = _load_pack()

    trigger_encounter(snap, pack, "combat", _PC, npcs_present=_explicit_opponent())
    enc = snap.encounter
    assert enc is not None

    cdef = find_confrontation_def(pack.rules.confrontations, "combat")
    assert cdef is not None, "test_genre pack must define a 'combat' confrontation"

    payload = build_confrontation_payload(encounter=enc, cdef=cdef, genre_slug="test_pack")

    actors = payload["actors"]
    ally_entries = [a for a in actors if a.get("name") == "Mara"]
    assert ally_entries, (
        "the friendly ally is absent from the CONFRONTATION payload actors roster "
        f"— the table would see a silent audience. actors={[(a.get('name'), a.get('side')) for a in actors]!r}"
    )
    assert ally_entries[0].get("side") == "player", (
        "the ally must be delivered to the table as a side='player' combatant; "
        f"got {ally_entries[0].get('side')!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — armed via EXISTING channels (hp_depletion, real space_opera pack)
# ---------------------------------------------------------------------------


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _space_opera_pack():
    try:
        return load_genre_pack(find_pack_path("space_opera"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


# Full SWN canonical stat block (mirrors test_72_8_presence_last_seen_stamp) so the
# hp_depletion instantiation seam can roll 1d8+DEX initiative without KeyError.
_SWN_STATS = {
    "STR": 12,
    "DEX": 12,
    "CON": 12,
    "INT": 12,
    "WIS": 12,
    "CHA": 12,
}


def _spacer(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="Station-side spacer.",
            personality="steady",
            inventory=Inventory(),
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Soldier",
        race="Coreworlder",
        backstory="Ex-Hegemonic infantry.",
        stats=dict(_SWN_STATS),
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_friendly_ally_armed_with_real_stat_block_in_hp_depletion_combat():
    """AC2: a friendly-seated ally in an hp_depletion combat is ARMED via the
    existing ADR-114 ``Npc.core`` HP channel — it keeps a REAL stat block (its
    own positive HP pool + a resolvable armor_class), NOT a placeholder and NOT
    the opponent's block. Driven end-to-end through the production
    ``instantiate_encounter_from_trigger`` hp_depletion gate (the seam that
    seeds opponents via ``_seed_combat_hp_depletion_to_npcs``).

    The ally is constructed with a DISTINCTIVE pool (7/12) so the assertion is
    non-vacuous: if the seating path zeroed it or overwrote it with the
    opponent's content stats, ``max`` would not be 12."""
    pack = _space_opera_pack()
    snap = _snap(genre_slug="space_opera", world_slug="coyote_star")
    snap.turn_manager = TurnManager(interaction=6)
    snap.character_locations[_PC] = "Docking Ring"
    snap.characters.append(_spacer(_PC))
    snap.npcs.append(
        _make_npc(
            "Mara",
            disposition=_FRIENDLY,
            last_seen_location="Docking Ring",
            hp=HpPool(current=7, max=12, base_max=12),
        )
    )

    trigger_encounter(
        snap,
        pack,
        "combat",
        _PC,
        npcs_present=[NpcMention(name="Corsair", side="opponent")],
    )

    enc = snap.encounter
    assert enc is not None
    mara_actor = _actor(enc, "Mara")
    assert mara_actor is not None and mara_actor.side == "player", (
        "the FRIENDLY ally was not seated as a player-side combatant in the "
        f"hp_depletion combat. actors={[(a.name, a.side) for a in enc.actors]!r}"
    )

    mara_npc = next((n for n in snap.npcs if n.core.name == "Mara"), None)
    assert mara_npc is not None, "the friendly ally Npc vanished from snapshot.npcs"
    assert mara_npc.core.hp.max == 12 and mara_npc.core.hp.current > 0, (
        "the friendly-seated ally must keep its own REAL ADR-114 HP pool (7/12), "
        "not a zeroed placeholder and not the opponent's content block. "
        f"got hp={mara_npc.core.hp.current}/{mara_npc.core.hp.max}"
    )
    # Non-vacuous: the ally is constructed with the CreatureCore default AC (10).
    # `_seed_combat_hp_depletion_to_npcs` seeds armor_class only for side=="opponent",
    # so a player-side ally's AC must be UNCHANGED at 10 — proving it was not
    # clobbered with the opponent's content armor_class. (A `>= 1` floor could
    # never fail and would be vacuous.)
    assert mara_npc.core.armor_class == 10, (
        "the seated ally must keep its own armor_class (default 10), NOT be "
        "overwritten by the opponent's content stat block. "
        f"got AC={mara_npc.core.armor_class}"
    )
