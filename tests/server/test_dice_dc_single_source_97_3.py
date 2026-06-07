"""Story 97-3 — the server is the single author of the pre-roll DC.

Regression pin for the fix to the two-sources-of-truth bug. Pre-fix state:

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

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

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
    CwnConfig,
    HackingConfig,
    MetricDef,
    ResolutionMode,
    RulesConfig,
    SwnConfig,
    WinCondition,
    WwnConfig,
)
from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.server.dispatch.dice import dispatch_dice_throw
from sidequest.telemetry.setup import init_tracer

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


# ---------------------------------------------------------------------------
# Rework round 1 (review 2026-06-07, REJECTED) — the review found two more
# DC authors the first pass missed:
#
#  HIGH-1: opposed_check confrontations resolve EVERY side's d20 against
#    ``_opposed_dc(beat)`` (narration_apply.py — the native 10+2*|base|
#    formula) regardless of ruleset. road_warrior (cwn) ships two live
#    opposed_check defs (Wasteland Bargain, Road Chase); the SWN-family
#    ``offer_difficulty`` advertised target AC on those beats, making the
#    banner WRONG where the pre-97-3 client formula was coincidentally right.
#    The offer must respect ``cdef.resolution_mode``.
#
#  MEDIUM: cwn hacking (net_run) resolves vs security-tier DC + alert
#    modifier (dispatch is_net_run branch) — the offer must advertise that,
#    not AC. wwn had zero round-trip coverage. The
#    ``confrontation.beat_dc_authored`` span (the GM-panel lie-detector for
#    this fix) was asserted by no test.
# ---------------------------------------------------------------------------

_CWN_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "DEXTERITY": "Reflex",
    "CONSTITUTION": "Body",
    "INTELLIGENCE": "Tech",
    "WISDOM": "Instinct",
    "CHARISMA": "Cool",
}
_CWN_ABILITY_NAMES = list(_CWN_ATTRIBUTE_MAP.values())


@pytest.fixture
def otel_capture():
    """Capture spans emitted to the live OTEL tracer provider singleton."""
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _cwn_rules(*, cdef: ConfrontationDef, hacking: HackingConfig | None = None) -> RulesConfig:
    return RulesConfig(
        ruleset="cwn",
        ability_score_names=_CWN_ABILITY_NAMES,
        cwn=CwnConfig(attribute_map=_CWN_ATTRIBUTE_MAP, hacking=hacking),
        confrontations=[cdef],
    )


def _opposed_cdef(*, base: int = 2) -> ConfrontationDef:
    return ConfrontationDef(
        type="chase",
        label="Road Chase",
        category="movement",
        resolution_mode=ResolutionMode.opposed_check,
        player_metric=MetricDef(name="distance", starting=0, threshold=10),
        opponent_metric=MetricDef(name="distance", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "ram",
                    "label": "Ram Them",
                    "kind": "strike",
                    "base": base,
                    "stat_check": "Reflex",
                }
            ),
        ],
    )


class TestOpposedCheckOfferRespectsResolutionMode:
    """HIGH-1: opposed_check resolves per-side vs the native-formula DC
    (``_opposed_dc``); the offer must advertise THAT number, not the
    SWN-family target AC — for every ruleset that hosts an opposed_check
    confrontation (road_warrior=cwn is live production content)."""

    def test_cwn_opposed_check_offer_is_per_side_dc_not_target_ac(self) -> None:
        cdef = _opposed_cdef(base=2)  # _opposed_dc: 10 + |2|*2 = 14
        rules = _cwn_rules(cdef=cdef)
        # Opponent seated with an AC deliberately distinct from the formula
        # value — if the offer says 17, the AC author is still in charge.
        snap, enc = _swn_snapshot_and_encounter(opponent_ac=17)
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="road_warrior",
            core_resolver=snap.find_creature_core,
            rules=rules,
        )
        offered = _offered_difficulty(payload, "ram")
        assert offered == 14, (
            f"opposed_check offer must be the per-side _opposed_dc formula DC (14); "
            f"got {offered!r} — 17 means the SWN-family AC is still authoring "
            "opposed banners (review HIGH-1, road_warrior regression)"
        )

    def test_native_opposed_check_offer_unchanged(self) -> None:
        """Native + opposed_check: formula DC both before and after the fix —
        pins that the resolution_mode branch doesn't disturb native packs."""
        cdef = _opposed_cdef(base=2)
        rules = RulesConfig(confrontations=[cdef])
        enc = _native_encounter()
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="test",
            rules=rules,
        )
        assert _offered_difficulty(payload, "ram") == 14


class TestHackingOfferIsSecurityDc:
    """MEDIUM (review): cwn hacking resolves vs security-tier DC + alert
    escalation (dispatch is_net_run branch). The offered difficulty must be
    that effective DC, not the opponent's armor class."""

    def test_cwn_hacking_offer_is_tier_dc_plus_alert(self) -> None:
        cdef = ConfrontationDef(
            type="net_run",
            label="Net Run",
            category="hacking",
            player_metric=MetricDef(name="data", starting=0, threshold=10),
            opponent_metric=MetricDef(name="alert", starting=0, threshold=10),
            beats=[
                BeatDef.model_validate(
                    {
                        "id": "run_program",
                        "label": "Run Program",
                        "kind": "strike",
                        "base": 2,
                        "stat_check": "Tech",
                    }
                ),
            ],
        )
        rules = _cwn_rules(
            cdef=cdef,
            hacking=HackingConfig(
                default_tier="office",
                security_tiers={"home": 7, "office": 9, "facility": 11},
            ),
        )
        snap, enc = _swn_snapshot_and_encounter(opponent_ac=17)
        enc.encounter_type = "net_run"
        enc.security_tier = "office"  # tier DC 9
        enc.opponent_metric.current = 2  # alert escalation +2 → effective 11
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="neon_dystopia",
            core_resolver=snap.find_creature_core,
            rules=rules,
        )
        offered = _offered_difficulty(payload, "run_program")
        assert offered == 11, (
            f"hacking offer must be security-tier DC + alert (9 + 2 = 11); got "
            f"{offered!r} — 17/10 means the AC author is still in charge of net runs"
        )


class TestWwnRoundTrip:
    """MEDIUM (review): wwn (elemental_harmony) had zero offer==resolution
    coverage despite inheriting the SWN AC path."""

    def test_wwn_offer_equals_dice_result_difficulty(self) -> None:
        wwn_amap = {
            "STRENGTH": "Strength",
            "DEXTERITY": "Dexterity",
            "CONSTITUTION": "Constitution",
            "INTELLIGENCE": "Intelligence",
            "WISDOM": "Wisdom",
            "CHARISMA": "Charisma",
        }
        flavor = list(wwn_amap.values())
        cdef = ConfrontationDef(
            type="combat",
            label="Elemental Clash",
            category="combat",
            resolution_mode=ResolutionMode.beat_selection,
            player_metric=MetricDef(name="momentum", starting=0, threshold=7),
            opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
            beats=[
                BeatDef.model_validate(
                    {
                        "id": "strike",
                        "label": "Strike",
                        "kind": "strike",
                        "base": 2,
                        "stat_check": "Dexterity",
                        "damage_channel": "strike",
                        "damage_override": DamageSpec(dice="1d6"),
                    }
                ),
            ],
        )
        rules = RulesConfig(
            ruleset="wwn",
            ability_score_names=flavor,
            wwn=WwnConfig(attribute_map=wwn_amap),
            confrontations=[cdef],
        )
        pack = MagicMock()
        pack.rules = rules
        pack.inventory = None
        snap, enc = _swn_snapshot_and_encounter(opponent_ac=17)
        # Re-stat the attacker for the wwn flavor names.
        snap.characters[0].stats = {name: 12 for name in flavor}
        payload = build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="elemental_harmony",
            core_resolver=snap.find_creature_core,
            rules=rules,
        )
        offered = _offered_difficulty(payload, "strike")

        outcome = dispatch_dice_throw(
            payload=_throw("strike", face=10),
            rolling_player_id="p-vane",
            character_name="Vane",
            character_stats={name: 12 for name in flavor},
            encounter=enc,
            pack=pack,  # type: ignore[arg-type]
            genre_slug="elemental_harmony",
            session_id="s-wwn",
            round_number=1,
            room_broadcast=None,
            snapshot=snap,
        )
        assert offered == outcome.result.difficulty, (
            f"wwn banner DC {offered!r} != resolved DC {outcome.result.difficulty}"
        )


class TestBeatDcAuthoredSpan:
    """MEDIUM (review): the ``confrontation.beat_dc_authored`` span is the
    GM-panel lie-detector for the single-DC-author fix — pin that it fires
    with the ruleset and the offered numbers (CLAUDE.md OTEL discipline)."""

    def test_span_fires_on_swn_offer_with_difficulties(self, otel_capture) -> None:
        pack = _swn_pack()
        snap, enc = _swn_snapshot_and_encounter()
        cdef = pack.rules.confrontations[0]
        build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="space_opera",
            core_resolver=snap.find_creature_core,
            rules=pack.rules,
        )
        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "confrontation.beat_dc_authored"
        ]
        assert spans, "confrontation.beat_dc_authored must fire when the offer is authored"
        attrs = spans[-1].attributes or {}
        assert attrs.get("ruleset") == "swn"
        assert f"shoot={_SWN_OPPONENT_AC}" in str(attrs.get("beat_difficulties", "")), (
            f"span must carry the offered numbers; got {attrs.get('beat_difficulties')!r}"
        )

    def test_span_absent_when_rules_not_supplied(self, otel_capture) -> None:
        """rules=None (legacy/bootstrap) authors nothing — and must not
        pretend otherwise to the GM panel."""
        pack = _native_pack()
        enc = _native_encounter()
        cdef = pack.rules.confrontations[0]
        build_confrontation_payload(
            encounter=enc,
            cdef=cdef,
            genre_slug="test",
        )
        spans = [
            s
            for s in otel_capture.get_finished_spans()
            if s.name == "confrontation.beat_dc_authored"
        ]
        assert not spans, "no DC authoring happened — the lie-detector must stay silent"
