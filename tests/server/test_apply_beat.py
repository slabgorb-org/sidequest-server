import random as _random

from sidequest.game.beat_filter import beats_available_for
from sidequest.game.beat_kinds import (
    apply_beat,
)
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.morale import OpponentState
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import BeatDef, ConfrontationDef
from sidequest.protocol.dice import RollOutcome
from sidequest.server.narration_apply import _apply_flee_consequences, _emit_morale_triggers


def _enc(*, p_thresh: int = 10, o_thresh: int = 10, p_cur: int = 0, o_cur: int = 0):
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(
            name="momentum", current=p_cur, starting=0, threshold=p_thresh
        ),
        opponent_metric=EncounterMetric(
            name="momentum", current=o_cur, starting=0, threshold=o_thresh
        ),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
            EncounterActor(name="Host", role="bystander", side="neutral"),
        ],
    )


def _strike_beat(beat_id: str = "attack", base: int = 2) -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": beat_id,
            "label": beat_id,
            "kind": "strike",
            "base": base,
            "stat_check": "STR",
        }
    )


def _angle_beat(beat_id: str = "feint", target_tag: str = "Off-Balance") -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": beat_id,
            "label": beat_id,
            "kind": "angle",
            "target_tag": target_tag,
            "stat_check": "DEX",
        }
    )


def _push_beat(beat_id: str = "flee") -> BeatDef:
    return BeatDef.model_validate(
        {
            "id": beat_id,
            "label": beat_id,
            "kind": "push",
            "base": 1,
            "stat_check": "DEX",
        }
    )


def test_strike_player_advances_player_metric_only():
    enc = _enc()
    sam = enc.find_actor("Sam")
    result = apply_beat(enc, sam, _strike_beat(base=2), RollOutcome.Success)
    assert enc.player_metric.current == 2
    assert enc.opponent_metric.current == 0
    assert result.resolved is False


def test_strike_opponent_advances_opponent_metric_only():
    enc = _enc()
    promo = enc.find_actor("Promo")
    result = apply_beat(enc, promo, _strike_beat(base=3), RollOutcome.Success)
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 3
    assert result.resolved is False


def test_neutral_actor_skipped_no_dial_change():
    enc = _enc()
    host = enc.find_actor("Host")
    result = apply_beat(enc, host, _strike_beat(), RollOutcome.Success)
    assert enc.player_metric.current == 0
    assert enc.opponent_metric.current == 0
    assert result.skipped_reason == "neutral_actor"


def test_withdrawn_actor_skipped():
    enc = _enc()
    sam = enc.find_actor("Sam")
    sam.withdrawn = True
    result = apply_beat(enc, sam, _strike_beat(), RollOutcome.Success)
    assert enc.player_metric.current == 0
    assert result.skipped_reason == "withdrawn_actor"


def test_threshold_cross_player_first_yields_player_victory():
    enc = _enc(p_cur=8)
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _strike_beat(base=3), RollOutcome.Success)
    assert enc.player_metric.current == 11
    assert enc.resolved is True
    assert enc.outcome == "player_victory"


def test_threshold_cross_opponent_yields_opponent_victory():
    enc = _enc(o_cur=8)
    promo = enc.find_actor("Promo")
    apply_beat(enc, promo, _strike_beat(base=3), RollOutcome.Success)
    assert enc.opponent_metric.current == 11
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"


def test_push_success_resolves_with_resolution_beat_outcome():
    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _push_beat("flee"), RollOutcome.Success)
    assert enc.resolved is True
    assert enc.outcome == "resolution_beat:flee"


def test_angle_success_creates_persistent_tag_with_leverage_one():
    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _angle_beat("feint", "Off-Balance"), RollOutcome.Success)
    assert len(enc.tags) == 1
    tag = enc.tags[0]
    assert tag.text == "Off-Balance"
    assert tag.leverage == 1
    assert tag.fleeting is False
    assert tag.created_by == "Sam"


def test_angle_critsuccess_creates_tag_with_leverage_two():
    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _angle_beat("feint", "Off-Balance"), RollOutcome.CritSuccess)
    assert enc.tags[0].leverage == 2


def test_angle_critfail_backfires_tag_onto_opposite_side():
    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _angle_beat("feint", "Off-Balance"), RollOutcome.CritFail)
    assert len(enc.tags) == 1
    tag = enc.tags[0]
    assert tag.fleeting is True
    assert tag.target == "Promo"


def test_strike_critsuccess_creates_fleeting_opening_tag():
    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _strike_beat(base=2), RollOutcome.CritSuccess)
    assert any(t.text == "Opening" and t.fleeting for t in enc.tags)


def test_strike_fail_publishes_beat_no_op_for_gm_panel(monkeypatch):
    """Regression: per spec, default delta tables for Fail tier on every
    kind are {own=0, opponent=0} — a Fail rolls narratively but neither
    dial moves. Without an OTEL surface for this case, the GM panel
    sees the beat fire and assumes the engine is responsive; nothing
    surfaces the silent stalemate. Playtest 2026-04-25 [P0] flagged
    the dual-track engine as "decorative" specifically because of this
    invisibility — the engine works as specified, but Fails feel inert.
    The `state_transition op=beat_no_op` event makes the design choice
    observable so Sebastien can see the encounter didn't progress.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        captured.append((event_type, fields, {"component": component, "severity": severity}))

    import sidequest.game.beat_kinds as _bk

    monkeypatch.setattr(_bk, "_watcher_publish", fake_publish)

    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _strike_beat(base=2), RollOutcome.Fail)

    no_ops = [
        (fields, meta)
        for et, fields, meta in captured
        if et == "state_transition" and fields.get("op") == "beat_no_op"
    ]
    assert no_ops, "Fail-tier strike must publish beat_no_op for GM panel visibility"
    fields, meta = no_ops[0]
    assert fields["actor_side"] == "player"
    assert fields["beat_kind"] == "strike"
    assert "spec" in fields["rationale"].lower()
    assert meta["component"] == "encounter"


def test_strike_success_does_not_publish_beat_no_op(monkeypatch):
    """Counterpart: a Success-tier strike DOES move the dial, so the
    no-op event must NOT fire (otherwise the GM panel would mis-flag a
    working engine as inert)."""
    captured: list[str] = []

    def fake_publish(event_type, fields, *, component="", severity="info"):
        if event_type == "state_transition" and fields.get("op") == "beat_no_op":
            captured.append(fields["beat_id"])

    import sidequest.game.beat_kinds as _bk

    monkeypatch.setattr(_bk, "_watcher_publish", fake_publish)

    enc = _enc()
    sam = enc.find_actor("Sam")
    apply_beat(enc, sam, _strike_beat(base=2), RollOutcome.Success)
    assert captured == []


def test_post_resolution_apply_is_dropped_with_skipped_reason():
    enc = _enc()
    enc.resolved = True
    enc.outcome = "player_victory"
    sam = enc.find_actor("Sam")
    result = apply_beat(enc, sam, _strike_beat(), RollOutcome.Success)
    assert result.skipped_reason == "encounter_resolved"
    assert enc.player_metric.current == 0


def test_per_tier_override_applies_critfail_own_minus_two():
    enc = _enc()
    sam = enc.find_actor("Sam")
    bash = BeatDef.model_validate(
        {
            "id": "shield_bash",
            "label": "Shield Bash",
            "kind": "strike",
            "base": 4,
            "stat_check": "STR",
            "deltas": {"crit_fail": {"own": -2}},
        }
    )
    apply_beat(enc, sam, bash, RollOutcome.CritFail)
    # CritFail on strike default is 0 own; override drops to -2; ascending dial
    # is clamped at 0 (from spec — never go negative on a side's own dial)
    assert enc.player_metric.current == 0


# ──────────────────────────────────────────────────────────────────────────
# Content-tuning regression: space_opera/negotiation deltas must give
# opponent leverage on Fail/CritFail (playtest 2026-05-03 [BUG] — Edge meters
# never advance from failed rolls).
#
# Two test layers:
#  - Engine layer: per-tier ``opponent: +N`` override on a strike beat
#    advances the opponent dial when the player rolls Fail/CritFail.
#    No filesystem dep — pins the apply_beat seam.
#  - Content layer: load the real space_opera pack from sidequest-content/
#    and verify the negotiation beats carry the override. Bypasses the
#    test conftest's fixture symlink (which redirects ``space_opera`` to
#    ``test_genre``, a stripped mutant_wasteland clone with no negotiation
#    confrontation). Skips when the content dir is absent so the suite
#    stays portable on a sidequest-server-only checkout.
# ──────────────────────────────────────────────────────────────────────────


def test_strike_fail_with_opponent_override_advances_opponent_metric():
    """Engine seam — verify per-tier ``opponent: +N`` override on strike Fail.

    DEFAULT_DELTAS gives strike ``Fail: 0/0``; a beat that sets
    ``deltas.fail.opponent = 1`` must override that and credit the opposing
    side's metric when the actor rolls Fail.
    """
    enc = _enc()
    sam = enc.find_actor("Sam")
    persuade = BeatDef.model_validate(
        {
            "id": "persuade",
            "label": "Make Your Case",
            "kind": "strike",
            "base": 2,
            "stat_check": "Influence",
            "deltas": {"fail": {"opponent": 1}, "crit_fail": {"opponent": 2}},
        }
    )
    apply_beat(enc, sam, persuade, RollOutcome.Fail)
    assert enc.opponent_metric.current == 1
    assert enc.player_metric.current == 0  # player's own dial unchanged on Fail


def test_strike_critfail_opponent_override_outpaces_fail():
    """CritFail must hit harder than Fail when both have opponent overrides."""
    persuade = BeatDef.model_validate(
        {
            "id": "persuade",
            "label": "Make Your Case",
            "kind": "strike",
            "base": 2,
            "stat_check": "Influence",
            "deltas": {"fail": {"opponent": 1}, "crit_fail": {"opponent": 2}},
        }
    )
    enc_fail = _enc()
    apply_beat(enc_fail, enc_fail.find_actor("Sam"), persuade, RollOutcome.Fail)
    enc_crit = _enc()
    apply_beat(enc_crit, enc_crit.find_actor("Sam"), persuade, RollOutcome.CritFail)
    assert enc_crit.opponent_metric.current > enc_fail.opponent_metric.current


def test_angle_fail_with_opponent_override_advances_opponent_metric():
    """Same seam test for angle (concede_point is a negotiation angle beat)."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    concede = BeatDef.model_validate(
        {
            "id": "concede_point",
            "label": "Concede a Point",
            "kind": "angle",
            "target_tag": "Real Goal Revealed",
            "stat_check": "Influence",
            "deltas": {"fail": {"opponent": 1}, "crit_fail": {"opponent": 2}},
        }
    )
    apply_beat(enc, sam, concede, RollOutcome.Fail)
    assert enc.opponent_metric.current == 1


def test_push_fail_with_opponent_override_advances_opponent_metric():
    """Same seam test for push (walk_away is a negotiation push beat)."""
    enc = _enc()
    sam = enc.find_actor("Sam")
    walk_away = BeatDef.model_validate(
        {
            "id": "walk_away",
            "label": "Walk Away",
            "kind": "push",
            "stat_check": "Resolve",
            "deltas": {"fail": {"opponent": 1}, "crit_fail": {"opponent": 2}},
        }
    )
    apply_beat(enc, sam, walk_away, RollOutcome.Fail)
    assert enc.opponent_metric.current == 1


def test_space_opera_negotiation_beats_carry_opponent_overrides():
    """Content layer — the real space_opera pack must declare the overrides.

    Loads via ``load_genre_pack`` (path-explicit, cache-free) to bypass the
    test conftest's fixture symlink that redirects ``space_opera`` → frozen
    ``test_genre`` (a stripped mutant_wasteland clone with no negotiation
    confrontation). Skips when the content dir is absent.
    """
    import pytest

    from sidequest.genre.loader import load_genre_pack
    from tests._helpers.genre_paths import PackNotFound, find_pack_path

    try:
        content = find_pack_path("space_opera")
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")

    pack = load_genre_pack(content)
    negotiation = next(
        c for c in pack.rules.confrontations if c.confrontation_type == "negotiation"
    )
    expected_ids = {"persuade", "threaten", "concede_point", "walk_away"}
    assert {b.id for b in negotiation.beats} == expected_ids, (
        "space_opera negotiation beat roster drift — playtest fix expects "
        "all four beats to carry per-tier opponent overrides"
    )
    for beat in negotiation.beats:
        assert beat.deltas is not None, (
            f"negotiation beat {beat.id!r} dropped its deltas override — "
            f"DEFAULT_DELTAS Fail tier is 0/0 so the dial would freeze"
        )
        assert "fail" in beat.deltas and "opponent" in beat.deltas["fail"], (
            f"negotiation beat {beat.id!r} missing fail.opponent override"
        )
        assert beat.deltas["fail"]["opponent"] >= 1
        assert "crit_fail" in beat.deltas
        assert beat.deltas["crit_fail"]["opponent"] >= beat.deltas["fail"]["opponent"]


# ──────────────────────────────────────────────────────────────────────────
# beats_available_for — class-based beat filtering (Task 7, C&C B/X port)
#
# Two tests: a Fighter must not see cast_spell; a Mage at full slots must.
# Tests call beats_available_for directly (same style as apply_beat tests
# above — inline factories, no fixtures).
# ──────────────────────────────────────────────────────────────────────────


def _combat_confrontation_with_class_beats() -> ConfrontationDef:
    """Build a minimal ConfrontationDef with two beats:
    - 'attack': universal (class_filter=None)
    - 'cast_spell': Mage-only (class_filter=['Mage'])
    """
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "player_metric": {"name": "momentum", "threshold": 10},
            "opponent_metric": {"name": "momentum", "threshold": 10},
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                },
                {
                    "id": "cast_spell",
                    "label": "Cast Spell",
                    "kind": "strike",
                    "base": 3,
                    "stat_check": "INT",
                    "class_filter": ["Mage"],
                },
            ],
        }
    )


def _fighter_class_def() -> ClassDef:
    return ClassDef.model_validate(
        {
            "id": "fighter",
            "display_name": "Fighter",
            "rpg_role": "tank",
            "jungian_default": "hero",
            "prime_requisite": "STR",
            "minimum_score": 9,
            "kit_table": "fighter_kit",
            "encounter_beat_choices": ["attack"],
        }
    )


def _mage_class_def() -> ClassDef:
    return ClassDef.model_validate(
        {
            "id": "mage",
            "display_name": "Mage",
            "rpg_role": "caster",
            "jungian_default": "sage",
            "prime_requisite": "INT",
            "minimum_score": 9,
            "kit_table": "mage_kit",
            "encounter_beat_choices": ["attack", "cast_spell"],
            "magic_access": "arcane",
        }
    )


def test_player_beat_selection_filtered_by_class():
    """Fighter must not see Mage-only cast_spell beat in available actions."""
    cdef = _combat_confrontation_with_class_beats()
    fighter = _fighter_class_def()
    available = beats_available_for(cdef, fighter, spell_slots_remaining=0.0)
    assert "cast_spell" not in [b.id for b in available]
    assert "attack" in [b.id for b in available]


def test_player_beat_selection_includes_class_signature():
    """Mage at full slots sees cast_spell in available actions."""
    cdef = _combat_confrontation_with_class_beats()
    mage = _mage_class_def()
    available = beats_available_for(cdef, mage, spell_slots_remaining=1.0)
    assert "cast_spell" in [b.id for b in available]


# ──────────────────────────────────────────────────────────────────────────
# Morale trigger emission tests (Task 9 — C&C B/X morale integration)
#
# Tests call _emit_morale_triggers directly, matching the existing test style
# of operating at the function level rather than through a session wrapper.
# The function takes explicit pre/post OpponentState lists so the caller
# controls exactly what changed between beats.
# ──────────────────────────────────────────────────────────────────────────


def _morale_confrontation(*, score: int = 8) -> ConfrontationDef:
    """Build a ConfrontationDef with a morale block for morale trigger tests."""
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "player_metric": {"name": "momentum", "threshold": 10},
            "opponent_metric": {"name": "momentum", "threshold": 10},
            "morale": {
                "score": score,
                "triggers": ["first_blood", "half_killed", "leader_killed"],
                "flee_consequence": "rout",
            },
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ],
        }
    )


def _opp(id_: str, *, alive: bool = True, is_leader: bool = False) -> OpponentState:
    return OpponentState(id=id_, alive=alive, is_leader=is_leader)


def _enc_morale(*, n_opponents: int = 2, side: str = "goblins") -> StructuredEncounter:
    """Build a StructuredEncounter with n opponent actors."""
    actors = [EncounterActor(name="Hero", role="combatant", side="player")]
    for i in range(n_opponents):
        actors.append(EncounterActor(name=f"Goblin{i + 1}", role="combatant", side="opponent"))
    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=actors,
    )


def _rng_always_flee() -> _random.Random:
    """RNG that always produces 6+6=12 (flee on any score < 12)."""

    class _AlwaysFlee(_random.Random):
        def randint(self, a, b):  # noqa: ARG002
            return 6

    return _AlwaysFlee()


def _rng_always_stay() -> _random.Random:
    """RNG that always produces 1+1=2 (stay on any score >= 2)."""

    class _AlwaysStay(_random.Random):
        def randint(self, a, b):  # noqa: ARG002
            return 1

    return _AlwaysStay()


def test_first_blood_fires_once_per_side():
    """first_blood emits exactly once when the first opponent goes down.
    Second kill must NOT re-emit first_blood."""
    cdef = _morale_confrontation()
    enc = _enc_morale(n_opponents=2)
    rng = _rng_always_stay()

    pre = [_opp("g1", alive=True), _opp("g2", alive=True)]
    post_kill1 = [_opp("g1", alive=False), _opp("g2", alive=True)]

    # First kill — first_blood should fire.
    _emit_morale_triggers(enc, cdef, "goblins", pre, post_kill1, False, rng)
    assert "first_blood:goblins" in enc.morale_events

    fb_count_after_first = enc.morale_events.count("first_blood:goblins")
    assert fb_count_after_first == 1

    # Second kill — first_blood must NOT fire again.
    post_kill2 = [_opp("g1", alive=False), _opp("g2", alive=False)]
    _emit_morale_triggers(enc, cdef, "goblins", post_kill1, post_kill2, False, rng)
    assert enc.morale_events.count("first_blood:goblins") == 1


def test_half_killed_fires_when_side_crosses_half():
    """half_killed fires when standing opponents drop to ⌊initial/2⌋ = 2."""
    cdef = _morale_confrontation()
    enc = _enc_morale(n_opponents=4)
    rng = _rng_always_stay()

    # Kill 1 of 4 — 3 left, does not cross half.
    pre4 = [_opp(f"g{i}", alive=True) for i in range(4)]
    post3 = [_opp("g0", alive=False)] + [_opp(f"g{i}", alive=True) for i in range(1, 4)]
    _emit_morale_triggers(enc, cdef, "goblins", pre4, post3, False, rng)
    assert "half_killed:goblins" not in enc.morale_events

    # Kill 1 more — 2 left = ⌊4/2⌋, crosses the half threshold.
    post2 = [_opp("g0", alive=False), _opp("g1", alive=False)] + [
        _opp(f"g{i}", alive=True) for i in range(2, 4)
    ]
    _emit_morale_triggers(enc, cdef, "goblins", post3, post2, False, rng)
    assert "half_killed:goblins" in enc.morale_events


def test_leader_killed_fires_only_for_tagged_leader():
    """leader_killed fires only when killed_was_leader=True."""
    cdef = _morale_confrontation()
    enc = _enc_morale(n_opponents=2)
    rng = _rng_always_stay()

    pre = [_opp("grunt", alive=True), _opp("boss", alive=True, is_leader=True)]
    post_grunt = [_opp("grunt", alive=False), _opp("boss", alive=True, is_leader=True)]

    # Kill grunt (not leader) — leader_killed must NOT fire.
    _emit_morale_triggers(enc, cdef, "warband", pre, post_grunt, False, rng)
    assert "leader_killed:warband" not in enc.morale_events

    # Kill boss (leader) — leader_killed MUST fire.
    post_boss = [_opp("grunt", alive=False), _opp("boss", alive=False, is_leader=True)]
    _emit_morale_triggers(enc, cdef, "warband", post_grunt, post_boss, True, rng)
    assert "leader_killed:warband" in enc.morale_events


def test_per_beat_dial_advance_emits_first_blood_through_apply_pipeline():
    """Integration: a single player strike that advances player_metric by 1
    (from 0/threshold=4) fires first_blood through the full apply pipeline.

    This proves the wire-up at the legacy beat loop is live — not just
    the helper. Specifically asserts ``first_blood:combat`` lands on
    ``enc.morale_events`` after one ``_apply_narration_result_to_snapshot``
    call, with no resolution (encounter still active because dial reached
    1 of 4, not threshold).

    Architect feedback 2026-05-08 (commit 602a909 follow-up): morale must
    fire BEFORE the dial saturates so a flee outcome can interrupt the
    fight. Earlier wire-up that gated on ``player_victory`` left the
    morale system structurally non-functional.

    Note on metric choice: the architect's spec referenced
    ``opponent_metric``, but this codebase uses ``player_metric`` for
    "player progress toward winning" (= opponents being defeated).
    The implementation uses ``player_metric``; this test follows.
    """
    from unittest.mock import MagicMock

    from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult, NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import MetricDef, MoraleDef, MoraleTrigger, RulesConfig
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    # Build a confrontation with morale block + a strike beat that
    # advances the dial by exactly 1 on Success.
    cdef = ConfrontationDef(
        type="combat",
        label="Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", starting=0, threshold=4),
        opponent_metric=MetricDef(name="momentum", starting=0, threshold=4),
        morale=MoraleDef(
            score=8,
            triggers=[MoraleTrigger.first_blood, MoraleTrigger.half_killed],
        ),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "STR",
                }
            ),
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    # Story 72-4: the apply pipeline now resolves a culture for invented NPCs
    # via pack.effective_cultures(world); stub it so this bare mock returns the
    # 2-tuple a real GenrePack would (no cultures bound → loud raw-name degrade).
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None

    # Snapshot with an active encounter (player at 0, threshold=4).
    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=4),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=4),
        actors=[
            EncounterActor(name="Sam", role="combatant", side="player"),
            EncounterActor(name="Promo", role="combatant", side="opponent"),
        ],
    )

    result = NarrationTurnResult(
        narration="Sam swings.",
        beat_selections=[
            BeatSelection(actor="Sam", beat_id="attack", outcome=RollOutcome.Success),
        ],
        npcs_present=[NpcMention(name="Promo", side="opponent", role="hostile")],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Sam",
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
    )

    enc = snap.encounter
    # Dial advanced from 0 → 1 (player_metric.current).
    assert enc.player_metric.current == 1
    # first_blood fired and was recorded for deduplication.
    assert "first_blood:combat" in enc.morale_events
    # Encounter still active (not yet at threshold=4).
    assert not enc.resolved


# ──────────────────────────────────────────────────────────────────────────
# Task 10 — sidecar-driven intimidated morale trigger (ADR-039)
# ──────────────────────────────────────────────────────────────────────────


def _morale_confrontation_with_intimidated(*, score: int = 8) -> ConfrontationDef:
    """ConfrontationDef that declares intimidated in its morale triggers."""
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "player_metric": {"name": "momentum", "threshold": 10},
            "opponent_metric": {"name": "momentum", "threshold": 10},
            "morale": {
                "score": score,
                "triggers": ["first_blood", "intimidated"],
                "flee_consequence": "rout",
            },
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "STR",
                }
            ],
        }
    )


def test_intimidated_sidecar_fires_when_morale_block_present():
    """Narrator JSON sidecar `morale_event: intimidated` fires the trigger
    and records an entry in enc.morale_events when the confrontation has
    a morale block."""
    from unittest.mock import MagicMock

    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    cdef = _morale_confrontation_with_intimidated()
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Hero", role="combatant", side="player"),
            EncounterActor(name="Goblin1", role="combatant", side="opponent"),
        ],
    )

    result = NarrationTurnResult(
        narration="Hero glares down the goblins.",
        game_patch_dict={"morale_event": "intimidated"},
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Hero",
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
    )

    enc = snap.encounter
    assert any("intimidated" in e for e in enc.morale_events), (
        f"expected 'intimidated' in morale_events, got {enc.morale_events}"
    )


def test_intimidated_sidecar_ignored_when_no_morale_block():
    """Narrator sidecar `morale_event: intimidated` does not record a morale
    entry when the confrontation has no morale block."""
    from unittest.mock import MagicMock

    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    # Confrontation WITHOUT a morale block.
    cdef_no_morale = ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "player_metric": {"name": "momentum", "threshold": 10},
            "opponent_metric": {"name": "momentum", "threshold": 10},
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "STR",
                }
            ],
        }
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef_no_morale])

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Hero", role="combatant", side="player"),
            EncounterActor(name="Goblin1", role="combatant", side="opponent"),
        ],
    )

    result = NarrationTurnResult(
        narration="Hero glares down the goblins.",
        game_patch_dict={"morale_event": "intimidated"},
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        "Hero",
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
    )

    enc = snap.encounter
    assert enc.morale_events == [], (
        f"expected no morale_events when confrontation has no morale block, got {enc.morale_events}"
    )


def test_unknown_sidecar_morale_event_raises():
    """ValueError on narrator sidecar drift — unknown morale_event values."""
    from unittest.mock import MagicMock

    import pytest

    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    cdef = _morale_confrontation_with_intimidated()
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])

    snap = GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        turn_manager=TurnManager(),
    )
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Hero", role="combatant", side="player"),
            EncounterActor(name="Goblin1", role="combatant", side="opponent"),
        ],
    )

    result = NarrationTurnResult(
        narration="The goblins panic and scatter.",
        game_patch_dict={"morale_event": "panic"},
    )

    with pytest.raises(ValueError, match="morale_event.*panic"):
        _apply_narration_result_to_snapshot(
            snap,
            result,
            "Hero",
            pack=pack,
            from_explicit_action=True,
            room=room_for(snap),
        )


# ──────────────────────────────────────────────────────────────────────────
# Task 11 — flee consequence dispatch (chase / surrender / rout)
#
# Tests call _emit_morale_triggers with _rng_always_flee() to force a
# flee outcome, then assert on the resulting encounter state.
# Uses the same inline-factory style as Tasks 9/10 above.
# ──────────────────────────────────────────────────────────────────────────


def _morale_cdef_flee(*, flee_consequence: str = "rout") -> ConfrontationDef:
    """Minimal ConfrontationDef with morale score=2 so any 2d6 roll > 2 → flee.

    With _rng_always_flee() (each die = 6, sum = 12 > 2) the roll always fails
    and outcome is MoraleOutcome.flee.
    """
    return ConfrontationDef.model_validate(
        {
            "type": "combat",
            "label": "Combat",
            "category": "combat",
            "player_metric": {"name": "momentum", "threshold": 10},
            "opponent_metric": {"name": "momentum", "threshold": 10},
            "morale": {
                "score": 2,
                "triggers": ["first_blood"],
                "flee_consequence": flee_consequence,
            },
            "beats": [
                {
                    "id": "attack",
                    "label": "Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ],
        }
    )


def _run_flee_consequence(*, flee_consequence: str) -> StructuredEncounter:
    """Helper: fire first_blood with rng_always_flee, apply flee consequence.

    Returns the encounter so the caller can assert on its state.
    """
    cdef = _morale_cdef_flee(flee_consequence=flee_consequence)
    enc = _enc_morale(n_opponents=2)
    rng = _rng_always_flee()

    pre = [_opp("g1", alive=True), _opp("g2", alive=True)]
    post = [_opp("g1", alive=False), _opp("g2", alive=True)]

    fired = _emit_morale_triggers(enc, cdef, "combat", pre, post, False, rng)
    _apply_flee_consequences(enc, cdef, fired)
    return enc


def test_flee_consequence_chase_sets_pending_flag():
    """flee_consequence=chase: enc.flee_consequence_pending set to 'chase'.

    Chase does NOT resolve the encounter (V1 — full chase-launch is a
    follow-up story). The orchestrator picks up flee_consequence_pending
    to launch a chase confrontation.
    """
    enc = _run_flee_consequence(flee_consequence="chase")
    assert enc.flee_consequence_pending == "chase"
    # Chase does NOT end the combat encounter in V1.
    assert enc.resolved is False


def test_flee_consequence_surrender_ends_combat():
    """flee_consequence=surrender: encounter resolved, opponents surrendered."""
    enc = _run_flee_consequence(flee_consequence="surrender")
    assert enc.flee_consequence_pending == "surrender"
    assert enc.opponents_disposition == "surrendered"
    assert enc.resolved is True
    assert enc.outcome == "surrender"


def test_flee_consequence_rout_ends_combat():
    """flee_consequence=rout: encounter resolved, opponents routed."""
    enc = _run_flee_consequence(flee_consequence="rout")
    assert enc.flee_consequence_pending == "rout"
    assert enc.opponents_disposition == "routed"
    assert enc.resolved is True
    assert enc.outcome == "rout"
