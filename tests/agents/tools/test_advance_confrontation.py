"""Tests for the advance_confrontation tool — Phase C Task 21 + Story 73-3.

WRITE tool. ADR-033 is *partial* — no formal ``Confrontation`` class
exists yet. v1 binds to :class:`StructuredEncounter`'s dual dials:
``player_metric`` and ``opponent_metric``. ``axis`` is a
``Literal["player", "opponent"]`` selector. ``confrontation_id`` is
accepted forward-compat (eventually it'll select among multiple
concurrent confrontations) but v1 always operates on
``snapshot.encounter``.

Story 73-3 — canonical-snapshot contract
----------------------------------------
The lost-update bug: the tool used to ``repository.load()`` a *fresh*
``SavedSession``, mutate the dial on that throwaway copy, and
``repository.save()`` it. But the narration pipeline runs the whole turn
against ONE canonical ``GameSnapshot`` (owned by the ``SessionRoom`` under
ADR-037). The end-of-turn ``room.save()`` writes that canonical object
*after* the tool runs — clobbering the tool's fresh-copy save. The dial
move silently reverted: the prose said "two steps closer to breaking,"
the GM panel showed a ``tool.confrontation`` span, and next turn the dial
was exactly where it started.

The fix routes the tool through the canonical in-turn snapshot
(``ToolContext.snapshot``, threaded from ``TurnContext.snapshot`` at the
``orchestrator`` construction site), mutating the same object the
end-of-turn save persists — and drops the tool's own save. These tests
encode that contract:

* The dial move must survive a *subsequent* canonical save
  (``store.save(canonical)``) — the load-bearing assertion is
  load-after-canonical-save, NOT the tool's return payload (a payload-only
  test passed against the buggy code, which is why the bug shipped).
* The tool mutates the canonical object *in place* (object identity).
* A missing canonical snapshot fails loud (``ERROR_FATAL``) — no silent
  ``repository.load()`` fallback (CLAUDE.md "No Silent Fallbacks").
* OTEL reports a ``tool.confrontation.canonical`` signal and a
  ``value_after`` that matches the persisted dial (the lie-detector now
  tells the truth).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import (
    advance_confrontation as _advance_confrontation_module,  # noqa: F401
)
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(items=[], gold=0),
        statuses=[],
        hp=HpPool(current=10, max=10, base_max=10),
    )
    return Character(
        core=core,
        backstory="bs",
        char_class="Delver",
        race="Human",
    )


def _encounter(
    *,
    player_current: int = 2,
    player_threshold: int = 10,
    opponent_current: int = 1,
    opponent_threshold: int = 10,
    encounter_type: str = "brawl",
) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type=encounter_type,
        player_metric=EncounterMetric(
            name="momentum",
            current=player_current,
            threshold=player_threshold,
        ),
        opponent_metric=EncounterMetric(
            name="menace",
            current=opponent_current,
            threshold=opponent_threshold,
        ),
        beat=0,
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=[],
        encounter=encounter,
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(
    store,
    *,
    snapshot: GameSnapshot | None = None,
    session_id: str = "s",
    genre_pack: Any = None,
) -> ToolContext:
    """Build a ToolContext carrying the canonical in-turn snapshot (Story 73-3).

    ``snapshot`` is the canonical ``GameSnapshot`` the narration pipeline holds
    and the end-of-turn save persists — NOT a fresh ``repository.load()`` copy.
    Pass the *same* object that was handed to ``_store_with`` so the test models
    production: the room owns the canonical object; the repo holds a serialized
    copy.
    """
    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=genre_pack,
        snapshot=snapshot,
    )


def _pack_with_mode(resolution_mode: str, *, confrontation_type: str = "brawl") -> Any:
    """Minimal GenrePack whose rules carry one cdef of the given mode.

    ``model_construct`` skips full-pack validation (mirrors
    tests/server/test_opposed_check_wiring.py) — only ``rules`` is
    consulted by the guard under test.
    """
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import ConfrontationDef, RulesConfig

    cdef = ConfrontationDef.model_validate(
        {
            "type": confrontation_type,
            "label": "Brawl",
            "category": "combat",
            "resolution_mode": resolution_mode,
            "opponent_default_stats": {"STR": 12},
            "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
            "opponent_metric": {"name": "menace", "starting": 0, "threshold": 10},
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
    return GenrePack.model_construct(rules=RulesConfig(confrontations=[cdef]))


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["advance_confrontation"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _otel_attrs(ctx: ToolContext) -> dict[str, Any]:
    span = cast(MagicMock, ctx.otel_span)
    return {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_advance_confrontation_is_registered() -> None:
    assert "advance_confrontation" in default_registry.list_names()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_advance_player_axis_positive_delta() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["axis"] == "player"
    assert p["delta"] == 3
    assert p["value_before"] == 2
    assert p["value_after"] == 5
    assert p["threshold"] == 10
    assert p["crossed_threshold"] is False
    assert p["metric_name"] == "momentum"
    assert p["confrontation_id"] == ""

    # Persisted through the end-of-turn canonical save (room.save()).
    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 5
    # Opponent dial unchanged.
    assert reloaded.snapshot.encounter.opponent_metric.current == 1


async def test_advance_opponent_axis_negative_delta() -> None:
    """Negative deltas are permitted — the engine doesn't clamp."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=5),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": -2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["axis"] == "opponent"
    assert p["delta"] == -2
    assert p["value_before"] == 5
    assert p["value_after"] == 3
    assert p["metric_name"] == "menace"

    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.opponent_metric.current == 3


async def test_advance_confrontation_refuses_resolved_encounter() -> None:
    """road_warrior chase bug symptom #2 (DRIVER 2026-06-04): the narrator kept
    calling advance_confrontation on an ABANDONED (resolved) chase, creeping the
    separation dial 0→2→5 with zero mechanical backing — the exact 'convincing
    narration, no engine' lie the OTEL principle exists to catch. The tool must
    refuse to mutate a resolved encounter: fail loud (recoverable), leave the
    dial untouched, and emit tool.confrontation.refused_resolved so the GM panel
    sees the refusal (No Silent Fallbacks).
    """
    enc = _encounter(player_current=2)
    enc.resolved = True
    enc.outcome = "abandoned_on_location_change"
    snap = _build_snapshot(characters=[_character("Alice")], encounter=enc)
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE, (
        f"advancing a resolved encounter's dial must fail loud (recoverable), got status={r.status}"
    )
    # The zombie dial must NOT move.
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 2, (
        "a resolved encounter's dial must stay frozen — the refused nudge "
        f"silently moved it to {snap.encounter.player_metric.current}"
    )
    attrs = _otel_attrs(ctx)
    assert attrs.get("tool.confrontation.refused_resolved") is True, (
        "the GM panel must see the refusal via tool.confrontation.refused_resolved"
    )


async def test_advance_confrontation_refuses_hp_depletion_encounter() -> None:
    """barsoom-2 playtest 2026-06-10: on a ``win_condition=hp_depletion`` combat
    the dials are inert 1e6 placeholders (the HP channel is the authoritative
    track — apply_beat suppresses dial mutation for exactly this reason), but
    the narrator free-handed advance_confrontation across the fight, drifting
    the dead dial 0→4→−2. The drift polluted forensics
    (``final_player_metric=-2``) badly enough that the DRIVER hypothesized a
    momentum sign-flip in the resolver. The tool must refuse the same way
    apply_beat suppresses: fail loud (recoverable), dial frozen,
    tool.confrontation.refused_hp_depletion on the GM panel.
    """
    enc = _encounter(player_current=0, encounter_type="combat")
    enc.win_condition = "hp_depletion"
    snap = _build_snapshot(characters=[_character("Alice")], encounter=enc)
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 4}, ctx)
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE, (
        f"advancing an inert hp_depletion dial must fail loud (recoverable), got status={r.status}"
    )
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 0, (
        "an hp_depletion dial is inert and must stay frozen — the refused nudge "
        f"silently moved it to {snap.encounter.player_metric.current}"
    )
    attrs = _otel_attrs(ctx)
    assert attrs.get("tool.confrontation.refused_hp_depletion") is True, (
        "the GM panel must see the refusal via tool.confrontation.refused_hp_depletion"
    )


async def test_confrontation_id_default_recorded_in_otel() -> None:
    """``confrontation_id=""`` is the default and is forwarded to OTEL."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["confrontation_id"] == ""

    assert _otel_attrs(ctx)["tool.confrontation.id"] == ""


async def test_confrontation_id_passthrough() -> None:
    """v1 ignores ``confrontation_id`` for selection but records it."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call(
        {"axis": "player", "delta": 1, "confrontation_id": "future-id-123"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["confrontation_id"] == "future-id-123"

    assert _otel_attrs(ctx)["tool.confrontation.id"] == "future-id-123"


# ---------------------------------------------------------------------------
# Threshold crossing
# ---------------------------------------------------------------------------


async def test_crossed_threshold_true_when_delta_pushes_past() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=8, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 4}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 8
    assert p["value_after"] == 12
    assert p["crossed_threshold"] is True

    assert _otel_attrs(ctx)["tool.confrontation.crossed_threshold"] is True


async def test_crossed_threshold_true_exactly_at_threshold() -> None:
    """Reaching ``current == threshold`` is the trigger condition."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=7, opponent_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_after"] == 10
    assert p["crossed_threshold"] is True


async def test_crossed_threshold_false_when_still_below() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["crossed_threshold"] is False


async def test_crossed_threshold_false_when_already_past() -> None:
    """If the metric was already past threshold, additional advancement
    is not a *new* crossing."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=11, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 2}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["value_before"] == 11
    assert p["value_after"] == 13
    assert p["crossed_threshold"] is False


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_no_encounter_returns_fatal_error() -> None:
    snap = _build_snapshot(characters=[_character("Alice")], encounter=None)
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active encounter" in r.message


async def test_missing_canonical_snapshot_fails_loud_no_repo_fallback() -> None:
    """AC2 (No Silent Fallbacks): the canonical snapshot is absent from the
    ToolContext, yet the repository HAS a saved session with a live encounter.

    The tool must NOT silently ``repository.load()`` that encounter and succeed —
    a silent fallback re-introduces the exact lost-update this story fixes
    (it would mutate a fresh copy the end-of-turn save then clobbers). It must
    fail loud with ``ERROR_FATAL``.
    """
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2),
    )
    store = _store_with(snap)  # repo HAS a session + encounter...
    ctx = _make_ctx(store, snapshot=None)  # ...but no canonical snapshot

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None

    # And it did not silently mutate-and-persist via a fresh load.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 2


async def test_invalid_axis_rejected_by_args_model() -> None:
    """``Literal["player", "opponent"]`` rejects other strings; the
    validation error surfaces as a recoverable dispatch error."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-bad-axis",
            name="advance_confrontation",
            arguments={"axis": "neutral", "delta": 1},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


# ---------------------------------------------------------------------------
# OTEL attributes
# ---------------------------------------------------------------------------


async def test_otel_attrs_set_on_success() -> None:
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=4, player_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call(
        {
            "confrontation_id": "fight-7",
            "axis": "player",
            "delta": 2,
            "reason": "feint succeeds",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    recorded = _otel_attrs(ctx)
    assert recorded["tool.confrontation.id"] == "fight-7"
    assert recorded["tool.confrontation.axis"] == "player"
    assert recorded["tool.confrontation.delta"] == 2
    assert recorded["tool.confrontation.value_after"] == 6
    assert recorded["tool.confrontation.reason"] == "feint succeeds"
    assert recorded["tool.confrontation.crossed_threshold"] is False
    # Story 73-3: the canonical-mutation signal (distinguishes the fixed path
    # from the old fresh-load path).
    assert recorded["tool.confrontation.canonical"] is True


# ---------------------------------------------------------------------------
# Concurrency — sequential WRITE-lock
# ---------------------------------------------------------------------------


async def test_parallel_advance_against_same_session_runs_sequentially() -> None:
    """Two concurrent dispatches share the per-session WRITE lock AND the same
    canonical snapshot — the second call accumulates on the first's in-place
    mutation, not the initial value. The composed total survives one
    end-of-turn canonical save."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=0, player_threshold=100),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="d1",
                name="advance_confrontation",
                arguments={"axis": "player", "delta": 3},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="d2",
                name="advance_confrontation",
                arguments={"axis": "player", "delta": 5},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # Sequential ordering: 0 → 3 → 8, accumulated in-place on the canonical
    # snapshot, then persisted by the single end-of-turn save.
    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 8

    # The two payloads form a serial sequence by value_after:
    after_values = sorted([json.loads(r.content)["value_after"] for r in results])
    assert after_values == [3, 8]


# ---------------------------------------------------------------------------
# Story 73-3 — canonical-snapshot lost-update fix
# ---------------------------------------------------------------------------


async def test_advance_survives_end_of_turn_canonical_save() -> None:
    """AC1 (the lost-update regression). The dial move must survive a
    subsequent end-of-turn canonical save.

    Against the buggy code the tool mutates a fresh ``repository.load()`` copy
    and saves it; the canonical ``snap`` is never touched, so
    ``store.save(snap)`` writes the *original* value back over the tool's write
    and the dial reverts. The load-after-canonical-save assertion is what
    catches this — a return-payload-only assertion passes against the bug.
    """
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=3, opponent_threshold=20),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": 4}, ctx)
    assert r.status is ToolResultStatus.OK

    # End-of-turn canonical save (room.save() / repository.save(canonical)),
    # which previously clobbered the tool's fresh-copy write.
    store.save(snap)

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.opponent_metric.current == 7  # 3 + 4, NOT 3


async def test_negative_advance_survives_end_of_turn_canonical_save() -> None:
    """AC1 symmetry — a negative ("regroup"/"de-escalate") delta also persists
    canonically through the end-of-turn save."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=6, opponent_threshold=20),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": -2}, ctx)
    assert r.status is ToolResultStatus.OK

    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.opponent_metric.current == 4  # 6 - 2


async def test_mutates_canonical_snapshot_in_place() -> None:
    """AC2 — the tool mutates the canonical in-turn snapshot object the pipeline
    holds (mutation-in-place / object identity), BEFORE any save. The fresh-load
    code path leaves the canonical object untouched."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK

    # The SAME object the pipeline holds reflects the change — no save needed.
    assert snap.encounter is not None
    assert snap.encounter.player_metric.current == 5


async def test_sequential_same_axis_advances_compose() -> None:
    """AC3 — two same-axis advances in one turn accumulate on the canonical
    snapshot and the cumulative total persists through one end-of-turn save."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=0, opponent_threshold=100),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    assert (await _call({"axis": "opponent", "delta": 2}, ctx)).status is ToolResultStatus.OK
    assert (await _call({"axis": "opponent", "delta": 3}, ctx)).status is ToolResultStatus.OK

    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.opponent_metric.current == 5  # 0 + 2 + 3


async def test_sequential_mixed_axis_advances_both_persist() -> None:
    """AC3 — advancing player then opponent in one turn leaves both deltas
    intact on the canonical snapshot (no axis clobbers the other)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2, opponent_current=1),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    assert (await _call({"axis": "player", "delta": 2}, ctx)).status is ToolResultStatus.OK
    assert (await _call({"axis": "opponent", "delta": 1}, ctx)).status is ToolResultStatus.OK

    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    assert reloaded.snapshot.encounter.player_metric.current == 4  # 2 + 2
    assert reloaded.snapshot.encounter.opponent_metric.current == 2  # 1 + 1


async def test_advance_and_resolve_same_turn_both_persist() -> None:
    """AC4 — when an advance crosses a threshold and the encounter resolves that
    same turn, BOTH the crossing dial value and the resolved state persist
    through the single end-of-turn canonical save (the resolution reads the
    canonical dial the tool moved, and the final dial is not clobbered)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=8, opponent_threshold=10),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": 3}, ctx)  # 8 -> 11, crosses
    assert r.status is ToolResultStatus.OK
    assert _payload(r)["crossed_threshold"] is True

    # Resolution fires this same turn on the canonical snapshot, as the
    # narration-apply pipeline would when the dial reaches threshold.
    assert snap.encounter is not None
    snap.encounter.resolved = True
    snap.encounter.outcome = "opponent_victory"

    store.save(snap)  # single end-of-turn canonical save

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    enc = reloaded.snapshot.encounter
    assert enc.opponent_metric.current == 11  # crossing dial value, not clobbered
    assert enc.resolved is True
    assert enc.outcome == "opponent_victory"


# ---------------------------------------------------------------------------
# RW-2 (playtest 2026-06-05, the_circuit chase) — opposed_check guard
# ---------------------------------------------------------------------------
#
# With beat_selections zeroed on the SDK path, ALL dial movement in the chase
# came from the narrator free-handing this tool with invented deltas (PG
# telemetry: only phase-`advanced` events "moved the dial without a beat";
# ZERO opposed_roll_resolved all session). On an opposed_check confrontation
# the dice engine — not the narrator — owns the deltas: the narrator's job is
# to emit the OPPONENT's beat_selection, which the resolver pairs with the
# player's stashed d20. The tool must refuse the mode (recoverable, loud).


async def test_refuses_opposed_check_confrontation() -> None:
    enc = _encounter(player_current=2, encounter_type="brawl")
    snap = _build_snapshot(characters=[_character("Alice")], encounter=enc)
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap, genre_pack=_pack_with_mode("opposed_check"))

    r = await _call({"axis": "opponent", "delta": 2}, ctx)
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE, (
        "advance_confrontation must refuse an opposed_check confrontation — "
        f"the dice engine owns those deltas; got status={r.status}"
    )
    # The dial must NOT move on a narrator free-hand.
    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 1, (
        "refused advance silently moved the opposed_check dial to "
        f"{snap.encounter.opponent_metric.current}"
    )
    # The refusal steers the narrator to the sanctioned channel.
    assert r.message is not None
    assert "beat_selection" in r.message
    # GM-panel visibility for the refusal (No Silent Fallbacks).
    attrs = _otel_attrs(ctx)
    assert attrs.get("tool.confrontation.refused_opposed_check") is True


async def test_allows_non_opposed_confrontation_with_pack() -> None:
    """beat_selection mode (legacy dial engine) keeps working with a pack wired."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2, encounter_type="brawl"),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap, genre_pack=_pack_with_mode("beat_selection"))

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.OK
    assert _payload(r)["value_after"] == 5


async def test_allows_when_encounter_type_not_in_pack() -> None:
    """An encounter type with no matching cdef cannot be mode-checked — the
    guard stands down (the dial engine remains the narrator's channel)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2, encounter_type="unlisted_type"),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap, genre_pack=_pack_with_mode("opposed_check"))

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.OK


async def test_allows_when_genre_pack_none() -> None:
    """Legacy fixtures construct ToolContext without a genre_pack — the guard
    must tolerate None (every pre-existing test in this file runs that way)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(player_current=2),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap, genre_pack=None)

    r = await _call({"axis": "player", "delta": 1}, ctx)
    assert r.status is ToolResultStatus.OK


# ---------------------------------------------------------------------------
# RW-2 — GM-timeline watcher event on narrator-driven dial moves
# ---------------------------------------------------------------------------
#
# The DRIVER could not attribute the pursuit 4→5 tick: the tool's OTEL span
# attrs exist, but no watcher state_transition reaches the GM timeline, so a
# narrator-driven dial move is invisible next to the engine's own events.


async def test_successful_advance_publishes_watcher_state_transition(
    monkeypatch,
) -> None:
    from sidequest.agents.tools import advance_confrontation as ac_module

    published: list[tuple[str, dict, dict]] = []

    def _capture(event_type: str, fields: dict, **kwargs: Any) -> None:
        published.append((event_type, fields, kwargs))

    monkeypatch.setattr(ac_module, "_watcher_publish", _capture)

    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=4, encounter_type="brawl"),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call(
        {"axis": "opponent", "delta": 1, "reason": "they're closing"},
        ctx,
    )
    assert r.status is ToolResultStatus.OK

    assert len(published) == 1, "a successful advance must publish exactly one watcher event"
    event_type, fields, kwargs = published[0]
    assert event_type == "state_transition"
    assert fields["op"] == "narrator_dial_advance"
    assert fields["axis"] == "opponent"
    assert fields["delta"] == 1
    assert fields["reason"] == "they're closing"
    assert fields["value_before"] == 4
    assert fields["value_after"] == 5
    assert fields["encounter_type"] == "brawl"
    assert kwargs.get("component") == "encounter"


async def test_refused_advance_does_not_publish_dial_advance_event(
    monkeypatch,
) -> None:
    """Refusals (resolved encounter / opposed_check) must not emit a
    narrator_dial_advance event — the dial did not move."""
    from sidequest.agents.tools import advance_confrontation as ac_module

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ac_module,
        "_watcher_publish",
        lambda event_type, fields, **kw: published.append((event_type, fields)),
    )

    enc = _encounter(player_current=2)
    enc.resolved = True
    enc.outcome = "abandoned_on_location_change"
    snap = _build_snapshot(characters=[_character("Alice")], encounter=enc)
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "player", "delta": 3}, ctx)
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert published == []


async def test_otel_reports_canonical_persisted_delta() -> None:
    """AC5 — the emitted span reports a ``tool.confrontation.canonical`` signal
    and a ``value_after`` that matches the dial actually persisted to the
    canonical snapshot. The GM panel can now verify the advance is real,
    closing the "span fires but the write is lost" lie."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        encounter=_encounter(opponent_current=4, opponent_threshold=20),
    )
    store = _store_with(snap)
    ctx = _make_ctx(store, snapshot=snap)

    r = await _call({"axis": "opponent", "delta": 3, "reason": "composure cracks"}, ctx)
    assert r.status is ToolResultStatus.OK

    store.save(snap)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.encounter is not None
    persisted = reloaded.snapshot.encounter.opponent_metric.current

    attrs = _otel_attrs(ctx)
    # Signal that distinguishes the canonical-mutation path from the old
    # fresh-load path.
    assert attrs["tool.confrontation.canonical"] is True
    # value_after matches the persisted dial, and the intended delta landed.
    assert attrs["tool.confrontation.value_after"] == persisted
    assert attrs["tool.confrontation.value_after"] - 4 == 3
