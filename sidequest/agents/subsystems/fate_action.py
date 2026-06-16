"""fate_action subsystem dispatch handler — Intent Router live engager for the
Fate Core channel (ADR-144 F2a).

The router (``sidequest/agents/intent_router.py``) classifies a freeform player
action in a Fate-bound pack and emits a ``SubsystemDispatch`` with
``subsystem="fate_action"`` and ``params`` mirroring ``FateActionPayload``
(action / skill / target / difficulty / invoke_aspect / aspect_text). This
handler is the freeform-text counterpart to F1d's explicit ``FATE_ACTION``
message channel: it builds the payload and routes it to the SAME engine entry,
``dispatch_fate_action`` (which ``isinstance``-gates to the Fate engine and seals
or resolves the exchange).

Engagement happens BEFORE the narrator runs, so the narrator (F2b/F2c) narrates
already-real Fate state. Returns an empty ``SubsystemOutput`` on success — the
engagement is the directive (the resolved exchange is the narrator's grounding
truth), exactly like ``run_confrontation_dispatch``.

No silent fallbacks: an invalid action propagates as ``ValueError`` (the bank
records the error span); a non-Fate ruleset / no active encounter / an unseated
actor surface as ``FateConflictError`` from ``dispatch_fate_action``, caught and
returned as ``data["error"]`` so the bank continues and the watcher sees the gap.
"""

from __future__ import annotations

import logging
import random

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import FateConflictError, dispatch_fate_action
from sidequest.telemetry.spans.fate import fate_action_classified_span

logger = logging.getLogger(__name__)

_VALID_ACTIONS = ("overcome", "create_advantage", "attack", "concede")


async def run_fate_action_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: GenrePack,
    player_name: str,
    rng: random.Random | None = None,
) -> SubsystemOutput:
    """Engage one classified Fate action on the canonical snapshot.

    ``rng`` is an injection seam (mirrors ``dispatch_fate_action``'s own ``rng``
    contract): production omits it and the bank's context-filtering supplies
    nothing, so a fresh ``random.Random()`` is used; tests pass a fixed RNG via
    ``run_dispatch_bank(context={"rng": ...})`` to make the 4dF roll deterministic.
    """
    params = dispatch.params
    action = params.get("action")
    if action not in _VALID_ACTIONS:
        # No silent fallback: a fate_action dispatch with no valid action is a
        # router-output bug. The bank's exception-catch wraps this as an error
        # span; the watcher then sees zero engagement.
        raise ValueError(
            f"fate_action dispatch has invalid params['action']={action!r}; "
            f"must be one of {_VALID_ACTIONS}"
        )

    skill = str(params.get("skill", ""))
    raw_target = params.get("target")
    target = str(raw_target) if raw_target else None
    payload = FateActionPayload(
        request_id=dispatch.idempotency_key,
        action=action,
        skill=skill,
        target=target,
        difficulty=int(params.get("difficulty", 0) or 0),
        invoke_aspect=str(params.get("invoke_aspect", "")),
        # Story 118-10: the F2a freeform channel mirrors F1d — carry the invoke
        # KIND and the RP-flavor rider so a router-classified reroll-invoke or a
        # "chandelier swing" reaches the SAME dispatch engine entry the explicit
        # FATE_ACTION message uses. ``invoke_mode`` defaults to 'bonus' (the wire
        # Literal rejects an out-of-band value); ``player_action`` defaults to ''.
        invoke_mode=str(params.get("invoke_mode", "bonus") or "bonus"),  # type: ignore[arg-type]
        aspect_text=str(params.get("aspect_text", "")),
        player_action=str(params.get("player_action", "")),
    )

    # The F2 lie-detector anchor: a Fate action was classified from language.
    # Emitted before dispatch so the classification is recorded even if the
    # engine rejects the action (the GM panel then shows classify-without-engage).
    fate_action_classified_span(
        actor=player_name,
        action=action,
        skill=skill,
        target=target or "",
        confidence=float(dispatch.confidence),
    )

    ruleset = get_ruleset_module(pack.rules.ruleset)
    try:
        dispatch_fate_action(
            payload=payload,
            actor_name=player_name,
            encounter=snapshot.encounter,
            ruleset=ruleset,
            snapshot=snapshot,
            rng=rng or random.Random(),  # fresh RNG in prod; tests inject a fixed one
            round_number=snapshot.turn_manager.interaction,
        )
    except FateConflictError as exc:
        logger.warning("fate_action.dispatch_error error=%s", exc)
        return SubsystemOutput(data={"error": "fate_dispatch_error"})
    return SubsystemOutput()


__all__ = ["run_fate_action_dispatch"]
