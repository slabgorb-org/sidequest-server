"""RED (story 126-30): de-nativize Fate confrontation SEATING.

Keith ruling 2026-06-19 (sq-playtest 150-1/150-2, "[ENGINE/CRUNCH] De-nativize Fate
confrontation SEATING"): under ``ruleset == 'fate'`` a standoff/conflict must seat as
a pure Fate contest/conflict — 4dF + ladder, ablative stress — and MUST NOT seat or
feed the native ``opponent_metric.tension`` dial / beat track. The win/progress signal
is the opponent's stress + consequence fill toward taken-out (ADR-143/144 "Bind the
Ruleset, Don't Balance It"), NOT the vestigial dial (observed live at 6/10 in play).

This is the UPSTREAM half of the cleanup #964 only partially did: #964 gated the native
ConfrontationOverlay PAYLOAD from co-rendering with FATE_STATE; this story de-nativizes
the SEATING in ``instantiate_encounter_from_trigger``.

Ground truth (empirically confirmed against the real spaghetti_western standoff shape —
``category: pre_combat`` + native ``tension`` dials @ threshold 10 + native beats with
``stat_check``):

    seat under ruleset='fate'  ->  enc.win_condition == 'dial_threshold', enc.contest is None
                                   (the native tension dial IS the win track — the bug)
    seat under ruleset='dial'  ->  identical native dial track (correct for a native pack)

The seating combat block in encounter_lifecycle.py (``if cdef.category == 'combat':``)
and the StructuredEncounter dial stamping branch on ``cdef.category``/``win_condition``
but NOT on ``pack.rules.ruleset`` — so a Fate pack inherits the native dial.

HOW-agnostic assertion: a faithful fix removes the native dial from the Fate path by
EITHER routing the seat to a Fate Contest (``enc.contest is not None``) OR moving the
win condition off the native ``dial_threshold``. The test fails iff the seat is left on
the bare native-dial track (``win_condition == 'dial_threshold' AND contest is None``),
which is exactly today's Fate state. The regression pin proves the change is
ruleset-gated: a native pack KEEPS its dial track untouched.

Span capture mirrors test_fate_contest.py::test_seating_stamps_contest_state_and_emits_span
(init_tracer + a fresh InMemorySpanExporter installed on the global provider immediately
before the seat, so only the seat's spans are captured).
"""

from __future__ import annotations

from types import SimpleNamespace

import opentelemetry.trace as otel_trace
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate import FateRulesetModule
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.models.rules import (
    BeatDef,
    BeatKind,
    ConfrontationDef,
    FateConfig,
    MetricDef,
    ResolutionMode,
    RulesConfig,
    WinCondition,
)
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger
from sidequest.telemetry.setup import init_tracer

# A credible peer gunhand ladder (the genre's OWN skills — "Bind the Ruleset").
_SKILLS = {"Shoot": 4, "Fight": 2, "Provoke": 3, "Athletics": 1, "Will": 2, "Notice": 3}


def _native_standoff_cdef() -> ConfrontationDef:
    """A standoff in the NATIVE dial shape: pre_combat category, native ascending
    ``tension`` dials, and native beats carrying ``stat_check`` (the dial surface).
    Legal only on a NON-Fate (dial) pack — story 153-3's loud guard rejects a native
    ``beat_selection`` confrontation on a Fate pack, so the Fate fixture below is a
    ``conflict``-mode def instead (which seats through the same Fate-conflict path)."""
    return ConfrontationDef(
        type="standoff",
        label="Standoff",
        category="pre_combat",
        player_metric=MetricDef(name="tension", starting=0, threshold=10),
        opponent_metric=MetricDef(name="tension", starting=0, threshold=10),
        beats=[
            BeatDef(
                id="size_up",
                label="Size Up",
                kind=BeatKind.angle,
                target_tag="Opponent Read",
                stat_check="CUNNING",
            ),
            BeatDef(id="draw", label="Draw", kind=BeatKind.push, stat_check="DRAW"),
        ],
    )


def _fate_standoff_cdef() -> ConfrontationDef:
    """A standoff authored as a Fate Conflict (story 153-3): ``resolution_mode:
    conflict``, display-only beats, no native dial metrics. Seats through the same
    ``seat_as_fate_conflict`` path the native beat_selection def used to (the
    de-nativization invariant this file pins is unchanged) — but it is a VALID Fate
    def under the loud Fate-mode guard."""
    return ConfrontationDef(
        type="standoff",
        label="Standoff",
        category="pre_combat",
        resolution_mode=ResolutionMode.conflict,
        beats=[BeatDef(id="size_up", label="Size Up"), BeatDef(id="draw", label="Draw")],
    )


def _fate_pack() -> SimpleNamespace:
    return SimpleNamespace(
        rules=RulesConfig(
            ruleset="fate",
            fate=FateConfig(skills=dict(_SKILLS), refresh=3),
            confrontations=[_fate_standoff_cdef()],
        )
    )


def _dial_pack() -> SimpleNamespace:
    return SimpleNamespace(
        rules=RulesConfig(ruleset="dial", confrontations=[_native_standoff_cdef()])
    )


def _snapshot() -> GameSnapshot:
    pc = Character(
        core=CreatureCore(name="Reb", description="d", personality="p"),
        char_class="Agent",
        race="Human",
        backstory="b",
    )
    foe = Npc(core=CreatureCore(name="Foe", description="d", personality="p"))
    return GameSnapshot(genre_slug="fate_seat_test", characters=[pc], npcs=[foe])


def _seat(pack: SimpleNamespace):
    """Seat the standoff and return (enc, span_names:set[str], snapshot).

    Installs a fresh exporter on the global provider immediately before the seat so the
    captured spans are exactly the seat's (idempotent init_tracer; same pattern as
    test_fate_contest.py::test_seating_stamps_contest_state_and_emits_span).
    """
    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    snap = _snapshot()
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,  # type: ignore[arg-type]
        encounter_type="standoff",
        player_name="Reb",
        npcs_present=[NpcMention(name="Foe", side="opponent", role="foe")],
        genre_slug="fate_seat_test",
    )
    assert enc is not None, "instantiate_encounter_from_trigger returned None"
    names = {s.name for s in exporter.get_finished_spans()}
    return enc, names, snap


# ---------------------------------------------------------------------------
# 1 — RED (AC-1, AC-2, AC-4): a Fate standoff must not seat the native dial track.
# ---------------------------------------------------------------------------


def test_fate_standoff_seat_removes_native_tension_dial() -> None:
    """AC-1/AC-2: under Fate, the seat must NOT leave the encounter on the bare native
    dial track. A faithful fix EITHER stamps a Fate Contest (enc.contest not None) OR
    moves the win condition off the native ``dial_threshold``. Today the Fate standoff
    seats with win_condition='dial_threshold' AND contest=None — the vestigial native
    dial the ruling says to remove — so this fails (RED).
    """
    enc, names, _snap = _seat(_fate_pack())

    on_native_dial_track = (
        enc.win_condition == WinCondition.dial_threshold.value and enc.contest is None
    )
    assert not on_native_dial_track, (
        "Fate standoff seated on the native tension-dial track "
        f"(win_condition={enc.win_condition!r}, contest={enc.contest!r}); ADR-143/144 "
        "requires the native opponent_metric.tension dial be REMOVED from the Fate path "
        "— seat it as a Fate Contest/Conflict (4dF + ladder, ablative stress), not the dial."
    )

    # AC-4: the Fate substrate IS engaged at seating (the opponent is given a FateSheet),
    # so the win signal has a home — this proves the seat went Fate, not merely 'no dial'.
    assert "fate.opponent.seeded" in names, (
        f"fate.opponent.seeded must fire for a Fate seat; got spans: {sorted(names)}"
    )

    # AC-4 (Dev marker-tightening, story 126-30): the green-phase marker is
    # win_condition='fate_conflict' + the de-nativization span ``fate.conflict.seeded``
    # (the GM-panel lie-detector that the native tension dial was REMOVED at seating, the
    # upstream sibling of ``fate.contest.seeded``). Assert the chosen marker fired — this
    # is the OTEL wiring test for the seating de-nativization (CLAUDE.md "Every Test Suite
    # Needs a Wiring Test" / OTEL Observability Principle). TEA's RED was HOW-agnostic and
    # invited this tightening once a concrete marker was picked.
    assert enc.win_condition == "fate_conflict", (
        "the de-nativized Fate standoff must carry the engine-only 'fate_conflict' win "
        f"track (the native dial removed); got win_condition={enc.win_condition!r}"
    )
    assert "fate.conflict.seeded" in names, (
        f"fate.conflict.seeded (the de-nativization lie-detector) must fire for a Fate "
        f"standoff seat; got spans: {sorted(names)}"
    )


def test_fate_standoff_win_signal_is_opponent_stress_not_dial() -> None:
    """AC-2: the win/progress signal under Fate is the opponent's stress + consequence
    fill toward taken-out — so the seated opponent must carry a FateSheet with real
    stress tracks (the substrate the win meter reads), NOT a native HP/tension dial.
    """
    _enc, _names, snap = _seat(_fate_pack())
    opp = snap.find_creature_core("Foe")
    assert opp is not None and opp.fate_sheet is not None, (
        "Fate seat must give the opponent a FateSheet (the taken-out win substrate)"
    )
    assert set(opp.fate_sheet.stress) >= {"physical", "mental"}, (
        "the opponent's FateSheet must carry the SRD stress tracks the win meter reads; "
        f"got stress tracks: {sorted(opp.fate_sheet.stress)}"
    )


# ---------------------------------------------------------------------------
# 2 — PIN (the watch-out): a NON-Fate pack keeps its native dial untouched.
# ---------------------------------------------------------------------------


def test_native_pack_standoff_keeps_native_dial_track() -> None:
    """Regression guard: the de-nativization MUST be ``ruleset == 'fate'``-gated, never
    a blanket removal. A native (dial) pack's standoff still seats the native tension
    dial (win_condition='dial_threshold', no Fate contest, no Fate opponent seeding).
    Passes before AND after the fix — it pins that native seating is unchanged.
    """
    enc, names, snap = _seat(_dial_pack())

    assert enc.win_condition == WinCondition.dial_threshold.value, (
        "a native pack standoff must keep its dial_threshold win condition; "
        f"got {enc.win_condition!r}"
    )
    assert enc.contest is None, "a native dial standoff must not be stamped as a Fate Contest"
    assert "fate.opponent.seeded" not in names, (
        "the Fate opponent sweep must be a no-op off a Fate pack (it seeds native packs "
        f"elsewhere); got spans: {sorted(names)}"
    )
    # The de-nativization (and its span) MUST be ruleset-gated — a native pack never
    # de-nativizes, so ``fate.conflict.seeded`` must NOT fire off a non-Fate pack.
    assert "fate.conflict.seeded" not in names, (
        "fate.conflict.seeded must be ruleset-gated (Fate only) — it must never fire for a "
        f"native (dial) pack; got spans: {sorted(names)}"
    )
    # The opponent stays a native creature — no FateSheet fabricated on a non-Fate pack.
    foe_core = snap.find_creature_core("Foe")
    assert foe_core is not None and foe_core.fate_sheet is None


# ---------------------------------------------------------------------------
# 3 — PIN (AC-3): the ADR-144 No-Silent-Fallbacks guard stays intact.
# ---------------------------------------------------------------------------


def test_fate_compute_dc_guard_preserved() -> None:
    """AC-3: de-nativizing seating must NOT relax the ``compute_dc`` tripwire — the Fate
    ruleset resolves via the 4dF conflict engine, never the d20/beat DC surface. If a
    Fate seat ever reaches compute_dc, this guard is the loud failure (ADR-144).
    """
    module = get_ruleset_module("fate")
    assert isinstance(module, FateRulesetModule)
    with pytest.raises(NotImplementedError):
        module.compute_dc(beat=None)  # type: ignore[arg-type]  # guard fires regardless of input
