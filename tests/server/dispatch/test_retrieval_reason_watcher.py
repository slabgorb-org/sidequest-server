"""Story 84-4 (WI-6) — embed_skipped + card_reasons on the GM-panel watcher event (RED).

The GM panel (Keith's lie-detector) subscribes to the **WatcherHub** event stream,
NOT raw OTLP spans (see ``server/dispatch/universal_retrieval.py`` docstring). 84-1
put ``retrieval.embed_skipped`` on the ``retrieval.universal`` span only — so the
drama-gate decision is visible in Jaeger but INVISIBLE on the dashboard. And the
per-card score decomposition reaches neither.

WI-6 extends the ``state_transition`` event published by ``retrieve_for_turn`` with:
  * ``embed_skipped`` (bool) — currently MISSING from the event fields.
  * ``card_reasons`` (list of per-card payloads) — the §A5 decomposition.

This suite drives the dispatch wrapper with a known ``RetrievedEntities`` (its
``card_scores`` + ``embed_skipped`` populated by 84-1) and a spy on the module's
``_watcher_publish`` alias — the exact pattern of
``test_universal_retrieval_dispatch.py`` — asserting the PUBLISHED EVENT fields,
never source text.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sidequest.agents.npc_context import build_npc_working_set
from sidequest.game.pertinence import PertinenceScore
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager

# ---------------------------------------------------------------------------
# Helpers — known RetrievedEntities + publish spy (mirror 75-7 dispatch tests)
# ---------------------------------------------------------------------------


def _empty_floor() -> Any:
    snap = GameSnapshot(genre_slug="caverns_and_claudes", turn_manager=TurnManager(interaction=1))
    return build_npc_working_set(snap, current_turn=1)


def _score(card_id: str, *, mention: float = 0.0, sim: float = 0.0) -> PertinenceScore:
    return PertinenceScore(
        card_id=card_id,
        mention_contribution=mention,
        here_contribution=0.0,
        recency_contribution=0.0,
        sim_contribution=sim,
        score=mention + sim,
        present_scene=False,
        embed_used=sim != 0.0,
    )


def _retrieved(
    *,
    outcome: str = "success",
    embed_skipped: bool = False,
    card_scores: list[PertinenceScore] | None = None,
) -> Any:
    from sidequest.game.retrieval_orchestration import RetrievedEntities

    return RetrievedEntities(
        floor=_empty_floor(),
        retrieved_npcs=None,
        retrieved_locations=None,
        retrieved_factions=None,
        budget_total=4000,
        floor_count=0,
        floor_token_cost=0,
        fill_candidate_count=len(card_scores or []),
        fill_selected_count=len(card_scores or []),
        fill_token_cost=0,
        rejected_below_similarity=0,
        dimension_mismatch_count=0,
        outcome=outcome,
        embed_skipped=embed_skipped,
        card_scores=card_scores or [],
    )


def _capture_publish(monkeypatch, module) -> list[tuple]:
    captured: list[tuple] = []

    def _spy(event_type, fields, component=None, severity="info", **kwargs):
        captured.append((event_type, fields, component, severity))

    monkeypatch.setattr(module, "_watcher_publish", _spy)
    return captured


def _retrieval_fields(captured: list[tuple]) -> dict:
    events = [c for c in captured if c[1].get("field") == "universal_retrieval"]
    assert len(events) == 1, f"exactly one universal_retrieval event; got {len(events)}"
    return events[0][1]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ===========================================================================
# AC-3 — embed_skipped reaches the watcher event
# ===========================================================================


class TestEmbedSkippedOnWatcherEvent:
    def test_watcher_event_carries_embed_skipped_true_on_gate_skip(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """A named/present drama-gate skip → the published event carries
        ``embed_skipped: True``, so the GM panel sees the cosine pass was
        bypassed (today this only reaches Jaeger, not the dashboard)."""
        from sidequest.server.dispatch import universal_retrieval

        async def _fake_retrieve(*a, **k):
            return _retrieved(outcome="success", embed_skipped=True, card_scores=[])

        monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
        captured = _capture_publish(monkeypatch, universal_retrieval)

        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _run(universal_retrieval.retrieve_for_turn(handler, sd, "I attack Borin"))

        fields = _retrieval_fields(captured)
        assert "embed_skipped" in fields, (
            "the watcher event must carry embed_skipped (the GM-panel reader path)"
        )
        assert fields["embed_skipped"] is True

    def test_watcher_event_carries_embed_skipped_false_on_fill(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """The complement: a thin action that embedded → ``embed_skipped: False``."""
        from sidequest.server.dispatch import universal_retrieval

        async def _fake_retrieve(*a, **k):
            return _retrieved(
                outcome="success",
                embed_skipped=False,
                card_scores=[_score("npc:vex", sim=0.3)],
            )

        monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
        captured = _capture_publish(monkeypatch, universal_retrieval)

        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _run(universal_retrieval.retrieve_for_turn(handler, sd, "look around"))

        fields = _retrieval_fields(captured)
        assert fields.get("embed_skipped") is False


# ===========================================================================
# AC-4 — per-card reasons reach the watcher event
# ===========================================================================


class TestCardReasonsOnWatcherEvent:
    def test_watcher_event_carries_card_reasons_for_selected_cards(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """The published event carries ``card_reasons`` — one decomposition per
        selected fill card — so the dashboard renders WHY each note surfaced from
        the WatcherHub stream (not just Jaeger)."""
        from sidequest.server.dispatch import universal_retrieval

        scores = [_score("npc:vex", sim=0.3), _score("loc:black_hart", mention=1.0)]

        async def _fake_retrieve(*a, **k):
            return _retrieved(outcome="success", embed_skipped=False, card_scores=scores)

        monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
        captured = _capture_publish(monkeypatch, universal_retrieval)

        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _run(universal_retrieval.retrieve_for_turn(handler, sd, "ask around the tavern"))

        fields = _retrieval_fields(captured)
        assert "card_reasons" in fields, (
            "the watcher event must carry card_reasons (§A5 decomposition)"
        )
        reasons = fields["card_reasons"]
        assert isinstance(reasons, list) and len(reasons) == 2
        ids = {r["card_id"] for r in reasons}
        assert ids == {"npc:vex", "loc:black_hart"}
        # Each reason carries the dominant signal so the panel can headline it.
        for r in reasons:
            assert "dominant" in r and "score" in r

    def test_watcher_card_reasons_empty_on_gate_skip(
        self, session_handler_factory, monkeypatch
    ) -> None:
        """A gate-skip has no fill → ``card_reasons`` is an empty list (present,
        never absent — the panel always reads a parseable field)."""
        from sidequest.server.dispatch import universal_retrieval

        async def _fake_retrieve(*a, **k):
            return _retrieved(outcome="success", embed_skipped=True, card_scores=[])

        monkeypatch.setattr(universal_retrieval, "retrieve_turn_context", _fake_retrieve)
        captured = _capture_publish(monkeypatch, universal_retrieval)

        sd, handler = session_handler_factory(genre="caverns_and_claudes")
        _run(universal_retrieval.retrieve_for_turn(handler, sd, "I attack Borin"))

        fields = _retrieval_fields(captured)
        assert fields.get("card_reasons") == []
