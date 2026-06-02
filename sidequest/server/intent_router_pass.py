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

from sidequest.agents.dispatch_precondition_gate import (
    run_dispatch_precondition_gate,
    run_unregistered_subsystem_gate,
)
from sidequest.agents.intent_router import IntentRouter
from sidequest.agents.subsystems import BankResult, get_registered, run_dispatch_bank
from sidequest.game.npc_scene import is_npc_in_scene
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.intent_router import (
    intent_router_confrontation_vocabulary_span,
    intent_router_witnessed_act_vocabulary_span,
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


def _present_npc_names(snapshot: GameSnapshot) -> list[str]:
    """Return the names of NPCs the player's action could be witnessed by.

    The witness candidate set for ``witnessed_act`` classification (spec §5: an
    act with no witness moves nothing). Reuses the canonical scene-membership
    predicate (``sidequest/game/npc_scene.py``) — the SAME one the narrator's
    scene projection trusts — so "present" means here what it means everywhere
    else in the system (no parallel, divergent definition). Scene id is the
    party's consensus location; an unresolved location (pre-chargen / party
    split) yields an empty set unless an unresolved encounter anchors actors.
    """
    current_room = snapshot.party_location()
    encounter = getattr(snapshot, "encounter", None)
    names: list[str] = []
    for npc in snapshot.npcs or []:
        if is_npc_in_scene(npc, current_room=current_room, encounter=encounter):
            names.append(npc.core.name)
    return names


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

    # Witnessed-act vocabulary + witness candidate set (wry_whimsy political
    # substrate, Plan 2b). Double-gated: the pack must declare witnessed-act
    # archetypes AND the world must have hydrated a political layer
    # (snapshot.political_state). The second gate keeps us from prompting the
    # model to emit a dispatch the precondition gate would immediately drop —
    # and keeps every non-political genre's router prompt free of this noise.
    if (
        pack is not None
        and getattr(pack, "witnessed_acts", None)
        and snapshot.political_state is not None
    ):
        summary["witnessed_act_vocabulary"] = [
            {"id": a.id, "label": a.label, "description": a.description}
            for a in pack.witnessed_acts
        ]
        present = _present_npc_names(snapshot)
        summary["present_npcs"] = present
        with intent_router_witnessed_act_vocabulary_span(
            act_count=len(pack.witnessed_acts),
            present_npc_count=len(present),
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
) -> tuple[DispatchPackage, BankResult]:
    """Run the IntentRouter and dispatch bank pre-narrator.

    Returns ``(package, bank_result)``. The caller assigns the package to
    ``turn_context.dispatch_package`` (the narrator prompt builder reads it
    for redaction) and the ``BankResult`` to ``turn_context.bank_result``.
    This pass is the SINGLE dispatch-bank run for the turn: the engines
    engage here, on the snapshot, before the narrator. The orchestrator
    consumes ``bank_result`` for narrator directives + the lethality
    arbiter rather than re-running the bank — re-running would engage every
    engine a second time (a PC moves twice, a clue is consumed twice).

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

    # Unregistered-subsystem gate (Story 71-27): drop dispatches whose
    # ``subsystem`` names no registered handler — the canonical case is the
    # router emitting ``combat`` (a confrontation *type*, routed through the
    # ``confrontation`` subsystem) as if it were a subsystem key. Such a
    # dispatch can NEVER engage; gating it here — BEFORE the precondition gate,
    # the bank, AND the caller's ``turn_context.dispatch_package`` — stops the
    # router from emitting an unhandlable dispatch into the redaction path and
    # the post-turn watcher, while a loud ``intent_router.dispatch.unregistered``
    # span records each drop (NOT a silent fallback). The dispatch bank's own
    # unknown-subsystem skip remains as a defense-in-depth backstop. The
    # registry is the single source of truth — injected here so the gate stays
    # registry-free and testable.
    package = run_unregistered_subsystem_gate(package=package, registered=set(get_registered()))

    # Precondition gate (Story 59-8): drop dispatches that are STRUCTURALLY
    # inert on this snapshot — they can never engage no matter what the
    # narrator does (e.g. scenario_clue with no ADR-053 scenario graph loaded),
    # so engaging them only ever produces a guaranteed
    # ``dispatch_engagement.*.mismatch`` false-positive. Gating here — BEFORE
    # the bank AND before the package is returned to the caller (which assigns
    # it to ``turn_context.dispatch_package`` for the post-turn watcher) —
    # removes the inert dispatch from both the engine run and the lie-detector,
    # while a loud ``intent_router.dispatch.gated`` span records each skip (NOT
    # a silent fallback). A real scenario world is unaffected: the gate only
    # fires when the precondition is unmet.
    package = run_dispatch_precondition_gate(package=package, snapshot=snapshot)

    bank_result = await run_dispatch_bank(
        package,
        context={
            "snapshot": snapshot,
            "pack": pack,
            "player_name": player_name,
            "npcs_present": [],
            "additional_player_names": additional_player_names,
            # ``npc_pool`` is required (kw-only, no default) by
            # ``run_npc_agency``; sourced from the live snapshot so the NPC
            # disposition subsystem engages in THIS pass instead of failing
            # on a missing kwarg.
            "npc_pool": list(snapshot.npc_pool or []),
            # ``npcs`` — the authored roster. npc_agency resolves its target
            # against the roster FIRST (roster NPCs are not mirrored into
            # npc_pool; presence is tracked via last_seen_location), so
            # without this the subsystem never engaged for the game's primary
            # NPCs (playtest #C1, 2026-05-28). Signature-filtered by the bank.
            "npcs": list(snapshot.npcs or []),
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

    return package, bank_result


__all__ = ["execute_intent_router_pre_narrator_pass"]
