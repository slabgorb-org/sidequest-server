"""Story 153-3 [WRY-WHIMSY-NO-FATE-CONTEST-DEFS] — a Fate Conflict requires an
Other (ADR-116), RED phase.

A `conflict`-mode confrontation resolves against the Other's FateSheet stress, so
it MUST seat an opponent-side Other regardless of category — it can never run
solo. The seating branch (`seat_as_fate_conflict = is_fate and resolution_mode
not in (contest, sealed_letter_lookup)`) already routes `conflict` into the Fate
Conflict path with no change, but `_requires_opponent` must fold `conflict` in so
the No-Opponent guard fires for a Conflict of ANY category — not only the
`combat`/`movement` adversarial-category branch. The combat case is covered by
that existing branch; the social case is the discriminator that pins the
`_requires_opponent` edit.

RED today: `ResolutionMode` has no `conflict` value, so the ConfrontationDef
cannot even be constructed (ValidationError).
"""

from __future__ import annotations

from sidequest.genre.models.rules import ConfrontationDef
from sidequest.server.dispatch.encounter_lifecycle import _requires_opponent


def _conflict_cdef(category: str) -> ConfrontationDef:
    return ConfrontationDef(
        type="clash",
        label="Clash",
        category=category,
        resolution_mode="conflict",
        beats=[{"id": "press", "label": "Press"}],
    )


def test_conflict_combat_requires_opponent():
    assert _requires_opponent(_conflict_cdef("combat")) is True


def test_conflict_social_requires_opponent():
    # The footgun-closer: a Fate Conflict needs an Other even for a non-adversarial
    # category, because it resolves against the opponent's FateSheet stress. This
    # fails unless `conflict` is folded into `_requires_opponent` (the combat-only
    # adversarial-category branch would NOT cover a social conflict).
    assert _requires_opponent(_conflict_cdef("social")) is True
