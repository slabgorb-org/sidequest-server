"""intent_router_pass — pre-narrator engagement helper (Story 59-4, ADR-113).

The Intent Router (``sidequest/agents/intent_router.py``) and the
dispatch bank (``sidequest/agents/subsystems/__init__.py:run_dispatch_bank``)
are independently testable units. The wiring that runs the router and
then the bank against a turn's snapshot/pack/action lived as a dormant
comment in ``websocket_session_handler._execute_narration_turn`` between
Stories 59-2 and 59-4; this module is the extracted callable that
replaces the comment with real wiring.

Extracting the pre-narrator pass into one function does three things:

  1. Makes the wiring testable in isolation (the session-handler's
     turn pipeline has many cross-cutting concerns — monster manual
     injection, status-tick handshakes, OTEL bridge bookkeeping — that
     swamp a focused test of the router→bank ordering).
  2. Keeps the failure surface explicit: ``IntentRouterFailure``
     propagates out of this function and gets handled by the caller
     with no swallow-to-fallback shenanigans (memory rule
     ``feedback_no_fallbacks_hard``).
  3. Provides a reusable seam for future scenes/openings that need to
     engage the spine outside the main narration turn (e.g., scene
     transitions, opening setpieces) — Story 59-5+ may call this from
     non-_execute_narration_turn sites.

Ordering contract: when this function returns successfully, all
mechanical engines for the dispatched subsystems have engaged on the
snapshot. The narrator (which runs after this) sees already-real
state. That is the SOUL Illusionism counter the epic exists to deliver.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.agents.intent_router import IntentRouter
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.intent_router import (
    intent_router_confrontation_vocabulary_span,
)

logger = logging.getLogger(__name__)


def build_intent_router_for_session() -> IntentRouter:
    """Construct the production IntentRouter for a turn.

    Extracted module-level so tests can monkeypatch this factory with
    a stub that returns a ``MagicMock`` (or a fake router yielding an
    empty ``DispatchPackage``) without needing ``ANTHROPIC_API_KEY`` in
    the test environment. Tests MUST NOT spawn a real Claude client —
    pre-existing project rule mirrored from how ``Orchestrator`` is
    stubbed via ``MagicMock(spec=Orchestrator)`` in
    ``tests/server/conftest.py``.

    Production callers reach this through
    ``websocket_session_handler._execute_narration_turn`` once per turn
    (the SDK client is lightweight; per-turn construction matches the
    transient AnthropicAsync client lifecycle and avoids stale
    connection state across long-lived sessions).
    """
    # Lazy import — keeps the helper module importable in test envs
    # that have not set ANTHROPIC_API_KEY (the SDK client validates the
    # key at construction).
    from sidequest.agents.llm_factory import build_intent_router_llm

    return IntentRouter(llm=build_intent_router_llm())


def _build_state_summary(
    snapshot: GameSnapshot,
    *,
    pack: GenrePack | None = None,
) -> dict[str, Any]:
    """Build the slimmed JSON-able state summary the router consumes.

    Mirrors the Valley-zone slimming applied to the narrator prompt
    (ADR-110 Phase A): ``model_dump`` with ``exclude_defaults`` and
    ``exclude_none`` drops empty/zero pydantic defaults so the
    router's Haiku call doesn't pay for noise.

    Story 59-4 keeps this local to avoid coupling the router pass to
    the much heavier ``session_helpers._build_turn_context`` (which
    does sealed-letter handshakes, party-peer assembly, notorious-party
    gating, etc. — orthogonal concerns the router does not care
    about). If the router's needs grow in 59-5+ (richer state for
    magic_working / scenario_clue dispatches), revisit centralizing
    the slimmer.

    Story 59-10: when ``pack`` is provided, appends a compact
    ``confrontation_types`` projection so the Haiku router knows the
    valid type names for the current genre pack. Haiku classifies
    player intent through language understanding — the projection
    provides the closed enum of available types, not verb lists.
    """
    summary = snapshot.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )

    if pack is not None:
        confrontation_defs = pack.rules.confrontations if pack.rules else []
        if confrontation_defs:
            summary["confrontation_types"] = [
                {
                    "type": cdef.confrontation_type,
                    "category": cdef.category,
                }
                for cdef in confrontation_defs
            ]
            with intent_router_confrontation_vocabulary_span(
                type_count=len(confrontation_defs),
                genre_slug=snapshot.genre_slug or "",
            ):
                pass

    return summary


async def execute_intent_router_pre_narrator_pass(
    *,
    intent_router: IntentRouter,
    snapshot: GameSnapshot,
    pack: GenrePack,
    action: str,
    player_name: str,
    additional_player_names: list[str] | None = None,
    dungeon_store: Any | None = None,
    palette: Any | None = None,
    lookahead_handle: Any | None = None,
) -> DispatchPackage:
    """Run the IntentRouter and dispatch bank pre-narrator.

    Returns the ``DispatchPackage`` the router produced — the caller
    assigns this to ``turn_context.dispatch_package`` so the narrator
    prompt builder (downstream) can consume it for redaction and
    narrator-directive injection.

    Raises ``IntentRouterFailure`` (from the router itself) if the
    bounded retry fails. The caller MUST NOT swallow this — the player's
    turn surfaces an explicit error rather than silently continuing to
    the narrator with no mechanical backing.

    The dispatch bank itself catches per-handler exceptions and records
    them as error spans (the bank does not re-raise) — so a single
    subsystem regression does not break the turn, but the watcher
    (Story 59-3) catches the resulting dispatch-without-engagement
    mismatch on the post-turn snapshot.
    """
    state_summary = _build_state_summary(snapshot, pack=pack)
    package = await intent_router.decompose(
        action=action,
        state_summary=state_summary,
    )

    await run_dispatch_bank(
        package,
        context={
            "snapshot": snapshot,
            "pack": pack,
            "player_name": player_name,
            "npcs_present": [],
            "additional_player_names": additional_player_names,
            # Movement subsystem (§0 context threading): the live region
            # graph + palette + worker handle the movement handler needs.
            # The bank signature-filters context, so subsystems that do not
            # declare these kwargs are unaffected.
            "dungeon_store": dungeon_store,
            "palette": palette,
            "lookahead_handle": lookahead_handle,
        },
    )

    logger.debug(
        "intent_router_pass.complete turn_id=%s dispatch_count=%d player=%s encounter_engaged=%s",
        package.turn_id,
        sum(len(pd.dispatch) for pd in package.per_player)
        + sum(len(ca.dispatch) for ca in package.cross_player),
        player_name,
        snapshot.encounter is not None,
    )

    return package


__all__ = ["execute_intent_router_pre_narrator_pass"]
