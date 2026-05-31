"""RED-phase wiring tests for the post-turn lore-accretion dispatch (story 75-1).

Sibling of ``tests/server/dispatch/test_lore_embed.py``. Proves the
accretion seam is actually connected end-to-end, not just that the pure
minting helper works in isolation:

1. The dispatch module exposes ``accrete_for_turn`` (import guard).
2. ``accrete_for_turn`` sweeps the snapshot's PC known_facts into the
   lore store and emits a watcher event (AC3 — GM-panel observability).
3. ``WebSocketSessionHandler._accrete_lore_for_turn`` delegates to the
   module function (refactor-stable wiring guard, per
   ``test_lore_embed.py``).
4. A real narration turn drives accretion — a fact present on the PC at
   turn time becomes a fragment in ``sd.lore_store`` (production-path
   proof; this is the test that fails if the dev adds the function but
   never calls it from the turn).
5. AC4 end-to-end: an accreted fragment, once embedded by the existing
   worker, is retrievable via ``query_by_similarity`` on the next turn.

INTENTIONALLY RED until 75-1 lands — ``lore_accretion`` does not exist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sidequest.game.character import KnownFact
from sidequest.game.lore_embedding import embed_pending_fragments
from sidequest.game.lore_store import LoreSource
from sidequest.protocol.models import FactCategory


# ---------------------------------------------------------------------------
# Deterministic daemon stand-in (mirrors test_lore_rag_wiring._WiringFakeClient)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Returns a constant embedding so query_by_similarity scores cosine=1.0."""

    def __init__(self, embedding: list[float] | None = None) -> None:
        self._embedding = embedding or [1.0, 0.0]
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict:
        self.calls.append(text)
        return {"embedding": list(self._embedding), "model": "fake", "latency_ms": 1}


def _seed_fact(sd, content: str, *, category: FactCategory = FactCategory.Lore) -> KnownFact:
    """Attach a freshly-discovered fact to the factory's PC (named 'Rux')."""
    fact = KnownFact(content=content, category=category, source="GameEvent")
    sd.snapshot.characters[0].known_facts.append(fact)
    return fact


# ---------------------------------------------------------------------------
# 1. Import guard
# ---------------------------------------------------------------------------


def test_lore_accretion_dispatch_exposes_required_function() -> None:
    from sidequest.server.dispatch import lore_accretion

    assert hasattr(lore_accretion, "accrete_for_turn")


# ---------------------------------------------------------------------------
# 2/3. accrete_for_turn behavior + AC3 watcher observability
# ---------------------------------------------------------------------------


def test_accrete_for_turn_mints_pc_known_facts_into_store(
    session_handler_factory,
) -> None:
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    fact = _seed_fact(sd, "The bell tower hides a smuggler's hatch.")

    lore_accretion.accrete_for_turn(handler, sd)

    frag_id = f"lore_kf_{fact.fact_id}"
    assert frag_id in sd.lore_store.fragments
    frag = sd.lore_store.fragments[frag_id]
    assert frag.content == "The bell tower hides a smuggler's hatch."
    assert frag.source == LoreSource.GameEvent
    assert frag.embedding_pending is True


def test_accrete_for_turn_emits_watcher_event(
    session_handler_factory, monkeypatch
) -> None:
    """AC3: accretion is observable on the GM panel. Mirrors the
    run_worker watcher contract (component='lore')."""
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_fact(sd, "An observable fact.")

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(lore_accretion, "_watcher_publish", _capture)

    lore_accretion.accrete_for_turn(handler, sd)

    accretion_events = [
        c for c in captured if c[1].get("field") == "lore_accretion"
    ]
    assert len(accretion_events) == 1
    kind, payload, component, _severity = accretion_events[0]
    assert kind == "state_transition"
    assert payload["op"] == "accreted"
    assert payload["accreted"] == 1
    assert component == "lore"


def test_accrete_for_turn_is_idempotent_across_turns(
    session_handler_factory,
) -> None:
    """Sweeping every turn must not duplicate already-accreted facts."""
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_fact(sd, "Said once, swept twice.")

    lore_accretion.accrete_for_turn(handler, sd)
    lore_accretion.accrete_for_turn(handler, sd)

    assert len(sd.lore_store.fragments) == 1


# ---------------------------------------------------------------------------
# 4. Handler delegate wiring guard (mirror test_lore_embed.py)
# ---------------------------------------------------------------------------


def test_handler_delegate_calls_accrete_for_turn(
    session_handler_factory, monkeypatch
) -> None:
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    captured: list[tuple] = []

    def _spy(h, sd_arg):
        captured.append((h, sd_arg))

    monkeypatch.setattr(lore_accretion, "accrete_for_turn", _spy)

    handler._accrete_lore_for_turn(sd)

    assert captured == [(handler, sd)]


# ---------------------------------------------------------------------------
# 5. Production-path proof — a real turn invokes accretion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_turn_accretes_known_facts(
    session_handler_factory,
) -> None:
    """The strongest wiring guard: drive the real narration turn (mocked
    LLM) with a fact present on the PC, and assert it was accreted. Fails
    if accretion exists but is never called from the turn pipeline."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.session_handler import _build_turn_context

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    fact = _seed_fact(sd, "A fact the narrator surfaced this turn.")
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="You survey the room.")
    )

    await handler._execute_narration_turn(
        sd, "look around", _build_turn_context(sd)
    )

    assert f"lore_kf_{fact.fact_id}" in sd.lore_store.fragments


# ---------------------------------------------------------------------------
# 6. AC4 — accreted fragment is retrievable after embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accreted_fact_is_retrievable_after_embedding(
    session_handler_factory,
) -> None:
    """AC4: accrete → embed (existing worker) → query_by_similarity hits
    the accreted fragment. The full restoration payoff in one test."""
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    fact = _seed_fact(sd, "The reliquary key is buried under the altar.")

    lore_accretion.accrete_for_turn(handler, sd)
    frag_id = f"lore_kf_{fact.fact_id}"
    assert sd.lore_store.fragments[frag_id].embedding_pending is True

    fake = _FakeClient(embedding=[1.0, 0.0])
    result = await embed_pending_fragments(sd.lore_store, client=fake)
    assert result.embedded >= 1
    assert sd.lore_store.fragments[frag_id].embedding_pending is False

    hits = sd.lore_store.query_by_similarity([1.0, 0.0], top_k=5)
    hit_ids = [frag.id for _score, frag in hits]
    assert frag_id in hit_ids


# ---------------------------------------------------------------------------
# Rework (review [HIGH]) — accretion failure must never crash the turn
# ---------------------------------------------------------------------------


def test_accrete_for_turn_does_not_propagate_accretion_exception(
    session_handler_factory, monkeypatch
) -> None:
    """`accrete_for_turn` runs in `_execute_narration_turn` BEFORE narration
    is delivered. Like its sibling post-narration side-effects (`session.persist`,
    `round_invariant` telemetry — both wrapped 'must never crash a turn'), an
    accretion failure must be isolated: logged, surfaced as an `op="failed"`
    watcher event, and SWALLOWED — never re-raised — so the player still gets
    their narration."""
    from sidequest.server.dispatch import lore_accretion

    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    _seed_fact(sd, "A fact that will trip a broken accretion path.")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated accretion failure")

    monkeypatch.setattr(lore_accretion, "accrete_facts_to_lore", _boom)

    captured: list[tuple] = []

    def _capture(event_kind, payload, component=None, severity=None):
        captured.append((event_kind, payload, component, severity))

    monkeypatch.setattr(lore_accretion, "_watcher_publish", _capture)

    # MUST NOT raise — the turn survives.
    lore_accretion.accrete_for_turn(handler, sd)

    failed = [c for c in captured if c[1].get("op") == "failed"]
    assert len(failed) == 1, "accretion failure must emit an op='failed' watcher event"
    kind, payload, component, severity = failed[0]
    assert kind == "state_transition"
    assert payload["field"] == "lore_accretion"
    assert payload["error"] == "RuntimeError"
    assert component == "lore"
    assert severity == "error"
