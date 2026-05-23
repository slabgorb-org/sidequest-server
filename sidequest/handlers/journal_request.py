"""JournalRequestHandler — replies to JOURNAL_REQUEST with the player's journal.

ADR-100 Seam C (story 50-14). Closes the server-side gap: per ADR-100 the
UI consumer (``sidequest-ui/src/hooks/useStateMirror.ts:130-155``) was
ready, but no server handler emitted ``JOURNAL_RESPONSE``. This module is
that handler.

Player-to-character resolution goes through
``snapshot.player_seats[player_id]``. Per ADR-036 a player can only
introspect their own seat — there is no ``to`` field on the request, and
no cross-player journal access is supported.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sidequest.protocol.messages import (
    JournalResponseMessage,
    JournalResponsePayload,
)
from sidequest.protocol.models import FactCategory, JournalEntry
from sidequest.server.reference_anchors import reference_url_for_journal_entry
from sidequest.server.session_helpers import _error_msg
from sidequest.telemetry.spans import SPAN_JOURNAL_REPLAY, tracer
from sidequest.telemetry.spans.reference import (
    reference_url_attached_span,
    reference_url_skipped_span,
)

if TYPE_CHECKING:
    from sidequest.game.character import KnownFact
    from sidequest.protocol import GameMessage
    from sidequest.server.websocket_session_handler import WebSocketSessionHandler

logger = logging.getLogger(__name__)

# Categories for which a None URL is a real miss (should fire skipped span).
_URL_EXPECTED_CATEGORIES = frozenset({FactCategory.Lore, FactCategory.Place})


def _build_journal_entry(
    fact: KnownFact,
    *,
    pack_id: str,
    world_slug: str,
    legend_names: tuple[str, ...],
    history_entries: tuple[str, ...],
    location_names: tuple[str, ...],
) -> JournalEntry:
    """Build a JournalEntry from a KnownFact, attaching a reference_url where applicable.

    Lore / Place categories attempt URL attachment via world registries.
    Person / Quest / Ability categories get reference_url=None (normal flow, no span).

    OTEL spans are emitted only for Lore/Place:
    - ``reference.url_attached`` on a successful registry hit.
    - ``reference.url_skipped`` on a registry miss (content drift recoverable).
    """
    url = reference_url_for_journal_entry(
        pack=pack_id,
        world=world_slug,
        category=fact.category,
        content=fact.content,
        legend_names=legend_names,
        history_entries=history_entries,
        location_names=location_names,
    )

    if fact.category in _URL_EXPECTED_CATEGORIES:
        if url is not None:
            with reference_url_attached_span(
                kind=fact.category.value.lower(),
                pack=pack_id,
                world=world_slug,
                keys=(fact.content,),
            ):
                pass
        else:
            with reference_url_skipped_span(
                kind=fact.category.value.lower(),
                pack=pack_id,
                world=world_slug,
                keys=(fact.content,),
                reason="not_in_world_registries",
            ):
                pass

    return JournalEntry(
        fact_id=fact.fact_id,
        content=fact.content,
        category=fact.category,
        source=fact.source,
        confidence=fact.confidence,
        learned_turn=fact.learned_turn,
        reference_url=url,
    )


class JournalRequestHandler:
    """Resolve a JOURNAL_REQUEST against the bound room's snapshot."""

    async def handle(
        self,
        session: WebSocketSessionHandler,
        msg: GameMessage,
    ) -> list[object]:
        room = session._room  # noqa: SLF001
        if room is None or room.snapshot is None:
            logger.info(
                "session.message_rejected_unbound type=JOURNAL_REQUEST state=%s",
                session._state.name,  # noqa: SLF001
            )
            return [
                _error_msg(
                    "Cannot process JOURNAL_REQUEST: room not bound",
                    code="session_unbound",
                )
            ]

        player_id: str = getattr(msg, "player_id", "") or ""
        if not player_id:
            logger.warning(
                "session.journal_request_missing_player_id slug=%s",
                room.slug,
            )
            return [
                _error_msg(
                    "Cannot process JOURNAL_REQUEST: missing player_id",
                    code="invalid_player_id",
                )
            ]

        snapshot = room.snapshot
        character_name = snapshot.player_seats.get(player_id)
        if not character_name:
            logger.info(
                "session.journal_request_unseated slug=%s player_id=%s",
                room.slug,
                player_id,
            )
            return [
                _error_msg(
                    f"Cannot process JOURNAL_REQUEST: player {player_id!r} is not seated",
                    code="player_unseated",
                )
            ]

        character = next(
            (c for c in snapshot.characters if c.core.name == character_name),
            None,
        )
        if character is None:
            logger.warning(
                "session.journal_request_seat_broken slug=%s player_id=%s seat=%s",
                room.slug,
                player_id,
                character_name,
            )
            return [
                _error_msg(
                    f"Cannot process JOURNAL_REQUEST: seat {character_name!r} has no character",
                    code="seat_broken",
                )
            ]

        # Resolve session-level pack/world context for reference URL attachment.
        # _session_data is None only pre-bind — in that case we fall through to
        # plain entries with reference_url=None and no spans (safest, not silent:
        # the join-span above already makes the unbound state visible).
        sd = session._session_data  # noqa: SLF001
        if sd is not None:
            pack_id: str = sd.genre_slug
            world_slug: str = sd.world_slug
            world = sd.genre_pack.worlds.get(world_slug)
            if world is not None:
                legend_names: tuple[str, ...] = tuple(legend.name for legend in world.legends)
                # world.history is Any/free-form — schema not yet normalised.
                # History attachment is deferred until world.history carries a
                # typed list of titled entries (planned Task 9 follow-up).
                history_entries: tuple[str, ...] = ()
                # location_names: world model has no locations registry yet.
                # Location attachment lands fully in Task 9 when LocationEntity
                # is wired into the world loader.
                location_names: tuple[str, ...] = ()
            else:
                legend_names = ()
                history_entries = ()
                location_names = ()

            entries = [
                _build_journal_entry(
                    fact,
                    pack_id=pack_id,
                    world_slug=world_slug,
                    legend_names=legend_names,
                    history_entries=history_entries,
                    location_names=location_names,
                )
                for fact in character.known_facts
            ]
        else:
            # Pre-bind fallback: no pack/world context, no URL attachment, no spans.
            entries = [
                JournalEntry(
                    fact_id=fact.fact_id,
                    content=fact.content,
                    category=fact.category,
                    source=fact.source,
                    confidence=fact.confidence,
                    learned_turn=fact.learned_turn,
                )
                for fact in character.known_facts
            ]

        with tracer().start_as_current_span(SPAN_JOURNAL_REPLAY) as span:
            span.set_attribute("character_name", character_name)
            span.set_attribute("entry_count", len(entries))

        return [
            JournalResponseMessage(
                payload=JournalResponsePayload(entries=entries),
                player_id=player_id,
            )
        ]


HANDLER = JournalRequestHandler()
