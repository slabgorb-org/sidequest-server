"""Incapacitation predicate — the single read seam for "is this PC out of play?".

sq-playtest 2026-06-07 (heavy_metal/barsoom-3, blocking): a PC the genre
lethality policy ruled ``dead`` kept full turn agency for four rounds. The
narrator kept responding to a corpse's actions and the player had to infer his
own death. The durable signal is :attr:`Status.incapacitating` (set by
``post_resolution_lethality`` on a LETHAL verdict); this module is the one place
the rest of the engine asks "does this actor carry an incapacitating status?" so
the turn-intake gate and the death-surface message agree on the same answer.

Deliberately keyed on the structured ``incapacitating`` flag, NOT on the status
``text`` (CLAUDE.md: no source/text scraping as a wiring assertion).
"""

from __future__ import annotations

from sidequest.game.status import Status


def find_incapacitating_status(core: object) -> Status | None:
    """Return the first incapacitating status on *core*, or None.

    ``core`` is duck-typed: any object exposing a ``statuses`` iterable of
    :class:`Status` (i.e. a :class:`CreatureCore`). A core with no ``statuses``
    attribute is treated as not incapacitated (returns None) rather than raising
    — callers pass already-resolved cores, and a malformed core is not a reason
    to crash turn intake.
    """
    statuses = getattr(core, "statuses", None)
    if not statuses:
        return None
    for status in statuses:
        if getattr(status, "incapacitating", False):
            return status
    return None


def is_incapacitated(core: object) -> bool:
    """True if *core* carries any incapacitating status (a dead/dying PC)."""
    return find_incapacitating_status(core) is not None
