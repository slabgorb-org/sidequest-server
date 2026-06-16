"""Story 118-6 (ADR-144 F3f / F3d invoke) AC#1 — REROLL EXECUTION.

118-10 wired ``invoke_mode='reroll'`` over the wire, but
``ruleset.invoke_aspect`` returns 0 for reroll ("the reroll itself is the
caller's job") and ``dispatch_fate_action`` performs NO reroll — it keeps the
first 4dF roll. So a player who spends a fate point / free invocation for a
"reroll" gets a no-op, while the ``fate.aspect.invoked{mode='reroll'}`` span
reports a reroll that never happened. That is the El Dorado / Illusionism failure
the OTEL lie-detector exists to catch (CLAUDE.md OTEL Observability Principle;
SOUL "OTEL as an Illusionism detector"). The story is explicit: do NOT ship the
reroll affordance before the server actually rerolls.

These pin the fix BEHAVIORALLY — the roll itself and the span the GM panel reads
— never a source grep (CLAUDE.md "No Source-Text Wiring Tests"). Determinism
comes from a seeded ``random.Random``: the first 4dF roll consumes draws 1-4, a
reroll consumes draws 5-8, so the KEPT dice tell us whether a reroll happened and
which roll the engine honored.

RED today:
  * ``…reroll_keeps_the_second_roll`` — dispatch performs no reroll, keeps draws 1-4.
  * ``…reroll_outcome_is_attested_on_the_resolved_span`` — only the discarded
    first roll reaches ``fate.action_resolved``; the GM panel never sees the kept reroll.

GREEN GUARDS (must stay green — pin back-compat / no over-reach):
  * ``…bonus_mode_keeps_the_first_roll`` — a +2 invoke single-rolls (no spurious reroll).
  * ``…reroll_requires_an_invoke`` — ``invoke_mode='reroll'`` with no aspect to
    invoke does NOT reroll (the reroll is what the spent invocation BUYS; rerolling
    for free would be a No-Silent-Fallbacks violation in the other direction).
"""

from __future__ import annotations

import random

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_resolution import roll_4df
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import dispatch_fate_action

_RESOLVED_SPAN = "fate.action_resolved"


def _otel() -> tuple[InMemorySpanExporter, object]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _seed_with_distinct_first_two_rolls() -> tuple[
    int, tuple[int, int, int, int], tuple[int, int, int, int]
]:
    """A seed whose first two 4dF rolls differ, plus those rolls. The reroll keeps
    the SECOND; the original is the FIRST. Distinct rolls make "did a reroll
    happen, and was it honored?" a single equality check. Search is deterministic
    (no Math.random equivalent) and effectively always succeeds on the first seeds."""
    for seed in range(10_000):
        ref = random.Random(seed)
        first = roll_4df(ref)
        second = roll_4df(ref)
        if first != second:
            return seed, first, second
    raise AssertionError("no seed produced two distinct 4dF rolls (impossible)")


def _depleted_thug() -> Npc:
    sheet = FateSheet(skills={"Athletics": 0})
    for b in sheet.stress["physical"].boxes:
        b.checked = True
    for c in sheet.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    return Npc(core=CreatureCore(name="Thug", description="d", personality="p", fate_sheet=sheet))


def _hero_with_invokable_aspect() -> Character:
    """A hero with a one-free-invoke aspect so a pre-roll reroll invoke can fire
    off the free invocation (mirrors test_fate_invoke_mode_wire_118_10)."""
    core = CreatureCore(
        name="Hero",
        description="d",
        personality="p",
        fate_sheet=FateSheet(
            fate_points=2,
            skills={"Fight": 4},
            aspects=[Aspect(text="High Ground", kind="situation", free_invokes=1)],
        ),
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _plain_pc(name: str) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills={"Fight": 2})
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _pending_combat(hero: Character) -> tuple[GameSnapshot, StructuredEncounter]:
    """Hero plus an idle Ally on the player side: the hero's roll SEALS but the
    barrier stays open (Ally never commits), so ``run_fate_exchange`` does not fire.
    That isolates the hero's own rolls — the only ``fate.action_resolved`` spans are
    the hero's, with no opponent defense roll polluting the seeded draw stream
    (mirrors test_fate_dispatch_routing::test_unclosed_barrier_seals_and_pends)."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Ally", role="muscle", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ],
    )
    snap = GameSnapshot(genre_slug="fate_test", characters=[hero, _plain_pc("Ally")], encounter=enc)
    snap.npcs.append(_depleted_thug())
    return snap, enc


def _reroll_payload() -> FateActionPayload:
    return FateActionPayload(
        request_id="r1",
        action="attack",
        skill="Fight",
        target="Thug",
        invoke_aspect="High Ground",
        invoke_mode="reroll",
    )


# ---------------------------------------------------------------------------
# RED — the reroll must actually fire and be honored
# ---------------------------------------------------------------------------


def test_reroll_keeps_the_second_roll():
    """RED: ``invoke_mode='reroll'`` must re-roll the 4dF and KEEP the new result.
    With a seeded RNG the original roll is draws 1-4 and the reroll is draws 5-8;
    the acting PC's ``action_roll`` must be the reroll, not the original. Today the
    dispatch performs no reroll and keeps the original — a no-op the
    ``fate.aspect.invoked{mode='reroll'}`` span falsely reports as a reroll."""
    seed, first, second = _seed_with_distinct_first_two_rolls()
    snap, enc = _pending_combat(_hero_with_invokable_aspect())

    result = dispatch_fate_action(
        payload=_reroll_payload(),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=random.Random(seed),
    )

    assert result.action_roll is not None, "a proactive attack must produce the PC's 4dF roll"
    assert result.action_roll.dice == second, (
        "invoke_mode='reroll' must re-roll the 4dF and keep the new result; the "
        f"dispatch kept {result.action_roll.dice} — expected the reroll {second}, not "
        f"the discarded original {first}. The dispatch performs no reroll today, so a "
        "player spends a fate point / free invoke for nothing (story AC#1)."
    )
    assert result.action_roll.dice != first, (
        "the kept roll equals the ORIGINAL — no reroll occurred (the invocation was a no-op)"
    )


def test_reroll_outcome_is_attested_on_the_resolved_span():
    """RED: the GM panel reads ``fate.action_resolved``; after a reroll it must
    carry the KEPT reroll's dice, not the discarded original. Otherwise the panel
    sees a roll the player didn't keep — the reroll is Illusionism (the engine
    reports a reroll it never performed)."""
    seed, first, second = _seed_with_distinct_first_two_rolls()
    snap, enc = _pending_combat(_hero_with_invokable_aspect())
    exporter, tracer = _otel()

    dispatch_fate_action(
        payload=_reroll_payload(),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=random.Random(seed),
        _tracer=tracer,
    )

    resolved = [s for s in exporter.get_finished_spans() if s.name == _RESOLVED_SPAN]
    assert resolved, f"the resolved roll must emit {_RESOLVED_SPAN!r}"
    dice_attrs = [dict(s.attributes or {}).get("dice") for s in resolved]
    kept = ",".join(str(d) for d in second)
    assert kept in dice_attrs, (
        f"no {_RESOLVED_SPAN!r} span carried the REROLL's dice {kept!r}; the spans "
        f"carried {dice_attrs!r}. The GM panel must see the kept reroll, not the "
        "discarded first roll — a span reporting the first roll while the player "
        "rerolled is the El Dorado failure the lie-detector exists to prevent (AC#1)."
    )


# ---------------------------------------------------------------------------
# GREEN GUARDS — no spurious / free reroll
# ---------------------------------------------------------------------------


def test_bonus_mode_keeps_the_first_roll():
    """GUARD: a +2 invoke (``invoke_mode='bonus'``) single-rolls — the kept dice are
    the first roll (draws 1-4). The +2 lands on the ladder total, not the dice. Pins
    that the reroll path does not bleed into the bonus path."""
    seed, first, _second = _seed_with_distinct_first_two_rolls()
    snap, enc = _pending_combat(_hero_with_invokable_aspect())

    result = dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1",
            action="attack",
            skill="Fight",
            target="Thug",
            invoke_aspect="High Ground",
            invoke_mode="bonus",
        ),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=random.Random(seed),
    )

    assert result.action_roll is not None
    assert result.action_roll.dice == first, (
        "a +2 'bonus' invoke must keep the single original roll (draws 1-4); got "
        f"{result.action_roll.dice}, expected {first}. The bonus path must never reroll."
    )


def test_reroll_requires_an_invoke():
    """GUARD: ``invoke_mode='reroll'`` with NO ``invoke_aspect`` must NOT reroll — the
    reroll is what the spent invocation buys. Rerolling on the bare mode flag (no
    aspect invoked, nothing spent) is a free reroll: a No-Silent-Fallbacks violation
    that hands the player a benefit the economy never charged for."""
    seed, first, _second = _seed_with_distinct_first_two_rolls()
    snap, enc = _pending_combat(_hero_with_invokable_aspect())

    result = dispatch_fate_action(
        payload=FateActionPayload(
            request_id="r1",
            action="attack",
            skill="Fight",
            target="Thug",
            invoke_mode="reroll",  # no invoke_aspect → nothing is spent → no reroll
        ),
        actor_name="Hero",
        encounter=enc,
        ruleset=get_ruleset_module("fate"),
        snapshot=snap,
        rng=random.Random(seed),
    )

    assert result.action_roll is not None
    assert result.action_roll.dice == first, (
        "a reroll mode with no aspect to invoke rerolled anyway (kept "
        f"{result.action_roll.dice}, the original was {first}) — a free reroll the "
        "economy never charged for. The reroll must ride an actual invocation."
    )
