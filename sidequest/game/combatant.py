"""Combatant — structural ``typing.Protocol`` for anything that
participates in combat or HP-driven scenes (ADR-114).

Defined as a ``@runtime_checkable`` Protocol so ``isinstance(x, Combatant)``
works for structural typing of ``Character`` (and eventually ``Npc`` /
``Enemy``).

Per the story 42-1 design deviation (see ``.session/42-1-session.md`` ->
Design Deviations -> TEA), default implementations of ``is_broken`` and
``hp_fraction`` are **not** provided as Protocol defaults — each
concrete implementer carries the two-line bodies verbatim. The Protocol
is a pure contract; ``@runtime_checkable`` only introspects method
*names* and would silently accept subtly-wrong defaults.

Required semantics every implementer MUST carry:

- ``is_broken()``:    ``return self.hp() <= 0``        (not ``== 0``)
- ``hp_fraction()``: ``0.0`` if ``max_hp() == 0`` (not ``1.0``, not
  ``ZeroDivisionError``)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Combatant(Protocol):
    """Common interface for combat / HP participants (ADR-114).

    Character, Npc, and (eventually) Enemy all satisfy this protocol
    structurally.
    """

    def name(self) -> str:
        """The combatant's display name."""
        ...

    def hp(self) -> int:
        """Current HP (HpPool ``current``, clamped to [0, max_hp])."""
        ...

    def max_hp(self) -> int:
        """Maximum HP (HpPool ``max``; may be mid-scene reduced)."""
        ...

    def level(self) -> int:
        """Character level."""
        ...

    def is_broken(self) -> bool:
        """Whether the combatant is broken (HP at or below zero).

        Implementations MUST return ``self.hp() <= 0`` — negative HP
        reads as broken.
        """
        ...

    def hp_fraction(self) -> float:
        """Current HP as a fraction of max (0.0 to 1.0).

        Implementations MUST return ``0.0`` when ``max_hp == 0`` — not
        ``1.0``, not ``ZeroDivisionError``.
        """
        ...
