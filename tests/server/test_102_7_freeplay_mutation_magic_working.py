"""Story 102-7 RED — explicit free-play mutation use routes magic_working to
the AWN mutation engine (the 102-3 cast-spine mirror for mutant_wasteland).

THE BUG (epic 102 gap #3 applied to AWN): a player types "I use my Acid Spit
on the raider" in free play in an awn world. The intent router classifies it
``magic_working`` ("spell or magical ability usage"), but the dispatch can
never mechanically engage:

  1. The precondition gate keys structural liveness on ``magic_state`` (the
     retired ADR-126 plugin) and — since 102-3 — on a WWN ``core.spellcasting``
     surface. An awn world has NEITHER: its magic is ``snapshot.mutation_state``
     (AWN Plan 2, server PR #781). The dispatch is dropped and the narrator
     improvises the mutation with zero mechanical backing — no Strain, no
     usage limit, no ``awn.mutation.*`` span. Illusionism, undetected.
  2. Even unGated, ``run_magic_working_dispatch`` only knows the pact-working
     path and the WWN cast spine — there is no route to
     ``sidequest.mutation.use_ops.use_mutation`` (the existing, tested engine).

THE CONTRACT THESE TESTS PIN (mirrors 102-3's, which these tests reuse
deliberately — same param shape the router already emits):

  * Param contract: ``params={"actor": <name>, "spell": <the working AS THE
    PLAYER TYPED IT>}``. On an awn-surface snapshot the handler resolves the
    typed text against the pack's MUTATION catalog — by id
    ("exotic/acid_spit") or display name ("Acid Spit") — and the actor's
    owned list.
  * Engagement: routes to ``use_mutation`` — ``awn.mutation.used`` fires, the
    Strain cost lands on the PC pool, the usage counter ticks, and the bank
    output carries at least one narrator directive.
  * Refusal is engagement: exhausted usage → ``awn.mutation.refused``
    (``limit_exhausted``), no Strain, no
    ``dispatch_engagement.magic_working.mismatch``.
  * Unknown working → failed premise: loud refusal evidence, no state change,
    never improv-with-no-cost.
  * Native-pack safety: the same dispatch in a native genre emits zero
    ``awn.*`` spans and does not crash.

Test discipline (102-3 precedent): the router is STUBBED everywhere here —
classification and dispatch-routing are separate seams.

CLAUDE.md rule coverage:
  * "Every Test Suite Needs a Wiring Test" —
    ``test_pre_narrator_pass_routes_freeplay_mutation`` drives the real
    ``execute_intent_router_pre_narrator_pass`` (real gates, real bank).
  * "No Source-Text Wiring Tests" — spans + state deltas only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.system_strain import SystemStrainPool
from sidequest.game.turn import TurnManager
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    SaveVs,
    StigmaTables,
)
from sidequest.mutation.state import (
    CharacterMutationState,
    MutationState,
    UsageCounter,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Fixture builders (shape mirrors tests/server/test_102_3_freeplay_cast_*.py)
# ---------------------------------------------------------------------------

_ATTRIBUTE_MAP = {
    "STRENGTH": "Brawn",
    "CONSTITUTION": "Toughness",
    "DEXTERITY": "Reflexes",
    "INTELLIGENCE": "Wits",
    "WISDOM": "Instinct",
    "CHARISMA": "Presence",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 10 for name in _ABILITY_SCORE_NAMES}

_PC = "Rux"
_MUTATION_ID = "exotic/acid_spit"
_MUTATION_DISPLAY = "Acid Spit"
_STRAIN_COST = 2


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(
            body_part=["a"] * 6,
            nature=["b"] * 6,
            flavor=["c"] * 12,
        ),
        negatives=[
            NegativeMutationDef(
                id="negative/frail", name="Frail", roll_range=(1, 100), effect="frail"
            )
        ],
        positives=[
            PositiveMutationDef(
                id=_MUTATION_ID,
                name=_MUTATION_DISPLAY,
                category="exotic",
                effect="spit acid",
                strain_cost=_STRAIN_COST,
                usage="per_scene",
                save=SaveVs(stat="evasion", effect="negates"),
            ),
            PositiveMutationDef(
                id="sense/dark_sight",
                name="Dark Sight",
                category="sense",
                effect="see in dark",
                strain_cost=0,
                usage="at_will",
            ),
        ],
    )


def _make_awn_pack() -> Any:
    """Free play: an awn pack with a mutation catalog, NO confrontations."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import (
        AwnConfig,
        RulesConfig,
        SystemStrainConfig,
    )

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="awn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        awn=AwnConfig(
            attribute_map=dict(_ATTRIBUTE_MAP),
            system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        ),
    )
    pack.mutations = _catalog()
    pack.worlds = {}
    pack.wwn_spell_catalog = None
    pack.witnessed_acts = None
    return pack


def _make_native_pack() -> Any:
    """A native-ruleset pack: no mutation catalog, no WN surface of any kind."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    pack.mutations = None
    pack.worlds = {}
    pack.wwn_spell_catalog = None
    pack.witnessed_acts = None
    return pack


def _awn_snapshot(
    *, with_mutation_state: bool = True, usage_used: int = 0
) -> GameSnapshot:
    """Free-play awn snapshot: one Mutant PC, NO encounter, NO ``magic_state``
    (the retired plugin), NO ``core.spellcasting`` (AWN has no spells) — the
    mutation surface is ``snapshot.mutation_state`` alone."""
    core = CreatureCore(
        name=_PC,
        description="A mutant of the flickering wastes.",
        personality="watchful",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        system_strain=SystemStrainPool(current=0, max=10),
    )
    char = Character(
        core=core,
        char_class="Mutant",
        race="Mutant Human",
        backstory="Born under the fallout sky.",
        stats=dict(_STATS),
    )
    snap = GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        turn_manager=TurnManager(),
        player_seats={"player:Keith": _PC},
    )
    snap.characters.append(char)
    assert snap.magic_state is None, "awn fixture invariant: no pact-working plugin"
    assert core.spellcasting is None, "awn fixture invariant: no WWN cast surface"

    if with_mutation_state:
        usage = (
            {_MUTATION_ID: UsageCounter(period="per_scene", used=usage_used)}
            if usage_used
            else {}
        )
        snap.mutation_state = MutationState(
            characters={
                _PC: CharacterMutationState(
                    mp_remaining=0,
                    positive_ids=[_MUTATION_ID, "sense/dark_sight"],
                    usage=usage,
                )
            }
        )
    return snap


def _native_snapshot() -> GameSnapshot:
    snap = _awn_snapshot(with_mutation_state=False)
    snap.genre_slug = "tea_and_murder"
    snap.world_slug = "glenross"
    return snap


def _mutation_dispatch(
    *,
    working: str = _MUTATION_ID,
    actor: str = _PC,
    key: str = "k-mut-1",
    confidence: float = 1.0,
) -> SubsystemDispatch:
    """The 102-3 param contract, reused verbatim: actor + the working AS THE
    PLAYER TYPED IT (the router does not know spell from mutation — the
    HANDLER resolves against the pack's surface)."""
    return SubsystemDispatch(
        subsystem="magic_working",
        params={"actor": actor, "spell": working},
        idempotency_key=key,
        confidence=confidence,
        visibility=VisibilityTag(visible_to="all"),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Keith",
                raw_action=f"I use my {_MUTATION_DISPLAY} on the raider.",
                dispatch=list(dispatches),
                narrator_instructions=[],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )


def _all_dispatch_subsystems(package: DispatchPackage) -> list[str]:
    out: list[str] = []
    for pd in package.per_player:
        out.extend(d.subsystem for d in pd.dispatch)
    for ca in package.cross_player:
        out.extend(d.subsystem for d in ca.dispatch)
    return out


def _spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _pc_strain(snap: GameSnapshot) -> int:
    core = snap.find_creature_core(_PC)
    assert core is not None and core.system_strain is not None
    return core.system_strain.current


def _usage_used(snap: GameSnapshot) -> int:
    cs = snap.mutation_state.characters[_PC]
    counter = cs.usage.get(_MUTATION_ID)
    return counter.used if counter is not None else 0


async def _run_bank(package: DispatchPackage, *, snapshot: GameSnapshot, pack: Any):
    from sidequest.agents.subsystems import run_dispatch_bank

    return await run_dispatch_bank(
        package,
        context={"snapshot": snapshot, "pack": pack, "player_name": _PC},
    )


# ===========================================================================
# Seam 1 — precondition gate: a mutation surface is a live magic surface
# ===========================================================================


def test_gate_keeps_magic_working_for_mutation_surface() -> None:
    """THE GATE HALF: ``magic_state is None`` + no spellcasting must stop
    meaning "structurally inert" when the snapshot carries a populated
    ``mutation_state`` — the third live magic surface after pact-working and
    the WN cast spine. Today this dispatch is dropped and the named mutation
    use can never reach the engine.
    """
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    snap = _awn_snapshot()
    package = _package_with(_mutation_dispatch())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["magic_working"], (
        "an awn snapshot with a populated mutation_state is NOT structurally "
        "inert for magic_working — the dispatch must pass the gate so it can "
        f"route to use_mutation; got subsystems="
        f"{_all_dispatch_subsystems(filtered)}, gated={gated}"
    )
    assert gated == []


def test_gate_still_drops_magic_working_with_no_surface_at_all() -> None:
    """Regression guard: with NO pact-working state, NO spellcasting, and NO
    mutation state, magic_working stays inert at the gate (the Glenross
    lesson — 102-7 must not reopen the every-turn parse-error storm)."""
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    snap = _awn_snapshot(with_mutation_state=False)
    package = _package_with(_mutation_dispatch())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == []
    assert [g.subsystem for g in gated] == ["magic_working"]
    assert gated[0].reason, "the gate must record WHY the dispatch was inert"


# ===========================================================================
# Seam 2 — dispatch bank: magic_working routes to the mutation engine
# ===========================================================================


@pytest.mark.asyncio
async def test_freeplay_mutation_by_id_fires_used_span_and_pays_strain(
    otel_capture,
) -> None:
    """Happy path, typed id: ``awn.mutation.used`` fires, Strain 0 -> 2,
    per-scene usage ticks, the bank records no error, and the narrator
    receives at least one directive carrying the mechanical outcome."""
    snap = _awn_snapshot()
    pack = _make_awn_pack()
    dispatch = _mutation_dispatch(working=_MUTATION_ID)

    result = await _run_bank(_package_with(dispatch), snapshot=snap, pack=pack)

    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        "a named free-play mutation use must reach use_mutation and emit "
        f"exactly one awn.mutation.used span; got {len(used)} "
        f"(bank.errors={result.errors})"
    )
    assert used[0].attributes["actor"] == _PC
    assert used[0].attributes["mutation_id"] == _MUTATION_ID

    assert _pc_strain(snap) == _STRAIN_COST, (
        f"the use must COST: Strain 0 -> {_STRAIN_COST} (today it stays 0 — "
        "a power that costs nothing is the improv a mechanics-first player "
        "notices instantly)"
    )
    assert _usage_used(snap) == 1
    assert result.errors == [], f"the mutation route must not error; got {result.errors}"

    out = result.outputs_by_key.get(dispatch.idempotency_key)
    assert out is not None, "the bank must record the magic_working engagement output"
    assert out.directives, (
        "narration must reflect the MECHANICAL outcome — at least one "
        "narrator directive describing the resolved use"
    )


@pytest.mark.asyncio
async def test_freeplay_mutation_by_display_name_resolves(otel_capture) -> None:
    """The player types the DISPLAY name ("Acid Spit"), not the catalog id —
    the handler must resolve it exactly as the 102-3 cast spine resolves
    display names against the spell catalog."""
    snap = _awn_snapshot()
    pack = _make_awn_pack()

    await _run_bank(
        _package_with(_mutation_dispatch(working=_MUTATION_DISPLAY)),
        snapshot=snap,
        pack=pack,
    )

    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        f"display-name resolution must reach the engine; got {len(used)} "
        "awn.mutation.used spans"
    )
    assert used[0].attributes["mutation_id"] == _MUTATION_ID


@pytest.mark.asyncio
async def test_freeplay_exhausted_usage_refusal_is_engagement(otel_capture) -> None:
    """Refusal IS engagement: a per-scene mutation already used this scene
    refuses (``limit_exhausted``), pays no Strain, ticks nothing — and the
    refusal reaches narration as a directive, never silent improv."""
    snap = _awn_snapshot(usage_used=1)
    pack = _make_awn_pack()
    dispatch = _mutation_dispatch()

    result = await _run_bank(_package_with(dispatch), snapshot=snap, pack=pack)

    refused = _spans_named(otel_capture, "awn.mutation.refused")
    assert len(refused) == 1, (
        f"exhausted usage must emit awn.mutation.refused; got {len(refused)}"
    )
    assert refused[0].attributes["reason"].startswith("limit_exhausted")
    assert _spans_named(otel_capture, "awn.mutation.used") == []
    assert _pc_strain(snap) == 0, "a refused use must not pay Strain"
    assert _usage_used(snap) == 1, "the counter must not tick past the limit"

    out = result.outputs_by_key.get(dispatch.idempotency_key)
    assert out is not None and out.directives, (
        "the refusal must surface to narration (a directive), so the narrator "
        "describes the body failing to answer — engagement, not omission"
    )


@pytest.mark.asyncio
async def test_freeplay_unknown_working_is_failed_premise(otel_capture) -> None:
    """A working the catalog doesn't know ("I crystallize time") must be a
    failed premise: loud refusal evidence, NO state change — never
    improv-with-no-cost."""
    snap = _awn_snapshot()
    pack = _make_awn_pack()

    result = await _run_bank(
        _package_with(_mutation_dispatch(working="crystallize time")),
        snapshot=snap,
        pack=pack,
    )

    refused = _spans_named(otel_capture, "awn.mutation.refused")
    assert len(refused) >= 1, (
        "an unknown working must leave GM-panel evidence (awn.mutation.refused); "
        f"got none (bank.errors={result.errors})"
    )
    assert _spans_named(otel_capture, "awn.mutation.used") == []
    assert _pc_strain(snap) == 0
    assert _usage_used(snap) == 0


@pytest.mark.asyncio
async def test_native_pack_emits_no_awn_spans_and_does_not_crash(otel_capture) -> None:
    """Non-AWN safety: the same dispatch in a native genre crosses no awn
    seam — zero ``awn.*`` spans, no exception."""
    snap = _native_snapshot()
    pack = _make_native_pack()

    await _run_bank(_package_with(_mutation_dispatch()), snapshot=snap, pack=pack)

    awn_spans = [
        s for s in otel_capture.get_finished_spans() if s.name.startswith("awn.")
    ]
    assert awn_spans == [], (
        f"a native pack must never cross the mutation seam; got {awn_spans}"
    )


# ===========================================================================
# Seam 3 — wiring test: the REAL pre-narrator pass (stubbed router only)
# ===========================================================================


@pytest.mark.asyncio
async def test_pre_narrator_pass_routes_freeplay_mutation(
    otel_capture, monkeypatch
) -> None:
    """THE WIRING TEST (CLAUDE.md: every suite needs one): drive the real
    ``execute_intent_router_pre_narrator_pass`` — real precondition gate, real
    dispatch bank — with only the Haiku classification stubbed to return the
    magic_working dispatch. The mutation engine must engage end-to-end:
    ``awn.mutation.used`` fires and the Strain pool moves on the production
    pre-narrator path, not just when the bank is called directly.
    """
    from sidequest.server.intent_router_pass import (
        execute_intent_router_pre_narrator_pass,
    )

    snap = _awn_snapshot()
    pack = _make_awn_pack()
    package = _package_with(_mutation_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=f"I use my {_MUTATION_DISPLAY} on the raider.",
        player_name=_PC,
    )

    assert _all_dispatch_subsystems(returned) == ["magic_working"], (
        "the free-play mutation use must NOT be gated out of the returned "
        f"package; got {_all_dispatch_subsystems(returned)}"
    )
    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        "the production pre-narrator pass must carry a classified free-play "
        "mutation use through gate + bank to use_mutation; got "
        f"{len(used)} awn.mutation.used spans (bank.errors={bank.errors}) — "
        "the dispatch died at a seam"
    )
    assert _pc_strain(snap) == _STRAIN_COST
