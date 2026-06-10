"""Story 102-1 (RED) — PC-death runs the WN downed seam on the reprisal path.

THE MEASURED AC5b GAP (FIXER investigation oq-2, 2026-06-10; 90-3 free-play
playtest heavy_metal/long_foundry, PC Vesska): ``run_cwn_wwn_downed_seam`` fires
``{ruleset}.mortal_injury``/``.shock`` only when the PLAYER drops an OPPONENT
(strike path, dice.py:760). When the player is dropped by the opponent's
reprisal (``_resolve_opponent_reprisal`` → ``check_hp_depletion``),
``post_resolution_lethality`` applies the generic genre verdict (verdict=dead)
with ZERO ``wwn.*`` spans — a dying PC emits ``hp_depletion.resolved`` +
``post_resolution_lethality.applied`` and nothing module-scoped, so the GM
panel cannot show WN lethality engaged on the combat half of AC5b.

THE CONTRACT THESE TESTS PIN (behavior, not interface — Dev may wire the seam
from ``post_resolution_lethality`` or from the reprisal close; the assertion is
dispatch-level either way):

  1. A PC dropped to 0 HP by the reprisal in a WN-bound pack (Cwn/WwnConfig
     capability, NOT a slug string) with a LETHAL genre verdict gets the WN
     Mortal Injury resolution: ``{ruleset}.mortal_injury.declared`` with
     actor=PC, plus the Mortal Injury death-clock status — IN ADDITION to the
     generic Downed status (additive, never a replacement).
  2. A Traumatic Hit scene + failed Physical save additionally rolls the Major
     Injury table (``{ruleset}.major_injury.roll``, actor=PC) — the same rules
     the strike path already applies to a dropped opponent.
  3. The opponent's reprisal MISS applies WN Shock chip damage to the PC when
     its authored ``opponent_damage`` carries a Shock rating (the
     ``{ruleset}.shock`` half of the story title — Shock is THE signature WN
     melee rule: a miss still chips).
  4. Capability gate: an SWN-bound pack (SwnConfig has no ``trauma``) emits NO
     mortal-injury span on the same path, does not crash, and the generic genre
     verdict still applies.
  5. Genre Truth: a NON-LETHAL verdict (e.g. EH's ``defeated``) recovers the PC
     to the 1-HP floor and must NOT declare a WN death clock — a Mortal Injury
     ("dies in N rounds") on a PC the genre policy just recovered would
     contradict both the verdict and the recovered HP.
  6. AWN rides the seam free (AwnConfig subclasses CwnConfig) — same as the
     strike path proven in test_awn_combat_dispatch.py.

Fixture strategy mirrors tests/server/test_awn_combat_dispatch.py (synthetic
MagicMock pack with a REAL RulesConfig + REAL LethalityPolicy; no content
dependency) and tests/integration/test_opponent_reprisal_e2e.py (forced-hit /
forced-miss reprisal via the rng seam).

DETERMINISM: the reprisal's to-hit d20 rolls via ``random.randint`` (the
``random`` MODULE — shared by dice.py, downed_seam.py, and damage_roll.py, so
one monkeypatch governs every roll on the path). ``lambda a, b: b`` forces the
to-hit HIGH (guaranteed hit) and the 1d6 damage to 6 (guaranteed kill of a 1-HP
PC). The traumatic test uses an arg-dispatching fake (first d20 high = hit,
later d20s low = failed save). The shock test forces everything LOW (guaranteed
miss). The PLAYER's own check uses the thrown ``face=[...]``, never rng.

``otel_capture`` is provided by tests/server/conftest.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.telemetry.spans.encounter import SPAN_POST_RESOLUTION_LETHALITY
from sidequest.telemetry.spans.wwn import (
    SPAN_WWN_MAJOR_INJURY_ROLL,
    SPAN_WWN_MORTAL_INJURY_DECLARED,
    SPAN_WWN_SHOCK_APPLIED,
)

PLAYER = "Vesska"
OPPONENT = "Foundry Reaver"

# WN attribute_map: the six canonical attributes -> this fixture pack's flavor
# stats (validated complete by RulesConfig._validate_{swn,wwn,awn}).
_ATTRIBUTE_MAP = {
    "STRENGTH": "STR",
    "CONSTITUTION": "CON",
    "DEXTERITY": "DEX",
    "INTELLIGENCE": "INT",
    "WISDOM": "WIS",
    "CHARISMA": "CHA",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 12 for name in _ABILITY_SCORE_NAMES}

_OPPONENT_AC = 12
# Opponent HP high enough that the player's single 1d6 strike can NEVER resolve
# the encounter first — the reprisal must be what ends the fight.
_OPPONENT_HP = 30


# ---------------------------------------------------------------------------
# Pack builders — synthetic WN-bound packs with a REAL lethality policy
# ---------------------------------------------------------------------------


def _lethality_policy(pc_verdict: str):
    from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroHp

    return LethalityPolicy(
        genre_key="test_wn_reprisal",
        default_reversibility="permanent",
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc=pc_verdict, npc="dead"),
        soul_md_constraint="genre_truth:test",
        must_narrate="Render the cost.",
        must_not_narrate="soften the damage after the fact",
    )


def _ruleset_config_block(ruleset: str):
    """The {swn,wwn,awn} config block for the requested binding."""
    from sidequest.genre.models.rules import (
        AwnConfig,
        SwnConfig,
        SystemStrainConfig,
        TraumaConfig,
        WwnConfig,
    )

    if ruleset == "wwn":
        return {
            "wwn": WwnConfig(
                attribute_map=dict(_ATTRIBUTE_MAP),
                system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
                trauma=TraumaConfig(default_trauma_target=6),
            )
        }
    if ruleset == "awn":
        return {
            "awn": AwnConfig(
                attribute_map=dict(_ATTRIBUTE_MAP),
                system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
                trauma=TraumaConfig(default_trauma_target=6),
            )
        }
    if ruleset == "swn":
        return {"swn": SwnConfig(attribute_map=dict(_ATTRIBUTE_MAP))}
    raise ValueError(f"unknown fixture ruleset {ruleset!r}")


def _make_reprisal_pack(ruleset: str, *, pc_verdict: str, opponent_damage=None):
    """MagicMock pack with a REAL hp_depletion RulesConfig and a REAL
    LethalityPolicy. ``opponent_damage`` (a DamageSpec) is the opponent's
    authored reprisal weapon; when None the reprisal falls back to the strike
    beat's damage_override (1d6), mirroring test_awn_combat_dispatch.py."""
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.genre.models.rules import (
        BeatDef,
        ConfrontationDef,
        MetricDef,
        ResolutionMode,
        RulesConfig,
        WinCondition,
    )

    strike_beat = BeatDef.model_validate(
        {
            "id": "strike",
            "label": "Strike",
            "kind": "strike",
            "base": 2,
            "stat_check": "STR",
            "damage_channel": "strike",
            "effect": "Target takes damage this round",
            "narrator_hint": "Steel answers steel.",
            "damage_override": DamageSpec(dice="1d6"),
        }
    )

    cdef = ConfrontationDef(
        type="combat",
        label="WN Reprisal Combat",
        category="combat",
        resolution_mode=ResolutionMode.beat_selection,
        win_condition=WinCondition.hp_depletion,
        player_metric=MetricDef(name="momentum", starting=0, threshold=7),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=7),
        beats=[strike_beat],
        opponent_damage=opponent_damage,
        opponent_default_stats={
            **{name: 12 for name in _ABILITY_SCORE_NAMES},
            # Reserved hp_depletion combat keys (required at load).
            "hp": _OPPONENT_HP,
            "armor_class": _OPPONENT_AC,
            "dexterity": 10,
        },
    )

    pack = MagicMock()
    pack.rules = RulesConfig(
        ruleset=ruleset,
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        confrontations=[cdef],
        **_ruleset_config_block(ruleset),
    )
    pack.inventory = None
    pack.lethality_policy = _lethality_policy(pc_verdict)
    return pack


# ---------------------------------------------------------------------------
# Snapshot / encounter / drive helpers
# ---------------------------------------------------------------------------


def _make_snapshot_and_encounter(*, player_hp: int, player_ac: int = 10):
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

    player_core = CreatureCore(
        name=PLAYER,
        description="Foundry vagrant blade.",
        personality="grim",
        inventory=Inventory(),
        hp={"current": player_hp, "max": 12, "base_max": 12},
        armor_class=player_ac,
    )
    player = Character(
        core=player_core,
        char_class="Warrior",
        race="Human",
        backstory="Forged in the long dark.",
        stats=dict(_STATS),
    )
    opponent_core = CreatureCore(
        name=OPPONENT,
        description="Slag-scarred enforcer.",
        personality="merciless",
        inventory=Inventory(),
        hp={"current": _OPPONENT_HP, "max": _OPPONENT_HP, "base_max": _OPPONENT_HP},
        armor_class=_OPPONENT_AC,
    )

    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    snap.npcs.append(Npc(core=opponent_core))

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=PLAYER, role="combatant", side="player"),
            EncounterActor(name=OPPONENT, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )
    return snap, enc


def _drive_player_strike(*, snap, enc, pack, request_id: str):
    """One player ``strike`` turn through the production dispatcher. face=[20]
    clears the DC (base=2 → DC 14) so the beat applies and — because the
    opponent survives at 30 HP — the reprisal runs."""
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id=request_id,
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id="strike",
        ),
        rolling_player_id="player-vesska",
        character_name=PLAYER,
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="heavy_metal",
        session_id=request_id,
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )


def _spans_named(otel_capture, name: str):
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _mortal_injury_spans(otel_capture):
    """All ``{ruleset}.mortal_injury.declared`` spans regardless of slug."""
    return [
        s for s in otel_capture.get_finished_spans() if s.name.endswith(".mortal_injury.declared")
    ]


class _FirstD20HighThenLow:
    """Arg-dispatching randint fake for the traumatic-save test.

    First (1, 20) call → 20 (the reprisal to-hit: guaranteed HIT). Every later
    (1, 20) call → 1 (the Physical save: guaranteed FAIL below save_base 15).
    Any other range → its max (1d6 damage = 6 → the 1-HP PC drops; the 1d12
    Major Injury roll resolves a real table entry).
    """

    def __init__(self) -> None:
        self.d20_calls = 0

    def __call__(self, a: int, b: int) -> int:
        if (a, b) == (1, 20):
            self.d20_calls += 1
            return 20 if self.d20_calls == 1 else 1
        return b


# ---------------------------------------------------------------------------
# AC1 — the dying PC emits the WN mortal-injury span (the AC5b combat half)
# ---------------------------------------------------------------------------


def test_wwn_reprisal_kill_declares_mortal_injury_for_pc(otel_capture, monkeypatch):
    """A 1-HP PC dropped by the opponent's reprisal in a wwn-bound pack with a
    LETHAL genre verdict must get the WN Mortal Injury resolution: exactly ONE
    ``wwn.mortal_injury.declared`` span, actor = the dying PC. RED today — the
    reprisal/lethality path applies the generic verdict with zero wwn.* spans
    (post_resolution_lethality.py documents the asymmetry in its own docstring).

    Exactly-one also guards double-fire: the seam must not run from BOTH the
    reprisal close and post_resolution_lethality on the same kill turn."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_reprisal_pack("wwn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-kill")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the forced-hit reprisal must drop the 1-HP PC; hp={player_core.hp.current}"
    )
    assert enc.resolved and enc.outcome == "opponent_victory", (
        f"precondition: the kill must resolve the encounter against the player; "
        f"resolved={enc.resolved} outcome={enc.outcome}"
    )

    spans = _spans_named(otel_capture, SPAN_WWN_MORTAL_INJURY_DECLARED)
    assert len(spans) == 1, (
        f"a PC dying to the reprisal in a wwn-bound pack must declare exactly ONE "
        f"WN Mortal Injury (the AC5b combat-half lie-detector); got {len(spans)} "
        f"{SPAN_WWN_MORTAL_INJURY_DECLARED} spans — all spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("actor") == PLAYER, (
        f"the mortal-injury span's actor must be the dying PC, not the opponent; "
        f"got {attrs.get('actor')!r}"
    )


def test_wwn_reprisal_kill_attaches_death_clock_alongside_generic_downed(otel_capture, monkeypatch):
    """The WN seam is ADDITIVE: the dying PC carries BOTH the WN Mortal Injury
    death-clock status (from resolve_downed) AND the generic Downed status, and
    the generic post_resolution_lethality span still fires (decision=lethal_down).
    The module crunch backs the genre verdict; it never replaces it."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_reprisal_pack("wwn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-statuses")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    status_texts = [s.text for s in player_core.statuses]
    assert any("Mortal Injury" in t for t in status_texts), (
        f"the dying PC must carry the WN Mortal Injury death-clock status; statuses={status_texts}"
    )
    assert any(t.startswith("Downed") for t in status_texts), (
        f"the generic Downed status must STILL apply (the WN seam is additive); "
        f"statuses={status_texts}"
    )

    generic = _spans_named(otel_capture, SPAN_POST_RESOLUTION_LETHALITY)
    assert len(generic) == 1, (
        f"the generic lethality decision span must still fire exactly once; got {len(generic)}"
    )
    gattrs = dict(generic[0].attributes or {})
    assert gattrs.get("decision") == "lethal_down"
    assert gattrs.get("actor") == PLAYER


# ---------------------------------------------------------------------------
# AC2 — Traumatic Hit scene + failed Physical save rolls the Major Injury table
# ---------------------------------------------------------------------------


def test_wwn_reprisal_traumatic_scene_failed_save_rolls_major_injury(otel_capture, monkeypatch):
    """When a Traumatic Hit landed this scene and the dying PC FAILS the
    Physical save, the Major Injury table must roll for the PC —
    ``wwn.major_injury.roll`` with actor=PC and save_made=False. This is the
    same rule the strike path already applies to a dropped opponent
    (resolve_downed); the PC's death must run the SAME stack."""
    from sidequest.game.encounter_tag import EncounterTag

    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", _FirstD20HighThenLow())

    pack = _make_reprisal_pack("wwn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)
    enc.tags.append(
        EncounterTag(
            text="Traumatic Hit Landed",
            created_by=OPPONENT,
            target=None,
            leverage=0,
            fleeting=False,
            created_turn=1,
        )
    )

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-trauma")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the forced-hit reprisal must drop the 1-HP PC; hp={player_core.hp.current}"
    )

    major = _spans_named(otel_capture, SPAN_WWN_MAJOR_INJURY_ROLL)
    assert len(major) == 1, (
        f"a traumatic-scene PC death with a failed Physical save must roll the "
        f"Major Injury table exactly once; got {len(major)} — all spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(major[0].attributes or {})
    assert attrs.get("actor") == PLAYER
    assert attrs.get("save_made") is False, (
        f"the forced-low save (1 vs target 15) must FAIL; attrs={attrs}"
    )
    assert any("Major Injury" in s.text for s in player_core.statuses), (
        f"the failed save must attach the Major Injury Scar; "
        f"statuses={[s.text for s in player_core.statuses]}"
    )


# ---------------------------------------------------------------------------
# AC3 — the reprisal MISS applies WN Shock chip damage to the PC
# ---------------------------------------------------------------------------


def test_wwn_reprisal_miss_applies_shock_chip_to_pc(otel_capture, monkeypatch):
    """The ``{ruleset}.shock`` half of the story: WN Shock means a melee miss
    still chips. The opponent's authored ``opponent_damage`` carries
    ``shock=3 / shock_ac=35``; the PC's AC (30) is under the ceiling, so the
    forced-MISS reprisal must chip 3 HP off the PC and emit
    ``wwn.shock.applied`` + ``state_patch.hp``. RED today — a reprisal miss
    returns immediately and the PC takes nothing."""
    from sidequest.genre.models.inventory import DamageSpec
    from sidequest.telemetry.spans.state_patch import SPAN_STATE_PATCH_HP

    # Everything LOW: the reprisal to-hit d20=1 (total ≪ AC 30 → guaranteed
    # MISS); the player's own 1d6 damage = 1 (opponent survives at 29).
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: a)

    pack = _make_reprisal_pack(
        "wwn",
        pc_verdict="dying",
        opponent_damage=DamageSpec(dice="1d6", shock=3, shock_ac=35),
    )
    snap, enc = _make_snapshot_and_encounter(player_hp=12, player_ac=30)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-shock")

    shock_spans = _spans_named(otel_capture, SPAN_WWN_SHOCK_APPLIED)
    assert len(shock_spans) == 1, (
        f"the opponent's missed reprisal with a Shock-rated weapon must apply "
        f"the Shock chip to the PC (wwn.shock.applied); got {len(shock_spans)} — "
        f"all spans: {[s.name for s in otel_capture.get_finished_spans()]}"
    )

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current == 9, (
        f"Shock 3/AC 35 vs the AC-30 PC must chip exactly 3 HP (12 → 9) despite "
        f"the miss; hp={player_core.hp.current}"
    )
    assert _spans_named(otel_capture, SPAN_STATE_PATCH_HP), (
        "the Shock chip mutates HP, so state_patch.hp must fire (ADR-114)"
    )
    # A chip is not a kill: nothing may declare a Mortal Injury here.
    assert not _mortal_injury_spans(otel_capture), (
        "a Shock chip that leaves the PC above 0 HP must NOT declare a Mortal Injury"
    )
    assert enc.resolved is False, "a 3-HP chip must not resolve the encounter"


# ---------------------------------------------------------------------------
# AC4 — capability gate: SWN (no trauma config) stays generic, never crashes
# ---------------------------------------------------------------------------


def test_swn_reprisal_kill_emits_no_wn_lethality_span(otel_capture, monkeypatch):
    """SwnConfig carries no ``trauma`` block — the WN downed seam must gate on
    config CAPABILITY (isinstance Cwn/WwnConfig) and stay silent for swn: no
    ``*.mortal_injury.declared`` span, no crash, and the generic genre verdict
    still applies (post_resolution_lethality, decision=lethal_down)."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_reprisal_pack("swn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-swn")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the reprisal must still drop the 1-HP PC; hp={player_core.hp.current}"
    )
    assert not _mortal_injury_spans(otel_capture), (
        f"an swn-bound pack has no WN trauma surface — no mortal-injury span may "
        f"fire; got {[s.name for s in _mortal_injury_spans(otel_capture)]}"
    )
    generic = _spans_named(otel_capture, SPAN_POST_RESOLUTION_LETHALITY)
    assert (
        len(generic) == 1 and dict(generic[0].attributes or {}).get("decision") == "lethal_down"
    ), "the generic genre verdict must still apply for swn"


# ---------------------------------------------------------------------------
# AC5 — Genre Truth: a non-lethal verdict recovers the PC with NO death clock
# ---------------------------------------------------------------------------


def test_wwn_non_lethal_verdict_recovers_pc_without_death_clock(otel_capture, monkeypatch):
    """A wwn-bound pack whose genre verdict is NON-LETHAL (EH ships
    pc=`defeated`) recovers the PC to the 1-HP floor. Declaring a WN Mortal
    Injury ("dies in N rounds") on a PC the genre policy just recovered would
    contradict the verdict AND the recovered HP — the death clock is for LETHAL
    verdicts only. (The seam's own hp>0 gate makes this natural when the
    verdict applies first; this test pins that ordering.)"""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_reprisal_pack("wwn", pc_verdict="defeated")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-nonlethal")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current == 1, (
        f"the non-lethal verdict must recover the PC to the 1-HP floor; hp={player_core.hp.current}"
    )
    assert not _mortal_injury_spans(otel_capture), (
        "a recovered (non-lethal) PC must NOT get a WN Mortal Injury death clock"
    )
    assert not any("Mortal Injury" in s.text for s in player_core.statuses), (
        f"no Mortal Injury status on a recovering PC; "
        f"statuses={[s.text for s in player_core.statuses]}"
    )


# ---------------------------------------------------------------------------
# AC6 — AWN rides the seam free (AwnConfig subclasses CwnConfig)
# ---------------------------------------------------------------------------


def test_awn_reprisal_kill_declares_mortal_injury_for_pc(otel_capture, monkeypatch):
    """An awn-bound pack must run the same PC-death seam — the capability gate
    is isinstance(cfg, (CwnConfig, WwnConfig)), which AwnConfig satisfies by
    subclassing. Today AWN's resolve_downed is inherited from CWN and emits the
    cwn.* span names (the strike-path precedent in test_awn_combat_dispatch.py
    pins the same); the honest-slug rework is separate scope, so this asserts
    the mortal-injury declaration fired for the PC under EITHER slug."""
    monkeypatch.setattr("sidequest.server.dispatch.dice.random.randint", lambda a, b: b)

    pack = _make_reprisal_pack("awn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=1)

    _drive_player_strike(snap=snap, enc=enc, pack=pack, request_id="wn-reprisal-awn")

    player_core = snap.find_creature_core(PLAYER)
    assert player_core is not None
    assert player_core.hp.current <= 0, (
        f"precondition: the reprisal must drop the 1-HP PC; hp={player_core.hp.current}"
    )
    spans = _mortal_injury_spans(otel_capture)
    assert len(spans) == 1, (
        f"an awn-bound PC death must declare exactly one Mortal Injury "
        f"(inherited cwn.* spans today; honest awn.* slug acceptable); got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert dict(spans[0].attributes or {}).get("actor") == PLAYER
