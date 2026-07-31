"""Story 158-59 RED — the AWN mutation target actually gets to save.

THE GAP (verified at develop tip ``b7130e01``, this story's premise pass):
every production caller of ``sidequest.mutation.use_ops.use_mutation`` hands it
a resolver that cannot ever return "success" —

  * ``server/narration_apply.py::_resolve_mutation_for_beat``
    -> ``save_resolver=lambda stat, target: "fail"``
  * ``agents/subsystems/magic_working.py::_run_awn_freeplay_mutation``
    -> ``save_resolver=lambda stat, target: "fail"``
  * ``agents/tools/use_mutation.py::_save_resolver``  -> ``return "fail"``

``use_ops`` faithfully records what it is told (``save_result="fail"``, stamped
on ``awn.mutation.used``), so the OTEL trail says a save happened and the save
was blown — every single time, for every defender, forever. That was documented
as a v1 shortcut while mutations only reached the narrator route. Story 158-54
put the spine on the **primary combat path** (``dispatch/dice.py`` ->
``_resolve_mutation_for_beat``), which is what this suite drives, so the
player-favourable bias is now live crunch in a real ``mutant_wasteland`` fight.

This suite pins the DICE PATH — the path that owns a live WN round. The two
narrator-route resolvers above share the ``_resolve_mutation_for_beat`` seam
(narration_apply) or need a separate one (magic_working, the tool); see the
158-59 Delivery Findings.

────────────────────────────────────────────────────────────────────────────
Bind the ruleset, don't balance it (SOUL.md / ADR-143)
────────────────────────────────────────────────────────────────────────────
Not one number in this file is picked by the test author. ``mutant_wasteland``
binds ``ruleset: awn``; ``AwnRulesetModule`` inherits ``save_params`` unchanged
from ``WithoutNumberRulesetModule`` (``game/ruleset/without_number.py:380``),
which is the single authority for a WN saving throw:

    sides      = 20, count = 1                     (1d20)
    difficulty = cfg.save_base - (level - 1)       (SRD p.46; AwnConfig.save_base = 15)
    modifier   = best modifier across _SAVE_ATTRS[category]
                 + status_roll_modifier(core)

and ``agents/tools/wn_tools.py::wn_save`` is the codebase's one worked example
of the success test:  ``success = (d20 + modifier) >= difficulty``.

Every expected value below is obtained by CALLING ``module.save_params(...)``
and comparing against it. If the implementation invents a threshold, converts
to another die, gates on the attacker's roll, or flips ``>=`` to ``>``, these
tests fail — which is the point.

────────────────────────────────────────────────────────────────────────────
Who throws (ADR-074)
────────────────────────────────────────────────────────────────────────────
The dice path derives the defender as
``_opposite_side_first_actor(encounter, actor.side)`` inside
``_apply_player_beat`` — the first live actor on the side opposite the
committing PC. Almost always that is an NPC opponent, which has no client to
throw, so the server rolls its save. ADR-074, correctly applied. Every test in
this file except ``test_a_pc_defender_is_never_server_rolled`` exercises that
case.

It is NOT always an NPC. ``_opposite_side_first_actor``
(``game/beat_kinds.py``) returns a bare name with no roster check, and
opponent seats come from the intent router's free-text
``dispatch.params["opponent"]`` (``agents/subsystems/confrontation.py`` ->
``NpcMention(side="opponent")``). ``encounter_lifecycle.py`` canonicalizes that
name against the NPC roster only and then seats it verbatim — its own comment
says "A name resolving to no roster NPC (a PC, a novel opponent) is left
untouched." So in multiplayer, a router that hears "I grab Donut" seats the PC
Donut opponent-side, and ``GameSnapshot.find_creature_core`` resolves
characters FIRST, so every downstream lookup lands on Donut's own PC core.

There is no live client-thrown save anywhere in this codebase to route that
into: ``CHECK_THROW`` with ``kind="save"`` exists server-side
(``handlers/check_throw.py`` -> ``dispatch/check.py``) but no client ever sends
it, and ``roll_role`` has no ``save`` member. The only client-thrown defence
that IS wired is Fate's ``FATE_DEFEND_REQUEST`` (4dF, ADR-151) — a different
family.

So this story is scoped to NPC defenders, and ``test_a_pc_defender_is_never
_server_rolled`` holds the line: when the defender resolves to a seated PC the
engine must refuse loudly instead of rolling the save on that player's behalf.
Silently deciding a PC's save is the same error as the flat "fail" — the server
owning an outcome the player should own. Wiring the PC-throws path is a
separate story (see the 158-59 Delivery Findings).

Determinism: no test depends on RNG landing well. ``_pin_d20`` replaces
``random.randint`` so any 1d20 returns a chosen face and every other roll
returns its low bound (the 158-54 idiom). The save must therefore roll through
the stdlib ``random`` module object like every other WN d20 in this codebase
(``wn_tools.wn_save`` -> ``random.randint``; ``resolve_downed`` -> an ``rng``
parameter defaulting to that same module).

P2-4 discipline: mechanics and spans only — no catalog content is pinned
beyond "a save-vs mutation exists and a no-save mutation exists". Skips cleanly
when sidequest-content is not on disk.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_GENRE = "mutant_wasteland"
_SPAN_USED = "awn.mutation.used"
_SPAN_REFUSED = "awn.mutation.refused"
_SPAN_SAVE = "awn.save.resolved"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")


# ─────────────────────────────────────────────────────────────────────────────
# Fixture plumbing (shape borrowed from test_dice_path_mutation_use_158_54.py)
# ─────────────────────────────────────────────────────────────────────────────


def _load_pack(genre: str = _GENRE):
    """Load the REAL pack. ``load_genre_pack`` is uncached, so in-test mutation
    of the returned confrontation defs cannot leak into sibling tests."""
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(genre))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _module(pack):
    from sidequest.game.ruleset.registry import get_ruleset_module

    return get_ruleset_module(pack.rules.ruleset)


def _combat_cdef(pack):
    combat = next((c for c in pack.rules.confrontations if c.category == "combat"), None)
    assert combat is not None, "mutant_wasteland must declare a combat confrontation"
    return combat


def _mutation_beat(pack):
    beat = next(
        (b for b in _combat_cdef(pack).beats if getattr(b, "mutation_resolution", False)), None
    )
    assert beat is not None, (
        "the combat confrontation must carry a mutation_resolution beat (102-7)"
    )
    return beat


def _save_mutation(pack, *, category: str | None = None):
    """A positive mutation carrying a real ``SaveVs`` clause (optionally of a
    specific save category). Fail-loud fixture premise, not a content pin."""
    assert pack.mutations is not None, "mutant_wasteland must ship mutations.yaml (102-7)"
    md = next(
        (
            m
            for m in pack.mutations.positives
            if m.save is not None
            and m.save.stat is not None
            and (category is None or m.save.stat == category)
        ),
        None,
    )
    assert md is not None, (
        f"fixture premise: the catalog must offer a positive mutation with a "
        f"save clause (category={category!r}); the save-vs branch is the whole story"
    )
    return md


def _save_stat(md) -> str:
    """The mutation's ``SaveVs.stat``, narrowed. ``_save_mutation`` only ever
    returns a def that has one."""
    assert md.save is not None and md.save.stat is not None, (
        f"fixture premise: {md.id!r} must carry a SaveVs clause with a stat"
    )
    return md.save.stat


def _negates_mutation(pack):
    """A positive mutation whose ``SaveVs.effect == "negates"`` — 5 of 7
    save-vs positives in the real catalog (review round 2 SM SCOPE RULING
    follow-up). Fail-loud fixture premise, not a content pin."""
    assert pack.mutations is not None
    md = next(
        (
            m
            for m in pack.mutations.positives
            if m.save is not None and m.save.stat is not None and m.save.effect == "negates"
        ),
        None,
    )
    assert md is not None, (
        "fixture premise: the catalog must offer a save-vs positive whose "
        "SaveVs.effect == 'negates' — the majority shape and the review "
        "round 2 reproduction target"
    )
    return md


def _non_negating_save_mutation(pack):
    """A positive mutation whose ``SaveVs.effect`` is something OTHER than
    ``negates`` — ``half`` or ``partial``, 2 of the 7 save-vs positives in the
    real catalog. Honouring those is explicitly out of scope (158-82), so a
    successful save against one must NOT claim a negation. Fail-loud fixture
    premise, not a content pin."""
    assert pack.mutations is not None
    md = next(
        (
            m
            for m in pack.mutations.positives
            if m.save is not None and m.save.stat is not None and m.save.effect != "negates"
        ),
        None,
    )
    assert md is not None, (
        "fixture premise: the catalog must offer a save-vs positive whose "
        "SaveVs.effect is not 'negates' — the un-honoured shape 158-82 owns"
    )
    return md


def _awn_cfg(pack):
    """The pack's ruleset config, narrowed to the AWN block that owns
    ``save_base`` and ``attribute_map``."""
    from sidequest.genre.models.rules import AwnConfig

    cfg = pack.rules.ruleset_config()
    assert isinstance(cfg, AwnConfig), (
        f"fixture premise: mutant_wasteland must resolve an AwnConfig; got {type(cfg).__name__}"
    )
    return cfg


def _nosave_mutation(pack):
    """A positive mutation with NO save clause — AWN auto-apply."""
    assert pack.mutations is not None
    md = next((m for m in pack.mutations.positives if m.save is None), None)
    assert md is not None, "fixture premise: the catalog must offer a no-save positive"
    return md


def _make_mutant(pack, name: str):
    """A mutant-class PC. All six ability scores at 10 => every SWN attribute
    modifier is 0 (``swn_attribute_modifier``: 8-13 -> 0), so any non-zero save
    modifier observed in these tests can only have come from the DEFENDER."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.system_strain import SystemStrainPool

    stats = {n: 10 for n in pack.rules.ability_score_names}
    core = CreatureCore(
        name=name,
        description="A mutant of the flickering wastes.",
        personality="watchful",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
        armor_class=12,
        system_strain=SystemStrainPool(current=0, max=10),
    )
    char_class = (
        pack.mutations.mp_economy.mutant_classes[0] if pack.mutations is not None else "Mutant"
    )
    pc = Character(
        core=core,
        char_class=char_class,
        race="Mutant Human",
        backstory="Born under the fallout sky.",
        stats=stats,
    )
    return pc, stats


def _seat_combat(pack, pc, pc_name: str, opponent: str, *, extra_characters=()):
    """Seat the real combat confrontation through the production seam,
    initiative pinned so the 1d8+DEX roll never flips the choreography.

    ``extra_characters`` are additional seated PCs (the multiplayer table).
    They are added to ``snapshot.characters`` BEFORE seating so that naming one
    of them as ``opponent`` reproduces the real MP shape the intent router can
    produce — a PC left on the opponent seat.
    """
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug="flickering_reach",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(pc)
    for extra in extra_characters:
        snap.characters.append(extra)
        snap.character_locations[extra.core.name] = "The Glass Flats"
    snap.character_locations[pc_name] = "The Glass Flats"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=pc_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug=_GENRE,
        allow_synthetic_opponent=True,
    )
    assert enc is not None, "seating the real combat confrontation must succeed"
    snap.encounter = enc
    enc.initiative = [
        InitiativeEntry(token_id=pc_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]
    return snap, enc


def _hydrate_mutation_state(snap, pc_name: str, positive_ids: list[str]) -> None:
    from sidequest.mutation.state import CharacterMutationState, MutationState

    snap.mutation_state = MutationState(
        characters={pc_name: CharacterMutationState(mp_remaining=0, positive_ids=positive_ids)}
    )


def _dispatch(*, pack, snap, enc, pc_name: str, stats: dict[str, int], beat_id: str, mutation_id):
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    broadcasts: list[object] = []
    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id="req-158-59",
            throw_params=ThrowParams(
                velocity=(0.0, 5.0, -2.0),
                angular=(1.0, 1.0, 1.0),
                position=(0.5, 0.5),
            ),
            face=[20],
            beat_id=beat_id,
            mutation_id=mutation_id,
        ),
        rolling_player_id="player-rux",
        character_name=pc_name,
        character_stats=dict(stats),
        encounter=enc,
        pack=pack,
        genre_slug=_GENRE,
        session_id="mw-158-59-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )


def _pin_d20(monkeypatch: pytest.MonkeyPatch, face: int) -> None:
    """Deterministic dice: any ``randint(1, 20)`` returns ``face``; every other
    roll returns its low bound.

    The commit's own d20 rides ``DiceThrowPayload.face`` (physics-is-the-roll,
    ADR-074) and is NOT affected by this patch — only server-side rolls are.
    """

    def _fixed(a: int, b: int) -> int:
        return face if (a, b) == (1, 20) else a

    monkeypatch.setattr("random.randint", _fixed)


def _spans(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _used_span(otel_capture) -> dict[str, Any]:
    used = _spans(otel_capture, _SPAN_USED)
    assert len(used) == 1, (
        f"expected exactly one {_SPAN_USED} from the production dice seam; got "
        f"{len(used)} (refusals: "
        f"{[(s.attributes or {}).get('reason') for s in _spans(otel_capture, _SPAN_REFUSED)]})"
    )
    return dict(used[0].attributes or {})


def _defender(snap, enc):
    """The mutation's defender exactly as the dice path derives it."""
    from sidequest.game.beat_kinds import _opposite_side_first_actor

    name = _opposite_side_first_actor(enc, "player")
    assert name is not None, "fixture premise: the seated combat has an opponent-side actor"
    core = snap.find_creature_core(name)
    assert core is not None, f"the seated opponent {name!r} must have a CreatureCore"
    return name, core


def _ruleset_save_params(pack, *, save: str, defender_core, cdef):
    """THE authority for this save — ``WithoutNumberRulesetModule.save_params``.

    Defender stats/level are resolved the way the codebase's existing NPC-save
    seam already does it (``server/dispatch/downed_seam.py::
    physical_save_target_for``): an opponent's ability scores come from the
    confrontation's ``opponent_ability_scores()``, its level from the core.
    """
    stats = cdef.opponent_ability_scores()
    assert stats, (
        "fixture premise: the combat cdef must author opponent_default_stats — "
        "a defender with no ability scores has no save (No Silent Fallbacks)"
    )
    params = _module(pack).save_params(
        stats=stats,
        save=save,
        level=int(getattr(defender_core, "level", 1) or 1),
        label=f"{save} save",
        cfg=pack.rules.ruleset_config(),
        character_core=defender_core,
    )
    assert (params.sides, params.count) == (20, 1), (
        "premise: the Without Number save is 1d20 (SRD p.46). The ruleset module "
        f"reported {params.count}d{params.sides} — read save_params before trusting "
        "the rest of this suite"
    )
    return params


def _scenario(pack, mutation_ids: list[str], *, opponent: str = "Raider Scav"):
    pc, stats = _make_mutant(pack, "Rux")
    snap, enc = _seat_combat(pack, pc, "Rux", opponent)
    _hydrate_mutation_state(snap, "Rux", mutation_ids)
    return pc, stats, snap, enc


# ─────────────────────────────────────────────────────────────────────────────
# 1. The bias itself: a defender who rolls well must be allowed to save
# ─────────────────────────────────────────────────────────────────────────────


def test_defender_can_save_against_a_save_vs_mutation(otel_capture, monkeypatch):
    """A natural 20 on the defender's saving throw must produce
    ``save_result="success"`` on ``awn.mutation.used``.

    FAILS TODAY: every production caller hands ``use_ops`` a resolver that
    returns the string "fail" without looking at anything, so a 20 and a 1 are
    the same event. This is the whole story — crunch that quietly always
    favours the player is broken math wearing a mechanic's clothes.
    """
    pack = _load_pack()
    assert pack.rules.ruleset == "awn", "mutant_wasteland must stay bound ruleset: awn"
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    _pin_d20(monkeypatch, 20)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_stat") == _save_stat(md), (
        f"the span must name the SaveVs stat from the mutation definition "
        f"({_save_stat(md)!r}); got {attrs.get('save_stat')!r}"
    )
    assert attrs.get("save_result") == "success", (
        "a defender rolling a natural 20 on its saving throw MUST save; got "
        f"save_result={attrs.get('save_result')!r}. The target never saves — "
        "the resolver is a flat lambda returning 'fail'"
    )


def test_a_blown_save_still_lands_the_mutation(otel_capture, monkeypatch):
    """The negative case, and AC4's no-regression leg: a natural 1 must fail
    the save, the mutation still applies, and the Strain is still paid.

    Passes today (everything fails today). It is here so the fix cannot
    over-rotate into a defender who always saves — the mirror-image bias.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    pc, stats, snap, enc = _scenario(pack, [md.id])
    assert pc.core.system_strain is not None, "fixture premise: the PC carries a Strain pool"
    strain_before = pc.core.system_strain.current
    _pin_d20(monkeypatch, 1)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_result") == "fail", (
        f"a natural 1 must blow the save; got {attrs.get('save_result')!r}"
    )
    assert pc.core.system_strain.current == strain_before + md.strain_cost, (
        "the power fires and the target saves — a save outcome must never "
        "refund the Strain cost (use_ops: cost paid, then save resolved)"
    )
    assert not _spans(otel_capture, _SPAN_REFUSED), "a blown save is a RESOLVED use, not a refusal"


# ─────────────────────────────────────────────────────────────────────────────
# 2. The number is the ruleset's number (ADR-143 — do not homebrew save math)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, "success"),  # d20 + modifier == difficulty  -> WN save succeeds on >=
        (-1, "fail"),  # one pip below                 -> WN save fails
    ],
)
def test_save_threshold_is_the_rulesets_number(otel_capture, monkeypatch, offset, expected):
    """Pin the exact WN boundary, derived from the bound module — never typed
    in by hand.

    ``WithoutNumberRulesetModule.save_params`` yields ``difficulty`` and
    ``modifier``; ``wn_tools.wn_save`` defines the comparison as
    ``(d20 + modifier) >= difficulty``. So the smallest saving face is
    ``difficulty - modifier``, and one below it must fail.

    A homebrewed threshold, a converted die, a ``>`` instead of ``>=``, or a
    save gated on the attacker's committed face all break exactly here.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    _, defender_core = _defender(snap, enc)
    params = _ruleset_save_params(
        pack, save=_save_stat(md), defender_core=defender_core, cdef=_combat_cdef(pack)
    )
    face = params.difficulty - params.modifier + offset
    assert 1 <= face <= 20, (
        f"fixture premise: the derived boundary face {face} must be a legal d20 "
        f"result (difficulty={params.difficulty} modifier={params.modifier})"
    )
    _pin_d20(monkeypatch, face)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_result") == expected, (
        f"d20={face} + modifier={params.modifier} vs the ruleset's "
        f"difficulty={params.difficulty} must be {expected!r}; got "
        f"{attrs.get('save_result')!r}. The save target belongs to "
        "WithoutNumberRulesetModule.save_params (cfg.save_base - (level-1), "
        "SRD p.46) — bind the ruleset, don't balance it"
    )


def test_save_uses_the_defenders_modifier_not_the_actors(otel_capture, monkeypatch):
    """The DEFENDER saves. The attacker's stat block is not a party to it.

    The PC's six scores are all 10 (modifier 0). Here the defender's two
    save-relevant attributes are raised to 18 (``swn_attribute_modifier``:
    18 -> +2) and the face is set so the throw lands exactly on the boundary
    *with* the defender's +2 and two pips short *without* it.

    A resolver that reaches for the attacker's stats — they are right there in
    the dispatch as ``character_stats`` — records "fail" here.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    cdef = _combat_cdef(pack)
    cfg = _awn_cfg(pack)

    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule

    for swn_attr in WithoutNumberRulesetModule._SAVE_ATTRS[_save_stat(md)]:
        assert cdef.opponent_default_stats is not None
        cdef.opponent_default_stats[cfg.attribute_map[swn_attr]] = 18

    _, stats, snap, enc = _scenario(pack, [md.id])
    _, defender_core = _defender(snap, enc)
    params = _ruleset_save_params(pack, save=_save_stat(md), defender_core=defender_core, cdef=cdef)
    assert params.modifier == 2, (
        "fixture premise: an 18 in both save attributes must give the defender "
        f"+2 on this save; save_params reported {params.modifier}"
    )
    face = params.difficulty - params.modifier
    _pin_d20(monkeypatch, face)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_result") == "success", (
        f"d20={face} plus the DEFENDER's +2 reaches difficulty="
        f"{params.difficulty}; got {attrs.get('save_result')!r}. With the "
        "attacker's modifier (0, all scores 10) this same face falls two short "
        "— the save being resolved is the wrong creature's"
    )


def test_save_category_comes_from_the_mutation_definition(otel_capture, monkeypatch):
    """AC1: the save is resolved against the ``SaveVs`` stat *on the mutation
    def*, not a category the engine picked.

    Only the defender's MENTAL attributes (WISDOM/CHARISMA) are raised. Two
    mutations are fired at the identical face: the mental-save mutation must
    clear the bar on its +2, the evasion-save mutation must not (DEXTERITY /
    INTELLIGENCE are still 10 -> +0). A hardcoded category cannot produce both
    answers.
    """
    pack = _load_pack()
    mental = _save_mutation(pack, category="mental")
    evasion = _save_mutation(pack, category="evasion")
    beat = _mutation_beat(pack)
    cdef = _combat_cdef(pack)
    cfg = _awn_cfg(pack)

    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule

    for swn_attr in WithoutNumberRulesetModule._SAVE_ATTRS["mental"]:
        assert cdef.opponent_default_stats is not None
        cdef.opponent_default_stats[cfg.attribute_map[swn_attr]] = 18

    _, stats, snap, enc = _scenario(pack, [mental.id, evasion.id])
    _, defender_core = _defender(snap, enc)
    mental_params = _ruleset_save_params(
        pack, save="mental", defender_core=defender_core, cdef=cdef
    )
    evasion_params = _ruleset_save_params(
        pack, save="evasion", defender_core=defender_core, cdef=cdef
    )
    assert (mental_params.modifier, evasion_params.modifier) == (2, 0), (
        "fixture premise: the defender must be sharp of mind (+2 mental) and "
        "ordinary of reflex (+0 evasion); save_params reported "
        f"{mental_params.modifier} / {evasion_params.modifier}"
    )
    assert mental_params.difficulty == evasion_params.difficulty, (
        "fixture premise: both categories share one difficulty, so only the "
        "modifier can separate the two outcomes"
    )
    face = mental_params.difficulty - mental_params.modifier
    _pin_d20(monkeypatch, face)

    results: dict[str, Any] = {}
    for md in (mental, evasion):
        _dispatch(
            pack=pack,
            snap=snap,
            enc=enc,
            pc_name="Rux",
            stats=stats,
            beat_id=beat.id,
            mutation_id=md.id,
        )
        spans = [
            s
            for s in _spans(otel_capture, _SPAN_USED)
            if (s.attributes or {}).get("mutation_id") == md.id
        ]
        assert len(spans) == 1, f"expected exactly one {_SPAN_USED} for {md.id!r}; got {len(spans)}"
        results[_save_stat(md)] = dict(spans[0].attributes or {}).get("save_result")

    assert results == {"mental": "success", "evasion": "fail"}, (
        f"same defender, same d20={face}, two save categories — got {results}. "
        "The category must come from the mutation's SaveVs.stat "
        f"({mental.id} -> mental, {evasion.id} -> evasion)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Auto-apply mutations keep auto-applying (AC4, the other regression leg)
# ─────────────────────────────────────────────────────────────────────────────


def test_mutation_without_a_save_clause_resolves_no_save(otel_capture, monkeypatch):
    """``SaveVs`` absent (or ``stat=None``) means AWN auto-apply: there is no
    saving throw to make, so the span must carry NO save fields even when the
    dice would have been generous.

    Passes today. It is here so "wire the real save" cannot become "roll a
    save for everything" — inventing a save the mutation def never granted is
    the same homebrew failure as inventing a threshold.
    """
    pack = _load_pack()
    md = _nosave_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    _pin_d20(monkeypatch, 20)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_stat") == "", (
        f"a mutation with no SaveVs clause must record no save_stat; got {attrs.get('save_stat')!r}"
    )
    assert attrs.get("save_result") == "", (
        f"a mutation with no SaveVs clause must record no save_result; got "
        f"{attrs.get('save_result')!r} — the engine rolled a save nobody granted"
    )
    assert not _spans(otel_capture, _SPAN_SAVE), (
        "no saving throw was authored, so no save may be resolved"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. The GM panel must be able to check the arithmetic (OTEL doctrine)
# ─────────────────────────────────────────────────────────────────────────────


def test_save_arithmetic_is_visible_to_the_gm_panel(otel_capture, monkeypatch):
    """``save_result`` alone is unfalsifiable — a hardcoded "success" reads
    identically to a rolled one, which is exactly how the flat "fail" survived
    from 102-7 to now. The roll, the modifier and the target must be on a span.

    The emitter already exists and is already routed to the panel:
    ``telemetry/spans/wn.py::wn_save_resolved_span`` emits
    ``{slug}.save.resolved`` and ``SPAN_ROUTES`` registers it for all four WN
    slugs, ``awn`` included (``_WN_EVENTS["save.resolved"] -> "wn_save"``).
    ``wn_tools.wn_save`` is the working caller. Wire up what exists.

    FAILS TODAY: no save is rolled, so nothing is emitted.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    defender_name, defender_core = _defender(snap, enc)
    params = _ruleset_save_params(
        pack, save=_save_stat(md), defender_core=defender_core, cdef=_combat_cdef(pack)
    )
    face = params.difficulty - params.modifier
    _pin_d20(monkeypatch, face)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    saves = _spans(otel_capture, _SPAN_SAVE)
    assert len(saves) == 1, (
        f"expected exactly one {_SPAN_SAVE}; got {len(saves)}. Without the "
        "roll, the modifier and the target on a span the GM panel cannot tell "
        "a resolved save from an asserted one"
    )
    attrs = dict(saves[0].attributes or {})
    assert attrs.get("actor") == defender_name, (
        f"the save belongs to the DEFENDER {defender_name!r}; span says {attrs.get('actor')!r}"
    )
    assert attrs.get("save") == _save_stat(md), (
        f"span save category {attrs.get('save')!r} != the mutation's {_save_stat(md)!r}"
    )
    assert attrs.get("d20") == face, f"span d20 {attrs.get('d20')!r} != the rolled {face}"
    assert attrs.get("modifier") == params.modifier, (
        f"span modifier {attrs.get('modifier')!r} != the ruleset's {params.modifier}"
    )
    assert attrs.get("target") == params.difficulty, (
        f"span target {attrs.get('target')!r} != the ruleset's {params.difficulty}"
    )
    assert attrs.get("success") is True, "d20 + modifier == target is a WN success (>=)"
    assert _used_span(otel_capture).get("save_result") == "success", (
        "the two spans must tell the same story — a save.resolved that "
        "disagrees with awn.mutation.used is worse than no span at all"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. ADR-074 in the other direction: nobody rolls a PC's save for them
# ─────────────────────────────────────────────────────────────────────────────


def test_a_pc_defender_is_never_server_rolled(otel_capture, monkeypatch):
    """The defender resolves to a seated PC. The engine must refuse — loudly,
    recorded, and without paying the Strain — rather than roll that player's
    saving throw on their behalf.

    Reachability (this is a real state, not a hypothetical): opponent seats are
    minted from the intent router's free-text ``params["opponent"]``, and
    ``encounter_lifecycle`` seats a name that resolves to no roster NPC
    verbatim — its own comment names "a PC" as one such case. This test
    reproduces that seat through the production seating seam.
    ``find_creature_core`` resolves ``snapshot.characters`` first, so the
    defender core here IS the other player's own core.

    ADR-074 is doctrine in both directions. The server rolls for NPCs because
    they have no client to throw; a PC HAS one. There is no live client-thrown
    WN save to route this into (``CHECK_THROW`` ``kind="save"`` exists but no
    client sends it; ``roll_role`` has no ``save`` member), so the honest
    behaviour at this scope is the 158-54 opposed_check precedent: refuse until
    a story defines the semantics — never quietly resolve it.

    FAILS TODAY for exactly the reason the story exists: the use is applied,
    the Strain is spent, and ``awn.mutation.used`` records ``save_result="fail"``
    — the server has decided a player's saving throw for them, and decided it
    against them. That is the flat-"fail" disease, aimed at a teammate.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    pc, stats = _make_mutant(pack, "Rux")
    donut, _ = _make_mutant(pack, "Donut")
    snap, enc = _seat_combat(pack, pc, "Rux", "Donut", extra_characters=[donut])
    _hydrate_mutation_state(snap, "Rux", [md.id])

    defender_name, _ = _defender(snap, enc)
    assert defender_name == "Donut", (
        f"fixture premise: the dice path must derive the PC as the defender; got {defender_name!r}"
    )
    assert any(c.core.name == defender_name for c in snap.characters), (
        f"fixture premise: {defender_name!r} must be a seated PC in snapshot.characters"
    )
    assert pc.core.system_strain is not None and donut.core.system_strain is not None
    strain_before = pc.core.system_strain.current
    _pin_d20(monkeypatch, 20)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    pc_saves = [
        s for s in _spans(otel_capture, _SPAN_SAVE) if (s.attributes or {}).get("actor") == "Donut"
    ]
    assert not pc_saves, (
        f"the server rolled {defender_name!r}'s saving throw: {pc_saves and dict(pc_saves[0].attributes or {})}. "
        "A PC throws their own dice (ADR-074) — physics-is-the-roll is not "
        "optional when the outcome is a player's"
    )
    assert not _spans(otel_capture, _SPAN_USED), (
        "the use must not resolve against a PC defender at all — resolving it "
        "with no save is the flat-'fail' bias wearing a different hat (the "
        f"target lands at full effect and {defender_name!r} was never asked)"
    )
    refused = _spans(otel_capture, _SPAN_REFUSED)
    assert len(refused) == 1, (
        f"expected exactly one {_SPAN_REFUSED} naming why this use could not "
        f"resolve; got {len(refused)}. Silence is indistinguishable from improv "
        "— the GM panel must see the refusal (102-3: refusal IS engagement)"
    )
    reason = str((refused[0].attributes or {}).get("reason", ""))
    assert reason, "the refusal span must carry a reason slug the GM panel can group on"
    assert pc.core.system_strain.current == strain_before, (
        f"a refused use pays no Strain; {strain_before} -> {pc.core.system_strain.current}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Review round 2 (Dev, [HIGH] fix) — a defender-resolution miss refuses,
#    it does not debit Strain and then crash
# ─────────────────────────────────────────────────────────────────────────────
#
# Reviewer reproduced both of these empirically against the pre-fix code
# through the production dispatch_dice_throw seam:
#
#   PROBE-WITHDRAWN: strain 0.0 -> 1.0; ValueError: mutation
#     'sense/thermal_vision' save.stat='evasion' has no resolvable
#     defender '' to save against
#   PROBE-NOSTATS:   strain 0.0 -> 1.0; ValueError: opponent 'Scrapjaw'
#     has no ability scores
#
# Both guards used to live INSIDE the save_resolver closure, which use_ops
# only invokes AFTER Strain is already spent — a miss there raised an
# uncaught ValueError (not DiceDispatchError), so the player paid Strain,
# the GM panel saw nothing, and the exception escaped the dispatch layer's
# error handling straight to the websocket handler's disconnect path. Both
# tests below pin the fix: refuse loud, pay no Strain, never raise.


def test_defender_withdrawn_refuses_without_charging_strain_or_raising(otel_capture, monkeypatch):
    """The sole opponent withdrew — a designed morale outcome
    (``narration_apply.py`` sets ``EncounterActor.withdrawn = True`` on a
    narrator-named disengagement without resolving the encounter), so
    ``_opposite_side_first_actor`` has nothing to return and
    ``target_name == ""``. A save-vs mutation committed against an empty
    battlefield must refuse — no defender core to resolve is a mechanical
    fact, not a crash.

    FAILED review round 1: this used to debit Strain then raise an uncaught
    ``ValueError`` with zero OTEL.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    pc, stats, snap, enc = _scenario(pack, [md.id])
    assert pc.core.system_strain is not None
    strain_before = pc.core.system_strain.current
    for a in enc.actors:
        if a.side == "opponent":
            a.withdrawn = True
    _pin_d20(monkeypatch, 20)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    assert not _spans(otel_capture, _SPAN_USED), "no defender to save against means no resolved use"
    assert not _spans(otel_capture, _SPAN_SAVE), "no defender means no saving throw was rolled"
    refused = _spans(otel_capture, _SPAN_REFUSED)
    assert len(refused) == 1, (
        f"expected exactly one {_SPAN_REFUSED} naming why this use could not "
        f"resolve; got {len(refused)}. The GM panel must see the refusal, not "
        "an uncaught exception it never learns about"
    )
    reason = str((refused[0].attributes or {}).get("reason", ""))
    assert reason, "the refusal span must carry a reason slug the GM panel can group on"
    assert pc.core.system_strain.current == strain_before, (
        f"a refused use pays no Strain; {strain_before} -> {pc.core.system_strain.current} — "
        "the guard must run BEFORE use_mutation, not inside the save_resolver "
        "AFTER Strain is already spent"
    )


def test_defender_missing_ability_scores_refuses_without_charging_strain_or_raising(
    otel_capture, monkeypatch
):
    """The opponent core exists (seating already gave it hp/armor_class) but
    the confrontation authored no ability scores for it — ``save_params``
    would have nothing to compute a modifier from. This must refuse, not
    silently default the modifier to 0 (No Silent Fallbacks) and not raise
    after Strain is already spent.

    FAILED review round 1: this used to debit Strain then raise an uncaught
    ``ValueError`` with zero OTEL.
    """
    pack = _load_pack()
    md = _save_mutation(pack)
    beat = _mutation_beat(pack)
    cdef = _combat_cdef(pack)
    pc, stats, snap, enc = _scenario(pack, [md.id])
    assert pc.core.system_strain is not None
    strain_before = pc.core.system_strain.current

    # Seating already baked hp/armor_class/dexterity into the opponent's
    # CreatureCore; strip everything else so opponent_ability_scores()
    # returns {} for the save that fires AFTER seating.
    from sidequest.genre.models.rules import OPPONENT_RESERVED_STAT_KEYS

    assert cdef.opponent_default_stats is not None
    cdef.opponent_default_stats = {
        k: v for k, v in cdef.opponent_default_stats.items() if k in OPPONENT_RESERVED_STAT_KEYS
    }
    assert not cdef.opponent_ability_scores(), (
        "fixture premise: stripping every non-reserved key must leave no "
        "ability scores for save_params to read"
    )
    _pin_d20(monkeypatch, 20)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    assert not _spans(otel_capture, _SPAN_USED), "no ability scores means no resolved use"
    assert not _spans(otel_capture, _SPAN_SAVE), (
        "no ability scores means no saving throw was rolled"
    )
    refused = _spans(otel_capture, _SPAN_REFUSED)
    assert len(refused) == 1, (
        f"expected exactly one {_SPAN_REFUSED} naming why this use could not "
        f"resolve; got {len(refused)}"
    )
    reason = str((refused[0].attributes or {}).get("reason", ""))
    assert reason, "the refusal span must carry a reason slug the GM panel can group on"
    assert pc.core.system_strain.current == strain_before, (
        f"a refused use pays no Strain; {strain_before} -> {pc.core.system_strain.current} — "
        "the guard must run BEFORE use_mutation, not inside the save_resolver "
        "AFTER Strain is already spent"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Review round 2 (Dev) — a successful negating save is visible AT THE TABLE
# ─────────────────────────────────────────────────────────────────────────────


def test_a_successful_negating_save_leaves_a_mechanical_truth_narrator_hint(
    otel_capture, monkeypatch
):
    """SM SCOPE RULING follow-up: ``SaveVs.effect == "negates"`` honouring
    was dead code until the table could see it — ``result.effect`` was read
    in exactly one place server-wide (``magic_working.py``, whose resolver
    is flat ``"fail"`` and can never observe a success), so a successful
    negating save changed nothing a player would notice. A defender that
    saves against a ``negates`` mutation must leave exactly one MECHANICAL
    TRUTH narrator hint (the same idiom 158-57 established for refusals) so
    the narrator cannot write the full effect landing over a save the
    defender won.
    """
    pack = _load_pack()
    md = _negates_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    defender_name, defender_core = _defender(snap, enc)
    params = _ruleset_save_params(
        pack, save=_save_stat(md), defender_core=defender_core, cdef=_combat_cdef(pack)
    )
    face = params.difficulty - params.modifier
    _pin_d20(monkeypatch, face)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_result") == "success", (
        "fixture premise: the pinned face must clear the boundary and win the save"
    )

    hits = [
        h
        for h in enc.narrator_hints
        if "MECHANICAL TRUTH" in h and defender_name in h and md.id in h
    ]
    assert len(hits) == 1, (
        "a successful negating save must leave exactly one MECHANICAL TRUTH "
        "narrator hint naming the defender and the mutation, so the narrator "
        f"cannot write the effect landing over the save; hints present: {enc.narrator_hints}"
    )


def test_a_successful_non_negating_save_claims_no_negation(otel_capture, monkeypatch):
    """The other direction of the same honesty rule. ``SaveVs.effect`` of
    ``half``/``partial`` is NOT honoured — those mutations still land at full
    effect on a successful save (158-82 owns closing that). So the save
    succeeding must leave NO hint claiming the effect was negated: asserting a
    negation the engine did not deliver is the same Illusionism failure as
    narrating a full-effect hit over a save the defender won, pointed the
    other way.

    This also pins the gate's shape. It reads ``md.save.effect == "negates"``
    — the same condition ``use_ops`` blanks the effect on — rather than
    inferring negation from ``result.effect == ""``, which collides with the
    field's own default whenever a mutation authors no ``effect`` text.
    """
    pack = _load_pack()
    md = _non_negating_save_mutation(pack)
    beat = _mutation_beat(pack)
    _, stats, snap, enc = _scenario(pack, [md.id])
    defender_name, defender_core = _defender(snap, enc)
    params = _ruleset_save_params(
        pack, save=_save_stat(md), defender_core=defender_core, cdef=_combat_cdef(pack)
    )
    face = params.difficulty - params.modifier
    _pin_d20(monkeypatch, face)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=md.id,
    )

    attrs = _used_span(otel_capture)
    assert attrs.get("save_result") == "success", (
        "fixture premise: the pinned face must clear the boundary and win the save"
    )
    assert md.save is not None and md.save.effect != "negates", (
        "fixture premise: this mutation's save must be one we do NOT honour"
    )

    negation_claims = [
        h for h in enc.narrator_hints if "MECHANICAL TRUTH" in h and md.id in h and "negated" in h
    ]
    assert not negation_claims, (
        f"a {md.save.effect!r} save is not honoured — the effect still lands at "
        "full strength, so nothing may tell the narrator it was negated; "
        f"offending hints: {negation_claims}"
    )
