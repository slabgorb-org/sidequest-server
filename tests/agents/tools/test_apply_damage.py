"""Tests for the apply_damage tool — Phase C Task 3.

WRITE tool. The narrator says "apply HP damage" — the engine model is
ADR-078 edge/composure. This tool translates: damage amount is subtracted
from the target's ``CreatureCore.hp.current`` via ``apply_hp_delta``.
"""

from __future__ import annotations

import asyncio
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
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _character(name: str, *, edge_current: int = 10, edge_max: int = 10) -> Character:
    core = CreatureCore(
        name=name,
        description="d",
        personality="p",
        inventory=Inventory(),
        hp=HpPool(current=edge_current, max=edge_max, base_max=edge_max),
    )
    return Character(
        core=core,
        backstory="A test hero.",
        char_class="Delver",
        race="Human",
    )


def _npc(name: str, *, edge_current: int = 8, edge_max: int = 8) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=edge_current, max=edge_max, base_max=edge_max),
        ),
    )


def _build_snapshot(
    *,
    characters: list[Character] | None = None,
    npcs: list[Npc] | None = None,
) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
        characters=characters or [],
        npcs=npcs or [],
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
    """Invoke the registered handler directly (bypass dispatch span)."""
    registered = default_registry._tools["apply_damage"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


def _payload(r: ToolResult) -> dict[str, Any]:
    assert r.payload is not None
    return cast(dict[str, Any], r.payload)


def test_apply_damage_is_registered() -> None:
    assert "apply_damage" in default_registry.list_names()


async def test_damage_reduces_target_edge() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 3, "damage_type": "slashing"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == "Alice"
    assert p["amount"] == 3
    assert p["damage_type"] == "slashing"
    assert p["target_hp_after"] == 7

    # Persisted: reloading should reflect the mutation.
    reloaded = store.load()
    assert reloaded is not None
    found = reloaded.snapshot.find_creature_core("Alice")
    assert found is not None
    assert found.hp.current == 7


async def test_damage_zero_is_noop_but_returns_ok() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 0}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["amount"] == 0
    assert p["target_hp_after"] == 10
    assert p["damage_type"] == "untyped"  # default
    assert p["source"] == ""  # default


async def test_damage_targets_npc() -> None:
    # Story 158-3: damaging an NPC opponent requires a seated confrontation —
    # opponent HP is undefined outside one (ADR-116). Seat a combat encounter
    # so this stays a valid "NPC damage during combat" case. The no-encounter
    # rejection path is covered by test_apply_damage_confrontation_guard.py.
    snap = _build_snapshot(
        characters=[_character("Alice")],
        npcs=[_npc("Goblin", edge_current=8)],
    )
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        category="combat",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=0),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=0),
        actors=[
            EncounterActor(name="Alice", role="combatant", side="player"),
            EncounterActor(name="Goblin", role="combatant", side="opponent"),
        ],
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Goblin", "amount": 5, "source": "Alice's swing"}, ctx)
    assert r.status is ToolResultStatus.OK
    p = _payload(r)
    assert p["target"] == "Goblin"
    assert p["target_hp_after"] == 3
    assert p["source"] == "Alice's swing"


async def test_damage_clamps_at_zero() -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=2)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 99}, ctx)
    assert r.status is ToolResultStatus.OK
    assert _payload(r)["target_hp_after"] == 0


async def test_unknown_target_returns_not_found() -> None:
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "Nobody", "amount": 3}, ctx)
    assert r.status is ToolResultStatus.NOT_FOUND
    assert r.message is not None
    assert "Nobody" in r.message


async def test_no_active_session_returns_error() -> None:
    from tests.agents.tools.conftest import pg_empty_store

    store = pg_empty_store()
    # Note: no init_session / save — load() returns None.
    ctx = _make_ctx(store)

    r = await _call({"target": "Alice", "amount": 3}, ctx)
    assert r.status is ToolResultStatus.ERROR_FATAL
    assert r.message is not None
    assert "no active session" in r.message


async def test_negative_amount_rejected_by_args_model() -> None:
    """ge=0 constraint on the Pydantic args model surfaces as a validation error
    through the registry dispatch, not the handler."""
    snap = _build_snapshot(characters=[_character("Alice")])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-neg",
            name="apply_damage",
            arguments={"target": "Alice", "amount": -3},
        ),
        ctx,
    )
    assert out.is_error is True
    assert "argument validation failed" in out.content


async def test_otel_span_carries_damage_attrs(otel_capture) -> None:
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-otel",
            name="apply_damage",
            arguments={
                "target": "Alice",
                "amount": 4,
                "damage_type": "fire",
                "source": "lava splash",
            },
        ),
        ctx,
    )
    assert out.is_error is False
    payload = json.loads(out.content)

    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_damage"]
    assert write_spans, f"no tool.write.apply_damage span; got: {[s.name for s in spans]}"
    attrs = dict(write_spans[-1].attributes or {})
    # Dispatcher-seeded standard attrs
    assert attrs.get("tool.name") == "apply_damage"
    assert attrs.get("tool.category") == "write"
    assert attrs.get("tool.result_status") == "ok"
    # Handler-set per-tool attrs — must land on the dispatch span
    assert attrs.get("tool.damage.target") == "Alice"
    assert attrs.get("tool.damage.amount") == 4
    assert attrs.get("tool.damage.damage_type") == "fire"
    assert attrs.get("tool.damage.source") == "lava splash"
    assert attrs.get("tool.damage.target_hp_after") == 6
    assert payload["target_hp_after"] == 6


async def test_otel_span_emitted_for_zero_amount(otel_capture) -> None:
    """amount=0 still emits the span (with target_hp_after unchanged)."""
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store)

    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t-zero",
            name="apply_damage",
            arguments={"target": "Alice", "amount": 0},
        ),
        ctx,
    )
    assert out.is_error is False
    spans = otel_capture.get_finished_spans()
    write_spans = [s for s in spans if s.name == "tool.write.apply_damage"]
    assert write_spans
    attrs = dict(write_spans[-1].attributes or {})
    assert attrs.get("tool.damage.amount") == 0
    assert attrs.get("tool.damage.target_hp_after") == 10


async def test_parallel_damage_against_same_session_runs_sequentially() -> None:
    """Two concurrent apply_damage dispatches share the per-session WRITE lock
    (Phase B Registry), so they cannot overlap. Verified via persisted state:
    both reductions land cleanly (no torn read-modify-write)."""
    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    store = _store_with(snap)
    ctx = _make_ctx(store, session_id="shared-session")

    results = await asyncio.gather(
        default_registry.dispatch(
            ToolUseBlock(
                id="d1",
                name="apply_damage",
                arguments={"target": "Alice", "amount": 3},
            ),
            ctx,
        ),
        default_registry.dispatch(
            ToolUseBlock(
                id="d2",
                name="apply_damage",
                arguments={"target": "Alice", "amount": 4},
            ),
            ctx,
        ),
    )
    assert all(r.is_error is False for r in results)

    # Sequential ordering means the second invocation sees the first's write.
    # 10 - 3 - 4 = 3 — *not* 10 - 3 = 7 (which would happen if reads raced).
    reloaded = store.load()
    assert reloaded is not None
    found = reloaded.snapshot.find_creature_core("Alice")
    assert found is not None
    assert found.hp.current == 3

    # And the payloads' target_hp_after values are a serial sequence:
    payloads = sorted([json.loads(r.content)["target_hp_after"] for r in results], reverse=True)
    assert payloads == [7, 3]


# ---------------------------------------------------------------------------
# Story 166-10 (ADR-156 §6) — the coal→diamond promotion, driven END TO END.
#
# Round 1 of 166-10 renamed the seated Other's entity id to the narrator's prose
# name and shipped an enemy that could not be hit: every mechanical seam resolves
# the opponent through ``find_creature_core``, and the id no longer matched. The
# suite was green because it never left the blast radius — it drove the two
# functions that were going to change and asserted on a payload.
#
# The test that was owed and never written is one sentence long: **rename the
# enemy, then attack it.** Not "assert the resolver resolves" — actually swing,
# and watch the HP come off. These are that test. They live here because this is
# where the damage harness lives, and the damage path is the thing that broke.
# ---------------------------------------------------------------------------

_COAL = "the Scrapborn"
_PROSE = "Ihnsch of the Rusted Works"


def _seat_coal_other(snap: GameSnapshot, coal: Npc) -> None:
    """Seat ``coal`` as the lone live Other of an unresolved combat — the shape the
    ADR-156 §3.3 target-first seater leaves behind before the narrator names it."""
    snap.npcs.append(coal)
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="resolve", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Alice", role="delver", side="player"),
            EncounterActor(name=coal.core.name, role="foe", side="opponent"),
        ],
        resolved=False,
    )


def _name_the_other(snap: GameSnapshot, prose_name: str = _PROSE) -> None:
    """Drive the REAL narrator-mention path — the attach-before-mint seated-Other leg
    that performs the promotion (``sidequest.server.narration_apply``)."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.server.narration_apply import _apply_npc_mentions

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name=prose_name, role="hostile")],
        turn_num=6,
    )


def _promoted_scene() -> GameSnapshot:
    """A coal Other, seated, then named by the narrator's prose. Its alias ledger now
    holds the prose name; its canonical name and seat id are both still the coal."""
    from sidequest.game.origin import Origin, OriginKind

    snap = _build_snapshot(characters=[_character("Alice", edge_current=10)])
    coal = Npc(
        core=CreatureCore(
            name=_COAL,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=8, max=8, base_max=8),
        ),
        origin=Origin(kind=OriginKind.GENERIC, creature_id="scrapborn_raider"),
    )
    _seat_coal_other(snap, coal)
    _name_the_other(snap)
    assert coal.aliases == [_PROSE], (
        f"precondition: the prose name must attach to the Other's alias ledger; "
        f"aliases={coal.aliases!r}"
    )
    return snap


async def test_the_promoted_other_actually_takes_damage_by_its_prose_name() -> None:
    """THE TEST ROUND 1 OWED AND DID NOT WRITE.

    The world has named the enemy. So the narrator's next turn calls it "Ihnsch of
    the Rusted Works" — and hands THAT STRING to ``apply_damage(target=...)``. The
    damage has to land. Not "the resolver returns non-None": the enemy's HP has to
    actually go down, and stay down after a reload.

    This is the swing the first suite never took. It resolves the target through the
    real tool, through the real registry handler, against the real persisted store.
    """
    snap = _promoted_scene()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": _PROSE, "amount": 3, "damage_type": "crushing"}, ctx)

    assert r.status is ToolResultStatus.OK, (
        f"the narrator now knows this enemy as {_PROSE!r} and will target it by that "
        f"name — apply_damage must resolve it, not return not_found. result={r!r}"
    )
    p = _payload(r)
    assert p["target_hp_after"] == 5, (
        f"the damage must LAND: 8 - 3 = 5. A resolver that finds the creature but a "
        f"tool that damages nothing is the same bug wearing a hat. payload={p!r}"
    )

    reloaded = store.load()
    assert reloaded is not None
    # Resolved by the CANONICAL name — the id the engine has always keyed on. The
    # promotion is display-only; if the identity forked, this reads the stale twin.
    found = reloaded.snapshot.find_creature_core(_COAL)
    assert found is not None and found.hp.current == 5, (
        f"and it must persist against the SAME identity the seat is keyed on — one "
        f"enemy, one stat block, two names. hp={None if found is None else found.hp!r}"
    )


async def test_the_promoted_other_still_takes_damage_by_its_seat_id() -> None:
    """The other direction, and the one a naive rename breaks.

    Every mechanical caller — ``apply_beat``, the WN round walk, ``_primary_hp``, the
    player's own dice throw — passes the canonical SEAT ID, because that is what tag
    targets, initiative tokens and sealed commits all carry. Both names must land on
    the same creature, or half the engine stops being able to hit an enemy the player
    can see.
    """
    snap = _promoted_scene()
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": _COAL, "amount": 2}, ctx)

    assert r.status is ToolResultStatus.OK, (
        f"the seat id {_COAL!r} is what the engine's own callers pass; it must keep "
        f"resolving. result={r!r}"
    )
    assert _payload(r)["target_hp_after"] == 6, (
        f"8 - 2 = 6, on the same stat block the prose name reaches. payload={_payload(r)!r}"
    )


async def test_damage_hits_the_exactly_named_npc_not_its_fold_twin() -> None:
    """Story 166-10 widened ``find_creature_core`` to resolve through the alias
    ledger (``resolve_roster_npc``) so the narrator can target a promoted Other by its
    prose name. That widening compares NORMALIZED names on the canonical pass — so two
    roster NPCs differing only by case fold together, and the lookup returns whichever
    is FIRST IN ROSTER ORDER rather than the one actually named.

    The consequence is not an abstract resolver quirk. It is this: a mook and a boss
    are both seated in the same fight, the narrator swings at the boss ("The Courier"),
    and the 5-HP mook standing next to it eats the hit instead — while the boss walks
    away untouched at full health. Silent, deterministic, and reachable: AUTHORED NPCs
    key the Green Room's dedup gate on ``authored_id``, so it does NOT dedup them by
    name, and any pre-gate legacy save can carry any pair at all.

    Exact first, then widen.
    """
    from sidequest.game.origin import Origin, OriginKind

    lowercase = Npc(
        core=CreatureCore(
            name="the courier",
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=5, max=5, base_max=5),
        ),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="courier_generic"),
    )
    proper = Npc(
        core=CreatureCore(
            name="The Courier",
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=99, max=99, base_max=99),
        ),
        origin=Origin(kind=OriginKind.AUTHORED, authored_id="courier_named"),
    )
    # Roster ORDER matters: the lowercase twin is listed FIRST, so a normalized
    # whole-roster canonical pass returns it for either spelling.
    snap = _build_snapshot(
        characters=[_character("Alice", edge_current=10)], npcs=[lowercase, proper]
    )
    # Both are seated in the SAME fight — a mook and a boss. `apply_damage` refuses to
    # touch opponent HP outside a live confrontation (ADR-116), so the collision is
    # only reachable, and only meaningful, with the encounter seated.
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="resolve", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Alice", role="delver", side="player"),
            EncounterActor(name="the courier", role="mook", side="opponent"),
            EncounterActor(name="The Courier", role="boss", side="opponent"),
        ],
        resolved=False,
    )
    store = _store_with(snap)
    ctx = _make_ctx(store)

    r = await _call({"target": "The Courier", "amount": 4}, ctx)

    assert r.status is ToolResultStatus.OK, (
        f"precondition: the boss is seated and damageable; result={r!r}"
    )
    assert _payload(r)["target_hp_after"] == 95, (
        f"an EXACT name must damage the creature that bears it: 'The Courier' has 99 "
        f"HP, so 99 - 4 = 95. Folding it onto the lowercase 'the courier' (5 HP) "
        f"listed before it damages the WRONG NPC. payload={_payload(r)!r}"
    )

    reloaded = store.load()
    assert reloaded is not None
    bystander = next(n for n in reloaded.snapshot.npcs if n.core.name == "the courier")
    assert bystander.core.hp.current == 5, (
        f"and the fold twin must be UNTOUCHED — it was never the target. hp={bystander.core.hp!r}"
    )
