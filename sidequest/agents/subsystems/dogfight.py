"""dogfight subsystem dispatch handler — Intent Router live engager
(Story 153-6, [SWN-DOGFIGHT-UNREACHABLE]; ADR-077 × ADR-113).

The ADR-077 sealed-letter dogfight engine already exists end-to-end
(``game/dogfight_shot.py``, ``server/dispatch/sealed_letter.py``, the SWN
``dogfight`` ConfrontationDef with ``resolution_mode: sealed_letter_lookup``).
It is seated by the SAME production primitive a confrontation uses,
``instantiate_encounter_from_trigger``. What was missing was the pre-narrator
IntentRouter route: a player who painted a hostile contact and brought weapons
hot got narrator improvisation (the contact breaking off) with zero encounter
starts, because the router named no ship-combat dispatch key and the dispatch
bank had no dogfight handler.

This handler is the producer side of the SOUL Illusionism counter for ship
combat: a high-confidence ship-combat intent SEATS the dogfight FIRST, OTEL
records it (``dogfight.dispatch``), and the narrator can only describe an
already-real engagement. It is the direct sibling of ``course.py`` (Story
153-5) — one subsystem, registered in the dispatch ``_REGISTRY`` and reachable
through the REAL ``run_dispatch_bank``.

Don't Reinvent: seating reuses ``instantiate_encounter_from_trigger`` — the same
entrypoint ``run_confrontation_dispatch`` calls — so the engine's role tagging
(red/blue), frame_hp seeding, and ``encounter.confrontation_initiated`` span all
fire unchanged. A dogfight is just a ConfrontationDef; the subsystem owns the
ADR-077 dogfight TYPE resolution so the router need only name the ship-combat
intent and its opponent.

No Silent Fallbacks (AC-5): a dispatch that cannot seat an Other (no
``opponent`` and no NPC in scene) or finds no dogfight ConfrontationDef is
rejected LOUD via a ``dogfight.dispatch.rejected`` span carrying the reason; it
never silently returns control to the narrator with no indication of why the
engine did not engage.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ResolutionMode
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch
from sidequest.server.dispatch.encounter_lifecycle import (
    NoOpponentAvailableError,
    SealedLetterArityError,
    instantiate_encounter_from_trigger,
)
from sidequest.telemetry.spans.dogfight import (
    dogfight_dispatch_rejected_span,
    dogfight_dispatch_span,
)

logger = logging.getLogger(__name__)


def _resolve_dogfight_type(pack: GenrePack) -> str | None:
    """Return the world's ADR-077 sealed-letter dogfight confrontation type.

    A dogfight is the combat ConfrontationDef whose ``resolution_mode`` is
    ``sealed_letter_lookup`` (the simultaneous-commit duel). Resolving it here
    means the router need not disambiguate ship-combat types (a pack may also
    carry a ``beat_selection`` ``ship_combat`` type — only the sealed-letter one
    is the ADR-077 dogfight). Returns ``None`` when the pack authors no such def
    — the handler then rejects LOUD rather than guessing a type.
    """
    # getattr-walk so duck-typed packs (bare fakes without ``rules`` /
    # ConfrontationDefs without ``resolution_mode``) pass through cleanly — this
    # resolver is now reached from the pre-narrator pass (158-29 force-dispatch),
    # which legitimately sees fixture/fake packs, the same access style
    # ``_confrontation_verb_hits`` already uses. A real pack's ConfrontationDef
    # always carries ``resolution_mode`` (required field), so behavior is
    # unchanged for live content — only fakes resolve to None.
    rules = getattr(pack, "rules", None)
    confrontations = getattr(rules, "confrontations", None) or []
    for cdef in confrontations:
        if (
            getattr(cdef, "resolution_mode", None) == ResolutionMode.sealed_letter_lookup
            and getattr(cdef, "category", None) == "combat"
        ):
            return cdef.confrontation_type
    return None


async def run_dogfight_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: GenrePack,
    player_name: str,
    npcs_present: list[Any] | None = None,
    additional_player_names: list[str] | None = None,
) -> SubsystemOutput:
    """Seat the ADR-077 dogfight on the canonical snapshot from a ship-combat intent.

    Resolves the dogfight ConfrontationDef (``params["type"]`` if the router named
    it, else the pack's sealed-letter combat type), materializes
    ``params["opponent"]`` as the Other (ADR-116, exactly as
    ``run_confrontation_dispatch`` does), and seats the encounter through the
    shared ``instantiate_encounter_from_trigger`` primitive. Emits
    ``dogfight.dispatch`` on success.

    Every failure to seat — no Other (``NoOpponentAvailableError`` /
    ``SealedLetterArityError``), no dogfight type, or an encounter that did not
    land — emits a ``dogfight.dispatch.rejected`` span with the reason and returns
    an error-coded ``SubsystemOutput`` (No Silent Fallbacks; AC-5).
    """
    enc_type = dispatch.params.get("type") or _resolve_dogfight_type(pack)
    if not isinstance(enc_type, str) or not enc_type:
        logger.warning(
            "dogfight.dispatch.rejected reason=no_dogfight_type player=%s genre=%s",
            player_name,
            snapshot.genre_slug,
        )
        with dogfight_dispatch_rejected_span(reason="no_dogfight_type", opponent=""):
            pass
        return SubsystemOutput(data={"error": "no_dogfight_type"})

    actor_list = list(npcs_present) if npcs_present else []

    # ADR-116 (a confrontation requires an Other): materialize the named hostile
    # ship/pilot as the blue actor so the sealed-letter duel can seat even when
    # the contact was only minted in narration. Mirrors run_confrontation_dispatch
    # — ``opponent`` is the general field, ``threat`` the original ship_combat
    # alias (story 59-23); read both. Either may be a {"name", "description"}
    # object or a bare name string.
    materialized_threat = None
    threat = dispatch.params.get("opponent") or dispatch.params.get("threat")
    threat_name = ""
    if threat and not actor_list:
        from sidequest.agents.orchestrator import NpcMention

        raw_name = threat.get("name") if isinstance(threat, dict) else threat
        threat_name = str(raw_name) if raw_name else ""
        if threat_name:
            materialized_threat = NpcMention(
                name=threat_name,
                role="hostile",
                side="opponent",
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
            materialized_threat=materialized_threat,
        )
    except (NoOpponentAvailableError, SealedLetterArityError) as exc:
        logger.warning(
            "dogfight.dispatch.rejected type=%s player=%s reason=%s",
            enc_type,
            player_name,
            exc,
        )
        with dogfight_dispatch_rejected_span(reason=type(exc).__name__, opponent=threat_name):
            pass
        return SubsystemOutput(data={"error": "no_opponent_available"})

    # Verify the engine actually seated a live dogfight on the snapshot — a
    # dispatch span with no encounter behind it would be exactly the
    # convincing-prose-with-no-backing failure the lie-detector exists to catch.
    seated = snapshot.encounter
    if seated is None or seated.encounter_type != enc_type or seated.resolved:
        logger.warning(
            "dogfight.dispatch.rejected reason=not_seated type=%s player=%s encounter=%s",
            enc_type,
            player_name,
            seated.encounter_type if seated is not None else None,
        )
        with dogfight_dispatch_rejected_span(reason="not_seated", opponent=threat_name):
            pass
        return SubsystemOutput(data={"error": "dogfight_not_seated"})

    # ADR-153 §4: surface the seated opponent ace's stance — derived from its
    # disposition — as a narrator directive, so the narrator's PRIMARY maneuver
    # pick is goal-driven rather than improvised (mirrors npc_agency's directive
    # shape). The engine floor (the deterministic disposition fallback at the
    # sealed-letter seam) still guards a skipped or illegal narrator commit.
    opp = next((n for n in snapshot.npcs if n.core.name == threat_name), None)
    stance = opp.disposition.attitude().value if opp is not None else "hostile"
    tendency = {
        "hostile": "presses the attack — favors aggressive reversals even at an energy cost",
        "neutral": "flies a balanced fight — breaks when threatened, takes shots when offered",
        "friendly": "is trying to disengage — favors evasive breaks and energy recovery",
    }.get(stance, "flies to its disposition")
    stance_directive = NarratorDirective(
        kind="must_narrate",
        payload=(
            f"The enemy ace's stance (disposition toward the player: {stance}) {tendency}. "
            "Choose its maneuver consistent with that stance from the legal maneuver menu."
        ),
        visibility=dispatch.visibility,
    )

    with dogfight_dispatch_span(encounter_type=enc_type, opponent=threat_name):
        pass
    return SubsystemOutput(directives=[stance_directive])


__all__ = ["run_dogfight_dispatch"]
