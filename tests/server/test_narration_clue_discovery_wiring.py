"""Wiring test: narration turn → scenario clue discovery (Story 50-5).

This is the integration test that proves seams A + B of ADR-100 are
actually engaged from the production narration path, not just unit-tested
in isolation. Per CLAUDE.md: every test suite needs a wiring test.

It drives ``WebSocketSessionHandler._execute_narration_turn`` with a
mocked ``NarrationTurnResult`` carrying a ``Footnote`` whose ``fact_id``
matches a ``ClueNode`` on the snapshot's bound ``ScenarioState``, and
asserts both the subsystem-level effect (``discovered_clues`` grows,
``SPAN_SCENARIO_ADVANCE`` fires) and the character-level effect
(``KnownFact`` appended with the expected provenance).

If this test passes but the unit tests in
``test_scenario_clue_intake.py`` pass too, the helper is wired.  If the
unit tests pass but this one does not, the helper is dead code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.scenario_state import ScenarioState
from sidequest.genre.models.scenario import ClueGraph, ClueNode
from sidequest.telemetry.spans.scenario import SPAN_SCENARIO_ADVANCE
from tests.server.conftest import _build_turn_context_for_test, span_attrs_by_name


def _seat_character(snap, *, name: str) -> Character:
    """Append a Character named ``name`` to the snapshot and return it.

    ``session_fixture`` populates ``player_seats`` / ``character_locations``
    but does NOT add a Character entry to ``snap.characters``; the active
    player's character must exist so seam B can mint a KnownFact onto it.
    """
    char = Character(
        core=CreatureCore(
            name=name,
            description=f"{name} the adventurer",
            personality="stoic",
            inventory=Inventory(),
        ),
        char_class="Fighter",
        race="Human",
        backstory="placeholder",
    )
    snap.characters.append(char)
    return char


def _fake_local_dm(turn_id: str = "t-clue") -> MagicMock:
    """Match the DispatchPackage stub used by other dispatch-wiring tests."""
    from sidequest.protocol.dispatch import DispatchPackage

    fake_dm = MagicMock()
    fake_dm.decompose = AsyncMock(
        return_value=DispatchPackage(
            turn_id=turn_id,
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
            degraded=False,
            degraded_reason=None,
        )
    )
    return fake_dm


def _bind_scenario_to_snapshot(snap, *, clue_ids: list[str]) -> None:
    snap.scenario_state = ScenarioState(
        clue_graph=ClueGraph(
            nodes=[
                ClueNode(
                    id=cid,
                    type="testimony",
                    description=f"clue {cid}",
                    discovery_method="conversation",
                    visibility="public",
                )
                for cid in clue_ids
            ]
        )
    )


def _scenario_advance_attrs(exporter) -> list[dict]:
    return span_attrs_by_name(exporter, SPAN_SCENARIO_ADVANCE)


# ---------------------------------------------------------------------------
# The wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_turn_discovers_matching_clue_and_mints_known_fact(
    session_fixture, otel_exporter
) -> None:
    """End-to-end: a narration turn whose footnote.fact_id matches the bound
    scenario's clue_graph must:

    1. Add the clue id to ``snapshot.scenario_state.discovered_clues``.
    2. Fire ``SPAN_SCENARIO_ADVANCE`` exactly once.
    3. Append a ``KnownFact`` to the active character with the expected
       provenance (``source='ScenarioClue'``, ``confidence='Discovered'``,
       ``learned_turn`` set to the post-turn interaction counter).
    """
    sd, handler = session_fixture
    _seat_character(sd.snapshot, name=sd.player_name)
    _bind_scenario_to_snapshot(sd.snapshot, clue_ids=["library_key", "muddy_boot"])
    pre_interaction = sd.snapshot.turn_manager.interaction

    # Narrator returns one footnote whose fact_id matches a scenario clue.
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You find the brass library key on the desk.",
            is_degraded=False,
            agent_duration_ms=1,
            footnotes=[
                {
                    "marker": 1,
                    "fact_id": "library_key",
                    "summary": "The brass key opens the library door.",
                    "category": "Lore",
                    "is_new": True,
                }
            ],
        )
    )
    sd.local_dm = _fake_local_dm("t-clue-1")

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I check the desk.", turn_context)

    # Seam A — subsystem state advanced.
    assert sd.snapshot.scenario_state is not None
    assert "library_key" in sd.snapshot.scenario_state.discovered_clues
    advance_events = _scenario_advance_attrs(otel_exporter)
    assert len(advance_events) == 1, (
        f"expected exactly one SPAN_SCENARIO_ADVANCE from the turn, got {len(advance_events)}"
    )
    assert advance_events[0]["clue_id"] == "library_key"
    assert advance_events[0]["duplicate"] is False

    # Seam B — KnownFact minted on the active character with full provenance.
    active = next(c for c in sd.snapshot.characters if c.core.name == sd.player_name)
    minted = [kf for kf in active.known_facts if kf.source == "ScenarioClue"]
    assert len(minted) == 1, (
        f"expected exactly one ScenarioClue-sourced KnownFact on {sd.player_name}, "
        f"got {len(minted)}: {active.known_facts}"
    )
    kf = minted[0]
    assert kf.content == "The brass key opens the library door."
    assert kf.confidence == "Discovered"
    # The turn pipeline may bump interaction during the call; the captured
    # value should be either the pre- or post-turn counter, never zero.
    assert kf.learned_turn >= pre_interaction
    assert kf.learned_turn > 0


@pytest.mark.asyncio
async def test_narration_turn_without_scenario_does_not_emit_or_mint(
    session_fixture, otel_exporter
) -> None:
    """When no scenario is bound, a footnote with fact_id is a no-op for clue
    discovery — no span, no KnownFact mutation from this path."""
    sd, handler = session_fixture
    _seat_character(sd.snapshot, name=sd.player_name)
    assert sd.snapshot.scenario_state is None  # session_fixture default

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The desk is dusty.",
            is_degraded=False,
            agent_duration_ms=1,
            footnotes=[
                {
                    "marker": 1,
                    "fact_id": "library_key",
                    "summary": "A brass key lies on the desk.",
                    "category": "Lore",
                    "is_new": True,
                }
            ],
        )
    )
    sd.local_dm = _fake_local_dm("t-no-scenario")

    active = next(c for c in sd.snapshot.characters if c.core.name == sd.player_name)
    pre_known_facts = list(active.known_facts)

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I check the desk.", turn_context)

    assert _scenario_advance_attrs(otel_exporter) == []
    # No ScenarioClue-sourced KnownFact may have been minted.
    new_facts = [kf for kf in active.known_facts if kf not in pre_known_facts]
    scenario_facts = [kf for kf in new_facts if kf.source == "ScenarioClue"]
    assert scenario_facts == [], (
        f"no scenario bound, but {len(scenario_facts)} ScenarioClue KnownFacts minted"
    )


@pytest.mark.asyncio
async def test_narration_turn_with_non_matching_fact_id_is_silent(
    session_fixture, otel_exporter
) -> None:
    """A footnote whose fact_id is not a scenario clue must not fire the span
    or mint a ScenarioClue KnownFact — the narrator emits worldbuilding facts
    every turn; only matches against the clue_graph belong to the scenario
    pipeline."""
    sd, handler = session_fixture
    _seat_character(sd.snapshot, name=sd.player_name)
    _bind_scenario_to_snapshot(sd.snapshot, clue_ids=["library_key"])

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="The wind howls outside.",
            is_degraded=False,
            agent_duration_ms=1,
            footnotes=[
                {
                    "marker": 1,
                    "fact_id": "weather_note",
                    "summary": "A storm is brewing.",
                    "category": "Lore",
                    "is_new": True,
                }
            ],
        )
    )
    sd.local_dm = _fake_local_dm("t-no-match")

    active = next(c for c in sd.snapshot.characters if c.core.name == sd.player_name)
    pre_known_facts = list(active.known_facts)

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I listen.", turn_context)

    assert _scenario_advance_attrs(otel_exporter) == []
    assert sd.snapshot.scenario_state is not None
    assert sd.snapshot.scenario_state.discovered_clues == set()
    new_scenario_facts = [
        kf for kf in active.known_facts if kf not in pre_known_facts and kf.source == "ScenarioClue"
    ]
    assert new_scenario_facts == [], (
        "non-matching fact_id must not produce a ScenarioClue KnownFact"
    )


@pytest.mark.asyncio
async def test_narration_turn_mints_fact_id_when_narrator_omits(
    session_fixture, otel_exporter, monkeypatch
) -> None:
    """sq-playtest 2026-05-15 [BUG-LOW] dropped KnownFacts wiring test.

    When the narrator emits a footnote *without* a fact_id (per the legacy
    prompt that only required fact_id for callbacks), the server MUST mint
    a stable hash-based id before forwarding the Footnote downstream. The
    UI's strict drop policy (useStateMirror.ts:198) would otherwise silently
    swallow load-bearing world facts. Asserts both the defensive mint and
    the OTEL watcher span that lets the GM panel see the rate.
    """
    from sidequest.server import websocket_session_handler as wsh

    sd, handler = session_fixture
    _seat_character(sd.snapshot, name=sd.player_name)

    captured: list[tuple[str, dict]] = []

    def _capture(event: str, payload: dict, *args, **kwargs) -> None:
        captured.append((event, payload))

    monkeypatch.setattr(wsh, "_watcher_publish", _capture)

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration=("Brother Hesh studies the bond [1]. The courier [2] never speaks."),
            is_degraded=False,
            agent_duration_ms=1,
            footnotes=[
                {
                    "marker": 1,
                    # No fact_id — the bug case.
                    "summary": "Brother Hesh signs the bonds at Ashgate.",
                    "category": "Person",
                    "is_new": True,
                },
                {
                    "marker": 2,
                    # No fact_id — second omitted, both must be minted.
                    "summary": "The Downriver Courier reads lips.",
                    "category": "Person",
                    "is_new": True,
                },
            ],
        )
    )
    sd.local_dm = _fake_local_dm("t-mint-1")

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I greet him.", turn_context)

    mint_events = [p for (e, p) in captured if e == "state.footnote_fact_id_minted"]
    assert len(mint_events) == 1, (
        f"expected one aggregated mint event per turn, got {len(mint_events)}: {captured}"
    )
    assert mint_events[0]["count"] == 2, (
        f"expected count=2 (both footnotes minted), got {mint_events[0]['count']}"
    )
    assert mint_events[0]["reason"] == "narrator_omitted_fact_id"

    # And the forwarded Footnotes (carried inside the broadcast NarrationPayload)
    # must each have a non-None fact_id with the "fn-" prefix.
    forwarded_events = [
        p for (e, p) in captured if e == "state_transition" and p.get("field") == "footnotes"
    ]
    assert forwarded_events, "footnotes_forwarded watcher event missing"
    assert forwarded_events[0]["count"] == 2


@pytest.mark.asyncio
async def test_narration_turn_preserves_narrator_supplied_fact_id(
    session_fixture, otel_exporter, monkeypatch
) -> None:
    """Narrator-supplied fact_ids must NOT be replaced — scenario clue_intake
    matches them against ClueNode.id (genre-authored), and Seam A would break
    if we overwrote them with our defensive hash."""
    from sidequest.server import websocket_session_handler as wsh

    sd, handler = session_fixture
    _seat_character(sd.snapshot, name=sd.player_name)
    _bind_scenario_to_snapshot(sd.snapshot, clue_ids=["library_key"])

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        wsh,
        "_watcher_publish",
        lambda event, payload, *a, **kw: captured.append((event, payload)),
    )

    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(
            narration="You find the brass library key on the desk.",
            is_degraded=False,
            agent_duration_ms=1,
            footnotes=[
                {
                    "marker": 1,
                    "fact_id": "library_key",
                    "summary": "The brass key opens the library door.",
                    "category": "Lore",
                    "is_new": True,
                }
            ],
        )
    )
    sd.local_dm = _fake_local_dm("t-preserve-1")

    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, "I check the desk.", turn_context)

    # No mint event — narrator supplied the id.
    mint_events = [p for (e, p) in captured if e == "state.footnote_fact_id_minted"]
    assert mint_events == [], (
        f"narrator-supplied fact_id must not trigger a mint, got {mint_events}"
    )
