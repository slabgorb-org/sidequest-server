"""108-2 — the confrontation seater resolves a router-named opponent to a bound,
statted adversary from the roster BEFORE minting a generic stub.

Playtest 2026-06-14 (beneath_sunden MP + heavy_metal/barsoom): the intent
router names the adversary as a FREE STRING (``params["opponent"]``). When that
string does not match a bound ``Npc``, the seater fabricated a generic
``CreatureCore`` (HP 10 / AC default) and appended it to ``snapshot.npcs`` — the
"Hold-Dead, Still at the Shift" / "Arena Opponent" stubs. Meanwhile the
narrator's prose referenced the BOUND creature ("Molgrath the Eyeless", HP 24,
``creature_id='Thief'``), producing a player-visible identity split (prose name
≠ combat-panel name) and discarding the WWN-balanced bestiary stats the
Monster-Manual binding (107-2) seeded.

ADR-059 / ADR-116 / SOUL "Bind the Ruleset": the Other must be a bound, statted
adversary when one is present in the scene — not a free-string invention. These
tests pin:

1. A router-named opponent with no roster match resolves to a co-located,
   statted, hostile bound creature (the production ``instantiate_*`` path).
2. The resolved bound creature keeps its OWN statted HP — the hp_depletion
   seater must not clobber it with the confrontation's generic default.
3. When no bound adversary is in scene, the seater still mints a stub, but marks
   it ephemeral and emits the ``encounter.opponent_minted_stub`` lie-detector
   span (No Silent Fallbacks).
4. An ephemeral fabricated stub is reaped with its resolved encounter and never
   persists as durable roster canon (MINTING-MAJOR persist).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import (
    _seed_combat_hp_depletion_to_npcs,
    instantiate_encounter_from_trigger,
    reap_resolved_encounter_husk,
)

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"

_LOC = "the_dropmouth"


def _load_pack():
    return load_genre_pack(_FIXTURE_PACK)


def _statted_creature(
    name: str,
    *,
    creature_id: str = "Thief",
    hp: int = 24,
    location: str | None = _LOC,
    disposition: int = -20,
) -> Npc:
    """A bound bestiary creature: ``creature_id`` set, real HP pool, hostile."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="A dead delver still at the shift.",
            personality="Relentless.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
        ),
        creature_id=creature_id,
        threat_level=1,
        disposition=disposition,
        last_seen_location=location,
        last_seen_turn=4,
    )


@pytest.fixture
def otel_capture():
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


def _snapshot_with(npc: Npc | None, *, player: str = "Kirk") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = _LOC
    if npc is not None:
        snap.npcs.append(npc)
    return snap


# ---------------------------------------------------------------------------
# 1. Resolution: a router-named opponent resolves to the bound creature
# ---------------------------------------------------------------------------


def test_router_named_opponent_resolves_to_colocated_bound_creature():
    """Production path: the router invents "Hold-Dead, Still at the Shift", which
    matches no roster entry; Molgrath the Eyeless (statted, hostile, in-room) is
    seated instead — no stub is appended."""
    snap = _snapshot_with(_statted_creature("Molgrath the Eyeless"))
    pack = _load_pack()

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(
            name="Hold-Dead, Still at the Shift", role="hostile", side="opponent"
        ),
    )

    opponents = [a.name for a in enc.actors if a.side == "opponent"]
    assert opponents == ["Molgrath the Eyeless"], (
        f"expected the bound creature seated as the Other, got {opponents!r}"
    )
    # The router invention must never be appended as a fabricated roster entry.
    assert not any("Hold-Dead" in n.core.name for n in snap.npcs), (
        "router-invented stub leaked into snapshot.npcs"
    )


def test_resolution_emits_decision_span(otel_capture):
    """The resolution decision is observable on the GM panel (OTEL principle):
    router name → bound name + creature_id."""
    snap = _snapshot_with(_statted_creature("Molgrath the Eyeless"))
    pack = _load_pack()

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(
            name="Hold-Dead, Still at the Shift", role="hostile", side="opponent"
        ),
    )

    names = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.opponent_resolved_from_roster" in names, (
        f"resolution decision span not emitted; saw {sorted(names)}"
    )


def test_router_name_matching_roster_is_left_alone():
    """When the router names an opponent that IS a bound roster entry, it is
    seated directly — resolution must not re-point it to a different creature."""
    snap = _snapshot_with(_statted_creature("Molgrath the Eyeless"))
    # A second, unrelated statted adversary also in the room.
    snap.npcs.append(_statted_creature("Wailthroat", hp=32))
    pack = _load_pack()

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(name="Wailthroat", role="hostile", side="opponent"),
    )
    opponents = [a.name for a in enc.actors if a.side == "opponent"]
    assert opponents == ["Wailthroat"], (
        f"a roster-matching router name must be seated as-is, got {opponents!r}"
    )


def test_no_colocated_bound_creature_seats_router_name():
    """No statted adversary in scene → the router name is seated (and minted by
    the seater) rather than hijacking a far-off creature."""
    snap = _snapshot_with(None)
    # A statted creature exists but is in a DIFFERENT room — must not be grabbed.
    snap.npcs.append(_statted_creature("Molgrath the Eyeless", location="elsewhere"))
    pack = _load_pack()

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(name="Arena Opponent", role="hostile", side="opponent"),
    )
    opponents = [a.name for a in enc.actors if a.side == "opponent"]
    assert opponents == ["Arena Opponent"], (
        f"expected the router name seated when no co-located creature, got {opponents!r}"
    )


# ---------------------------------------------------------------------------
# 1b. Non-combat confrontations must NOT conscript an ambient bestiary creature
#     (150-2 dust_and_lead Defect A: a Fate standoff seated a "Western
#     Diamondback" rattlesnake as the Other for a human drifter).
# ---------------------------------------------------------------------------


def test_non_combat_confrontation_does_not_conscript_colocated_bestiary_creature():
    """150-2 / Defect A: the 108-2 roster reconciliation exists to preserve a
    BOUND creature's COMBAT hp stats (ADR-059). It is creature_id-gated, so the
    ONLY thing it can conscript is a bestiary monster — never an authored human
    NPC. For a NON-combat confrontation (a Fate standoff, a social duel, a
    chase) a bestiary monster is never the right Other. A co-located ambient
    "Western Diamondback" must NOT be seated against a router-named human
    threat; the router name is seated instead.
    """
    snake = _statted_creature(
        "Western Diamondback", creature_id="diamondback_rattler", hp=2
    )
    snap = _snapshot_with(snake)
    pack = _load_pack()

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        # chase → category "movement" in the fixture: a non-combat, adversarial
        # confrontation (seats an opponent-side actor), exactly the category
        # class the dust_and_lead "standoff" (pre_combat) belongs to.
        encounter_type="chase",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(
            name="the drifter", role="hostile", side="opponent"
        ),
    )

    opponents = [a.name for a in enc.actors if a.side == "opponent"]
    assert opponents == ["the drifter"], (
        "a non-combat confrontation must seat the router-named human threat, "
        f"not conscript the ambient bestiary creature; got {opponents!r}"
    )


def test_combat_confrontation_still_reconciles_same_bestiary_creature():
    """Regression guard for the 108-2 value: the SAME co-located bestiary
    creature a non-combat confrontation must decline IS still reconciled for a
    COMBAT confrontation — combat is exactly where binding the bound creature's
    statted HP matters (ADR-059). Proves the 150-2 gate keys on the confrontation
    CATEGORY, not the creature.
    """
    snake = _statted_creature(
        "Western Diamondback", creature_id="diamondback_rattler", hp=2
    )
    snap = _snapshot_with(snake)
    pack = _load_pack()

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(
            name="Hold-Dead", role="hostile", side="opponent"
        ),
    )

    opponents = [a.name for a in enc.actors if a.side == "opponent"]
    assert opponents == ["Western Diamondback"], (
        f"combat reconciliation (108-2) regressed; got {opponents!r}"
    )


def test_non_combat_skip_emits_decision_span(otel_capture):
    """OTEL principle / CLAUDE.md lie-detector: declining to conscript an ambient
    bestiary hazard into a non-combat confrontation is a subsystem decision and
    MUST be observable on the GM panel."""
    snake = _statted_creature(
        "Western Diamondback", creature_id="diamondback_rattler", hp=2
    )
    snap = _snapshot_with(snake)
    pack = _load_pack()

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="chase",
        player_name="Kirk",
        npcs_present=[],
        genre_slug=snap.genre_slug,
        materialized_threat=NpcMention(
            name="the drifter", role="hostile", side="opponent"
        ),
    )

    spans = {s.name: s for s in otel_capture.get_finished_spans()}
    assert "encounter.roster_resolution_skipped" in spans, (
        f"declined-conscription decision span not emitted; saw {sorted(spans)}"
    )
    attrs = spans["encounter.roster_resolution_skipped"].attributes or {}
    assert attrs.get("declined_name") == "Western Diamondback", (
        f"span must name the declined creature; got {dict(attrs)!r}"
    )


# ---------------------------------------------------------------------------
# 2. Statted HP preservation in the hp_depletion seater
# ---------------------------------------------------------------------------


def _fake_hp_depletion_cdef(*, hp: int = 10, ac: int = 13):
    return SimpleNamespace(
        opponent_hp=hp,
        opponent_armor_class=ac,
        opponent_damage="1d6",  # resolvable reprisal -> not toothless
        beats=[],
        confrontation_type="arena_duel",
    )


def test_statted_bound_creature_keeps_own_hp_not_cdef_default():
    """A resolved bound creature (creature_id set, HP 24) seated for an
    hp_depletion combat must keep its OWN statted pool — the seater must NOT
    overwrite it with the confrontation's generic opponent_default_stats HP."""
    creature = _statted_creature("Molgrath the Eyeless", hp=24)
    snap = _snapshot_with(creature)
    actors = [
        EncounterActor(name="Kirk", role="combatant", side="player"),
        EncounterActor(name="Molgrath the Eyeless", role="combatant", side="opponent"),
    ]

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=actors,
        cdef=_fake_hp_depletion_cdef(hp=10),
        turn=5,
        source="test",
        acting_character_name="Kirk",
        ruleset=get_ruleset_module("wwn"),
    )

    assert creature.core.hp.max == 24, "statted creature HP clobbered to cdef default"
    assert creature.core.hp.current == 24, "statted creature not healed to its OWN full"


def test_non_statted_existing_npc_still_seeded_from_cdef():
    """A narrator-declared NPC (no creature_id, no real stats) keeps the existing
    behavior: the confrontation's opponent_default_stats seed its pool."""
    plain = Npc(
        core=CreatureCore(
            name="Nameless Thug",
            description="A thug.",
            personality="Mean.",
            inventory=Inventory(),
            hp=HpPool(current=5, max=5, base_max=5),
        ),
        last_seen_location=_LOC,
    )
    snap = _snapshot_with(plain)
    actors = [
        EncounterActor(name="Kirk", role="combatant", side="player"),
        EncounterActor(name="Nameless Thug", role="combatant", side="opponent"),
    ]

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=actors,
        cdef=_fake_hp_depletion_cdef(hp=10),
        turn=5,
        source="test",
        acting_character_name="Kirk",
        ruleset=get_ruleset_module("wwn"),
    )

    assert plain.core.hp.max == 10, "non-statted NPC should seed from cdef opponent_hp"


# ---------------------------------------------------------------------------
# 3. Minted stub is marked ephemeral + emits the lie-detector span
# ---------------------------------------------------------------------------


def test_minted_stub_marked_ephemeral_and_spanned(otel_capture):
    """When the seater must fabricate an opponent (no backing Npc), it marks the
    stub ``ephemeral`` and emits ``encounter.opponent_minted_stub`` so the GM
    panel sees the fabrication (No Silent Fallbacks)."""
    snap = _snapshot_with(None)
    actors = [
        EncounterActor(name="Kirk", role="combatant", side="player"),
        EncounterActor(name="Arena Opponent", role="combatant", side="opponent"),
    ]

    _seed_combat_hp_depletion_to_npcs(
        snapshot=snap,
        actors=actors,
        cdef=_fake_hp_depletion_cdef(hp=10),
        turn=5,
        source="test",
        acting_character_name="Kirk",
        ruleset=get_ruleset_module("wwn"),
    )

    stub = next(n for n in snap.npcs if n.core.name == "Arena Opponent")
    assert stub.ephemeral is True, "fabricated combat stub must be marked ephemeral"
    names = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.opponent_minted_stub" in names, (
        f"minted-stub lie-detector span not emitted; saw {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# 4. Ephemeral stub reaped with its resolved encounter
# ---------------------------------------------------------------------------


def _resolved_encounter_with_opponent(name: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="arena_duel",
        win_condition="hp_depletion",
        category="combat",
        player_metric=EncounterMetric(name="pc", threshold=1),
        opponent_metric=EncounterMetric(name="opp", threshold=1),
        actors=[
            EncounterActor(name="Kirk", role="combatant", side="player"),
            EncounterActor(name=name, role="combatant", side="opponent"),
        ],
        resolved=True,
        outcome="player_victory",
    )


def test_ephemeral_stub_reaped_with_resolved_encounter():
    """A fabricated ephemeral stub must not persist as durable canon: it is
    reaped together with its resolved encounter at turn start."""
    stub = Npc(
        core=CreatureCore(
            name="Arena Opponent",
            description="Combat opponent",
            personality="Adversary",
            inventory=Inventory(),
            hp=HpPool(current=0, max=10, base_max=10),
        ),
        ephemeral=True,
        last_seen_location=_LOC,
    )
    snap = _snapshot_with(stub)
    snap.encounter = _resolved_encounter_with_opponent("Arena Opponent")

    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=False, turn=6)

    assert reaped is True
    assert snap.encounter is None
    assert not any(n.core.name == "Arena Opponent" for n in snap.npcs), (
        "ephemeral combat stub persisted after its encounter resolved"
    )


def test_bound_creature_not_reaped_with_resolved_encounter():
    """A real bound creature (creature_id set, not ephemeral) survives the husk
    reap — only fabricated stubs are quarantined."""
    creature = _statted_creature("Molgrath the Eyeless")
    snap = _snapshot_with(creature)
    snap.encounter = _resolved_encounter_with_opponent("Molgrath the Eyeless")

    reap_resolved_encounter_husk(snap, is_dice_replay=False, turn=6)

    assert any(n.core.name == "Molgrath the Eyeless" for n in snap.npcs), (
        "a bound roster creature must not be reaped as if it were a stub"
    )


def test_dice_replay_does_not_reap_stub():
    """The dice-resolution re-entry (same logical turn) must never reap — it
    narrates the just-resolved fight and needs the actors intact."""
    stub = Npc(
        core=CreatureCore(
            name="Arena Opponent",
            description="Combat opponent",
            personality="Adversary",
            inventory=Inventory(),
            hp=HpPool(current=0, max=10, base_max=10),
        ),
        ephemeral=True,
    )
    snap = _snapshot_with(stub)
    snap.encounter = _resolved_encounter_with_opponent("Arena Opponent")

    reaped = reap_resolved_encounter_husk(snap, is_dice_replay=True, turn=6)

    assert reaped is False
    assert snap.encounter is not None
    assert any(n.core.name == "Arena Opponent" for n in snap.npcs)
