"""Monster-manual spans — pre-generated NPC injection."""

from __future__ import annotations

from ._core import FLAT_ONLY_SPANS, SPAN_ROUTES, SpanRoute

SPAN_MONSTER_MANUAL_INJECTED = "monster_manual.injected"

# ``monster_manual.hp_preserved`` (BUG 2b) was RETIRED with
# ``GameSnapshot._merge_npc_patch`` (Green Room follow-up, 2026-07-11): the
# hp-preserve decision it observed no longer exists — ``green_room.admit()``
# structurally never touches live ``core.hp`` on a merge (ADR-139 Inv-2), so
# there is no per-turn "keep the damaged HP" branch left to attest.

# Story 107-2 (ADR-059 per-room binding): emitted when a room's structured
# ``encounter_creatures`` binding resolves to its authored bestiary creature(s)
# and that creature is materialized into game state. The GM-panel lie-detector
# that the narrator drew the room's AUTHORED opponent ("Gnaw-Swarm") instead of
# improvising a label ("the creature of animal musk").
SPAN_MONSTER_MANUAL_ROOM_BOUND = "monster_manual.room_bound"

# wry_whimsy/oz fix (H1, 2026-06-14): emitted when ``ensure_loaded`` backfills a
# world's authored ``npcs.yaml`` cast into an ALREADY-seeded Manual (one that no
# longer ``needs_seeding()``, so ``seed_manual`` never re-runs). The GM-panel
# lie-detector that the canonical companions entered the pool on an existing
# save — the original bug was that they never did, so the road surfaced only
# random-minted walk-ons.
SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL = "monster_manual.authored_backfill"

# Story 162-1 (derive-don't-cache, spec D3/V1-V3): emitted when ``ensure_loaded``
# DISCARDS a previously-stamped Manual pool because its ``content_sha`` no longer
# matches — a different content checkout (a multi-clone writer, spec V2).
# content_sha is the ONLY discard axis; session_seed is refreshed for attribution
# but never triggers a discard (a new session with the same content reuses the
# pool). This SUPERSEDES the two removed
# targeted purges (native-class + foreign-bestiary): staleness is now impossible
# because the pool is keyed by content version and discarded wholesale, so the
# beneath_sunden purge/reseed livelock and the barsoom foreign bleed cannot
# recur. Attributes carry the per-world discard counts incl. authored NPCs (the
# "what deleted beneath_sunden's authored NPCs" forensic, spec V3). The GM-panel
# lie-detector that a stale pool was dropped and will re-derive.
SPAN_MONSTER_MANUAL_POOL_DISCARDED = "monster_manual.pool_discarded"

# Story 162-1 rework (spec D4 / OTEL Observability Principle): emitted whenever
# the accumulation cap ENFORCES — a generated NPC/encounter dropped at the cap
# (``kind="npc_dropped"`` / ``"encounter_dropped"``), an authored insert
# evicting a generated walk-on (``kind="npc_evicted"``, with ``evicted``), an
# authored insert refused by an all-authored pool
# (``kind="npc_dropped_all_authored"``), or a legacy over-cap pool bounded
# unconditionally in ``ensure_loaded`` (``kind="trim"``, with
# ``npcs_trimmed``/``encounters_trimmed``) — the trim step runs independently of
# reconcile (story 162-9), so this span can fire even when content_sha is None and
# reconcile was skipped entirely.
# These are inventory mutations the GM panel must see — a dropped or vanished
# pool entry that only exists in a log line is invisible to the lie detector.
# The model returns CapEvent/PoolTrim data; the call sites (pregen seeding,
# authored backfill, and ensure_loaded's unconditional trim step) emit this span.
SPAN_MONSTER_MANUAL_CAP_ENFORCED = "monster_manual.cap_enforced"

# Story 153-x (ADR-106 region population): emitted when a generated region's
# frozen procedural roster (Task 3) is injected into snapshot.npcs, region-
# stamped for region-keyed seating. The GM-panel lie-detector that procedural
# rooms field real, statted creatures instead of leaving the narrator to improvise.
SPAN_MONSTER_MANUAL_REGION_POPULATION = "monster_manual.region_population"

# Finding 1 (final review, Green Room follow-up, 2026-07-12): the fill-absent
# merge (``green_room.admit()``'s ``_fill_absent``) only fills a winner's
# ABSENT fields, so a re-injected MM identity's ``location``/``region`` froze
# at first materialization — the deleted ``_merge_npc_patch`` used to refresh
# them every turn. ``monster_manual_inject.inject()`` now performs that
# refresh itself, scoped to identities ``admit()`` reports MERGED this turn
# only; HP/disposition/beliefs are never touched (ADR-139 Inv-2). Routed (not
# flat-only), mirroring ``spans/green_room.py``'s registration shape, so the
# GM panel's typed tab renders the moved location/region directly — the
# lie-detector that a re-injected identity's placement is actually current,
# not frozen at first sight.
SPAN_MONSTER_MANUAL_PLACEMENT_REFRESHED = "monster_manual.placement_refreshed"
SPAN_ROUTES[SPAN_MONSTER_MANUAL_PLACEMENT_REFRESHED] = SpanRoute(
    event_type="state_transition",
    component="monster_manual",
    extract=lambda span: {
        "field": "monster_manual",
        "op": "placement_refreshed",
        "identity_key": (span.attributes or {}).get("identity_key", ""),
        "location": (span.attributes or {}).get("location", ""),
        "region": (span.attributes or {}).get("region", ""),
    },
)

FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_INJECTED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_ROOM_BOUND)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_POOL_DISCARDED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_CAP_ENFORCED)
FLAT_ONLY_SPANS.add(SPAN_MONSTER_MANUAL_REGION_POPULATION)
