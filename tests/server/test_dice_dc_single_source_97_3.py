"""Story 97-3 — Dice banner DC has two sources of truth (RED).

Measured (ping-pong 2026-06-07 dice entry, FIXER notes ui #352): the pre-roll
TARGET banner DC is a CLIENT-side formula — App.tsx ``handleBeatSelect`` builds
a local DiceRequest with ``rawDc = clamp(10 + |beat.base|*2, 10..30)`` — while
the server resolves against its own effective difficulty
(``ruleset.attack_params(...).target_number``). Under the native ruleset the
two formulas coincide by luck; under SWN/hp_depletion the server resolves
against the opponent's *armor class*, a number the client cannot even know.
Chico repro: total=12 vs displayed DC 12 → outcome Fail, because the server's
effective DC wasn't 12.

The fix contract pinned here: **the server is the only DC author.** The beat
offer (``build_confrontation_payload``) must carry a server-computed
``difficulty`` on every serialized beat, equal to the difficulty
``dispatch_dice_throw`` will later resolve that beat against. The UI then
renders the offered number instead of inventing one (companion suite:
sidequest-ui ``dice-dc-server-authored-97-3.test.tsx``).

ACs:
 1. The displayed pre-roll target equals the difficulty the server resolves
    against, for native and hp_depletion rulesets.
 2. ``DICE_RESULT.difficulty`` matches the banner the player saw.

Contract chosen for the seam (TEA test-design decision, logged in the session
file): ``build_confrontation_payload`` gains a ``rules`` kwarg (the pack's
``RulesConfig``) so it can resolve the ruleset module and author per-beat
difficulty with the target core it already reaches via ``core_resolver``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    EncounterPhase,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.models.inventory import DamageSpec
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    ResolutionMode,
    RulesConfig,
    SwnConfig,
    WinCondition,
)
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.server.dispatch.dice import dispatch_dice_throw

# ---------------------------------------------------------------------------
# Fixtures — native (dial) pack
# ---------------------------------------------------------------------------

# Native formula (ruleset/native.py compute_dc): clamp(10 + |base|*2, 10..30).
_NATIVE_BASE = 2
_NATIVE_EXPECTED_DC = 14  # 10 + |2|*2


def _native_pack() -> object:
    cdef = ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=10),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "kick_door",
                    "label": "Kick Door",
                    "kind": "strike",
                    "base": _NATIVE_BASE,
                    "stat_check": "STRENGTH",
                }
            ),
            # base omitted → server defaults to 1 → DC 12. Pins the default-base
            # edge the client formula papered over with `beat.base ?? 1`.
            BeatDef.model_validate(
                {
                    "id": "shove",
                    "label": "Shove",
                    "kind": "push",
                    "stat_check": "STRENGTH",
                }
            ),
        ],
    )
    rules = RulesConfig(confrontations=[cdef])
    pack = MagicMock()
    pack.rules = rules
    pack.inventory = None
    return pack


def _native_encounter() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name="Bob", role="combatant", side="player"),
            EncounterActor(name="Grug", role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _native_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="test",
        world_slug="test",
        turn_manager=TurnManager(),
    )


# ---------------------------------------------------------------------------
# Fixtures — SWN hp_depletion pack (the divergent case)
# ---------------------------------------------------------------------------

_SWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Physique",
    "CONSTITUTION": "Resolve",
    "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect",
    "WISDOM": "Cunning",
    "CHARISMA": "Influence",
}
_SWN_ABILITY_NAMES = list(_SWN_ATTRIBUTE_MAP.values())
_SWN_STATS = {name: 12 for name in _SWN_ABILITY_NAMES}

# Deliberately distinct from every value the native formula can produce for
# base=2 (14) or base=1 (12) — if any test below sees 12/14 under SWN, the
# native client formula is still authoring the number.
_SWN_OPPONENT_AC = 17


def _swn_pack() -> object:
    strike = BeatDef.model_validate(
        {
            "id": "shoot",
            "label": "Open Fire",
            "kind": "strike",
            "base": 2,
            "stat_check": "Reflex",
            "damage_channel": "strike",
            "damage_override": DamageSpec(dice="1d6"),
        }
    )
    cdef = ConfrontationDef(
        type="combat",
        label="Spaceport Firefight",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition=WinCondition.hp_depletion,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike],
        opponent_default_stats={
            **{name: 12 for name in _SWN_ABILITY_NAMES},
            # Reserved hp_depletion combat keys (required at load).
            "hp": 30,
            "armor_class": _SWN_OPPONENT_AC,
            "dexterity": 10,
        },
    )
    rules = RulesConfig(
        ruleset="swn",
        ability_score_names=_SWN_ABILITY_NAMES,
        confrontations=[cdef],
        swn=SwnConfig(attribute_map=_SWN_ATTRIBUTE_MAP),
    )
    pack = MagicMock()
    pack.rules = rules
    pack.inventory = None
    return pack


def _swn_snapshot_and_encounter(
    *, opponent_ac: int = _SWN_OPPONENT_AC, seat_opponent: bool = True
) -> tuple[GameSnapshot, StructuredEncounter]:
    atk_core = CreatureCore(
        name="Vane",
        description="Spacer.",
        personality="wry",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        armor_class=12,
    )
    attacker = Character(
        core=atk_core,
        char_class="Warrior",
        race="Human",
        backstory="Born shipboard.",
        stats=dict(_SWN_STATS),
    )
    snap = GameSnapshot(
        genre_slug="space_opera",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(attacker)

    actors = [EncounterActor(name="Vane", role="combatant", side="player")]
    if seat_opponent:
        opp_core = CreatureCore(
            name="Scrag",
            description="Pirate.",
            personality="cruel",
            inventory=Inventory(),
            hp={"current": 30, "max": 30, "base_max": 30},
            armor_class=opponent_ac,
        )
        snap.npcs.append(Npc(core=opp_core))
        actors.append(EncounterActor(name="Scrag", role="combatant", side="opponent"))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=actors,
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def _throw(beat_id: str, face: int = 14) -> DiceThrowPayload:
    return DiceThrowPayload(
        request_id="req-97-3",
        throw_params=ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        face=[face],
        beat_id=beat_id,
    )


def _offered_difficulty(payload: dict, beat_id: str) -> object:
    beat_dicts = {b["id"]: b for b in payload["beats"]}
    assert beat_id in beat_dicts, (
        f"beat {beat_id!r} missing from offered beats {sorted(beat_dicts)}"
    )
    return beat_dicts[beat_id].get("difficulty")


# ---------------------------------------------------------------------------
# AC1 — the beat offer carries a server-authored difficulty
# ---------------------------------------------------------------------------


class TestBeatOfferCarriesServerAuthoredDifficulty:
    def test_native_beat_offer_difficulty_matches_native_dc(self) -> None:
        """Native ruleset: offered difficulty == compute_dc (10 + |base|*2)."""
        pack = _native_pack()
        enc = _native_encounter()
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="test",
            rules=pack.rules,
        )
        assert _offered_difficulty(payload, "kick_door") == _NATIVE_EXPECTED_DC

    def test_native_beat_offer_difficulty_defaults_base_to_1(self) -> None:
        """A beat with no explicit base offers DC 12 (base defaults to 1
        server-side) — the edge the client papered over with `base ?? 1`."""
        pack = _native_pack()
        enc = _native_encounter()
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="test",
            rules=pack.rules,
        )
        assert _offered_difficulty(payload, "shove") == 12

    def test_swn_beat_offer_difficulty_is_target_ac_not_native_formula(self) -> None:
        """SWN/hp_depletion: the offered difficulty is the opponent's armor
        class — a number the client-side native formula can NEVER produce
        from beat.base. This is the divergence the Chico repro measured."""
        pack = _swn_pack()
        snap, enc = _swn_snapshot_and_encounter()
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="space_opera",
            core_resolver=snap.find_creature_core,
            rules=pack.rules,
        )
        offered = _offered_difficulty(payload, "shoot")
        assert offered == _SWN_OPPONENT_AC, (
            f"offered difficulty {offered!r} != opponent AC {_SWN_OPPONENT_AC} — "
            "if this is 12 or 14, the native client formula is still the author"
        )


# ---------------------------------------------------------------------------
# AC1 + AC2 — round-trip: the offered number IS the number resolved against
# ---------------------------------------------------------------------------


class TestOfferedDifficultyMatchesResolution:
    def test_native_offer_equals_dice_result_difficulty(self) -> None:
        pack = _native_pack()
        enc = _native_encounter()
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="test",
            rules=pack.rules,
        )
        offered = _offered_difficulty(payload, "kick_door")

        outcome = dispatch_dice_throw(
            payload=_throw("kick_door", face=13),
            rolling_player_id="p1",
            character_name="Bob",
            character_stats={"STRENGTH": 16},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="test",
            session_id="s-native",
            round_number=1,
            room_broadcast=None,
            snapshot=_native_snapshot(),
        )
        assert outcome.request.difficulty == offered
        assert outcome.result.difficulty == offered

    def test_swn_offer_equals_dice_result_difficulty(self, monkeypatch) -> None:
        """The core 97-3 regression pin: under SWN the resolved difficulty is
        the target AC. The offered (banner) number must equal it — today the
        client banner shows the native formula instead and the player watches
        a roll 'succeed' against the number on screen yet come back Fail."""
        monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)
        pack = _swn_pack()
        snap, enc = _swn_snapshot_and_encounter()
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="space_opera",
            core_resolver=snap.find_creature_core,
            rules=pack.rules,
        )
        offered = _offered_difficulty(payload, "shoot")

        outcome = dispatch_dice_throw(
            payload=_throw("shoot", face=10),
            rolling_player_id="p-vane",
            character_name="Vane",
            character_stats=dict(_SWN_STATS),
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="space_opera",
            session_id="s-swn",
            round_number=1,
            room_broadcast=None,
            snapshot=snap,
        )
        assert outcome.result.difficulty == _SWN_OPPONENT_AC, (
            "precondition broke: dispatch no longer resolves SWN vs target AC"
        )
        assert offered == outcome.result.difficulty, (
            f"banner DC {offered!r} != resolved DC {outcome.result.difficulty} — "
            "two sources of truth (AC2)"
        )

    def test_swn_unseated_opponent_offer_still_matches_resolution(self, monkeypatch) -> None:
        """Degenerate seating (no opponent core resolvable): whatever default
        the server resolves against (SWN unarmored AC 10), the offer must say
        the same number. Consistency must hold even in the edge, or the banner
        lies precisely when the engine is already in a weird state."""
        monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)
        pack = _swn_pack()
        snap, enc = _swn_snapshot_and_encounter(seat_opponent=False)
        cdef = pack.rules.confrontations[0]
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="space_opera",
            core_resolver=snap.find_creature_core,
            rules=pack.rules,
        )
        offered = _offered_difficulty(payload, "shoot")

        outcome = dispatch_dice_throw(
            payload=_throw("shoot", face=10),
            rolling_player_id="p-vane",
            character_name="Vane",
            character_stats=dict(_SWN_STATS),
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="space_opera",
            session_id="s-swn-unseated",
            round_number=1,
            room_broadcast=None,
            snapshot=snap,
        )
        assert offered == outcome.result.difficulty, (
            f"banner DC {offered!r} != resolved DC {outcome.result.difficulty} "
            "for the unseated-opponent edge"
        )
