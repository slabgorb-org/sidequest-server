"""Wiring tests for AccusationEvaluator in the narration-response path.

Story 50-8 (ADR-053) — AC-5: the evaluator must be invoked in the
narration-response path when prosecution/judgment actions reference
accusations. These tests assert the dispatch sibling exists, has the
expected public callable, and is imported by the WebSocket session
handler — mirroring the ``consume_clue_footnotes`` wiring landed in
Story 50-5.

Without these wiring assertions, unit tests on the evaluator pass while
the GM panel sees no SPAN_SCENARIO_ACCUSATION during live play — the
exact "Claude winging it" failure mode that CLAUDE.md's OTEL
Observability Principle exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from sidequest.game.accusation import EvidenceSummary
from sidequest.game.character import Character, KnownFact
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.scenario_state import ScenarioState
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.scenario import ClueGraph, ClueNode

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _clue(node_id: str, *, red_herring: bool = False) -> ClueNode:
    return ClueNode(
        id=node_id,
        type="testimony",
        description=f"clue {node_id}",
        discovery_method="conversation",
        visibility="public",
        red_herring=red_herring,
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
    character_name: str = "Rux",
    interaction: int = 7,
    discovered: list[str] | None = None,
    known_facts: list[KnownFact] | None = None,
) -> GameSnapshot:
    snap = GameSnapshot(genre_slug="caverns_and_claudes")
    char = _character(character_name)
    if known_facts is not None:
        char.known_facts.extend(known_facts)
    snap.characters.append(char)
    snap.turn_manager.interaction = interaction
    snap.scenario_state = ScenarioState(
        clue_graph=ClueGraph(nodes=[_clue(cid) for cid in clue_ids]),
        guilty_npc="Erskine",
    )
    if discovered:
        snap.scenario_state.discovered_clues.update(discovered)
    return snap


# ---------------------------------------------------------------------------
# Dispatch module surface
# ---------------------------------------------------------------------------


class TestDispatchModuleSurface:
    def test_scenario_accusation_dispatch_module_importable(self) -> None:
        """The sibling dispatch module must exist at the canonical path
        consumers can import. ``scenario_clue_intake`` and
        ``scenario_bind`` already live here — accusation is the third
        sibling in the trio."""
        import sidequest.server.dispatch.scenario_accusation as mod  # noqa: F401

    def test_dispatch_module_exposes_consume_accusation_callable(self) -> None:
        """The dispatch entry point must be a top-level callable named
        ``consume_accusation_request`` — matching the
        ``consume_clue_footnotes`` naming pattern. A class-based or
        differently-named entry point breaks the wiring contract and
        fails this test loudly."""
        from sidequest.server.dispatch import scenario_accusation

        assert hasattr(scenario_accusation, "consume_accusation_request"), (
            "Dispatch module must expose consume_accusation_request — the "
            "narration-response handler invokes this name."
        )
        assert callable(scenario_accusation.consume_accusation_request)

    def test_dispatch_module_declares_all_export(self) -> None:
        """``__all__`` documents the public surface and gates star
        imports."""
        import sidequest.server.dispatch.scenario_accusation as mod

        exported = getattr(mod, "__all__", [])
        assert "consume_accusation_request" in exported, (
            "consume_accusation_request must appear in __all__"
        )


# ---------------------------------------------------------------------------
# Production wiring — websocket session handler must import the
# dispatch sibling in the narration-response code path. Without this,
# the dispatch module is dead code and SPAN_SCENARIO_ACCUSATION never
# fires during live play.
# ---------------------------------------------------------------------------


def _handler_source() -> str:
    handler_path = (
        Path(__file__).resolve().parents[2]
        / "sidequest"
        / "server"
        / "websocket_session_handler.py"
    )
    assert handler_path.exists(), (
        f"WebSocket session handler not found at expected path: {handler_path}"
    )
    return handler_path.read_text(encoding="utf-8")


class TestProductionWiring:
    def test_websocket_handler_references_accusation_dispatch(self) -> None:
        """The handler that drives narration-response must reach the
        accusation dispatch sibling. Grep the source for the module
        name — a non-test consumer is required, otherwise the wiring
        test in tests/game/test_accusation.py becomes vacuous against
        a dead module. Mirrors how Story 50-5 wired
        ``consume_clue_footnotes`` into the same handler."""
        source = _handler_source()
        assert "scenario_accusation" in source or "consume_accusation_request" in source, (
            "websocket_session_handler.py must import the accusation "
            "dispatch module or its public callable. Otherwise the "
            "evaluator never fires during live play — exactly the "
            "'Claude winging it' failure that the OTEL Observability "
            "Principle exists to prevent."
        )


# ---------------------------------------------------------------------------
# Behavior of the dispatch shim. The shim must:
#   1. No-op when no scenario is bound.
#   2. Build evidence from the active character's known_facts (the
#      ScenarioClue-source facts minted by ``consume_clue_footnotes``
#      in Story 50-5).
#   3. Delegate to the AccusationEvaluator and return its EvidenceSummary.
# ---------------------------------------------------------------------------


class TestDispatchBehavior:
    def test_no_scenario_bound_returns_none(self) -> None:
        """When ``snapshot.scenario_state is None`` the dispatch shim is
        a no-op — return ``None`` rather than fabricating a verdict
        against an empty world."""
        from sidequest.server.dispatch.scenario_accusation import (
            consume_accusation_request,
        )

        snap = GameSnapshot(genre_slug="caverns_and_claudes")
        snap.characters.append(_character("Rux"))
        # scenario_state stays None.

        result = consume_accusation_request(
            snap,
            accused_npc="Erskine",
            active_character_name="Rux",
        )

        assert result is None, (
            "No bound scenario must yield None; the shim does not "
            "fabricate verdicts when no clue graph is present."
        )

    def test_dispatch_returns_evidence_summary_from_known_facts(self) -> None:
        """Happy path: the active character carries ScenarioClue-sourced
        ``KnownFact`` entries from Story 50-5. The shim must convert
        these into ``EvidenceItem``s and return a populated
        ``EvidenceSummary``."""
        from sidequest.server.dispatch.scenario_accusation import (
            consume_accusation_request,
        )

        snap = _snapshot_with_scenario(
            clue_ids=["c1", "c2"],
            discovered=["c1", "c2"],
            known_facts=[
                KnownFact(
                    content="Erskine was seen near the library",
                    confidence="Discovered",
                    source="ScenarioClue",
                    learned_turn=4,
                ),
                KnownFact(
                    content="A glove was found in Erskine's study",
                    confidence="Discovered",
                    source="ScenarioClue",
                    learned_turn=5,
                ),
            ],
        )

        result = consume_accusation_request(
            snap,
            accused_npc="Erskine",
            active_character_name="Rux",
        )

        assert isinstance(result, EvidenceSummary), (
            "Shim must return an EvidenceSummary when a scenario is bound "
            "and the active character holds ScenarioClue-sourced facts."
        )
        assert result.accused_npc == "Erskine"
        # The two Discovered facts must have been converted to EvidenceItems.
        assert len(result.evidence) == 2, (
            f"Expected 2 evidence items (one per ScenarioClue KnownFact); "
            f"got {len(result.evidence)}."
        )

    def test_dispatch_emits_scenario_accusation_span(self, otel_capture) -> None:
        """The dispatch path must reach the evaluator, which fires
        SPAN_SCENARIO_ACCUSATION. This is the AC-5 lie-detector check:
        if the span doesn't fire from the dispatch surface, the GM
        panel will never see verdict events during live play."""
        from sidequest.server.dispatch.scenario_accusation import (
            consume_accusation_request,
        )
        from sidequest.telemetry.spans import SPAN_SCENARIO_ACCUSATION

        snap = _snapshot_with_scenario(
            clue_ids=["c1"],
            discovered=["c1"],
            known_facts=[
                KnownFact(
                    content="Erskine confessed under pressure",
                    confidence="Discovered",
                    source="ScenarioClue",
                    learned_turn=6,
                )
            ],
        )

        consume_accusation_request(
            snap,
            accused_npc="Erskine",
            active_character_name="Rux",
        )

        spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_SCENARIO_ACCUSATION]
        assert len(spans) == 1, (
            f"Dispatch shim must fire exactly one SPAN_SCENARIO_ACCUSATION; "
            f"got {len(spans)}. The GM panel reads this span — its absence "
            f"means accusations are happening invisibly."
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs["accused_npc"] == "Erskine"

    def test_dispatch_ignores_non_scenario_known_facts(self) -> None:
        """KnownFacts sourced from non-ScenarioClue origins (e.g.
        regular GameEvent facts) must not be promoted into the
        evidence list. The evaluator only adjudicates evidence that
        ties back to the clue graph; arbitrary backstory facts would
        let the player accuse on irrelevant trivia."""
        from sidequest.server.dispatch.scenario_accusation import (
            consume_accusation_request,
        )

        snap = _snapshot_with_scenario(
            clue_ids=["c1"],
            discovered=["c1"],
            known_facts=[
                KnownFact(
                    content="Erskine prefers Earl Grey",
                    confidence="Certain",
                    source="GameEvent",  # not a scenario clue
                    learned_turn=3,
                ),
                KnownFact(
                    content="Erskine was seen near the library",
                    confidence="Discovered",
                    source="ScenarioClue",
                    learned_turn=4,
                ),
            ],
        )

        result = consume_accusation_request(
            snap,
            accused_npc="Erskine",
            active_character_name="Rux",
        )

        assert result is not None
        assert len(result.evidence) == 1, (
            "Only the ScenarioClue-sourced fact must surface as evidence; "
            "the GameEvent fact must be filtered out."
        )
