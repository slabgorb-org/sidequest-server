"""confrontation subsystem dispatch handler — Intent Router live engager
(Story 59-4, ADR-113).

The router (``sidequest/agents/intent_router.py``) classifies a player
action and emits a ``DispatchPackage`` whose ``SubsystemDispatch``
entries may include ``subsystem="confrontation"`` with
``params={"type": "<encounter_type>"}``. This handler is what the
dispatch bank invokes for that key — it engages the confrontation
engine on the canonical snapshot BEFORE the narrator runs, so the
narrator narrates already-real engagement state instead of
self-reporting it via the (retired) ``begin_confrontation`` sidecar
lift.

This is the producer side of the SOUL Illusionism counter the epic
exists to deliver: mechanical engagement happens first, OTEL records
it, and the narrator can only describe what the engines actually did.

Engagement is performed by the existing
``instantiate_encounter_from_trigger`` helper — the same single creation
path the legacy ``narration_apply.py`` consumer (now removed) used. The
encounter lands on ``snapshot.encounter`` in place; the watcher
(``dispatch_engagement_watcher``) then confirms post-turn that the
dispatched subsystem actually engaged.

No silent fallbacks: an unknown encounter_type propagates as
``ValueError`` (from the lifecycle helper) so the dispatch bank records
the error span and the watcher observes the engagement gap.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.server.dispatch.encounter_lifecycle import (
    InitiativeUnresolvableError,
    NoOpponentAvailableError,
    SealedLetterArityError,
    instantiate_encounter_from_trigger,
)
from sidequest.telemetry.spans import encounter_empty_actor_list_span

logger = logging.getLogger(__name__)


async def run_confrontation_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: GenrePack,
    player_name: str,
    npcs_present: list[Any] | None = None,
    additional_player_names: list[str] | None = None,
    dungeon_store: Any | None = None,
) -> SubsystemOutput:
    """Engage a confrontation encounter on the canonical snapshot.

    The router-driven equivalent of the legacy
    ``narration_apply.py:2528`` consumer block (removed in Story 59-4).
    Reads ``dispatch.params["type"]`` and routes through the same
    ``instantiate_encounter_from_trigger`` entrypoint, so existing
    OTEL spans (``encounter_confrontation_initiated_span``,
    ``encounter_no_opponent_available_span``,
    ``encounter_invalid_side_span``,
    ``encounter_sealed_letter_arity_rejected_span``) keep firing on
    the new live path — the GM panel sees the same audit trail it had
    pre-cutover.

    ``npcs_present`` is best-effort: the router can include explicit
    actor mentions in dispatch.params when it has them, but most
    confrontation dispatches will arrive with an empty list and rely on
    the location-fallback NPC lookup inside the lifecycle helper
    (Story 45-18). This mirrors how the narrator's sidecar
    ``npcs_present`` worked pre-cutover — the field was almost always
    empty in practice because the narrator emits it AFTER deciding the
    scene, not as a deliberate input.

    Returns an empty ``SubsystemOutput`` on success: the engagement is
    the directive (a created encounter is the narrator's grounding
    truth), and downstream narrator-prompt assembly reads
    ``snapshot.encounter`` directly. No NarratorDirective entries are
    emitted because the narrator does not need to be TOLD to narrate
    an active encounter — the snapshot says it's active.

    Catches the same narrowly-scoped lifecycle exceptions the legacy
    consumer caught (``NoOpponentAvailableError``,
    ``SealedLetterArityError``) so a recoverable engagement gap does
    not crash the turn — the OTEL spans the helper emits record the
    rejection and the watcher catches it as a dispatch-without-engage
    mismatch (the 59-3 lie-detector path). Unknown encounter types
    propagate as ``ValueError`` (the helper raises) — that is a
    config/router error, not a recoverable engagement choice.
    """
    enc_type = dispatch.params.get("type")
    if not enc_type or not isinstance(enc_type, str):
        # No silent fallback (memory rule feedback_no_fallbacks_hard): a
        # confrontation dispatch with no type is a router-output bug. The
        # bank's exception-catch wraps this as an error span; the watcher
        # then sees zero engagement.
        raise ValueError(
            f"confrontation dispatch missing required params['type']; got params={dispatch.params!r}"
        )

    actor_list = list(npcs_present) if npcs_present else []

    # ADR-116 (a confrontation requires an Other): the router names the
    # adversary the contest targets in ``params["opponent"]`` — the person
    # grabbed, the NPC threatened, the hull pursued. Materialize it as the
    # Other so the instantiation seam seats THAT rather than falling back to
    # the location registry (which is empty when the opponent was only minted
    # in narration — playtest 2026-05-31 burning_peace: a contested grapple
    # against a narratively-present-but-unseated watcher found no opponent and
    # collapsed to prose, encounter=null / 0 beats / no DICE_THROW).
    #
    # The seating helper (encounter_lifecycle ``_seat_*``) dedupes by name, so
    # naming an EXISTING NPC simply reuses it; a narrative-only name is created
    # and appended to ``snapshot.npcs``. The backing CreatureCore (HP / AC) is
    # seeded downstream from the confrontation's ``opponent_default_stats``.
    #
    # ``opponent`` is the general field; ``threat`` is the original
    # ship_combat alias (story 59-23) — read both for back-compat. Either may
    # be a ``{"name", "description"}`` object or a bare name string.
    materialized_threat = None
    threat = dispatch.params.get("opponent") or dispatch.params.get("threat")
    if threat and not actor_list:
        from sidequest.agents.orchestrator import NpcMention

        threat_name = threat.get("name") if isinstance(threat, dict) else str(threat)
        if threat_name:
            materialized_threat = NpcMention(
                name=threat_name,
                role="hostile",
                side="opponent",
            )

    if not actor_list and materialized_threat is None:
        # Pre-existing observability: the legacy consumer logged this
        # before falling back to the location-scoped NPC registry. Keep
        # the span so the GM panel's per-turn audit retains the signal.
        with encounter_empty_actor_list_span(
            encounter_type=enc_type,
            genre_slug=snapshot.genre_slug or "",
            player_name=player_name,
        ):
            logger.debug(
                "encounter.empty_actor_list confrontation=%s player=%s "
                "(router dispatched without explicit actors — relying on location fallback)",
                enc_type,
                player_name,
            )

    try:
        instantiate_encounter_from_trigger(
            snapshot=snapshot,
            pack=pack,
            encounter_type=enc_type,
            player_name=player_name,
            npcs_present=actor_list,
            genre_slug=snapshot.genre_slug,
            additional_player_names=additional_player_names,
            security_tier=dispatch.params.get("security_tier"),
            materialized_threat=materialized_threat,
            # 165-3 (ADR-096 v2, Track C2): thread the dungeon store so the seating
            # chokepoint seats actor cells from the room's tactical grid. Sourced
            # from the intent-router pass context (already present there) via the
            # dispatch bank's context-filter — None off a procedural-dungeon world.
            dungeon_store=dungeon_store,
        )
    except NoOpponentAvailableError as exc:
        logger.warning(
            "encounter.no_opponent_available confrontation=%s player=%s reason=%s",
            enc_type,
            player_name,
            exc,
        )
        return SubsystemOutput(data={"error": "no_opponent_available"})
    except SealedLetterArityError as exc:
        logger.warning(
            "encounter.sealed_letter_arity_rejected confrontation=%s player=%s reason=%s",
            enc_type,
            player_name,
            exc,
        )
        return SubsystemOutput(data={"error": "sealed_letter_arity_rejected"})
    except InitiativeUnresolvableError as exc:
        # 158-28 / ADR-006: the combat is ALREADY seated (snapshot.encounter set
        # before the initiative roll); a player stat block missing DEXTERITY must
        # degrade LOUDLY — keep the seat, surface an error outcome the GM panel
        # sees (the bank stamps it on the subsystem span) — not wedge the turn.
        logger.warning(
            "encounter.initiative_unresolvable confrontation=%s player=%s reason=%s",
            enc_type,
            player_name,
            exc,
        )
        return SubsystemOutput(data={"error": "initiative_unresolvable"})

    return SubsystemOutput()


__all__ = ["run_confrontation_dispatch"]
