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
from typing import Protocol

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import GameStateView


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
    ) -> FilterDecision: ...


class PassThroughFilter:
    """Include-everything-unchanged filter. Used when no genre rules are configured."""

    def project(
        self,
        *,
        envelope: MessageEnvelope,
        view: GameStateView,
        player_id: str,
    ) -> FilterDecision:
        return FilterDecision(include=True, payload_json=envelope.payload_json)
