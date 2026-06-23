"""RED tests for Story 158-3 — block narrator apply-path opponent-HP writes
when no confrontation is seated (the "no-encounter case").

Finding (sq-playtest 2026-06-22, beneath_sunden): during a no-encounter
narrative beat the narrator narrated damage to an enemy that exists only in
prose, and ``apply_damage`` happily decremented an opponent's HP with ZERO
mechanical backing. Per the OTEL lie-detector principle (CLAUDE.md), a changed
HP field with no seated confrontation reads as a fired mechanic when nothing
fired. ADR-116 ("A Confrontation Requires an Other") makes opponent HP
*undefined* outside a confrontation — there is no Other to damage.

``apply_damage`` is the narrator's freeform/environmental damage path (see the
tool docstring). The guard this story adds:

  * Damage to a NON-player creature (an NPC — the potential "Other") is
    REJECTED when ``snapshot.encounter is None``. The write does not land and a
    warning OTEL span fires (``tool.damage.guard_rejected = True`` plus a
    reason) so the GM panel sees the rejection.
  * Damage to a player ``Character`` is ALWAYS allowed even with no encounter —
    that is the documented environmental/hazard path (a trap, a fall). Player
    HP is explicitly out of scope for this story.
  * During a seated confrontation (``snapshot.encounter is not None``) NPC
    damage is ACCEPTED and updates HP as before, with an info OTEL span
    (``tool.damage.guard_rejected = False``). No regression on legal writes.

The warning-vs-info distinction the AC asks for maps onto this codebase's idiom
as ``tool.damage.guard_rejected`` (bool) + ``tool.result_status``
(``error_recoverable`` on reject, ``ok`` on accept) — spans here carry
attributes + result status, not a free-standing log level.

These tests drive the guard through ``default_registry.dispatch`` (the real
production path the narrator's tool-use takes), so the suite doubles as the
wiring test required by CLAUDE.md.
"""

from __future__ import annotations

import json
from typing import Any, cast

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools import apply_damage as _apply_damage_module  # noqa: F401
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _character(name: str, *, hp_current: int = 10, hp_max: int = 10) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
        ),
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


def _npc(name: str, *, hp_current: int = 8, hp_max: int = 8) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
        ),
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    npcs: list[Npc] | None = None,
    encounter: StructuredEncounter | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=npcs or [],
        encounter=encounter,
    )


def _combat_encounter(*, player: str, opponent: str) -> StructuredEncounter:
    """A minimal seated combat confrontation with one PC and one opponent NPC.

    The guard only inspects ``snapshot.encounter is not None``; the metrics are
    inert placeholders kept valid for the model.
    """
    return StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        category="combat",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=0),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=0),
        actors=[
            EncounterActor(name=player, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
    )


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _make_ctx(store, *, session_id: str = "s") -> ToolContext:
    from unittest.mock import MagicMock

    return ToolContext(
        world_id="w",
        session_id=session_id,
        perspective_pc="Alice",
        turn_number=1,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Invoke the registered handler directly (bypass the dispatch span)."""
    registered = default_registry._tools["apply_damage"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def _hp(store, name: str) -> int:
    reloaded = store.load()
    assert reloaded is not None
    core = reloaded.snapshot.find_creature_core(name)
    assert core is not None
    return core.hp.current


def _latest_damage_span(otel_capture) -> dict[str, Any]:
    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_damage"]
    assert write_spans, f"no tool.write.apply_damage span; got: {[s.name for s in spans]}"
    return dict(write_spans[-1].attributes or {})


# --------------------------------------------------------------------------
# AC-1: guard blocks unbacked opponent (NPC) writes when no confrontation
# --------------------------------------------------------------------------


async def test_npc_damage_rejected_without_confrontation() -> None:
    """Damaging an NPC (the potential Other) with no seated confrontation is
    rejected — the unbacked opponent-HP write must not land."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=8)],
        encounter=None,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Goblin", "amount": 5, "source": "prose swing"}, ctx)

    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    assert "confrontation" in r.message.lower() or "encounter" in r.message.lower()
    # HP is untouched: no opponent exists to take the damage.
    assert _hp(store, "Goblin") == 8


# --------------------------------------------------------------------------
# Boundary: player-state environmental damage stays legal (out of scope to block)
# --------------------------------------------------------------------------


async def test_pc_environmental_damage_allowed_without_confrontation() -> None:
    """Damage to a player Character with no encounter is the documented
    freeform/environmental path (trap, fall) — it must keep working. Player HP
    is explicitly out of scope for the opponent-write guard."""
    snap = _build_snapshot(characters=[_character("Alice", hp_current=10)], encounter=None)
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 3, "source": "rockfall"}, ctx)

    assert r.status is ToolResultStatus.OK
    assert _payload(r)["target_hp_after"] == 7
    assert _hp(store, "Alice") == 7


# --------------------------------------------------------------------------
# AC-3: no regression — legal opponent writes during a seated confrontation
# --------------------------------------------------------------------------


async def test_npc_damage_accepted_with_confrontation() -> None:
    """During a seated combat confrontation, damaging the opponent NPC works
    end-to-end exactly as before the guard."""
    enc = _combat_encounter(player="Alice", opponent="Goblin")
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=8)],
        encounter=enc,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Goblin", "amount": 5, "source": "Alice's blade"}, ctx)

    assert r.status is ToolResultStatus.OK
    assert _payload(r)["target_hp_after"] == 3
    assert _hp(store, "Goblin") == 3


# --------------------------------------------------------------------------
# AC-2: OTEL observability for every opponent-state decision (via real dispatch)
# --------------------------------------------------------------------------


async def test_rejected_npc_write_emits_warning_span(otel_capture) -> None:
    """A rejected opponent write fires a warning span the GM panel can read:
    guard_rejected=True, a reason, the attempted write spec, and an error
    result_status. Driven through default_registry.dispatch (production path)."""
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=8)],
        encounter=None,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-reject",
            name="apply_damage",
            arguments={"target": "Goblin", "amount": 5},
        ),
        ctx,
    )
    assert out.is_error is True

    attrs = _latest_damage_span(otel_capture)
    assert attrs.get("tool.damage.guard_rejected") is True
    assert attrs.get("tool.damage.guard_reason") == "no_confrontation_seated"
    # The attempted write spec is recorded so the GM panel sees what was blocked.
    assert attrs.get("tool.damage.target") == "Goblin"
    assert attrs.get("tool.damage.amount") == 5
    # Warning-level signal in this codebase's idiom: an error result status.
    assert attrs.get("tool.result_status") == "error_recoverable"
    # And nothing was written.
    assert _hp(store, "Goblin") == 8


async def test_accepted_npc_write_emits_info_span(otel_capture) -> None:
    """An accepted opponent write (confrontation seated) fires an info span:
    guard_rejected=False, the post-write HP, and an ok result_status."""
    enc = _combat_encounter(player="Alice", opponent="Goblin")
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=8)],
        encounter=enc,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-accept",
            name="apply_damage",
            arguments={"target": "Goblin", "amount": 5},
        ),
        ctx,
    )
    assert out.is_error is False

    attrs = _latest_damage_span(otel_capture)
    assert attrs.get("tool.damage.guard_rejected") is False
    assert attrs.get("tool.damage.target_hp_after") == 3
    assert attrs.get("tool.result_status") == "ok"
    assert _hp(store, "Goblin") == 3


# --------------------------------------------------------------------------
# AC-4: repro-level end-to-end — no-encounter beat then combat beat (wiring)
# --------------------------------------------------------------------------


async def test_repro_no_encounter_then_combat_beat(otel_capture) -> None:
    """The full repro from the finding, through the real dispatch path:

    (a) a no-encounter beat where the narrator tries to damage an NPC is
        rejected and fires a warning span;
    (b) opponent HP is unchanged after that beat;
    (c) once a confrontation is seated, the same write is accepted and fires an
        info span;
    (d) opponent HP is updated after the combat beat.
    """
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", hp_current=8)],
        encounter=None,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    # --- No-encounter beat: rejected, warning span, HP unchanged ---
    out1 = await default_registry.dispatch(
        ToolUseBlock(
            id="repro-1",
            name="apply_damage",
            arguments={"target": "Goblin", "amount": 4},
        ),
        ctx,
    )
    assert out1.is_error is True  # (a)
    attrs1 = _latest_damage_span(otel_capture)
    assert attrs1.get("tool.damage.guard_rejected") is True  # (a)
    assert attrs1.get("tool.damage.guard_reason") == "no_confrontation_seated"
    assert _hp(store, "Goblin") == 8  # (b)

    # --- Seat a confrontation, then re-run the same write ---
    loaded = store.load()
    assert loaded is not None
    loaded.snapshot.encounter = _combat_encounter(player="Alice", opponent="Goblin")
    store.save(loaded.snapshot)

    out2 = await default_registry.dispatch(
        ToolUseBlock(
            id="repro-2",
            name="apply_damage",
            arguments={"target": "Goblin", "amount": 4},
        ),
        ctx,
    )
    assert out2.is_error is False  # (c)
    attrs2 = _latest_damage_span(otel_capture)
    assert attrs2.get("tool.damage.guard_rejected") is False  # (c)
    assert json.loads(out2.content)["target_hp_after"] == 4
    assert _hp(store, "Goblin") == 4  # (d)
