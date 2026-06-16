"""Per-player projection filter.

MP-03 shipped the Protocol + PassThroughFilter (pass-through default).
Production now uses ComposedFilter from sidequest.game.projection.composed
for real per-player rules; PassThroughFilter remains as the documented
"no rules configured" fallback and for tests that exercise the Protocol.

New signature as of the ProjectionFilter Rules feature: project takes a
MessageEnvelope + GameStateView + player_id. This supersedes the old
event=EventRow signature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import GameStateView

if TYPE_CHECKING:
    from sidequest.game.repository import SaveTransaction


@dataclass(frozen=True)
class FilterDecision:
    include: bool
    payload_json: str


class ProjectionFilter(Protocol):
    def project(
        self,
        *,
        envelope: MessageEnvelope,
        view: GameStateView,
        player_id: str,
        tx: SaveTransaction | None = None,
        event_seq: int | None = None,
    ) -> FilterDecision:
        """Project ``envelope`` for ``player_id``.

        ``tx`` / ``event_seq`` are threaded ONLY when this projection runs
        inside an open turn transaction (emit_event's ``with repo.transaction()``
        block). They let any telemetry the projection emits (e.g. the
        visibility-gated ``invariant.secret_routed`` watcher event) ride the
        SAME connection rather than opening a competing pooled connection that
        would self-deadlock on the per-session ``FOR UPDATE`` row lock under
        Postgres (ADR-115). Lazy-fill / out-of-frame callers omit them.
        """
        ...


class PassThroughFilter:
    """Include-everything-unchanged filter. Used when no genre rules are configured."""

    def project(
        self,
        *,
        envelope: MessageEnvelope,
        view: GameStateView,
        player_id: str,
        tx: SaveTransaction | None = None,
        event_seq: int | None = None,
    ) -> FilterDecision:
        # PassThroughFilter emits no telemetry, so tx/event_seq are accepted
        # (Protocol conformance) but unused.
        return FilterDecision(include=True, payload_json=envelope.payload_json)
