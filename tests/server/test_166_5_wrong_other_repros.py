"""166-5 wrong-Other repros as permanent fixtures. Fiction targets win seats.

Repro shapes from the 2026-07-10 playtest ping-pong and the parked coyote_star
finding. Fixture style: synthetic pack + snapshot + real
``instantiate_encounter_from_trigger``, per
tests/server/test_162_3_generics_last_resort_seating.py.

Amendment A (ADR-156) deletes the 108-2 roster conscription
(``_resolve_opponent_from_roster``): when the intent router names a threat
that is NOT an existing roster entity, the seater must seat THAT name — never
substitute a different, merely co-located bestiary creature. Before this
story the conscription would scan the scene for ANY co-located, statted
adversary and seat it in the named target's place whenever the router's
free-string name didn't exact-match the roster — exactly backwards from what
the table asked for (the router named a PERSON; the engine seated a
creature).

The one legitimate case survives: when the router's name IS a recorded
roster alias / canonical name / invented_from binding, the resolver
(``resolve_roster_npc``, unchanged) seats the canonical bound entity — that's
correct identity resolution, not conscription of an unrelated creature.

The ship-scale (158-34 / ADR-153 §6) firewall is a completely separate
seating door untouched by this deletion; the fourth test pins that the
collapse doesn't regress it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.green_room import MaterializationCandidate, admit
from sidequest.game.origin import derive_origin
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.bestiary import BestiaryEntry
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
    WwnConfig,
)
from sidequest.server.dispatch.confrontation import find_confrontation_def
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    instantiate_encounter_from_trigger,
)
from tests.fixtures.dogfight_playtest_encounter import GENRE_SLUG as _DOGFIGHT_GENRE_SLUG
from tests.fixtures.dogfight_playtest_encounter import (
    make_dogfight_pack,
    make_snapshot_with_npc,
)

_LOC = "Salt Camp"
_WWN_LOC = "The Dropmouth"


def _combat_cdef() -> ConfrontationDef:
    """Minimal hp_depletion combat cdef (the 162-2/162-3 fixture shape)."""
    strike = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "Strength",
            "damage_channel": "strike",
            "effect": "A blow.",
            "narrator_hint": "Hit.",
        }
    )
    return ConfrontationDef(
        type="combat",
        label="Skirmish",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition="hp_depletion",
        player_metric=None,
        opponent_metric=None,
        opponent_default_stats={
            "Strength": 10,
            "hp": 8,
            "armor_class": 12,
            "dexterity": 11,
        },
        opponent_damage=DamageSpec(dice="1d6"),
        beats=[strike],
    )


def _generic_entry() -> BestiaryEntry:
    """The sanctioned last-resort seat (162-3) for a genuinely unbacked
    router name — every repro's "materialized" opponent draws from this."""
    return BestiaryEntry(
        id="drifter",
        name="A Drifter",
        level=1,
        hp=8,
        armor_class=11,
        attack_bonus=1,
        damage="1d6",
        role="generic Other",
    )


_WWN_ATTRS = ["STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA"]


class _FakeGenrePack:
    """Duck-typed pack for the seater: real ``RulesConfig`` + a bestiary
    carrying one authored generic row. Mirrors
    ``test_162_3_generics_last_resort_seating.py::_FakeGenrePack``."""

    def __init__(self, *, ruleset: str = "dial") -> None:
        if ruleset == "wwn":
            # WWN's load-time validator requires a complete identity-mapped
            # attribute_map — an identity map is the minimal legal WWN block
            # (mirrors the swn_test_pack fixture's own attribute_map shape).
            self.rules = RulesConfig(
                confrontations=[_combat_cdef()],
                ruleset=ruleset,
                wwn=WwnConfig(attribute_map={a: a for a in _WWN_ATTRS}),
                ability_score_names=_WWN_ATTRS,
            )
        else:
            self.rules = RulesConfig(confrontations=[_combat_cdef()], ruleset=ruleset)
        self._bestiary = SimpleNamespace(entries=[], generics=[_generic_entry()])

    def effective_bestiary(self, world: str | None) -> tuple[object | None, str]:
        return self._bestiary, "world"


def mk_manual_pool_creature(
    name: str,
    *,
    creature_id: str,
    location: str,
    disposition: int = -20,
    hp: int = 8,
    last_seen_turn: int = 4,
) -> Npc:
    """A co-located MANUAL_POOL bestiary creature (``manual_origin=True``, no
    ``region`` stamp) — the shape the old conscription used to grab instead
    of the router's named target."""
    npc = Npc(
        core=CreatureCore(
            name=name,
            description="A bestiary creature sharing the scene.",
            personality="Feral.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
        ),
        creature_id=creature_id,
        manual_origin=True,
        threat_level=1,
        disposition=disposition,
        location=location,
        last_seen_location=location,
        last_seen_turn=last_seen_turn,
    )
    npc.origin = derive_origin(npc)
    return npc


def _cand(npc: Npc, source: str) -> MaterializationCandidate:
    """Mirrors tests/game/test_green_room_admit.py::_cand — the assert
    narrows ``Npc.origin: Origin | None`` for pyright; ``mk_manual_pool_creature``
    always stamps it before returning."""
    assert npc.origin is not None
    return MaterializationCandidate(npc=npc, origin=npc.origin, source=source)


def _snapshot(*, player: str, location: str, genre_slug: str, world_slug: str) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = location
    return snap


@pytest.fixture
def combat_pack_with_generics():
    """A native (default-ruleset) combat scene at ``Salt Camp``, Chico
    seated, with a generics-carrying bestiary so an unbacked router name
    still seats (162-3), never raises."""
    snapshot = _snapshot(
        player="Chico",
        location=_LOC,
        genre_slug="mutant_wasteland",
        world_slug="seaboard_of_saints",
    )
    return SimpleNamespace(snapshot=snapshot, pack=_FakeGenrePack())


@pytest.fixture
def wwn_pack_with_generics():
    """Same shape, WWN-bound pack, Groucho seated at the Dropmouth. A WWN
    binding makes ``_roll_and_persist_initiative`` resolve DEXTERITY (unlike
    the "dial" default), so Groucho needs a real ability-score block."""
    snapshot = _snapshot(
        player="Groucho",
        location=_WWN_LOC,
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    snapshot.characters.append(
        Character(
            core=CreatureCore(
                name="Groucho",
                description="A wandering delver.",
                personality="Wry.",
            ),
            char_class="Warrior",
            race="Human",
            backstory="A wandering survivor.",
            stats={a: 12 for a in _WWN_ATTRS},
        )
    )
    return SimpleNamespace(snapshot=snapshot, pack=_FakeGenrePack(ruleset="wwn"))


@pytest.fixture
def sealed_letter_pack():
    """The synthetic ``swn_test_pack`` fixture pack's sealed-letter dogfight
    def (``opponent_default_stats.hp == 8``) — same pack the 158-34 firewall
    tests drive, per tests/server/dispatch/test_dogfight_seating_scale.py."""
    return make_dogfight_pack()


def test_salt_camp_brawl_seats_the_named_person_not_the_comob(combat_pack_with_generics) -> None:
    """Chico: router names an unmaterialized person; a co-located MANUAL_POOL
    bestiary creature exists. The person is seated; the herbivore is not."""
    snapshot = combat_pack_with_generics.snapshot
    grazer = mk_manual_pool_creature(
        "Resonance Grazer", creature_id="resonance_grazer", location="Salt Camp", disposition=-20
    )
    admit(snapshot, [_cand(grazer, "mm.encounters")])
    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=combat_pack_with_generics.pack,
        encounter_type="combat",
        player_name="Chico",
        npcs_present=[],
        genre_slug="mutant_wasteland",
        materialized_threat=NpcMention(
            name="the loudest Scrapborn", role="hostile", side="opponent"
        ),
    )
    assert enc is not None
    opponent = next(a for a in enc.actors if a.side == "opponent")
    assert opponent.name == "the loudest Scrapborn"
    assert all(a.name != "Resonance Grazer" for a in enc.actors)


def test_courier_grapple_same_shape_second_pack(wwn_pack_with_generics) -> None:
    """Groucho: identical shape, WWN pack, ghost-spirit co-located."""
    snapshot = wwn_pack_with_generics.snapshot
    ghost = mk_manual_pool_creature(
        "Restless Battlefield Ghost",
        creature_id="restless_battlefield_ghost",
        location=_WWN_LOC,
        disposition=-20,
    )
    admit(snapshot, [_cand(ghost, "mm.encounters")])
    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=wwn_pack_with_generics.pack,
        encounter_type="combat",
        player_name="Groucho",
        npcs_present=[],
        genre_slug="caverns_and_claudes",
        materialized_threat=NpcMention(name="the message courier", role="hostile", side="opponent"),
    )
    assert enc is not None
    opponent = next(a for a in enc.actors if a.side == "opponent")
    assert opponent.name == "the message courier"
    assert all(a.name != "Restless Battlefield Ghost" for a in enc.actors)


def test_named_target_matching_roster_alias_seats_canonical(combat_pack_with_generics) -> None:
    """The conscription's one legitimate case survives its deletion: a
    router name that IS a recorded alias seats the bound creature."""
    snapshot = combat_pack_with_generics.snapshot
    thief = mk_manual_pool_creature("Thief", creature_id="thief", location="cavern")
    thief.aliases.append("Molgrath the Eyeless")
    admit(snapshot, [_cand(thief, "mm.encounters")])
    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=combat_pack_with_generics.pack,
        encounter_type="combat",
        player_name="Keth",
        npcs_present=[],
        genre_slug="caverns_and_claudes",
        materialized_threat=NpcMention(
            name="Molgrath the Eyeless", role="hostile", side="opponent"
        ),
    )
    assert enc is not None
    opponent = next(a for a in enc.actors if a.side == "opponent")
    assert opponent.name == "Thief"  # canonical, one identity


def test_ship_duel_frame_source_unaffected(sealed_letter_pack) -> None:
    """coyote_star shape: sealed-letter dogfight with no named contact still
    seats the def-frame Other (158-34 firewall intact), never a ground
    creature. Drive lifted from
    test_dogfight_seating_scale.py::test_sealed_letter_dogfight_does_not_seat_colocated_ground_creature
    (grep -rln "frame_default" tests/)."""
    pack = sealed_letter_pack
    snapshot = make_snapshot_with_npc(
        pc_name="Pilot",
        npcs=[("Gengineered Killer", {"role": "hostile", "is_creature": True})],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type="dogfight",
        player_name="Pilot",
        npcs_present=[],  # router named no opponent
        genre_slug=_DOGFIGHT_GENRE_SLUG,
    )

    assert enc is not None, "the dogfight must still seat (ADR-116 requires an Other)"
    opponents = [a for a in enc.actors if a.side == "opponent"]
    assert len(opponents) == 1
    cdef = find_confrontation_def(pack.rules.confrontations or [], "dogfight")
    assert cdef is not None
    assert opponents[0].name == (cdef.label or "Enemy Fighter"), (
        "the seated opponent must be the def-frame default, not a ground creature"
    )
    assert opponents[0].name != "Gengineered Killer"
    assert all(a.name != "Gengineered Killer" for a in enc.actors), (
        "the ground creature must not be seated in the dogfight at all"
    )


# ---------------------------------------------------------------------------
# 5. Task 6 (Green Room implementation plan) — the loud-failure fixture.
# Other-requiring type, no named target, empty room, no generics: the
# "nothing to seat, nowhere to look" shape must still raise loudly and
# leave nothing half-seated, unchanged through the Amendment-A refactor
# this file's other four tests exercise.
# ---------------------------------------------------------------------------


class _NoGenericsCombatPack:
    """A combat-capable pack that authors NO generics — the 162-3
    last-resort source is unavailable too. Note this guard fires BEFORE
    generics would ever be consulted (see the test docstring below), so a
    pack that DID carry generics would raise identically; this shape is
    kept genuinely generics-less to match the brief's stated scenario."""

    def __init__(self) -> None:
        self.rules = RulesConfig(confrontations=[_combat_cdef()])

    def effective_bestiary(self, world: str | None) -> tuple[object | None, str]:
        return None, "genre"


@pytest.fixture
def bare_combat_pack() -> SimpleNamespace:
    """Solo, alone, in an empty room, against a pack with no authored
    generics — every legitimate opponent source (roster, pool, location
    fallback, generics) comes up empty."""
    snapshot = _snapshot(
        player="Solo",
        location="An Empty Room",
        genre_slug="mutant_wasteland",
        world_slug="seaboard_of_saints",
    )
    return SimpleNamespace(snapshot=snapshot, pack=_NoGenericsCombatPack())


def test_no_target_no_room_no_generics_raises(bare_combat_pack: SimpleNamespace) -> None:
    """Other-requiring type + no named target + empty room + no generics:
    raises, nothing half-seated (162-3 rollback preserved through the
    Green Room refactor).

    ``NoOpponentAvailableError`` (a ``ValueError`` subclass,
    encounter_lifecycle.py:75) is what actually fires here:
    ``instantiate_encounter_from_trigger``'s own adversarial empty+empty
    guard (ADR-116, ~L2018) raises it BEFORE the seeder ever reaches
    162-3's generics last-resort branch — with ``npcs_present=[]`` and no
    ``materialized_threat``, the location fallback finds nobody in the
    empty room, so ``npcs_present`` stays empty and the guard trips first.
    The brief's ``(NoOpponentAvailableError, ValueError)`` tuple is kept
    verbatim for robustness against a hypothetical future where a
    different guard on this path raises a bare ``ValueError`` instead."""
    with pytest.raises((NoOpponentAvailableError, ValueError)):
        instantiate_encounter_from_trigger(
            snapshot=bare_combat_pack.snapshot,
            pack=bare_combat_pack.pack,  # type: ignore[arg-type]  # duck-typed
            encounter_type="combat",
            player_name="Solo",
            npcs_present=[],
            genre_slug="test",
            materialized_threat=None,
        )
    assert bare_combat_pack.snapshot.encounter is None
    assert bare_combat_pack.snapshot.npcs == []
