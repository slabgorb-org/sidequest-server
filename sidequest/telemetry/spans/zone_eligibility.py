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

# ``zone_eligibility.cast_staged`` (Seam 2, story 157-3) fires when authored
# cartography NPC cast is push-staged into the snapshot on region entry — the
# "right cast appears" complement to ``filtered``. Carries ``region`` and the
# staged ``npc_names`` so the GM panel sees the engine surfaced the Emperor /
# Reldresal on entering Mildendo rather than the narrator naming them by luck.
# Persisted, round-stamped game-engine event (flat-only, like its sibling).
SPAN_ZONE_ELIGIBILITY_CAST_STAGED = "zone_eligibility.cast_staged"

FLAT_ONLY_SPANS.add(SPAN_ZONE_ELIGIBILITY_CAST_STAGED)
