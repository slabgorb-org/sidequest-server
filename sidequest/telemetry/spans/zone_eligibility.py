"""Zone-eligibility spans — faction/zone-scoped content filtering (epic-157).

``zone_eligibility.filtered`` fires on every *exclusion* at a content seam: a
piece of pooled content (a creature/encounter in Seam 1; NPCs/tropes/seeds in
later seams) was tagged for a faction that does NOT match the party's active
zone, so the engine dropped it before it reached the narrator.

Per the OTEL Observability Principle this is the GM-panel lie-detector for the
scoping: it proves the engine *engaged* (a Houyhnhnm Yahoo was actively
suppressed on the Lilliput shore) rather than the narrator merely not mentioning
it by luck. The attributes carry ``subsystem`` (creature/npc/trope/seed),
``content_id``, ``content_factions``, ``active_factions`` and ``region`` so
forensics can reconstruct *which* item was dropped and *why* from a stored
session — it is a persisted game-engine event, not a live-only pipeline span.
"""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS

SPAN_ZONE_ELIGIBILITY_FILTERED = "zone_eligibility.filtered"

FLAT_ONLY_SPANS.add(SPAN_ZONE_ELIGIBILITY_FILTERED)
