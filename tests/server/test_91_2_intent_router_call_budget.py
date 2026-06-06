"""Story 91-2 (RED) — Intent Router per-turn call budget + dice-replay redundancy.

Epic 91 "Dark Spend": cost forensics (pingpong [COST-1], 2026-06-05) measured
~575 Haiku calls against ~70 game turns on Jun 4 — **~8 classification calls
per turn** where the design expectation is ~1 (the ADR-113 pre-narrator pass)
plus rare asides. The strongest *structural* suspect (context-story-91-2.md)
is the dice-resolution replay path: ``handlers/dice_throw.py`` re-enters
``_execute_narration_turn`` with a synthesized replay text (e.g.
``[BEAT_RESOLVED] ...``), which re-runs the full pre-narrator pass — a second
Haiku classification for the same player action whose only new input is a dice
outcome the router does not need to classify. The bounded retry
(``intent_router._MAX_TOTAL_ATTEMPTS = 2``) is the second suspect: a steady
schema-rejection rate is a silent 2x floor on SDK round-trips.

These tests pin the story's two testable ACs:

**AC-3 — regression assertion against a future 8x recurrence.** Driven through
the REAL handler path (``handle_message`` -> dice dispatch -> inline replay
narration turn), per the server's "Every Test Suite Needs a Wiring Test" rule
and its "No Source-Text Wiring Tests" corollary — behavior and spans only,
never a source grep:

* A dice-resolution replay turn fires ZERO router classifications (the dice
  outcome adds no new player intent to classify) while still delivering the
  replay narration. Today it fires one — this is the live multiplier, RED.
* A normal player action fires EXACTLY ONE classification (the expected ~1
  ratio the post-fix playtest must show).

**AC-4 — documented budget + loud OTEL breach event.** The per-turn Haiku
classification budget is a documented module constant,
``sidequest.server.intent_router_pass.INTENT_ROUTER_CALL_BUDGET_PER_TURN``
(read late-bound from the module dict at call time, so tests — and operators
diagnosing a storm — can patch it). When a turn's classification SDK
round-trip count exceeds the budget, the
``intent_router.call_budget.breach`` span fires carrying ``turn_id``,
``observed``, and ``budget``, at ERROR status (the "loud" register
``intent_router.failed`` already uses), and the span is registered in
``SPAN_ROUTES`` so the WatcherSpanProcessor routes it to the live GM panel
(ADR-103/ADR-132 — an unrouted span is dropped on the floor and the GM panel
stays blind, exactly the dark-spend failure mode this epic closes).

**Accounting contract:** ``observed`` counts SDK *round-trips*, not
``decompose()`` invocations — the bounded retry inside one decompose is a
second billed call and MUST count toward the budget (the retry storm is the
2x-floor suspect the attribution pass exists to settle; a decompose-level
counter would be structurally blind to it).

AC-1 (attribution evidence from a real playtest) and AC-2 (post-fix playtest
shows the expected ratio) are playtest-evidence ACs recorded in the session
file during the green/verify phases — they are not unit-testable and are
deliberately not faked here.

Harness: reuses the canonical light dice-drive from
``test_dice_throw_momentum_span.py`` (stub room + mocked orchestrator +
``handle_message``) — one source of truth for what a drivable combat
encounter looks like, same pattern 59-20 used when it imported the 59-16
helpers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.agents.intent_router import IntentRouter
from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.protocol.messages import PlayerActionMessage, PlayerActionPayload
from sidequest.protocol.types import NonBlankString
from sidequest.server.session_handler import _State
from sidequest.telemetry.spans._core import SPAN_ROUTES

# Canonical drivable-combat helpers (combat def + active encounter + throw
# message + stub room) — imported, not copied, so the encounter shape stays
# aligned with the momentum-span spec.
from tests.server.test_dice_throw_momentum_span import (
    _install_active_encounter,
    _install_combat_def,
    _StubRoom,
    _throw,
)

BREACH_SPAN = "intent_router.call_budget.breach"
_BUDGET_ATTR = "sidequest.server.intent_router_pass.INTENT_ROUTER_CALL_BUDGET_PER_TURN"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test
    (ADR-115: the narration turn pipeline persists events/projection).
    Mirrors test_dice_throw_momentum_span.py."""
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Counting router stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def classification_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Shadow the autouse router-factory guard with a COUNTING stub.

    Returns the call ledger: one entry (the classified action text) per
    ``decompose`` invocation. The stub never retries, so for these tests
    decompose invocations == SDK round-trips; the retry-accounting test
    below installs a real ``IntentRouter`` with a flaky LLM instead.
    """
    calls: list[str] = []

    async def _counting_decompose(*, action: str, state_summary: object) -> DispatchPackage:  # noqa: ARG001
        calls.append(action)
        return DispatchPackage(turn_id="91-2-count-stub", confidence_global=0.0)

    router = MagicMock()
    router.decompose = _counting_decompose
    monkeypatch.setattr(
        "sidequest.server.intent_router_pass.build_intent_router_for_session",
        lambda: router,
    )
    return calls


def _flaky_then_valid_llm(sdk_calls: list[int]) -> Any:
    """An ``IntentRouterLLM`` whose FIRST emit_tool round-trip times out and
    whose second returns a schema-valid DispatchPackage tool input — driving
    the real bounded-retry loop to exactly 2 SDK round-trips for 1 decompose.
    ``sdk_calls`` accumulates one entry per round-trip."""

    class _Llm:
        async def emit_tool(
            self,
            *,
            system: str,  # noqa: ARG002
            user: str,  # noqa: ARG002
            tool_name: str,  # noqa: ARG002
            tool_description: str,  # noqa: ARG002
            tool_schema: dict[str, Any],  # noqa: ARG002
        ) -> dict[str, Any]:
            sdk_calls.append(1)
            if len(sdk_calls) == 1:
                raise TimeoutError("91-2 synthetic first-attempt timeout")
            return {"turn_id": "91-2-retry-stub", "confidence_global": 0.0}

    return _Llm()


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


def _arm_combat_session(session_handler_factory, *, with_room: bool = True) -> tuple[Any, Any]:
    """A playing single-player session with a drivable live combat encounter.

    ``with_room=True`` (the dice drive): ``handler._room`` is the momentum-span
    ``_StubRoom`` — the dice dispatch needs a room with a ``session`` for
    scene-end accounting. ``with_room=False`` (the player-action drive):
    ``handler._room`` stays ``None`` so ``_handle_player_action`` takes the
    single-player branch straight into ``_execute_narration_turn`` — the
    ``_StubRoom`` does not implement the MP barrier surface
    (``record_pending_action`` et al.) and faking a barrier is not this
    story's business.
    """
    sd, handler = session_handler_factory(genre="caverns_and_claudes")
    handler._state = _State.Playing
    _install_combat_def(sd)
    _install_active_encounter(sd)
    sd.snapshot.characters[0].stats["STRENGTH"] = 14
    if with_room:
        handler._room = _StubRoom()
    sd.orchestrator.run_narration_turn = AsyncMock(
        return_value=NarrationTurnResult(narration="Strike lands."),
    )
    return sd, handler


def _player_action(text: str = "I search the rubble for the silver key.") -> PlayerActionMessage:
    return PlayerActionMessage(
        payload=PlayerActionPayload(
            action=NonBlankString.model_validate(text),
            round=0,
        ),
        player_id="player-1",
    )


def _breach_spans(otel_capture) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == BREACH_SPAN]


# ---------------------------------------------------------------------------
# AC-3 — the structural multiplier: dice-replay re-entry must not reclassify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dice_replay_turn_does_not_reclassify(
    session_handler_factory,
    classification_calls: list[str],
) -> None:
    """A dice-resolution replay narration turn fires ZERO router
    classifications — the dice outcome is a mechanical result, not a new
    player intent, and classifying it is pure spend (the live 8x driver).

    RED against current code: ``dice_throw.py`` re-enters
    ``_execute_narration_turn``, which re-runs the full pre-narrator pass —
    the ledger records one classification for the synthesized replay text.
    """
    sd, handler = _arm_combat_session(session_handler_factory)

    await handler.handle_message(_throw(face=15))

    # The replay narration itself must still happen — suppressing the
    # redundant classification must NOT kill the player-visible replay turn.
    assert sd.orchestrator.run_narration_turn.await_count == 1, (
        "the dice-resolution replay narration turn must still run "
        f"(got await_count={sd.orchestrator.run_narration_turn.await_count})"
    )
    assert classification_calls == [], (
        "a dice-resolution replay turn must NOT re-run the Intent Router — "
        "the dice outcome adds no new player intent to classify, and this "
        "re-entry is the structural suspect for the 8x/turn Haiku volume "
        f"([COST-1]); got {len(classification_calls)} classification(s) "
        f"for: {classification_calls!r}"
    )


@pytest.mark.asyncio
async def test_player_action_fires_exactly_one_classification(
    session_handler_factory,
    classification_calls: list[str],
) -> None:
    """The baseline ratio the post-fix playtest must show: ONE player action
    submit = ONE router classification. Not zero (the spine must engage —
    ADR-113), not two (no double pre-narrator pass)."""
    sd, handler = _arm_combat_session(session_handler_factory, with_room=False)

    await handler.handle_message(_player_action())

    assert sd.orchestrator.run_narration_turn.await_count == 1
    assert len(classification_calls) == 1, (
        "a normal player action must classify EXACTLY once "
        f"(expected ~1/turn per ADR-113); got {len(classification_calls)}: "
        f"{classification_calls!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 — documented budget + loud breach event
# ---------------------------------------------------------------------------


def test_call_budget_is_documented_and_positive() -> None:
    """The per-turn classification budget is a documented module constant —
    the 'documented budget' half of the AC. An undocumented budget is a
    number in someone's head, which is how the 8x went unnoticed."""
    from sidequest.server import intent_router_pass

    budget = intent_router_pass.INTENT_ROUTER_CALL_BUDGET_PER_TURN
    assert isinstance(budget, int), f"budget must be an int, got {type(budget)!r}"
    assert budget >= 1, f"budget must allow the ADR-113 classification itself (>=1); got {budget}"


@pytest.mark.asyncio
async def test_budget_breach_emits_loud_otel_event(
    session_handler_factory,
    classification_calls: list[str],
    otel_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceeding the per-turn classification budget fires the
    ``intent_router.call_budget.breach`` span carrying the turn id, the
    observed count, and the budget — the GM-panel lie-detector for a
    recurrence in production, not just in CI.

    Budget is patched to 0 so the single legitimate classification of a
    normal player action becomes a breach — driving the breach path through
    the REAL handler pipeline without depending on any specific multiplier
    bug. Requires the implementation to read the budget late-bound from the
    module attribute (same contract the 91-1 choke point uses for
    ``build_async_anthropic``).
    """
    monkeypatch.setattr(_BUDGET_ATTR, 0)
    sd, handler = _arm_combat_session(session_handler_factory, with_room=False)

    await handler.handle_message(_player_action())

    assert len(classification_calls) == 1  # the breach IS the one real call
    breaches = _breach_spans(otel_capture)
    assert len(breaches) == 1, (
        f"expected exactly one {BREACH_SPAN!r} span when observed(1) > budget(0); "
        f"got {len(breaches)} "
        f"(all spans: {[s.name for s in otel_capture.get_finished_spans()]!r})"
    )
    attrs = dict(breaches[0].attributes or {})
    assert "turn_id" in attrs, f"breach span must carry turn_id; got {attrs!r}"
    assert attrs.get("observed") == 1, (
        f"breach span must carry the observed SDK call count (1); got {attrs!r}"
    )
    assert attrs.get("budget") == 0, (
        f"breach span must carry the budget it breached (0); got {attrs!r}"
    )
    # "Loud" means the register intent_router.failed already uses: ERROR
    # status, so the GM panel and any status-filtered span query surface it.
    from opentelemetry.trace import StatusCode

    assert breaches[0].status.status_code == StatusCode.ERROR, (
        f"breach must be ERROR-status (loud), got {breaches[0].status.status_code!r}"
    )

    # Player-facing turn must NOT be killed by the breach — the budget is a
    # lie-detector, not a circuit breaker (that's ADR-134's job, story 91-4).
    assert sd.orchestrator.run_narration_turn.await_count == 1, (
        "a budget breach must surface loudly but must not abort the turn"
    )


@pytest.mark.asyncio
async def test_within_budget_no_breach_event(
    session_handler_factory,
    classification_calls: list[str],
    otel_capture,
) -> None:
    """No cry-wolf: a normal single-classification turn within the default
    documented budget emits NO breach span. A lie-detector that fires on
    healthy turns poisons the GM panel (cf. the 59-30 CRY-WOLF ruling)."""
    from sidequest.server import intent_router_pass

    assert intent_router_pass.INTENT_ROUTER_CALL_BUDGET_PER_TURN >= 1

    sd, handler = _arm_combat_session(session_handler_factory, with_room=False)
    await handler.handle_message(_player_action())

    assert len(classification_calls) == 1
    assert sd.orchestrator.run_narration_turn.await_count == 1
    assert _breach_spans(otel_capture) == [], (
        "no breach span may fire when the turn stays within budget"
    )


@pytest.mark.asyncio
async def test_retry_sdk_calls_count_toward_budget(
    session_handler_factory,
    otel_capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``observed`` counts SDK ROUND-TRIPS, not decompose invocations.

    The bounded retry inside one ``decompose`` is a second billed Haiku call
    — the steady "2x floor" suspect from [COST-1]. A budget counter that only
    sees decompose invocations is structurally blind to a retry storm, which
    is exactly the recurrence AC-3 demands we catch. With budget=1 and a
    first-attempt timeout, one player action = 2 SDK round-trips = breach
    with observed=2.
    """
    monkeypatch.setattr(_BUDGET_ATTR, 1)

    sdk_calls: list[int] = []
    real_router = IntentRouter(llm=_flaky_then_valid_llm(sdk_calls))
    monkeypatch.setattr(
        "sidequest.server.intent_router_pass.build_intent_router_for_session",
        lambda: real_router,
    )

    sd, handler = _arm_combat_session(session_handler_factory, with_room=False)
    await handler.handle_message(_player_action())

    assert len(sdk_calls) == 2, (
        f"harness self-check: the flaky LLM must drive exactly 2 SDK "
        f"round-trips (timeout + informed retry); got {len(sdk_calls)}"
    )
    assert sd.orchestrator.run_narration_turn.await_count == 1  # turn recovered
    breaches = _breach_spans(otel_capture)
    assert len(breaches) == 1, (
        f"2 SDK round-trips against budget=1 must breach; got "
        f"{len(breaches)} {BREACH_SPAN!r} span(s) — if this is 0 the counter "
        "is counting decompose invocations and is blind to retry storms"
    )
    attrs = dict(breaches[0].attributes or {})
    assert attrs.get("observed") == 2, (
        f"observed must count SDK round-trips (2: first attempt + bounded "
        f"retry); got {attrs.get('observed')!r}"
    )
    assert attrs.get("budget") == 1


# ---------------------------------------------------------------------------
# GM-panel reachability — the breach span must be routed (ADR-103 / ADR-132)
# ---------------------------------------------------------------------------


def test_breach_span_registered_in_span_routes() -> None:
    """The breach span MUST be registered in ``SPAN_ROUTES``.

    Without this entry the WatcherSpanProcessor drops the span on the floor —
    the live GM panel never sees a production breach, defeating the entire
    point of AC-4 (the GM panel is the lie detector). Mirrors
    ``test_momentum_broadcast_span_is_in_span_routes``.
    """
    assert BREACH_SPAN in SPAN_ROUTES, (
        f"{BREACH_SPAN!r} missing from SPAN_ROUTES — the GM panel will never "
        f"see a production budget breach (registered: "
        f"{sorted(k for k in SPAN_ROUTES if k.startswith('intent_router'))!r})"
    )
    route = SPAN_ROUTES[BREACH_SPAN]
    assert route.component == "intent_router"

    class _FakeSpan:
        attributes = {"turn_id": 7, "observed": 9, "budget": 2}

    extracted = route.extract(_FakeSpan())
    assert extracted.get("turn_id") == 7, f"extract must surface turn_id; got {extracted!r}"
    assert extracted.get("observed") == 9, f"extract must surface observed; got {extracted!r}"
    assert extracted.get("budget") == 2, f"extract must surface budget; got {extracted!r}"
