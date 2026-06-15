from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset.fate_resolution import Opposition
from sidequest.game.session import GameSnapshot
from sidequest.server.dispatch.fate_conflict import (
    FateConflictError,
    fate_barrier_closed,
    fate_turn_order,
    fate_waiting_actors,
    seal_fate_commit,
)


class _FixedRng:
    """A deterministic stand-in for random.Random: every 4dF face is ``value``."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def test_barrier_waits_on_pcs_then_closes():
    enc = _enc(
        [
            EncounterActor(name="Vesska", role="lead", side="player"),
            EncounterActor(name="Brakka", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Vesska", {"Fight": 3}), _pc("Brakka", {"Fight": 2})],
        encounter=enc,
    )
    assert set(fate_waiting_actors(encounter=enc, snapshot=snap)) == {"Vesska", "Brakka"}
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is False

    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Vesska"),
        action="attack",
        skill="Fight",
        target="Thug",
        ladder_total=5,
    )
    assert fate_waiting_actors(encounter=enc, snapshot=snap) == ["Brakka"]
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is False

    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Brakka"),
        action="overcome",
        skill="Athletics",
        difficulty=1,
        ladder_total=2,
    )
    assert fate_barrier_closed(encounter=enc, snapshot=snap) is True


def test_double_commit_fails_loud():
    enc = _enc([EncounterActor(name="Vesska", role="lead", side="player")])
    GameSnapshot(genre_slug="fate_test", characters=[_pc("Vesska", {"Fight": 3})], encounter=enc)
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Vesska"),
        action="overcome",
        skill="Athletics",
        difficulty=2,
        ladder_total=4,
    )
    with pytest.raises(FateConflictError):
        seal_fate_commit(
            encounter=enc,
            actor=enc.find_actor("Vesska"),
            action="attack",
            skill="Fight",
            target="x",
            ladder_total=4,
        )


def test_turn_order_uses_notice_for_physical_empathy_for_mental():
    enc_actors = [
        EncounterActor(name="Quick", role="a", side="player"),
        EncounterActor(name="Slow", role="b", side="opponent"),
    ]
    physical = _enc(enc_actors, category="combat")
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc("Quick", {"Notice": 4, "Empathy": 1})],
        npcs=[],
        encounter=physical,
    )
    # Seat the opponent as an NPC so find_creature_core resolves its sheet.
    from sidequest.game.session import Npc

    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Slow",
                description="d",
                personality="p",
                fate_sheet=FateSheet(skills={"Notice": 2, "Empathy": 5}),
            )
        )
    )
    assert fate_turn_order(encounter=physical, snapshot=snap, mental=False) == ["Quick", "Slow"]
    # Mental conflict flips it: Slow's Empathy 5 beats Quick's Empathy 1.
    assert fate_turn_order(encounter=physical, snapshot=snap, mental=True) == ["Slow", "Quick"]


def test_absorb_uses_one_stress_box_when_it_covers_the_hit():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()  # physical boxes [1,2]; consequences open
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=2, actor="Hero", source="Thug"
    )
    assert survived is True
    assert sheet.stress["physical"].boxes[1].checked is True  # the value-2 box
    assert all(c.aspect is None for c in sheet.consequences)  # no consequence needed


def test_absorb_combines_one_box_plus_consequences():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()  # best single box = 2; consequences 2/4/6/8
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=5, actor="Hero", source="Thug"
    )
    assert survived is True
    # 5 shifts: largest box (2) + mild consequence (2) leaves 1 → moderate (4) covers it.
    assert sheet.stress["physical"].boxes[1].checked is True
    filled = [c.level for c in sheet.consequences if c.aspect is not None]
    assert filled == ["mild", "moderate"]
    # The consequence text is a real, descriptive default naming the source.
    mild = next(c for c in sheet.consequences if c.level == "mild")
    assert "Thug" in mild.aspect.text


def test_absorb_returns_false_when_capacity_exhausted():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import absorb_shifts

    module = get_ruleset_module("fate")
    sheet = FateSheet()
    # Pre-deplete: check every stress box, fill every consequence slot.
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    survived = absorb_shifts(
        module=module, sheet=sheet, track="physical", shifts=1, actor="Hero", source="Thug"
    )
    assert survived is False  # nothing left to absorb with → taken out


def test_absorb_rejects_nonpositive_shifts():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import FateConflictError, absorb_shifts

    module = get_ruleset_module("fate")
    with pytest.raises(FateConflictError, match="shifts >= 1"):
        absorb_shifts(
            module=module,
            sheet=FateSheet(),
            track="physical",
            shifts=0,
            actor="Hero",
            source="Thug",
        )


def test_absorb_rejects_unknown_track():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import FateConflictError, absorb_shifts

    module = get_ruleset_module("fate")
    with pytest.raises(FateConflictError, match="stress track"):
        absorb_shifts(
            module=module,
            sheet=FateSheet(),
            track="corruption",
            shifts=1,
            actor="Hero",
            source="Thug",
        )  # type: ignore[arg-type]


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _seal_attack(enc, snapshot, module, attacker, skill_rating, target, *, skill="Fight"):
    # Compute the attacker's sealed roll deterministically (FixedRng(0) → 4dF=0).
    outcome = module.resolve_action(
        skill_rating=skill_rating, opposition=Opposition(value=0, kind="active"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor(attacker),
        action="attack",
        skill=skill,
        target=target,
        ladder_total=outcome.ladder_total,
        dice=outcome.dice,
    )


def test_attack_hits_and_target_absorbs_survives():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    hero = _pc("Hero", {"Fight": 4, "Notice": 3})
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Thug",
                description="d",
                personality="p",
                fate_sheet=FateSheet(skills={"Athletics": 1, "Notice": 1}),
            )
        )
    )
    _seal_attack(enc, snap, module, "Hero", 4, "Thug")  # ladder_total 4; defense 1 → shifts 3

    exporter, tracer = _otel()
    result = run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    thug = enc.find_actor("Thug")
    assert thug is not None
    thug_sheet = snap.find_creature_core("Thug").fate_sheet
    assert thug_sheet.stress["physical"].boxes[1].checked is True  # absorbed 3 via box2 + mild
    assert thug_sheet.consequences[0].aspect is not None  # mild absorbed the 3rd shift
    assert thug.withdrawn is False  # survived
    assert enc.resolved is False
    assert result.resolved is False  # the returned result mirrors the encounter (F1d consumes this)
    assert result.resolution_order == "Hero, Thug"  # Notice 3 → Hero first, then the unsorted foe
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.exchange.committed" in names
    assert "fate.exchange.order" in names
    assert "fate.exchange.resolved" in names
    assert not enc.fate_commits  # ledger cleared


def test_attack_takes_out_a_depleted_target_and_resolves():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 4})], encounter=enc
    )
    thug_sheet = FateSheet(skills={"Athletics": 1})
    for b in thug_sheet.stress["physical"].boxes:
        b.checked = True
    for c in thug_sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    snap.npcs.append(
        Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=thug_sheet))
    )
    _seal_attack(enc, snap, module, "Hero", 4, "Thug")  # shifts 3, target cannot absorb

    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    assert enc.find_actor("Thug").withdrawn is True
    assert enc.resolved is True
    assert enc.outcome == "opponent_yielded"  # all opponents out → player victory label
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.taken_out" in names


def test_create_advantage_places_a_situation_aspect():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 3})], encounter=enc
    )
    outcome = module.resolve_action(
        skill_rating=3, opposition=Opposition(value=0, kind="passive"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Hero"),
        action="create_advantage",
        skill="Notice",
        difficulty=2,
        ladder_total=outcome.ladder_total,
        aspect_text="Pinned Down",
    )  # shifts = 3 - 2 = 1 → 1 free invoke

    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    assert [a.text for a in enc.situation_aspects] == ["Pinned Down"]
    assert enc.situation_aspects[0].free_invokes == 1
    names = [s.name for s in exporter.get_finished_spans()]
    assert "fate.aspect.created" in names


def test_attack_that_misses_deals_no_damage():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Rival", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 1})], encounter=enc
    )
    # Rival's Athletics 3 defense beats Hero's Fight 1 attack → shifts -2 (clean miss).
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Rival",
                description="d",
                personality="p",
                fate_sheet=FateSheet(skills={"Athletics": 3}),
            )
        )
    )
    _seal_attack(enc, snap, module, "Hero", 1, "Rival")

    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    rival_sheet = snap.find_creature_core("Rival").fate_sheet
    assert all(not b.checked for b in rival_sheet.stress["physical"].boxes)  # clean miss: no damage


def test_concede_withdraws_and_earns_fate_points():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import concede_in_conflict

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    sheet = FateSheet(fate_points=1)
    sheet.consequences[0].aspect = Aspect(text="Twisted Ankle", kind="consequence", free_invokes=1)
    hero = _pc("Hero", {"Fight": 2})
    hero.core.fate_sheet = sheet
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero], encounter=enc)

    exporter, tracer = _otel()
    earned = concede_in_conflict(
        encounter=enc, snapshot=snap, ruleset=module, actor="Hero", _tracer=tracer
    )

    assert earned == 2  # 1 base + 1 consequence taken this conflict
    assert sheet.fate_points == 3
    assert enc.find_actor("Hero").withdrawn is True
    assert "fate.conceded" in [s.name for s in exporter.get_finished_spans()]


def test_tie_attack_grants_defender_boost():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.session import Npc
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Rival", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": 2})], encounter=enc
    )
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Rival",
                description="d",
                personality="p",
                fate_sheet=FateSheet(skills={"Athletics": 2}),
            )
        )
    )
    _seal_attack(enc, snap, module, "Hero", 2, "Rival")  # ladder 2 vs defense 2 → shifts 0 (tie)
    # F2d seats an opponent attack for any un-committed opponent; pre-seal Rival's
    # action as a clean MISS so seating skips it (no double-commit) and Rival's
    # resolution adds no aspect — isolating the assertion to Hero's tie boost.
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Rival"),
        action="attack",
        skill="Athletics",
        target="Hero",
        ladder_total=-5,  # guaranteed miss vs any 4dF defense → no boost from Rival
    )

    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    rival = snap.find_creature_core("Rival")
    assert rival is not None
    assert all(not b.checked for b in rival.fate_sheet.stress["physical"].boxes)  # tie = no stress
    boosts = [a for a in enc.situation_aspects if a.kind == "boost"]
    assert [b.text for b in boosts] == ["Momentum vs Hero"]  # only Hero's tie granted a boost
    assert "fate.aspect.created" in [s.name for s in exporter.get_finished_spans()]


def test_create_advantage_succeed_with_style_grants_two_invokes():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 4})], encounter=enc
    )
    outcome = module.resolve_action(
        skill_rating=4, opposition=Opposition(value=0, kind="passive"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Hero"),
        action="create_advantage",
        skill="Notice",
        difficulty=1,
        ladder_total=outcome.ladder_total,
        aspect_text="Flanked",
    )  # shifts = 4 - 1 = 3 → Succeed-with-Style → 2 free invokes

    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    assert [a.text for a in enc.situation_aspects] == ["Flanked"]
    assert enc.situation_aspects[0].free_invokes == 2


def test_concede_rejects_unseated_actor():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import FateConflictError, concede_in_conflict

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    ghost = _pc("Ghost", {"Fight": 2})  # has a sheet but is NOT seated in enc.actors
    snap = GameSnapshot(genre_slug="fate_test", characters=[ghost], encounter=enc)
    with pytest.raises(FateConflictError, match="not seated"):
        concede_in_conflict(encounter=enc, snapshot=snap, ruleset=module, actor="Ghost")


# ---------------------------------------------------------------------------
# Story 116-4 / F2c — Task 1: create-advantage SUCCESS rendering.
#
# F1c silently placed a situation aspect on a successful create-advantage and
# fired `fate.aspect.created`, but appended NO narrator_hint on success
# (fate_conflict.py:570-590) — only failure appended a hint. So the engine
# placed "Pinned Down (1 free invoke)" and the narrator never heard about it.
# F2c closes that gap: a successful create-advantage must append a narrator
# hint naming the aspect + its free-invoke count, mirroring the failure-hint
# style already present. F1c regression bar: the situation-aspect placement and
# `fate.aspect.created` math are UNCHANGED — these tests ADD a hint assertion.
# ---------------------------------------------------------------------------


def test_create_advantage_success_appends_narrator_hint():
    """A resolved create_advantage with shifts>=1 appends a SUCCESS narrator
    hint naming the actor, the created aspect, and its free-invoke count.

    Today the success branch appends only the situation aspect + span, no hint
    (fate_conflict.py:570-580) — so this FAILS until F2c adds the success hint.
    """
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 3})], encounter=enc
    )
    outcome = module.resolve_action(
        skill_rating=3, opposition=Opposition(value=0, kind="passive"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Hero"),
        action="create_advantage",
        skill="Notice",
        difficulty=2,
        ladder_total=outcome.ladder_total,
        aspect_text="Pinned Down",
    )  # shifts = 3 - 2 = 1 → success, 1 free invoke

    result = run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    # The situation aspect still lands (F1c math unchanged — regression guard).
    assert [a.text for a in enc.situation_aspects] == ["Pinned Down"]
    assert enc.situation_aspects[0].free_invokes == 1

    # NEW (F2c): a success hint reached the encounter's narrator hints AND the
    # exchange result, naming actor + aspect + free-invoke count.
    matches = [h for h in enc.narrator_hints if "Pinned Down" in h]
    assert matches, (
        "a successful create-advantage must append a narrator hint naming the "
        f"created aspect; got narrator_hints={enc.narrator_hints!r}"
    )
    hint = matches[0]
    assert "Hero" in hint, f"success hint must name the acting actor; got {hint!r}"
    assert "free invoke" in hint.lower(), (
        f"success hint must state the free-invoke grant; got {hint!r}"
    )
    assert "1" in hint, f"success hint must carry the free-invoke count (1); got {hint!r}"
    # The exchange result's narrator_hints carry the same success line (the F2
    # narrator consumes FateExchangeResult.narrator_hints).
    assert any("Pinned Down" in h for h in result.narrator_hints), (
        "FateExchangeResult.narrator_hints must include the create-advantage "
        f"success hint; got {result.narrator_hints!r}"
    )


def test_create_advantage_success_hint_reaches_render_summary():
    """The success hint must surface to the narrator prompt via
    render_encounter_summary's Hints line (encounter_render.py:44-45) — the
    plumbing that makes the created advantage visible in narration."""
    from sidequest.agents.encounter_render import render_encounter_summary
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 3})], encounter=enc
    )
    outcome = module.resolve_action(
        skill_rating=3, opposition=Opposition(value=0, kind="passive"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Hero"),
        action="create_advantage",
        skill="Notice",
        difficulty=2,
        ladder_total=outcome.ladder_total,
        aspect_text="Pinned Down",
    )

    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    summary = render_encounter_summary(enc)
    assert "Hints:" in summary, f"summary should carry a Hints line; got:\n{summary}"
    assert "Pinned Down" in summary, (
        "the created advantage must be visible to the narrator via the rendered "
        f"encounter summary; got:\n{summary}"
    )


def test_create_advantage_succeed_with_style_hint_names_two_invokes():
    """Succeed-with-style (shifts>=3) grants two free invokes; the success hint
    must report the count honestly (2, not 1)."""
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.server.dispatch.fate_conflict import run_fate_exchange

    module = get_ruleset_module("fate")
    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 4})], encounter=enc
    )
    outcome = module.resolve_action(
        skill_rating=4, opposition=Opposition(value=0, kind="passive"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor("Hero"),
        action="create_advantage",
        skill="Notice",
        difficulty=1,
        ladder_total=outcome.ladder_total,
        aspect_text="Flanked",
    )  # shifts = 4 - 1 = 3 → Succeed-with-Style → 2 free invokes

    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    matches = [h for h in enc.narrator_hints if "Flanked" in h]
    assert matches, f"succeed-with-style must append a hint; got {enc.narrator_hints!r}"
    assert "2" in matches[0], (
        f"succeed-with-style hint must report 2 free invokes; got {matches[0]!r}"
    )


def test_live_situation_aspects_surface_as_scene_aspects():
    """Regression guard (AC1, pre-satisfied by F2b): live situation aspects are
    honestly visible to the narrator/router via build_fate_projection's
    scene_aspects. F2c relies on this seam, so guard it stays wired."""
    from sidequest.game.ruleset.fate_projection import build_fate_projection

    enc = _enc([EncounterActor(name="Hero", role="lead", side="player")])
    enc.situation_aspects.append(Aspect(text="Pinned Down", kind="situation", free_invokes=2))
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Notice": 3})], encounter=enc
    )

    projection = build_fate_projection(snap)
    assert "Pinned Down" in projection["scene_aspects"], (
        "live situation aspects must surface as scene_aspects for the narrator "
        f"prompt; got {projection['scene_aspects']!r}"
    )
