"""Story 102-3 RED — explicit free-play cast routes magic_working to the WN
cast spine (epic 102, AC5b "from a world opening" blocker).

THE BUG (FIXER investigation oq-2, 2026-06-10, gap #3): a player types
"I cast foundation_of_flame" in free play (no confrontation seated) in a WN
genre. The intent router defines the ``magic_working`` category, but the
dispatch can never mechanically engage in a WN world:

  1. The precondition gate (``_magic_working_precondition_unmet``) drops the
     dispatch whenever ``snapshot.magic_state is None`` — which is EVERY WN
     world, because WN magic lives on ``core.spellcasting``/``core.effort``,
     not the retired ADR-126 pact-working plugin. The gate's own comment says
     "lets the channel be narrated via the existing beat path" — i.e. today
     the design deliberately lets the narrator improvise the working. That is
     the Illusionism the OTEL lie-detector doctrine exists to catch.
  2. Even unGated, ``run_magic_working_dispatch`` engages
     ``apply_magic_working`` against ``magic_state`` and raises
     ``MagicWorkingParseError`` — there is no route to
     ``WwnRulesetModule.resolve_spellcast`` (the existing, tested cast spine
     the apply_beat path already drives via ``_resolve_wwn_cast_for_beat``).

THE CONTRACT THESE TESTS PIN (story context, sprint/context/context-story-102-3.md):

  * Param contract: a WN free-play cast dispatch carries
    ``params={"actor": <caster name>, "spell": <the spell AS THE PLAYER TYPED
    IT>}``. The handler resolves the typed name (id "foundation_of_flame" or
    display name "Foundation of Flame") against the world/genre WWN spell
    catalog (``resolve_wwn_spell_catalog``) and the caster's prepared list.
  * Engagement: the dispatch routes to ``resolve_spellcast`` — ``wwn.spell.cast``
    fires, ``casts_remaining`` is spent, and the bank output carries at least
    one narrator directive so narration reflects the MECHANICAL outcome.
  * Refusal is engagement: 0 casts remaining → ``wwn.spell.cast`` with
    ``refused=True``, no spend, refusal surfaced to narration — and NO
    ``dispatch_engagement.magic_working.mismatch`` (the engine answered).
  * Unknown spell → failed-premise handling: NO spend and LOUD evidence
    (a refused ``wwn.spell.cast`` or a ``dispatch_engagement.magic_working
    .mismatch``) — never improv-with-no-spend (story guardrail "either the
    precondition gate fires a mismatch span ... or a typed refusal").
  * AC2 lie-detector: when classification succeeds but the dispatch cannot
    engage (no WN cast surface AND no pact-working magic_state), driving the
    real pre-narrator pass + post-turn watcher MUST emit
    ``dispatch_engagement.magic_working.mismatch`` with non-empty evidence.
    Today the gate silently strips the dispatch and the watcher stays quiet.
  * AC3 non-WN safety: the same dispatch in a native-ruleset genre does not
    crash and emits zero ``wwn.*`` spans.

Test discipline (story guardrail "router test discipline"): the router is
STUBBED everywhere except the env-gated live-classification test at the
bottom — classification and dispatch-routing are separate seams.

CLAUDE.md rule coverage:
  * "Every Test Suite Needs a Wiring Test" —
    ``test_pass_routes_wn_freeplay_cast_to_spell_cast_span`` drives the real
    ``execute_intent_router_pre_narrator_pass`` (real gates, real bank).
  * "No Source-Text Wiring Tests" — wiring proven by OTEL spans + state,
    never by grepping source.
  * "OTEL Observability Principle" — every assertion path is span-shaped.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# WWN attribute_map: the six SWN/WWN attributes -> this fixture pack's flavor
# stats (same shape as tests/server/test_wwn_cast_dispatch.py).
_ATTRIBUTE_MAP = {
    "STRENGTH": "Might",
    "CONSTITUTION": "Vigor",
    "DEXTERITY": "Grace",
    "INTELLIGENCE": "Lore",
    "WISDOM": "Wit",
    "CHARISMA": "Bearing",
}
_ABILITY_SCORE_NAMES = list(_ATTRIBUTE_MAP.values())
_STATS = {name: 10 for name in _ABILITY_SCORE_NAMES}

_CASTER = "Vesska"
_SPELL_ID = "foundation_of_flame"
_SPELL_DISPLAY = "Foundation of Flame"


def _make_wwn_pack() -> Any:
    """A MagicMock pack whose ``.rules`` is a real wwn-bound RulesConfig and
    whose genre-tier catalog ships ``foundation_of_flame`` (+ one unprepared
    sibling). Free play: NO confrontations authored — there is no beat path
    here, only the world opening.
    """
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig, SystemStrainConfig, WwnConfig
    from sidequest.genre.models.wwn_spell import WwnSpell, WwnSpellCatalog

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_ABILITY_SCORE_NAMES),
        wwn=WwnConfig(
            attribute_map=dict(_ATTRIBUTE_MAP),
            system_strain=SystemStrainConfig(max_source="CONSTITUTION"),
        ),
    )
    # World ships no catalog → resolver falls through to the genre tier.
    pack.worlds = {}
    pack.wwn_spell_catalog = WwnSpellCatalog(
        spells=[
            WwnSpell(
                id=_SPELL_ID,
                name=_SPELL_DISPLAY,
                level=1,
                save=None,
                damage_die="1d6",
                damage_per_level=False,
                genre_description="A floor of coals spreads from the caster's heel.",
                mechanical_effect="1d6 fire; ignites unattended tinder",
            ),
            WwnSpell(
                id="mirror_ward",
                name="Mirror Ward",
                level=1,
                save=None,
                damage_die=None,
                damage_per_level=False,
                genre_description="A skin of mirrored light.",
                mechanical_effect="deflects the first strike",
            ),
        ]
    )
    # _build_state_summary gates witnessed-act vocabulary on this attribute;
    # a bare MagicMock auto-attr is truthy and would mis-shape the summary.
    pack.witnessed_acts = None
    return pack


def _make_native_pack() -> Any:
    """A native-ruleset pack: no WWN block, no spell catalog."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import RulesConfig

    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig()
    pack.worlds = {}
    pack.wwn_spell_catalog = None
    pack.witnessed_acts = None
    return pack


def _wwn_snapshot(*, casts_remaining: int = 2, with_spellcasting: bool = True) -> GameSnapshot:
    """Free-play WN snapshot: one PC with seeded WWN spellcasting, NO
    encounter, NO ADR-126 ``magic_state`` (the WN-world invariant the
    precondition gate currently keys off)."""
    core = CreatureCore(
        name=_CASTER,
        description="A foundry-witch of the Long Foundry.",
        personality="deliberate",
        inventory=Inventory(),
        hp={"current": 10, "max": 10, "base_max": 10},
        spellcasting=(
            SpellcastingState(
                prepared=[_SPELL_ID],
                casts_remaining=casts_remaining,
                casts_per_day=2,
                max_spell_level=1,
            )
            if with_spellcasting
            else None
        ),
    )
    char = Character(
        core=core,
        char_class="Mage",
        race="Human",
        backstory="Apprenticed at the slag-gates.",
        stats=dict(_STATS),
    )
    snap = GameSnapshot(
        genre_slug="heavy_metal",
        world_slug="long_foundry",
        turn_manager=TurnManager(),
        player_seats={"player:Keith": _CASTER},
    )
    snap.characters.append(char)
    assert snap.magic_state is None, "WN fixture invariant: no pact-working plugin loaded"
    return snap


def _native_snapshot() -> GameSnapshot:
    """A native-genre snapshot with no magic surface of any kind."""
    snap = _wwn_snapshot(with_spellcasting=False)
    snap.genre_slug = "tea_and_murder"
    snap.world_slug = "glenross"
    return snap


def _cast_dispatch(
    *,
    spell: str = _SPELL_ID,
    actor: str = _CASTER,
    key: str = "k-cast-1",
    confidence: float = 1.0,
) -> SubsystemDispatch:
    """The 102-3 param contract: actor + the spell AS THE PLAYER TYPED IT."""
    return SubsystemDispatch(
        subsystem="magic_working",
        params={"actor": actor, "spell": spell},
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
                raw_action=f"I cast {_SPELL_ID} across the slag floor.",
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


def _casts_remaining(snap: GameSnapshot) -> int:
    core = snap.find_creature_core(_CASTER)
    assert core is not None and core.spellcasting is not None
    return core.spellcasting.casts_remaining


async def _run_bank(package: DispatchPackage, *, snapshot: GameSnapshot, pack: Any):
    from sidequest.agents.subsystems import run_dispatch_bank

    return await run_dispatch_bank(
        package,
        context={"snapshot": snapshot, "pack": pack, "player_name": _CASTER},
    )


# ===========================================================================
# Seam 1 — precondition gate: a WN caster is a live cast surface
# ===========================================================================


def test_gate_keeps_magic_working_for_wn_caster_with_spellcasting() -> None:
    """THE GATE HALF OF THE BUG: ``magic_state is None`` must stop meaning
    "structurally inert" when the snapshot carries a WN cast surface (a PC
    with seeded ``core.spellcasting``). Today this dispatch is dropped and
    the named cast can never reach any engine.
    """
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    snap = _wwn_snapshot()
    package = _package_with(_cast_dispatch())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == ["magic_working"], (
        "a WN snapshot (magic_state=None BUT a caster with core.spellcasting "
        "seeded) is NOT structurally inert for magic_working — the dispatch "
        "must pass the gate so it can route to resolve_spellcast; got "
        f"subsystems={_all_dispatch_subsystems(filtered)}, gated={gated}"
    )
    assert gated == []


def test_gate_still_drops_magic_working_with_no_cast_surface_at_all() -> None:
    """Regression guard (pins the Glenross/burning_peace lesson): when there
    is NEITHER a pact-working ``magic_state`` NOR any PC with WN spellcasting,
    magic_working remains structurally inert at the gate. 102-3 must not
    reopen the every-turn MagicWorkingParseError storm.
    """
    from sidequest.agents.dispatch_precondition_gate import gate_inert_dispatches

    snap = _wwn_snapshot(with_spellcasting=False)
    package = _package_with(_cast_dispatch())

    filtered, gated = gate_inert_dispatches(package=package, snapshot=snap)

    assert _all_dispatch_subsystems(filtered) == []
    assert [g.subsystem for g in gated] == ["magic_working"]
    assert gated[0].reason, "the gate must record WHY the dispatch was inert"


# ===========================================================================
# Seam 2 — dispatch bank: magic_working routes to the WN cast spine
# ===========================================================================


@pytest.mark.asyncio
async def test_freeplay_named_cast_fires_wwn_spell_cast_and_spends_cast(otel_capture) -> None:
    """AC1 happy path: a magic_working dispatch naming a prepared spell, in a
    WN snapshot with no encounter, engages ``resolve_spellcast`` —
    ``wwn.spell.cast`` fires (refused=False), exactly one cast is spent, the
    bank records no error, and the narrator receives at least one directive
    carrying the mechanical outcome.
    """
    snap = _wwn_snapshot(casts_remaining=2)
    pack = _make_wwn_pack()
    dispatch = _cast_dispatch()

    result = await _run_bank(_package_with(dispatch), snapshot=snap, pack=pack)

    cast_spans = _spans_named(otel_capture, "wwn.spell.cast")
    assert len(cast_spans) == 1, (
        "a named free-play cast must reach WwnRulesetModule.resolve_spellcast "
        f"and emit exactly one wwn.spell.cast span; got {len(cast_spans)} "
        f"(bank.errors={result.errors})"
    )
    assert cast_spans[0].attributes["refused"] is False
    assert cast_spans[0].attributes["actor"] == _CASTER
    assert cast_spans[0].attributes["spell_id"] == _SPELL_ID

    assert _casts_remaining(snap) == 1, (
        "the cast must SPEND: casts_remaining 2 -> 1 (today it stays 2 — the "
        "exact 'cast that costs nothing' a career-GM player notices instantly)"
    )
    assert result.errors == [], f"the WN route must not error; got {result.errors}"

    out = result.outputs_by_key.get(dispatch.idempotency_key)
    assert out is not None, "the bank must record the magic_working engagement output"
    assert out.directives, (
        "narration must reflect the MECHANICAL outcome — the handler returns at "
        "least one narrator directive describing the resolved cast"
    )


@pytest.mark.asyncio
async def test_freeplay_cast_resolves_display_name(otel_capture) -> None:
    """AC1 edge (fuzzy name): the player typed the display name
    ('Foundation of Flame'), not the id. Name resolution against the catalog
    must still land on foundation_of_flame and spend the cast.
    """
    snap = _wwn_snapshot(casts_remaining=2)
    pack = _make_wwn_pack()

    result = await _run_bank(
        _package_with(_cast_dispatch(spell=_SPELL_DISPLAY)), snapshot=snap, pack=pack
    )

    cast_spans = _spans_named(otel_capture, "wwn.spell.cast")
    assert len(cast_spans) == 1, (
        f"display-name cast must resolve to {_SPELL_ID!r} and engage; "
        f"got {len(cast_spans)} spans (bank.errors={result.errors})"
    )
    assert cast_spans[0].attributes["spell_id"] == _SPELL_ID
    assert cast_spans[0].attributes["refused"] is False
    assert _casts_remaining(snap) == 1


@pytest.mark.asyncio
async def test_unknown_spell_name_no_spend_and_loud_evidence(otel_capture) -> None:
    """AC1 edge (gibberish): an unresolvable spell name is a FAILED PREMISE —
    never a spend, never a silent pass-through. Loud evidence is required on
    one of the two contract channels: a refused ``wwn.spell.cast`` (typed
    refusal) or a ``dispatch_engagement.magic_working.mismatch`` from the
    post-turn watcher.
    """
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    snap = _wwn_snapshot(casts_remaining=2)
    pack = _make_wwn_pack()
    package = _package_with(_cast_dispatch(spell="zorblax_the_unreal"))

    await _run_bank(package, snapshot=snap, pack=pack)

    assert _casts_remaining(snap) == 2, "an unknown spell must never spend a cast"
    successful = [
        s
        for s in _spans_named(otel_capture, "wwn.spell.cast")
        if s.attributes.get("refused") is False
    ]
    assert successful == [], "an unknown spell must never resolve as a successful cast"

    # Post-turn lie-detector run, mirroring _execute_narration_turn ordering.
    run_dispatch_engagement_watcher(package=package, snapshot=snap)

    refusals = [
        s
        for s in _spans_named(otel_capture, "wwn.spell.cast")
        if s.attributes.get("refused") is True
    ]
    mismatches = _spans_named(otel_capture, "dispatch_engagement.magic_working.mismatch")
    assert refusals or mismatches, (
        "gibberish spell name must surface LOUD evidence — a refused "
        "wwn.spell.cast or a dispatch_engagement.magic_working.mismatch; got "
        "neither (the narrator is free to improvise the working unobserved)"
    )


@pytest.mark.asyncio
async def test_zero_casts_remaining_surfaces_mechanical_refusal(otel_capture) -> None:
    """AC1 edge (0 casts): the refusal is itself mechanical engagement —
    ``wwn.spell.cast`` fires with refused=True, nothing is spent, the refusal
    reaches the narrator as a directive, and the post-turn watcher emits NO
    mismatch (the engine answered; narrating the fizzle is honest).
    """
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher

    snap = _wwn_snapshot(casts_remaining=0)
    pack = _make_wwn_pack()
    dispatch = _cast_dispatch()
    package = _package_with(dispatch)

    result = await _run_bank(package, snapshot=snap, pack=pack)

    cast_spans = _spans_named(otel_capture, "wwn.spell.cast")
    assert len(cast_spans) == 1, (
        f"a 0-casts refusal must still run the spine; got {len(cast_spans)} spans "
        f"(bank.errors={result.errors})"
    )
    assert cast_spans[0].attributes["refused"] is True
    assert _casts_remaining(snap) == 0

    out = result.outputs_by_key.get(dispatch.idempotency_key)
    assert out is not None and out.directives, (
        "the mechanical refusal must be surfaced to narration (a directive), "
        "not silently succeed in prose"
    )

    run_dispatch_engagement_watcher(package=package, snapshot=snap)
    assert _spans_named(otel_capture, "dispatch_engagement.magic_working.mismatch") == [], (
        "a refused cast IS engagement — the lie-detector must not cry wolf on it"
    )


# ===========================================================================
# Seam 3 — wiring: the real pre-narrator pass end-to-end (router stubbed)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pass_routes_wn_freeplay_cast_to_spell_cast_span(otel_capture) -> None:
    """AC1+AC4 wiring (CLAUDE.md 'Every Test Suite Needs a Wiring Test'):
    drive the REAL ``execute_intent_router_pre_narrator_pass`` — real
    unregistered gate, real precondition gate, real dispatch bank — with only
    the router stubbed (router test discipline). The classified cast must
    survive both gates, engage the spine, and ride back in the returned
    package (the object the post-turn watcher reads).
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _wwn_snapshot(casts_remaining=2)
    pack = _make_wwn_pack()
    package = _package_with(_cast_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=f"I cast {_SPELL_ID} across the slag floor.",
        player_name=_CASTER,
    )

    assert _all_dispatch_subsystems(returned) == ["magic_working"], (
        "the WN free-play cast must NOT be gated out of the returned package; "
        f"got {_all_dispatch_subsystems(returned)}"
    )
    assert len(_spans_named(otel_capture, "wwn.spell.cast")) == 1, (
        f"the pass must engage the cast spine end-to-end (bank.errors={bank.errors})"
    )
    assert _casts_remaining(snap) == 1


@pytest.mark.asyncio
async def test_classified_but_unengageable_emits_mismatch_span(otel_capture) -> None:
    """AC2 — THE NAMED DELIVERABLE: classification succeeded but the dispatch
    cannot engage (no WN cast surface, no pact-working magic_state — the gate
    closes). The lie-detector span ``dispatch_engagement.magic_working.mismatch``
    MUST be emitted with non-empty evidence somewhere in the pass+watcher flow.
    Today the gate silently strips the dispatch and the watcher never sees it —
    the miss the GM panel cannot currently display.
    """
    from sidequest.agents.dispatch_engagement_watcher import run_dispatch_engagement_watcher
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _wwn_snapshot(with_spellcasting=False)  # gate closed: no surface at all
    pack = _make_wwn_pack()
    package = _package_with(_cast_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=f"I cast {_SPELL_ID} across the slag floor.",
        player_name=_CASTER,
    )
    run_dispatch_engagement_watcher(package=returned, snapshot=snap)

    mismatches = _spans_named(otel_capture, "dispatch_engagement.magic_working.mismatch")
    assert len(mismatches) >= 1, (
        "AC2: a classified magic_working that cannot engage must emit "
        "dispatch_engagement.magic_working.mismatch — that span IS the AC; "
        f"got spans={[s.name for s in otel_capture.get_finished_spans()]}"
    )
    assert mismatches[0].attributes.get("evidence"), (
        "the mismatch span must carry reason attributes (non-empty evidence) "
        "so the GM panel shows WHY the cast had no mechanical backing"
    )


@pytest.mark.asyncio
async def test_native_genre_cast_does_not_crash_or_emit_wwn_spans(otel_capture) -> None:
    """AC3 non-WN safety: the same classified cast in a native-ruleset genre
    must not crash the pass and must emit ZERO wwn.* spans (a clean mismatch
    or that genre's own magic story are both acceptable; borrowing WN spans
    is not — the slug is honest, per the epic invariants).
    """
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _native_snapshot()
    pack = _make_native_pack()
    package = _package_with(_cast_dispatch())

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    # Must not raise.
    await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action=f"I cast {_SPELL_ID} at the butler.",
        player_name=_CASTER,
    )

    wwn_spans = [s for s in otel_capture.get_finished_spans() if s.name.startswith("wwn.")]
    assert wwn_spans == [], (
        f"a native genre must not emit WN spans for a free-play cast; got "
        f"{[s.name for s in wwn_spans]}"
    )


# ===========================================================================
# Seam 4 — live classification (AC4, env-gated opt-in)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_live_router_classifies_explicit_named_cast_as_magic_working() -> None:
    """AC4 live half (opt-in): the REAL router (live Haiku classification, not
    stubbed) classifies an explicit named cast as ``magic_working``, carries
    the spell name in params, and clears the bank's engagement threshold
    (story guardrail: an explicit named cast must clear confidence gating).

    Gated like the other live-API verifications (test_91_3 pattern) so CI and
    xdist runs stay deterministic.
    """
    if os.environ.get("SIDEQUEST_VERIFY_FREEPLAY_CAST_LIVE") != "1":
        pytest.skip("set SIDEQUEST_VERIFY_FREEPLAY_CAST_LIVE=1 (+ ANTHROPIC_API_KEY) to run")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY required for live classification")

    from sidequest.agents.subsystems import DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD
    from sidequest.server.intent_router_pass import (
        _build_state_summary,
        build_intent_router_for_session,
    )

    snap = _wwn_snapshot(casts_remaining=2)
    pack = _make_wwn_pack()
    router = build_intent_router_for_session(session_id=None)

    package = await router.decompose(
        action="I cast foundation_of_flame at the rust-wight blocking the gate.",
        state_summary=_build_state_summary(snap, pack=pack),
    )

    magic_dispatches = [
        d for pd in package.per_player for d in pd.dispatch if d.subsystem == "magic_working"
    ] + [d for ca in package.cross_player for d in ca.dispatch if d.subsystem == "magic_working"]
    assert magic_dispatches, (
        "an explicit named cast ('I cast foundation_of_flame ...') must "
        "classify as magic_working; got subsystems="
        f"{[d.subsystem for pd in package.per_player for d in pd.dispatch]}"
    )
    d = magic_dispatches[0]
    assert "foundation" in str(d.params).lower(), (
        f"the dispatch params must carry the named spell; got params={d.params}"
    )
    assert d.confidence >= DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD, (
        f"an explicit named cast must clear the engagement threshold "
        f"({DEFAULT_DISPATCH_CONFIDENCE_THRESHOLD}); got {d.confidence}"
    )
