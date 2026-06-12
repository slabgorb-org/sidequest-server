"""Seam crossing types — result, recoverable error, registry error."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeamCrossingResult:
    """A completed crossing: the procedural node THIS PC is now bound to."""

    to_region: str


class SeamCrossingError(Exception):
    """A crossing that cannot resolve. Recoverable + fail-loud.

    Span-emission contract: resolvers intentionally emit NO span on the
    failure path — span emission (``movement.unresolved`` /
    ``region.entry_rejected``) is the CATCHER's obligation, because each
    consumer door owns its own failure-span vocabulary (movement dispatch
    emits ``movement.unresolved`` via ``_unresolved``; the narration guard
    emits ``region.entry_rejected``).
    """

    def __init__(self, *, reason: str, surface: str) -> None:
        super().__init__(f"seam crossing unresolvable: {reason}")
        self.reason = reason
        self.surface = surface


class UnknownSeamKindError(Exception):
    """A route's ``to_id`` named a seam kind with no registered resolver."""
