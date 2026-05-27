"""Tests for the scenario_clue subsystem dispatch handler (Story 59-6).

The scenario_clue handler is the third engine on the ADR-113 Intent Router
spine (following confrontation in 59-4 and magic_working in 59-5). Story
59-6 introduces ``sidequest/agents/subsystems/scenario_clue.py`` with
``run_scenario_clue_dispatch`` — a SubsystemDispatch handler that calls
``consume_clue_footnotes`` BEFORE the narrator runs, so the clue graph
advances proactively on investigation-shaped player actions.

**Key difference from 59-4/59-5:** this is a SUPPLEMENTING dispatch, NOT
a retirement. The narrator-footnote path (``consume_clue_footnotes`` at
``scenario_clue_intake.py``) stays alive — the handler adds proactive
engagement alongside it. Both paths converge on the same
``ScenarioState.discover_clue`` call, which is idempotent (re-discovery
emits a duplicate-flagged span but does not double-mint KnownFacts).

These tests pin the handler's contract:

  AC1 (unit slice): given a SubsystemDispatch(subsystem="scenario_clue",
    params={"fact_id": ..., "summary": ..., "category": ...}), the handler
    calls consume_clue_footnotes and advances prerequisite-eligible facts.
    Returns SubsystemOutput.

  AC2 (footnote path unchanged): the narrator-footnote path still discovers
    clues. No retirement — this is a supplement.

  AC3 (no double-engagement): when both the router handler and the footnote
    path discover the same clue, KnownFact is minted exactly once (the
    ``is_new`` guard in ``consume_clue_footnotes`` prevents double-mint).

  AC4 (lie-detector coverage): the dispatch engagement watcher emits
    dispatch_engagement.scenario_clue.mismatch when the router dispatches
    scenario_clue but discovery didn't land. (Already shipped in 59-3;
    these tests VERIFY existing coverage, not new implementation.)

Project rule coverage (CLAUDE.md / SOUL):
- "Every Test Suite Needs a Wiring Test" — registration test asserts the
  handler is reachable from the dispatch bank, not just importable.
- "No Source-Text Wiring Tests" — registration test uses
  get_registered() reflection, not file grepping.
- "No Silent Fallbacks" — handler propagates errors from the data layer,
  does not swallow them.
- "OTEL Observability Principle" — clue discovery emits
  SPAN_SCENARIO_ADVANCE via ScenarioState.discover_clue; the handler
  delegates to consume_clue_footnotes which fires it.

AC1/AC3 (handler leg)/AC4-wiring tests FAIL TODAY by design — handler
does not exist yet.
AC2/AC4-lie-detector tests PASS TODAY — existing coverage.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.scenario import ClueGraph, ClueNode
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)
from sidequest.protocol.models import Footnote

# ---------------------------------------------------------------------------
# Synthetic fixtures — minimal scenario + snapshot for clue discovery.
# Mirrors the shape used in tests/server/test_scenario_clue_intake.py.
# ---------------------------------------------------------------------------


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _clue_node(node_id: str) -> ClueNode:
    return ClueNode(
        id=node_id,
        type="testimony",
        description=f"clue {node_id}",
        discovery_method="conversation",
        visibility="public",
    )


def _character(name: str = "Rux") -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="placeholder",
            personality="stoic",
            inventory=Inventory(),
        ),
        char_class="Fighter",
        race="Human",
        backstory="placeholder",
    )


def _snapshot_with_scenario(
    *,
    clue_ids: list[str],
    interaction: int = 7,
    character_name: str = "Rux",
) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    snap.characters.append(_character(character_name))
    snap.turn_manager.interaction = interaction
    snap.scenario_state = ScenarioState(
        clue_graph=ClueGraph(nodes=[_clue_node(cid) for cid in clue_ids]),
    )
    return snap


def _snapshot_without_scenario(*, character_name: str = "Rux") -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    snap.characters.append(_character(character_name))
    assert snap.scenario_state is None
    return snap


def _scenario_clue_dispatch(
    *,
    fact_id: str = "library_key",
    summary: str = "The key fits the library door.",
    category: str = "Lore",
    idempotency_key: str = "k-clue-1",
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="scenario_clue",
        params={
            "fact_id": fact_id,
            "summary": summary,
            "category": category,
        },
        idempotency_key=idempotency_key,
        visibility=_open_viz(),
    )


def _package_with(*dispatches: SubsystemDispatch, turn_id: str = "turn-1") -> DispatchPackage:
    return DispatchPackage(
        turn_id=turn_id,
        per_player=[
            PlayerDispatch(
                player_id="player:Rux",
                raw_action="I search the desk for clues.",
                dispatch=list(dispatches),
            )
        ],
        confidence_global=1.0,
    )


def _footnote(
    *, summary: str, fact_id: str | None, marker: int = 1
) -> Footnote:
    return Footnote(
        marker=marker,
        fact_id=fact_id,
        summary=summary,
        category="Lore",
        is_new=True,
    )


# ---------------------------------------------------------------------------
# AC1: handler discovers clue on snapshot — pre-narrator engagement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_clue_handler_discovers_clue_on_snapshot() -> None:
    """AC1 (unit slice): the handler reads dispatch.params["fact_id"],
    constructs a Footnote, and calls consume_clue_footnotes against the
    snapshot's scenario_state.

    After the handler returns, snapshot.scenario_state.discovered_clues
    must contain the fact_id — proving the clue graph advanced pre-narrator.

    FAILS TODAY: module sidequest/agents/subsystems/scenario_clue.py
    does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )

    snap = _snapshot_with_scenario(clue_ids=["library_key", "muddy_boot"])
    dispatch = _scenario_clue_dispatch(fact_id="library_key")

    out = await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    assert snap.scenario_state is not None
    assert "library_key" in snap.scenario_state.discovered_clues, (
        "handler must advance the clue graph — narrator sees already-discovered "
        "state when it runs after dispatch bank"
    )

    from sidequest.agents.subsystems import SubsystemOutput

    assert isinstance(out, SubsystemOutput)


@pytest.mark.asyncio
async def test_scenario_clue_handler_mints_known_fact_on_first_discovery() -> None:
    """AC1 (KnownFact leg): the handler's consume_clue_footnotes call
    mints a KnownFact on the active character for first-time discoveries.

    Confirms the handler is calling the real consume path, not a stub
    that only fires discover_clue without the journal mint.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )

    snap = _snapshot_with_scenario(
        clue_ids=["library_key"],
        interaction=42,
        character_name="Rux",
    )
    dispatch = _scenario_clue_dispatch(
        fact_id="library_key",
        summary="The key fits the library door.",
    )

    await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    rux = snap.characters[0]
    assert len(rux.known_facts) == 1, (
        f"expected one KnownFact minted on first discovery, got {len(rux.known_facts)}"
    )
    kf = rux.known_facts[0]
    assert kf.content == "The key fits the library door."
    assert kf.confidence == "Discovered"
    assert kf.source == "ScenarioClue"
    assert kf.learned_turn == 42
    assert kf.fact_id == "library_key"


@pytest.mark.asyncio
async def test_scenario_clue_handler_noop_when_no_scenario_state() -> None:
    """When snapshot has no scenario_state, the handler delegates to
    consume_clue_footnotes which returns silently. The handler returns
    SubsystemOutput() without error — the watcher (59-3) catches the
    resulting dispatch-without-engagement mismatch.

    This is the correct behavior for a supplementing dispatch: the router
    may classify investigation intent in a non-scenario world; the handler
    gracefully no-ops and the watcher flags it.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )

    snap = _snapshot_without_scenario()
    dispatch = _scenario_clue_dispatch(fact_id="library_key")

    out = await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    from sidequest.agents.subsystems import SubsystemOutput

    assert isinstance(out, SubsystemOutput)
    assert snap.characters[0].known_facts == [], (
        "no KnownFact should be minted when scenario_state is None"
    )


@pytest.mark.asyncio
async def test_scenario_clue_handler_prerequisite_not_satisfied_does_not_raise() -> None:
    """When a clue's DAG prerequisites are not met, consume_clue_footnotes
    catches PrerequisiteNotSatisfiedError and continues. The handler must
    propagate this behavior — one unmet prerequisite does not crash the
    dispatch bank.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    # Add a prerequisite that hasn't been discovered
    assert snap.scenario_state is not None
    node = snap.scenario_state.clue_graph.nodes[0]
    node.requires = ["undiscovered_prereq"]

    dispatch = _scenario_clue_dispatch(fact_id="library_key")

    out = await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    from sidequest.agents.subsystems import SubsystemOutput

    assert isinstance(out, SubsystemOutput)
    assert "library_key" not in snap.scenario_state.discovered_clues, (
        "clue with unmet prerequisites must not be discovered"
    )


# ---------------------------------------------------------------------------
# Regression (playtest 2026-05-27, coyote_star turn 2): an off-enum
# router-supplied category must NOT crash the dispatch. The router's
# params.category is free-form (not schema-constrained like the
# commit_known_fact tool), so the LLM emits arbitrary words ("Object",
# "evidence"). Feeding that straight into Footnote(category=...) raised a
# pydantic enum ValidationError, crashed the whole dispatch, and the clue
# subsystem produced nothing while the narrator improvised the finding
# (the Illusionism OTEL exists to catch). Clue discovery is keyed by
# fact_id, not category — a bad category must never block it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_enum_category_does_not_crash_and_clue_still_discovers(caplog) -> None:
    import logging as _logging

    from sidequest.agents.subsystems import SubsystemOutput
    from sidequest.agents.subsystems.scenario_clue import run_scenario_clue_dispatch

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    # "Object" is a plausible LLM choice and is NOT a FactCategory member.
    dispatch = _scenario_clue_dispatch(fact_id="library_key", category="Object")

    with caplog.at_level(_logging.WARNING):
        out = await run_scenario_clue_dispatch(dispatch, snapshot=snap, player_name="Rux")

    assert isinstance(out, SubsystemOutput)
    assert snap.scenario_state is not None
    assert "library_key" in snap.scenario_state.discovered_clues, (
        "an off-enum category must not block fact_id-keyed clue discovery"
    )
    assert len(snap.characters[0].known_facts) == 1
    # Fail LOUD: the coercion must surface, not swallow silently.
    assert any(
        "category_coerced" in r.getMessage() for r in caplog.records
    ), "off-enum category coercion must emit a WARNING (no silent fallback)"


@pytest.mark.asyncio
async def test_case_insensitive_category_is_preserved_not_coerced(caplog) -> None:
    """A valid category in the wrong case ("lore") must map to the real
    enum member, NOT trip the loud-coerce path."""
    import logging as _logging

    from sidequest.agents.subsystems.scenario_clue import run_scenario_clue_dispatch

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    dispatch = _scenario_clue_dispatch(fact_id="library_key", category="lore")

    with caplog.at_level(_logging.WARNING):
        await run_scenario_clue_dispatch(dispatch, snapshot=snap, player_name="Rux")

    assert "library_key" in snap.scenario_state.discovered_clues  # type: ignore[union-attr]
    assert not any("category_coerced" in r.getMessage() for r in caplog.records), (
        "a valid (case-insensitive) category must not be reported as coerced"
    )


# ---------------------------------------------------------------------------
# AC2: Footnote path unchanged — regression guard (PASSES TODAY)
# ---------------------------------------------------------------------------


def test_footnote_path_still_discovers_clues() -> None:
    """AC2 (regression guard): the narrator-footnote path must continue
    to work. Story 59-6 is a SUPPLEMENT — the footnote consumer in
    scenario_clue_intake.py stays alive.

    This test SHOULD PASS TODAY — it verifies existing behavior is not
    broken by the handler addition.
    """
    from sidequest.server.dispatch.scenario_clue_intake import (
        consume_clue_footnotes,
    )

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    footnotes = [_footnote(summary="The key fits.", fact_id="library_key")]

    consume_clue_footnotes(snap, footnotes, active_character_name="Rux")

    assert snap.scenario_state is not None
    assert "library_key" in snap.scenario_state.discovered_clues
    assert len(snap.characters[0].known_facts) == 1
    assert snap.characters[0].known_facts[0].confidence == "Discovered"


# ---------------------------------------------------------------------------
# AC3: No double-engagement — idempotency between router + footnote paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_then_footnote_does_not_double_mint_known_fact() -> None:
    """AC3 (idempotency): when the router handler discovers a clue
    pre-narrator and the narrator footnote path later re-discovers the
    same clue, KnownFact is minted exactly once. The second discovery
    hits the ``is_new`` guard in consume_clue_footnotes (checks
    ``fact_id not in scenario.discovered_clues``) and skips the mint.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )
    from sidequest.server.dispatch.scenario_clue_intake import (
        consume_clue_footnotes,
    )

    snap = _snapshot_with_scenario(
        clue_ids=["library_key"],
        interaction=10,
        character_name="Rux",
    )

    # Phase 1: Router handler discovers the clue pre-narrator
    dispatch = _scenario_clue_dispatch(fact_id="library_key")
    await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    assert "library_key" in snap.scenario_state.discovered_clues  # type: ignore[union-attr]
    assert len(snap.characters[0].known_facts) == 1

    # Phase 2: Narrator footnote path re-discovers the same clue
    footnotes = [_footnote(summary="The key fits.", fact_id="library_key")]
    consume_clue_footnotes(snap, footnotes, active_character_name="Rux")

    assert len(snap.characters[0].known_facts) == 1, (
        "double-mint detected: KnownFact appended twice for the same clue. "
        "The is_new guard in consume_clue_footnotes should prevent this — "
        "the clue was already in discovered_clues from the router handler."
    )


@pytest.mark.asyncio
async def test_footnote_then_handler_does_not_double_mint_known_fact() -> None:
    """AC3 (reverse order): footnote path discovers first, then the
    router handler fires for the same clue. KnownFact minted once.

    FAILS TODAY: handler does not exist.
    """
    from sidequest.agents.subsystems.scenario_clue import (
        run_scenario_clue_dispatch,
    )
    from sidequest.server.dispatch.scenario_clue_intake import (
        consume_clue_footnotes,
    )

    snap = _snapshot_with_scenario(
        clue_ids=["library_key"],
        interaction=10,
        character_name="Rux",
    )

    # Phase 1: Footnote path discovers first
    footnotes = [_footnote(summary="The key fits.", fact_id="library_key")]
    consume_clue_footnotes(snap, footnotes, active_character_name="Rux")

    assert len(snap.characters[0].known_facts) == 1

    # Phase 2: Router handler fires for the same clue
    dispatch = _scenario_clue_dispatch(fact_id="library_key")
    await run_scenario_clue_dispatch(
        dispatch,
        snapshot=snap,
        player_name="Rux",
    )

    assert len(snap.characters[0].known_facts) == 1, (
        "double-mint detected: handler must not re-mint a KnownFact "
        "for a clue already discovered via the footnote path."
    )


# ---------------------------------------------------------------------------
# AC4: Lie-detector watcher covers scenario_clue dispatch mismatches
# (Verifies existing 59-3 coverage — these tests should PASS TODAY)
# ---------------------------------------------------------------------------


def test_lie_detector_emits_mismatch_when_scenario_clue_dispatched_not_engaged() -> None:
    """AC4 (verification): the dispatch engagement watcher shipped in
    59-3 AC4 must emit dispatch_engagement.scenario_clue.mismatch when
    the router dispatches scenario_clue but the fact_id is not in
    discovered_clues.

    This test SHOULD PASS TODAY — it verifies existing watcher coverage,
    not new implementation.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    dispatch = _scenario_clue_dispatch(fact_id="library_key")
    package = _package_with(dispatch)

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert len(mismatches) == 1, (
        "watcher must detect scenario_clue dispatch with no matching "
        f"discovery; got {len(mismatches)} mismatches"
    )
    assert mismatches[0].subsystem == "scenario_clue"
    assert mismatches[0].dispatched_type == "library_key"


def test_lie_detector_no_false_positive_when_scenario_clue_engaged() -> None:
    """AC4 (no false positive): when the clue has been discovered, the
    watcher must emit NO mismatch span.

    This test SHOULD PASS TODAY.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    assert snap.scenario_state is not None
    snap.scenario_state.discover_clue("library_key")

    dispatch = _scenario_clue_dispatch(fact_id="library_key")
    package = _package_with(dispatch)

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)
    assert len(mismatches) == 0, (
        "watcher must NOT emit a mismatch when the clue has been "
        f"discovered; got {len(mismatches)} false positives"
    )


def test_lie_detector_mismatch_when_no_scenario_state() -> None:
    """AC4 (edge): dispatching scenario_clue against a snapshot with
    no scenario_state is a genuine mismatch (the router classified
    investigation intent in a non-scenario world).

    This test SHOULD PASS TODAY.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        detect_dispatch_engagement_mismatch,
    )

    snap = _snapshot_without_scenario()
    dispatch = _scenario_clue_dispatch(fact_id="library_key")
    package = _package_with(dispatch)

    mismatches = detect_dispatch_engagement_mismatch(package=package, snapshot=snap)

    assert len(mismatches) == 1
    assert mismatches[0].subsystem == "scenario_clue"
    assert "scenario_state is None" in mismatches[0].evidence


# ---------------------------------------------------------------------------
# Wiring: handler registration — CLAUDE.md "Every Test Suite Needs a
# Wiring Test". Reflection-based, not source-grep.
# ---------------------------------------------------------------------------


def test_scenario_clue_handler_registered_with_dispatch_bank() -> None:
    """Wiring guarantee: importing sidequest.agents.subsystems runs
    _register_defaults() which must include the scenario_clue handler
    under the key "scenario_clue". Without this, the bank's
    _REGISTRY.get("scenario_clue") returns None and dispatches log as
    unknown_subsystem — silently dropping engagement.

    FAILS TODAY: _register_defaults() does not include scenario_clue.
    """
    from sidequest.agents.subsystems import get_registered

    registry = get_registered()
    assert "scenario_clue" in registry, (
        f"scenario_clue handler not registered; bank has {sorted(registry)}. "
        "Story 59-6 must add the registration in "
        "sidequest/agents/subsystems/__init__.py:_register_defaults()."
    )
    fn = registry["scenario_clue"]
    assert callable(fn) and getattr(fn, "__name__", "") == "run_scenario_clue_dispatch", (
        f"registered scenario_clue handler should be run_scenario_clue_dispatch; "
        f"got {fn!r}"
    )


@pytest.mark.asyncio
async def test_run_dispatch_bank_invokes_scenario_clue_handler() -> None:
    """End-to-end through the real dispatch bank: a package with one
    scenario_clue dispatch produces a snapshot whose scenario_state has
    the fact_id in discovered_clues.

    Also pins the kwargs filtering: the bank passes only the kwargs the
    handler signature declares (per _filter_context_for_callable).

    FAILS TODAY: handler does not exist; bank logs unknown_subsystem.
    """
    from sidequest.agents.subsystems import run_dispatch_bank

    snap = _snapshot_with_scenario(clue_ids=["library_key"])
    package = _package_with(_scenario_clue_dispatch())

    await run_dispatch_bank(
        package,
        context={
            "snapshot": snap,
            "pack": MagicMock(),
            "player_name": "Rux",
        },
    )

    assert snap.scenario_state is not None
    assert "library_key" in snap.scenario_state.discovered_clues, (
        "dispatch bank did not engage the clue graph — handler either "
        "unregistered, signature mismatch, or no-op'd silently"
    )
    assert len(snap.characters[0].known_facts) == 1, (
        "dispatch bank handler did not mint KnownFact on the character"
    )


__all__ = []
