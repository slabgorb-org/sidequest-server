"""Monster Manual injection seam — port of ADR-059 per-turn wiring.

This is the doctrine-divergent leaf of the Monster Manual port. The Rust
version (``crates/sidequest-server/src/dispatch/mod.rs:643-681``) appended
``format_nearby_npcs`` and ``format_area_creatures`` text directly to the
narrator's ``state_summary``. Python deviates: per ``project_narrator_
gaslighting_doctrine.md``, we materialize Manual entries into ``snap.npcs``
as runtime ``Npc`` records — built from :class:`NpcPatch` (the flavor/stat
shape each builder emits), then routed through the Green Room single gate
(:func:`sidequest.game.green_room.admit`, ADR-156) — so the narrator sees
them as world truth — never as "available list" prose.

Lifecycle:

1. :func:`ensure_loaded` — idempotent lazy load + seed.  Mirrors Rust
   ``MonsterManual::load`` + ``pregen::seed_manual`` at session-bind. The
   Manual lives on :class:`_SessionData` across the session; Rust re-loaded
   from disk on every turn — Python keeps it in memory and saves at turn
   end (same effective on-disk state, fewer JSON parses).
2. :func:`inject` — builds this turn's :class:`NpcPatch` list from the
   Manual's location-filtered Available pool plus the room-binding /
   region-population feeders, wraps each in a
   :class:`~sidequest.game.green_room.MaterializationCandidate`, and makes
   ONE :func:`~sidequest.game.green_room.admit` call (Green Room Task 2,
   ADR-156 §4).  Emits the ``monster_manual.injected`` OTEL span (Rust
   parity) plus ``green_room.materialized`` per resolved identity.
3. :func:`mark_active_from_narration` — scans narration for Manual NPC
   names and flips matches to ``EntryState.ACTIVE`` (Rust port of
   ``dispatch/mod.rs:1671-1695``).
4. :func:`mark_all_dormant` — wrapper called from the location-change site
   so Active anchors don't follow the party between scenes.

Save cadence: callers are responsible for ``sd.monster_manual.save()``
after the per-turn updates land (matches Rust ``ctx.monster_manual.save()``
at the end of ``intro_messages``).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from sidequest.game import zone_eligibility
from sidequest.game.green_room import MaterializationCandidate, admit
from sidequest.game.monster_manual import EntryState, MonsterManual
from sidequest.game.origin import Origin, OriginKind, identity_key, normalize_name
from sidequest.game.session import NpcPatch
from sidequest.genre.names.generator import sanitize_display_name
from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.monster_manual import (
    SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL,
    SPAN_MONSTER_MANUAL_CAP_ENFORCED,
    SPAN_MONSTER_MANUAL_INJECTED,
    SPAN_MONSTER_MANUAL_POOL_DISCARDED,
    SPAN_MONSTER_MANUAL_REGION_POPULATION,
)
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.server.session_handler import _SessionData

logger = logging.getLogger(__name__)


# Cap on Available NPCs surfaced into the snapshot when no anchor is
# active at the current location. Mirrors the Rust
# ``format_nearby_npcs`` "Other known NPCs" slice (top 3).
_AVAILABLE_NPC_INJECT_LIMIT = 3
# Cap on ACTIVE-at-location humans re-surfaced into the snapshot each turn.
# sq-playtest 2026-06-13 (oz): the Active-at-location loop was UNCAPPED, so a
# scene where the narrator had named several NPCs re-injected all of them every
# turn (observed 7-10 "nearby" humans on a quiet road) — over-feeding the
# narrator's bench and amplifying the entourage feel. A genuinely-present active
# NPC also persists as a stateful ``snapshot.npcs`` entry (the cite path), so
# bounding this re-injection bench does NOT make a present NPC vanish; it only
# stops dangling the whole roster as "nearby" candidates. Generous enough for a
# legitimately populated scene, bounded enough that a lull does not accrete a
# crowd.
_ACTIVE_NPC_INJECT_LIMIT = 5
# Cap on encounter blocks materialized outside of combat. In combat the
# narrator gets every Available encounter so the creature stat blocks land
# in ``snapshot.npcs``; out of combat we surface only the leading 2 so a
# marketplace doesn't spawn eight monsters into the world state.
_OUT_OF_COMBAT_ENCOUNTER_LIMIT = 2


def _patch_identity_key(patch: NpcPatch) -> str:
    """Identity key for a patch (story 162-2): the stamped origin where the
    builder set one, else the patch's own ``creature_id`` (an unstamped
    creature patch still keys on its bestiary id, never its display name)."""
    origin = patch.origin
    if origin is None and patch.creature_id:
        origin = Origin(kind=OriginKind.MANUAL_POOL, creature_id=patch.creature_id)
    return identity_key(origin, patch.name)


def _candidate(
    npc: Npc,
    *,
    kind: OriginKind,
    source: str,
    creature_id: str | None = None,
    authored_id: str | None = None,
) -> MaterializationCandidate:
    """Wrap a materialized ``Npc`` for the Green Room gate (ADR-156 Task 2).

    ``creature_id`` is passed through only where the builder's id is a
    genuine, per-individual identifier — room-binding's authored bestiary
    reference (``_creature_patch_from_bestiary_entry``). The encounter and
    region-population builders do NOT get their ``patch.creature_id`` routed
    here, on purpose: it is not a reliable per-individual id.

    * Encounters: ``pregen.py``'s own docstring records that
      ``encountergen.generate_enemy_from_bestiary`` "carries no creature_id"
      — ``_creature_patch_from_enemy`` falls back to the raw enemy's
      ``class``, which both content pipelines (``creature_to_enemy_block``
      and ``generate_enemy_from_bestiary``, encountergen.py) hardcode to the
      literal ``"creature"`` for every creature-type enemy. Every creature
      enemy in every encounter, in every genre, shares that one fallback
      value.
    * Region population: ``RegionCreature.creature_type`` is a SPECIES tag,
      not a per-instance id — ``test_inject_region_population_ooc_cap``'s own
      fixture proves it (five distinctly-named ``Mob0..Mob4`` rows all typed
      ``"mob"``).

    Routing either straight through as an ``Origin.creature_id`` would key
    ``identity_key`` on that shared tag and collapse every same-species /
    same-fallback-class enemy in one scene onto ONE seat — a materialization
    regression, not a dedup. Both feeders key on normalized display NAME
    instead (``creature_id=None`` here), which is exactly their
    pre-Green-Room name-keyed dedup behavior (``GameSnapshot.apply_world_patch``
    matched by ``npc_patch.name``), now just routed through ``admit()``.
    """
    return MaterializationCandidate(
        npc=npc,
        origin=Origin(kind=kind, creature_id=creature_id, authored_id=authored_id),
        source=source,
    )


def _sanitize_patch_names(patches: list[NpcPatch]) -> tuple[list[NpcPatch], int]:
    """Strip junk from Manual NPC names before they enter game state.

    The Monster Manual is a long-lived on-disk cache: a name minted by older
    generator code (playtest 2026-06-10: ``Vesper (version)`` in a stale
    coyote_star manual) survives every reload and would otherwise reach the
    player-facing snapshot verbatim. We clean at the injection boundary so the
    surface is correct regardless of cache vintage.

    Returns the kept patches (mutated in place with clean names) and the count
    that were altered. A name that sanitizes to nothing is unsalvageable —
    drop the patch loudly rather than inject a nameless NPC.
    """
    kept: list[NpcPatch] = []
    sanitized = 0
    for patch in patches:
        clean = sanitize_display_name(patch.name)
        if clean == patch.name:
            kept.append(patch)
            continue
        if not clean:
            logger.warning(
                "monster_manual.name_unsalvageable — dropping NPC patch (raw=%r)",
                patch.name,
            )
            continue
        logger.warning("monster_manual.name_sanitized — raw=%r clean=%r", patch.name, clean)
        patch.name = clean
        sanitized += 1
        kept.append(patch)
    return kept, sanitized


def _content_sha_for(pack: Any, world: str) -> str | None:
    """Content version the MM pool is derived under (story 162-1, spec D3).

    A stable hash of the world's effective bestiary — the creature roster the
    pool is seeded from, and the axis along which clones' content checkouts
    diverge (foreign-bestiary bleed, roster churn). When the roster changes the
    sha changes and :meth:`MonsterManual.reconcile_content` discards the
    now-stale pool.

    Returns ``None`` when the bestiary is UNRESOLVABLE — no pack, no
    ``effective_bestiary`` accessor, or it resolves ``None`` for this world.
    That is *no evidence*, not a content state: the caller must skip
    reconciliation entirely rather than judge staleness on a roster it cannot
    read (162-1 rework; the removed foreign-purge had the same None-bestiary
    conservatism — 87-4, never silently empty the pool). A bestiary that IS
    present but has zero entries is a real, hashable state and gets the stable
    empty-roster digest — the two cases must never be conflated.
    """
    # ``pack`` is duck-typed (a real GenrePack or a test stub) — keep the accessor
    # dynamic so the (Bestiary | None, str) unpack type-checks.
    effective_bestiary = getattr(pack, "effective_bestiary", None) if pack is not None else None
    if not callable(effective_bestiary):
        return None
    resolved: Any = effective_bestiary(world)
    bestiary, _source = resolved
    if bestiary is None:
        return None
    entries = [
        f"{entry.name}:{entry.hp}:{entry.level}:{entry.armor_class}" for entry in bestiary.entries
    ]
    payload = "|".join(sorted(entries)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _session_seed_for(sd: _SessionData) -> str:
    """Per-session ATTRIBUTION key (story 162-1) — never a staleness axis.

    The ``SessionRoom`` slug identifies the session that last reconciled the
    pool, carried on the ``pool_discarded`` span for the V1/V2 forensics
    (which session/clone touched this pool). It does NOT gate discards —
    content_sha is the only staleness axis; a new session with unchanged
    content reuses the pool (see ``MonsterManual.reconcile_content``). Falls
    back to the world slug when no room is bound (pre-room construction /
    synthetic paths) — still a stable non-empty attribution, never blank.
    The live per-turn path always has a room, so the fallback is defensive only.
    """
    room = getattr(sd, "_room", None)
    slug = getattr(room, "slug", None) if room is not None else None
    if slug:
        return slug
    return getattr(sd, "world_slug", "") or ""


def ensure_loaded(sd: _SessionData) -> MonsterManual | None:
    """Lazy-load and seed ``sd.monster_manual``. Idempotent.

    Returns the loaded Manual, or ``None`` when the session has no genre
    bound yet (pre-chargen sockets — the Manual is genre/world-keyed so
    there's nothing to load).

    On first call: reads ``~/.sidequest/manuals/{genre}_{world}.json``,
    calls :func:`sidequest.server.dispatch.pregen.seed_manual` if the
    Manual needs more Available entries, and stashes the result on
    ``sd.monster_manual``. Subsequent calls return the cached instance
    without touching disk — matches the Rust shape where Manual lifetime
    is the dispatch context (Python's _SessionData is the longer-lived
    analog).
    """
    if sd.monster_manual is not None:
        return sd.monster_manual
    if not sd.genre_slug:
        return None
    # The Manual is genre+WORLD-keyed — without a resolved world there is nothing
    # to load. Skip cleanly (same contract as the no-genre case above) rather than
    # keying the cache on an empty world slug — the `caverns_and_claudes_.json`
    # bug (story 162-1). MonsterManual.load also fails loud on a blank world; this
    # early return keeps pre-world-resolution turns from ever reaching it.
    if not sd.world_slug:
        return None

    manual = MonsterManual.load(sd.genre_slug, sd.world_slug)
    pack = sd.genre_pack

    # Derive-don't-cache (story 162-1, spec D3): this genre+world-keyed cache is
    # shared across sessions and ~4 clones with divergent content checkouts — the
    # source of the beneath_sunden purge/reseed livelock and the barsoom
    # foreign-bestiary bleed. The pool carries the content_sha it was derived
    # under and is DISCARDED wholesale when the CONTENT changed (multi-clone
    # divergent checkout), REPLACING the two removed targeted purge tourniquets —
    # a stale pool can never be reused or livelock a purge/reseed cycle.
    # session_seed is attribution only (V1/V2 forensics): a new session with
    # unchanged content REUSES the pool. reconcile_content adopts a
    # legacy/unstamped pool without discarding (no nuking pre-162-1 saves).
    # Legacy over-cap pools are bounded via trim_to_caps below — UNCONDITIONALLY
    # and independent of this reconcile step (story 162-9; see the comment at the
    # trim call), since pool size is knowable without content evidence. Emit the
    # forensic spans so the GM panel sees both decisions (OTEL Observability,
    # spec V1-V3).
    #
    # A None content_sha means the bestiary is UNRESOLVABLE (no pack, or a
    # transiently-broken content state) — no evidence, no reconcile: judging
    # staleness on a roster we cannot read would wholesale-discard a valid pool
    # (the removed purges' 87-4 None-bestiary conservatism, kept).
    content_sha = _content_sha_for(pack, sd.world_slug)
    if content_sha is None:
        logger.debug(
            "monster_manual.reconcile_skipped_no_bestiary genre=%s world=%s",
            sd.genre_slug,
            sd.world_slug,
        )
    else:
        session_seed = _session_seed_for(sd)
        discard = manual.reconcile_content(content_sha=content_sha, session_seed=session_seed)
        if discard is not None:
            manual.save()
            logger.warning(
                "monster_manual.pool_discarded genre=%s world=%s npcs=%d encounters=%d authored=%d",
                sd.genre_slug,
                sd.world_slug,
                discard.npcs_discarded,
                discard.encounters_discarded,
                discard.authored_discarded,
            )
            with Span.open(
                SPAN_MONSTER_MANUAL_POOL_DISCARDED,
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug,
                    "session_seed": session_seed,
                    "npcs_discarded": discard.npcs_discarded,
                    "encounters_discarded": discard.encounters_discarded,
                    "authored_discarded": discard.authored_discarded,
                },
            ):
                pass
    # Bound a legacy over-cap pool UNCONDITIONALLY — trimming is a pure size-bound
    # op that needs no content evidence (story 162-9). reconcile_content above is
    # gated on content_sha because it judges STALENESS against a roster it must be
    # able to read (a None bestiary is no evidence → skip). trim_to_caps judges
    # only POOL SIZE against a fixed cap — knowable without a bestiary — so it must
    # run even when content_sha is None. Leaving it inside the else branch meant a
    # bestiary-less over-cap pool (no pack / transiently unreadable content) was
    # never bounded: the 310/1,153-NPC runaways grandfathered forever whenever the
    # bestiary was unresolvable (spec D4). Model trims + warns; seam persists + spans.
    trim = manual.trim_to_caps()
    if trim is not None:
        manual.save()
        with Span.open(
            SPAN_MONSTER_MANUAL_CAP_ENFORCED,
            {
                "genre": sd.genre_slug,
                "world": sd.world_slug,
                "kind": "trim",
                "npcs_trimmed": trim.npcs_trimmed,
                "encounters_trimmed": trim.encounters_trimmed,
            },
        ):
            pass

    source_dir = getattr(pack, "source_dir", None) if pack is not None else None
    if manual.needs_seeding() and source_dir is not None:
        # Late import — pregen pulls the encountergen CLI, which is
        # heavy enough to keep out of session-handler import paths.
        from sidequest.server.dispatch.pregen import EncounterSeedError, seed_manual

        try:
            seed_manual(
                genre_packs_path=source_dir.parent,
                genre=sd.genre_slug,
                world=sd.world_slug or "",
                manual=manual,
            )
        except EncounterSeedError:
            # Story 90-5 (item 6, Keith policy 2026-06-10): a ruleset-module
            # pack with no bestiary is a fatal authoring/config error, not a
            # transient outage — fail LOUD. Swallowing it here would bind a
            # silently-empty Monster Manual pool, behaviorally the 87-4 bug.
            # Re-raise so the session bind crashes instead of running blind.
            raise
        except Exception as exc:  # noqa: BLE001
            # Don't crash the turn on a *transient* pregen failure (e.g. the
            # encountergen CLI is briefly unavailable) — the narrator can still
            # run with whatever the Manual already had on disk (ADR-006).
            logger.warning(
                "monster_manual.seed_failed genre=%s world=%s error=%s",
                sd.genre_slug,
                sd.world_slug,
                exc,
            )

    # H1 (wry_whimsy/oz, 2026-06-14): backfill the world's authored cast
    # UNCONDITIONALLY — independent of needs_seeding(). seed_manual only runs
    # when the Manual needs more Available entries, so an existing on-disk Manual
    # (>=4 NPCs + an encounter) never re-seeds and the authored companions never
    # enter the pool — the bug recurs on every prior save. _seed_authored_npcs
    # dedups by EXACT name (insert) and upserts stale placement tags (order-
    # insensitive), so this is safe to run every load; it only mutates when the
    # authored roster has something new or changed. Saves + emits a span when it
    # does (OTEL: the backfill is a subsystem decision the GM panel must see).
    if pack is not None:
        from sidequest.server.dispatch.pregen import _seed_authored_npcs

        backfilled = _seed_authored_npcs(pack, sd.world_slug or "", manual)
        if backfilled:
            manual.save()
            logger.info(
                "monster_manual.authored_backfilled genre=%s world=%s count=%d",
                sd.genre_slug,
                sd.world_slug,
                backfilled,
            )
            with Span.open(
                SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL,
                {
                    "genre": sd.genre_slug,
                    "world": sd.world_slug or "",
                    "authored_backfilled": backfilled,
                    "total_npcs": len(manual.npcs),
                },
            ):
                pass

    sd.monster_manual = manual
    return manual


def _authored_npc_ids(pack: Any, world_slug: str) -> dict[str, str]:
    """Normalized-name -> ``AuthoredNpc.id`` lookup (Green Room Task 2, feeder 1).

    ``ManualNpc.authored`` (monster_manual.py:168) marks a row backfilled from
    the world's ``npcs.yaml`` cast, but the Manual entry itself carries no id
    — only ``world_materialization.preload_authored_npcs`` stamps
    ``authored_id`` today. This mirrors that lookup (exact casefolded name,
    the same dedup key ``pregen._seed_authored_npcs`` uses) so an
    authored-backfill row can still resolve a real id at the injection seam.
    Returns ``{}`` when the pack/world/roster is unavailable — an authored
    row with no id match still gets ``AUTHORED`` kind (``npc.authored`` is
    the ground truth), just falls back to the name-keyed identity leg.
    """
    worlds = getattr(pack, "worlds", None) if pack is not None else None
    world_obj = worlds.get(world_slug) if worlds is not None and world_slug else None
    authored = getattr(world_obj, "authored_npcs", None) if world_obj is not None else None
    if not authored:
        return {}
    return {normalize_name(a.name): a.id for a in authored}


def _npc_patches_for_available_humans(
    manual: MonsterManual, current_location: str, *, authored_ids: dict[str, str] | None = None
) -> tuple[list[NpcPatch], int, int, int]:
    """Build patches for Active-at-location + top-N Available humans.

    Mirrors :meth:`MonsterManual.format_nearby_npcs` selection logic:

    - Active NPCs whose ``activated_location`` overlaps ``current_location``
      (substring either direction) — full-profile patch, stamped with
      the explicit anchor location. Capped at
      :data:`_ACTIVE_NPC_INJECT_LIMIT` (sq-playtest 2026-06-13): the loop was
      previously uncapped and re-surfaced every named NPC every turn.
    - Available NPCs, selected via :meth:`MonsterManual.available_at_location`
      (wry_whimsy/oz fix): a *placed* Available NPC (non-empty ``location_tags``)
      only surfaces where its tags match ``current_location``; an *unplaced* one
      stays eligible everywhere. Every *placed* match surfaces uncapped (it is the
      location's authored cast — story 158-11); only the *unplaced* walk-ons are
      bounded by :data:`_AVAILABLE_NPC_INJECT_LIMIT`. Each is a name-only patch
      stamped with the party's ``current_location`` so the projection layer's
      ``in_same_zone()`` matches.

    Dormant NPCs are skipped — same exclusion as the Rust formatter.

    Returns ``(patches, active_capped, available_placed_matched,
    available_placed_eligible)`` — ``active_capped`` is the number of
    Active-at-location humans dropped by the cap; ``available_placed_matched`` is
    how many of the surfaced Available humans were matched by ``location_tags``
    (vs unplaced fallback); ``available_placed_eligible`` is the count of placed
    NPCs matching this location. Since story 158-11 every placed match surfaces,
    so ``matched == eligible`` and the span's ``available_placed_dropped``
    (``eligible - matched``) is now always 0 — it remains emitted as a regression
    tripwire (a non-zero value would mean placed authored NPCs are being dropped
    again). All are surfaced in the injection span so the GM panel sees
    placement-aware selection working and the bench bounded (No Silent Fallbacks).

    Playtest 2026-05-11 regression: prior versions left ``location=None``
    on every patch, which silently masked every co-located target from
    ``in_same_zone()`` and gave the narrator ``npcs_present=0`` for the
    whole dive. Blank ``current_location`` (pre-bind / pre-chargen) is
    kept as None — there's no meaningful zone to stamp.
    """
    loc_lower = (current_location or "").lower()
    fallback_location = current_location or None
    authored_ids = authored_ids or {}

    def _aid(npc: Any) -> str | None:
        return authored_ids.get(normalize_name(npc.name))

    # Collect all Active-at-location matches first, then cap — so the cap drops
    # the tail deterministically (manual.npcs order) and we can report how many
    # were elided rather than silently swallowing them (No Silent Fallbacks).
    active_patches: list[NpcPatch] = []
    for npc in manual.npcs:
        if npc.state != EntryState.ACTIVE:
            continue
        anchor = npc.activated_location
        if anchor is None:
            active_patches.append(
                _human_patch(npc, location=fallback_location, authored_id=_aid(npc))
            )
            continue
        anchor_lower = anchor.lower()
        if loc_lower and (anchor_lower in loc_lower or loc_lower in anchor_lower):
            active_patches.append(_human_patch(npc, location=anchor, authored_id=_aid(npc)))

    active_capped = max(0, len(active_patches) - _ACTIVE_NPC_INJECT_LIMIT)
    if active_capped:
        logger.info(
            "monster_manual.active_inject_capped kept=%d dropped=%d location=%r",
            _ACTIVE_NPC_INJECT_LIMIT,
            active_capped,
            current_location,
        )
    patches: list[NpcPatch] = active_patches[:_ACTIVE_NPC_INJECT_LIMIT]

    # Placement-aware Available selection (wry_whimsy/oz fix): a placed NPC
    # (non-empty ``location_tags``) only surfaces where its tags match
    # ``current_location``; an unplaced NPC stays eligible everywhere. Placed
    # matches are ordered ahead of unplaced ones so authored roster NPCs win the
    # surfacing race against generic generated walk-ons. Mirrors
    # ``MonsterManual.available_at_location`` exactly.
    eligible_all = manual.available_at_location(current_location)
    # Story 158-11: a *placed* NPC (non-empty ``location_tags`` matching here) is
    # part of this location's intended authored cast, not a generic walk-on —
    # every one surfaces, UNCAPPED. ``_AVAILABLE_NPC_INJECT_LIMIT`` bounds only the
    # *unplaced* fallback walk-ons so a calm scene isn't flooded. Before this, the
    # cap sliced the combined list and silently guillotined the tail of an authored
    # roster larger than the cap: beneath_sunden's 4-NPC Ropefoot camp lost its 4th
    # member (Harmund Fuel-Count) every turn, leaving him manual_origin=False /
    # location=None while his 3 siblings seeded authored (the same class as the oz
    # road's "4 companions surfaced 3, dropped 1" — that work only made the drop
    # observable; this removes it). ``available_at_location`` already orders placed
    # matches ahead of unplaced, so partitioning preserves the surfacing order.
    placed = [n for n in eligible_all if n.location_tags]
    unplaced = [n for n in eligible_all if not n.location_tags]
    available_placed_eligible = len(placed)
    unplaced_budget = max(0, _AVAILABLE_NPC_INJECT_LIMIT - len(placed))
    available = placed + unplaced[:unplaced_budget]
    available_placed_matched = sum(1 for n in available if n.location_tags)
    for npc in available:
        patches.append(_human_patch(npc, location=fallback_location, authored_id=_aid(npc)))

    return patches, active_capped, available_placed_matched, available_placed_eligible


def _human_patch(npc: Any, *, location: str | None, authored_id: str | None = None) -> NpcPatch:
    """Build an :class:`NpcPatch` for a human Manual NPC.

    Pulls flavor fields (personality summary, dialogue quirks) from the
    namegen ``data`` blob but does NOT set creature fields — the
    materializer defaults disposition to 0 (neutral) for non-creature
    patches. ``location`` is the zone the projection should bind the NPC
    to so ``in_same_zone()`` can match them; ``None`` is reserved for the
    pre-bind / pre-chargen case where no meaningful zone exists yet.

    Green Room Task 2: the Manual's own ``authored`` flag (set by
    ``pregen._seed_authored_npcs`` on a world ``npcs.yaml`` backfill row) is
    the ONE marker distinguishing an authored-backfill human from a namegen
    pregen walk-on in this seam — pre-Task-2 both stamped MANUAL_POOL
    unconditionally. An authored row now stamps AUTHORED (+ ``authored_id``
    when the caller resolved one via :func:`_authored_npc_ids`); a pregen row
    keeps MANUAL_POOL.
    """
    data = npc.data if isinstance(npc.data, dict) else {}
    ocean_summary = data.get("ocean_summary") or None
    quirks_raw = data.get("dialogue_quirks") or []
    quirks: list[str] = [q for q in quirks_raw if isinstance(q, str)][:2]
    personality_bits: list[str] = []
    if isinstance(ocean_summary, str) and ocean_summary.strip():
        personality_bits.append(ocean_summary.strip())
    if quirks:
        personality_bits.append("Speech: " + "; ".join(quirks))
    personality = " — ".join(personality_bits) if personality_bits else None

    description_bits: list[str] = []
    if npc.role:
        description_bits.append(npc.role)
    if npc.culture:
        description_bits.append(npc.culture)
    description = ", ".join(description_bits) if description_bits else None

    origin = (
        Origin(kind=OriginKind.AUTHORED, authored_id=authored_id)
        if npc.authored
        else Origin(kind=OriginKind.MANUAL_POOL)
    )

    return NpcPatch(
        name=npc.name,
        description=description,
        personality=personality,
        role=npc.role or None,
        location=location,
        # Story 72-3: Monster Manual authorship marker (ADR-059).
        manual_origin=True,
        # Typed provenance (story 162-2; Green Room Task 2 splits authored
        # backfill from pregen — see the docstring above).
        origin=origin,
    )


def _npc_patches_for_encounters(
    manual: MonsterManual,
    in_combat: bool,
    current_location: str,
    *,
    zoned: bool,
    active: set[str],
    region: str,
) -> list[NpcPatch]:
    """Build creature patches from Available encounters.

    In combat: every Available encounter's enemy roster lands in
    ``snap.npcs`` so the narrator (and the GM panel) sees the real creatures
    the encounter intends.  Out of combat: cap at the first
    :data:`_OUT_OF_COMBAT_ENCOUNTER_LIMIT` encounters to avoid materializing
    eight monsters around a calm scene.

    Seam 1 of faction/zone-scoped eligibility (epic-157, ADR-059 amendment):
    each candidate encounter is gated by
    :func:`sidequest.game.zone_eligibility.is_eligible` BEFORE the out-of-combat
    limit slice, so a Houyhnhnm-tagged Yahoo is dropped on the Lilliput shore
    (and an in-zone encounter beyond the slice is never starved by a wrong-zone
    one ahead of it). Each exclusion fires the ``zone_eligibility.filtered`` span
    (the GM-panel lie-detector that the engine actively suppressed it, not that
    the narrator merely didn't mention it). ``active`` / ``zoned`` / ``region``
    are resolved once by :func:`inject` from the snapshot's canonical region.

    Stamps ``location=current_location`` on every creature patch so the
    projection layer's ``in_same_zone()`` matches them (playtest
    2026-05-11). Blank ``current_location`` keeps ``location=None`` —
    there's no meaningful zone to bind to.
    """
    available = manual.available_encounters()
    if not available:
        return []

    eligible: list[Any] = []
    for encounter in available:
        if zone_eligibility.is_eligible(encounter.factions, active, zoned=zoned):
            eligible.append(encounter)
            continue
        # Tagged-but-wrong-zone → drop it and emit the lie-detector span.
        logger.info(
            "zone_eligibility.filtered subsystem=creature content=%r region=%r factions=%s",
            encounter.label,
            region,
            encounter.factions,
        )
        with Span.open(
            SPAN_ZONE_ELIGIBILITY_FILTERED,
            {
                "subsystem": "creature",
                "content_id": encounter.label,
                "content_factions": sorted(encounter.factions),
                "active_factions": sorted(active),
                "region": region,
            },
        ):
            pass

    limit = len(eligible) if in_combat else _OUT_OF_COMBAT_ENCOUNTER_LIMIT
    creature_location = current_location or None
    patches: list[NpcPatch] = []
    for encounter in eligible[:limit]:
        enemies = encounter.data.get("enemies") if isinstance(encounter.data, dict) else None
        if not isinstance(enemies, list):
            continue
        for enemy in enemies:
            patch = _creature_patch_from_enemy(
                enemy, tier=encounter.tier, location=creature_location
            )
            if patch is not None:
                patches.append(patch)
    return patches


def _creature_patch_from_enemy(enemy: Any, *, tier: int, location: str | None) -> NpcPatch | None:
    """Translate one encountergen ``enemies[i]`` row into a creature patch.

    Required: ``name``.  The threat_level falls back to the encounter
    tier when the per-enemy row omits it — encountergen sometimes
    writes only the encounter-level tier.
    """
    if not isinstance(enemy, dict):
        return None
    name_raw = enemy.get("name")
    if not isinstance(name_raw, str) or not name_raw.strip():
        return None

    hp_raw = enemy.get("hp")
    hp = int(hp_raw) if isinstance(hp_raw, (int, float)) and hp_raw > 0 else None

    threat_raw = enemy.get("threat_level")
    threat_level = (
        int(threat_raw) if isinstance(threat_raw, (int, float)) and threat_raw > 0 else int(tier)
    )

    abilities_raw = enemy.get("abilities") or []
    abilities: list[str] = (
        [a for a in abilities_raw if isinstance(a, str)] if isinstance(abilities_raw, list) else []
    )

    morale_raw = enemy.get("morale")
    morale = morale_raw if isinstance(morale_raw, str) and morale_raw.strip() else None

    creature_id_raw = enemy.get("creature_id") or enemy.get("class")
    creature_id = (
        creature_id_raw if isinstance(creature_id_raw, str) and creature_id_raw.strip() else None
    )

    role_raw = enemy.get("role")
    description = role_raw if isinstance(role_raw, str) and role_raw.strip() else None

    return NpcPatch(
        name=name_raw.strip(),
        description=description,
        role=description,
        creature_id=creature_id,
        threat_level=threat_level,
        hp=hp,
        abilities=abilities or None,
        morale=morale,
        location=location,
        # Story 72-3: Monster Manual authorship marker (ADR-059).
        manual_origin=True,
        # Typed provenance (story 162-2): encountergen row from the Manual pool.
        origin=Origin(kind=OriginKind.MANUAL_POOL, creature_id=creature_id),
    )


def _creature_patch_from_bestiary_entry(entry: Any, *, location: str | None) -> NpcPatch:
    """Translate one :class:`BestiaryEntry` into a creature patch.

    Story 107-2: the per-room binding sources the opponent directly from the
    world bestiary by id, so the materialized NPC carries the bestiary's
    AUTHORED name ("Gnaw-Swarm") instead of an improvised label. Mirrors the
    encountergen creature-patch shape (:func:`_creature_patch_from_enemy`) but
    reads typed bestiary fields rather than a raw ``enemies[i]`` dict.
    """
    abilities = list(entry.abilities) if entry.abilities else None
    description = entry.description or entry.role or None
    return NpcPatch(
        name=entry.name,
        description=description,
        role=entry.role or None,
        creature_id=entry.id,
        threat_level=entry.level,
        hp=entry.hp,
        abilities=abilities,
        location=location,
        # Story 72-3: Monster Manual authorship marker (ADR-059).
        manual_origin=True,
        # Typed provenance (story 162-2): authored per-room binding — the ONLY
        # thing distinguishing this patch from encounter-pool filler.
        origin=Origin(kind=OriginKind.ROOM_BOUND, creature_id=entry.id),
    )


def _creature_patch_from_region_creature(rc: Any, *, location: str | None, region: str) -> NpcPatch:
    """Translate one frozen ``RegionCreature`` (Task 4) into a region-stamped
    creature patch. ``region`` is the engine-owned key co-location seats on
    (ADR-116); ``location`` is the free-text scene for narrator/UI display."""
    return NpcPatch(
        name=rc.name,
        description=rc.telegraph or None,
        role=rc.creature_type or None,
        creature_id=rc.creature_type or None,
        threat_level=rc.threat_level,
        hp=rc.hp,
        location=location,
        region=region,
        manual_origin=True,
        # Typed provenance (story 162-2): frozen procedural roster (ADR-106).
        origin=Origin(kind=OriginKind.REGION_POPULATION, creature_id=rc.creature_type or None),
    )


def _npc_patches_for_region_population(
    sd: Any, region_id: str, *, current_location: str, in_combat: bool
) -> list[NpcPatch]:
    """Build region-stamped creature patches from the region's frozen procedural
    roster (Task 3/4). Out of combat the roster is capped at
    ``_OUT_OF_COMBAT_ENCOUNTER_LIMIT`` (Keith ruling 2026-06-22); the big-bad is
    always present (it only SEATS when the player engages).
    Emits ``monster_manual.region_population``. Returns ``[]`` for a region with
    no frozen population (absent binding, not a fallback)."""
    repo = getattr(sd, "dungeon_repository", None)
    if repo is None:
        return []
    from sidequest.server.dispatch.region_population import load_region_population

    roster, big_bad = load_region_population(repo, region_id)
    if not roster and big_bad is None:
        return []
    creature_location = current_location or None
    capped = roster if in_combat else roster[:_OUT_OF_COMBAT_ENCOUNTER_LIMIT]
    patches = [
        _creature_patch_from_region_creature(rc, location=creature_location, region=region_id)
        for rc in capped
    ]
    if big_bad is not None:
        patches.append(
            _creature_patch_from_region_creature(
                big_bad, location=creature_location, region=region_id
            )
        )
    with Span.open(
        SPAN_MONSTER_MANUAL_REGION_POPULATION,
        {
            "region_id": region_id,
            "creature_count": len(capped),
            "big_bad": big_bad is not None,
        },
    ):
        pass
    return patches


def _npc_patches_for_room_binding(
    sd: _SessionData, room_id: str, current_location: str
) -> list[NpcPatch]:
    """Build creature patches for the room's authored ``encounter_creatures``.

    Resolves the room's structured binding (Story 107-2) and materializes each
    bound bestiary creature under its authored name. The resolve step emits the
    ``monster_manual.room_bound`` span and fails loud on a dangling ref (No
    Silent Fallbacks). Returns ``[]`` when the room declares no binding.
    """
    pack = getattr(sd, "genre_pack", None)
    if pack is None:
        return []
    world_slug = sd.world_slug or ""
    # Late import — keeps the resolver (yaml read + bestiary lookup) out of the
    # session-handler import path, matching the pregen late-import pattern above.
    from sidequest.server.dispatch.room_creature_binding import resolve_room_creatures

    bound_ids = resolve_room_creatures(pack, world_slug, room_id)
    if not bound_ids:
        return []
    bestiary, _ = pack.effective_bestiary(world_slug)
    by_id = {entry.id: entry for entry in bestiary.entries}
    creature_location = current_location or None
    patches: list[NpcPatch] = []
    for cid in bound_ids:
        entry = by_id.get(cid)
        if entry is None:
            # resolve_room_creatures already validated referential integrity;
            # a miss here would be a TOCTOU between resolve and materialize.
            continue
        patches.append(_creature_patch_from_bestiary_entry(entry, location=creature_location))
    return patches


def inject(
    sd: _SessionData,
    snapshot: GameSnapshot,
    *,
    current_location: str,
    in_combat: bool,
    room_id: str | None = None,
) -> int:
    """Materialize Manual entries into ``snapshot.npcs`` via the Green Room gate.

    Returns ``len(result.admitted) + len(result.merged)`` from
    :func:`sidequest.game.green_room.admit` — every candidate this turn's four
    feeders proposed that landed a seat (fresh or merged onto an existing
    one). Idempotent across turns: an identity already present in
    ``snapshot.npcs`` is never re-appended or stat-reset (live HP/disposition
    survive structurally — ADR-139 Invariant 2).

    Emits :data:`SPAN_MONSTER_MANUAL_INJECTED` with the same attribute
    shape as the Rust span so the existing GM-panel dashboard reads it
    without changes, plus one ``green_room.materialized`` span per resolved
    identity (ADR-156 §4) — the GM panel's lie detector that this seam's
    four feeders (``mm.available_humans``, ``mm.encounters``,
    ``mm.room_binding``, ``mm.region_population``) actually reached the gate.

    Story 107-2: when ``room_id`` is supplied (sourced from
    ``snapshot.region_for()`` / ``pc_regions`` — 107-1's per-room key), the
    room's structured ``encounter_creatures`` binding is resolved and its
    authored bestiary creature is materialized under its real name, emitting
    ``monster_manual.room_bound``. ``room_id=None`` (the default every existing
    caller uses) preserves today's behavior exactly — the binding path is
    strictly additive and gated on a room id being supplied.
    """
    manual = sd.monster_manual

    # Social, Composure-only packs (combat_encounters=False) have no combat —
    # never inject combat-encounter enemies (hostile -20 NPCs carrying B/X HP
    # and Strike abilities) into a drawing-room mystery. This gate is
    # defense-in-depth alongside the seed-side skip (pregen): a Manual cached to
    # disk before the flag landed still holds combat encounters, so the
    # injection seam must suppress them too (playtest 2026-06-01, blackthorn_moor).
    # Absent pack/rules (test stubs) default to combat-enabled = the model
    # default, preserving legacy behavior.
    pack = getattr(sd, "genre_pack", None)
    rules = getattr(pack, "rules", None)
    combat_encounters = getattr(rules, "combat_encounters", True)

    # Seam 1 zone-eligibility context (epic-157, ADR-059 amendment): resolve the
    # world's zoned-ness from the canonical region (``snapshot.region_for``) —
    # NOT the free-text ``current_location`` (which may be a POI/scene string).
    # The active-faction set + region id are resolved ONLY for a zoned world that
    # will actually field encounters this turn; an unzoned world (the 11 single-
    # zone packs), a non-combat pack, or a manual-less pre-bind turn skips the
    # region query entirely (Cost Scales with Drama). ``is_eligible`` is permissive
    # on ``zoned=False``, so the empty defaults below are a behavioral no-op.
    zoned = zone_eligibility.world_is_zoned(zone_eligibility.cartography_for(snapshot, pack))
    zone_active: set[str] = set()
    region = ""
    if zoned and combat_encounters and manual is not None:
        zone_active = zone_eligibility.active_factions(snapshot, pack)
        region = snapshot.region_for() or ""

    human_patches: list[NpcPatch] = []
    creature_patches: list[NpcPatch] = []
    active_capped = 0
    available_placed_matched = 0
    available_placed_eligible = 0
    if manual is not None:
        authored_ids = _authored_npc_ids(pack, getattr(sd, "world_slug", None) or "")
        (
            human_patches,
            active_capped,
            available_placed_matched,
            available_placed_eligible,
        ) = _npc_patches_for_available_humans(manual, current_location, authored_ids=authored_ids)
        creature_patches = (
            _npc_patches_for_encounters(
                manual,
                in_combat,
                current_location,
                zoned=zoned,
                active=zone_active,
                region=region,
            )
            if combat_encounters
            else []
        )
        # Cleanse junk names (stale-cache annotations, corpus leakage) before they
        # reach the snapshot. Loud per-name warnings + a span count below so the GM
        # panel sees the registry decision (playtest 2026-06-10, "Vesper (version)").
        human_patches, human_sanitized = _sanitize_patch_names(human_patches)
        creature_patches, creature_sanitized = _sanitize_patch_names(creature_patches)
        names_sanitized = human_sanitized + creature_sanitized

        # Playtest 2026-05-11 lie-detector: count how many patches actually
        # land with a bound location. Pre-fix this was always 0 (every patch
        # had location=None) which silently masked every NPC from
        # ``in_same_zone()``. Post-fix this matches
        # ``len(human_patches) + len(creature_patches)`` whenever
        # ``current_location`` is meaningful.
        patches_with_location = sum(1 for p in human_patches + creature_patches if p.location)

        # Placement-aware selection visibility (M5): ``eligible`` (the uncapped
        # count of placed NPCs whose tags match here) and ``matched`` (how many
        # surfaced through the _AVAILABLE_NPC_INJECT_LIMIT slice) come from the
        # SAME _npc_patches_for_available_humans pass — one available_at_location
        # traversal, consistent snapshot. ``dropped`` (eligible − matched) makes
        # the cap-loss visible — without it eligible-vs-matched looked like a
        # placement miss, not a bounded bench (No Silent Fallbacks). oz road: 4
        # eligible, 3 matched, 1 dropped.
        available_placed_dropped = max(0, available_placed_eligible - available_placed_matched)

        with Span.open(
            SPAN_MONSTER_MANUAL_INJECTED,
            {
                "available_npcs": len(manual.available_npcs()),
                "available_encounters": len(manual.available_encounters()),
                "total_npcs": len(manual.npcs),
                "total_encounters": len(manual.encounters),
                "npcs_injected": len(human_patches),
                "creatures_injected": len(creature_patches),
                "active_npcs_capped": active_capped,
                # Placement-aware selection visibility (wry_whimsy/oz fix): how
                # many surfaced Available humans were matched by ``location_tags``
                # vs. fell through as unplaced walk-ons, and how many placed NPCs
                # in the whole pool are eligible at this location. The GM-panel
                # lie-detector that authored roster placement is actually firing
                # (not silently ignored, the original bug).
                "available_placed_matched": available_placed_matched,
                "available_placed_eligible": available_placed_eligible,
                # M5: placed-eligible NPCs the inject slice dropped (cap-loss made
                # visible so eligible>matched isn't read as a placement failure).
                "available_placed_dropped": available_placed_dropped,
                "names_sanitized": names_sanitized,
                "patches_with_location": patches_with_location,
                "in_combat": bool(in_combat),
                "combat_encounters": bool(combat_encounters),
                "location": current_location or "",
            },
        ):
            pass

    # Story 107-2 per-room binding: when the party's room id is supplied, surface
    # the room's AUTHORED bestiary opponent (emits monster_manual.room_bound,
    # fails loud on a dangling ref). Strictly additive to the Manual pool above
    # and gated on combat — a non-combat pack never fields creatures.
    room_binding_patches: list[NpcPatch] = []
    region_population_patches: list[NpcPatch] = []
    if room_id and combat_encounters:
        room_binding_patches = _npc_patches_for_room_binding(sd, room_id, current_location)
        # Story 153-x (ADR-106 region population): inject the region's frozen
        # procedural roster, region-stamped so Task 6 can seat by region id.
        # Precedence between this and the room binding above is now
        # green_room.admit()'s ladder (ROOM_BOUND outranks REGION_POPULATION) —
        # not a pre-filter here (Green Room Task 2 deleted the old
        # identity-key/name pre-filter; admit() is the one gate).
        region_population_patches = _npc_patches_for_region_population(
            sd, room_id, current_location=current_location, in_combat=in_combat
        )

    # Green Room Task 2 (ADR-156 §4): every patch this turn's four feeders
    # proposed becomes a MaterializationCandidate and lands through ONE
    # admit() call — the single gate that arbitrates identity across
    # feeders, not four independent appends. ``creature_id`` is routed only
    # for room-binding (see :func:`_candidate`'s docstring for why the
    # encounter/region-population builders' ids are NOT trustworthy
    # per-individual identifiers and must key on name instead).
    candidates: list[MaterializationCandidate] = []
    for patch in human_patches:
        human_origin = patch.origin
        if human_origin is None:
            raise ValueError(
                f"monster_manual_inject: available-humans patch {patch.name!r} "
                "carries no stamped origin — _human_patch must always stamp "
                "AUTHORED or MANUAL_POOL (No Silent Fallbacks)"
            )
        candidates.append(
            _candidate(
                snapshot._npc_from_patch(patch),
                kind=human_origin.kind,
                source="mm.available_humans",
                authored_id=human_origin.authored_id,
            )
        )
    for patch in creature_patches:
        candidates.append(
            _candidate(
                snapshot._npc_from_patch(patch),
                kind=OriginKind.MANUAL_POOL,
                source="mm.encounters",
            )
        )
    for patch in room_binding_patches:
        candidates.append(
            _candidate(
                snapshot._npc_from_patch(patch),
                kind=OriginKind.ROOM_BOUND,
                source="mm.room_binding",
                creature_id=patch.creature_id,
            )
        )
    # Region-population candidates key on name (see _candidate's docstring —
    # RegionCreature.creature_type is a species tag shared by many distinct
    # individuals), EXCEPT when a patch's creature_type matches THIS turn's
    # room-binding id: that overlap is the authored-vs-procedural-counterpart
    # case story 162-2 pinned (test_name_drifted_procedural_counterpart_dedups_
    # on_creature_id) — the frozen roster naming the room's own bound creature
    # under a drifted display name must still collapse onto the authored seat.
    # Scoped to THIS room's authored ids only, so an unrelated generic tag
    # ("mob") never collapses distinct procedural individuals onto each other.
    room_bound_ids = {p.creature_id for p in room_binding_patches if p.creature_id}
    for patch in region_population_patches:
        candidates.append(
            _candidate(
                snapshot._npc_from_patch(patch),
                kind=OriginKind.REGION_POPULATION,
                source="mm.region_population",
                creature_id=patch.creature_id if patch.creature_id in room_bound_ids else None,
            )
        )

    if not candidates:
        return 0

    result = admit(snapshot, candidates)
    return len(result.admitted) + len(result.merged)


def mark_active_from_narration(
    manual: MonsterManual,
    narration: str,
    current_location: str,
    *,
    snapshot: GameSnapshot | None = None,
    pack: Any = None,
    perspective: str | None = None,
) -> list[str]:
    """Scan narration for Available Manual NPC names and mark Active.

    Returns the list of NPC names activated this pass. Mirrors the Rust
    pattern at ``dispatch/mod.rs:1671-1695``: case-sensitive substring
    match against the cleaned narration text (the Python ``result.narration``
    is already the post-strip equivalent of Rust's ``clean_narration``).

    ``snapshot``/``pack``/``perspective`` (epic-157 Seam 2, story 157-3):
    generated walk-on **origin-stamp**. In a zoned world, an unplaced generated
    NPC activated here is stamped with the acting PC's region ``controlled_by``
    faction so it cannot later resurface in a different zone (the gulliver bleed).
    The faction is resolved via :func:`zone_eligibility.active_factions` for
    ``perspective`` and is only applied when exactly one faction resolves (a
    zoned, resolvable region); an unzoned world, an unresolvable region, or a
    split-party union (>1) stamps nothing. Omitting all three keeps the legacy
    no-stamp behavior (back-compatible).
    """
    if not narration:
        return []
    activated: list[str] = []
    for npc in manual.npcs:
        if npc.state != EntryState.AVAILABLE:
            continue
        if npc.name and npc.name in narration:
            activated.append(npc.name)

    faction = _origin_stamp_faction(snapshot, pack, perspective)
    for name in activated:
        manual.mark_active(name, current_location, faction=faction)
        logger.info(
            "monster_manual.npc_activated name=%r location=%r faction=%r",
            name,
            current_location,
            faction,
        )
    return activated


def _origin_stamp_faction(
    snapshot: GameSnapshot | None, pack: Any, perspective: str | None
) -> str | None:
    """The single ``controlled_by`` faction to origin-stamp a walk-on with, or None.

    Returns the lone active faction for ``perspective`` (the acting PC's zoned
    region) only when exactly one resolves; an unzoned world, an unresolvable
    region (∅), or a split-party union (>1) returns None — never an arbitrary
    pick. ``snapshot``/``pack`` absent (legacy callers) → None (no stamp).
    """
    if snapshot is None or pack is None:
        return None
    active = zone_eligibility.active_factions(snapshot, pack, perspective=perspective)
    if len(active) == 1:
        return next(iter(active))
    return None


def mark_all_dormant(manual: MonsterManual | None) -> None:
    """Transition all Active Manual entries to Dormant.

    Thin wrapper over :meth:`MonsterManual.mark_all_dormant` so call
    sites can pass an optional Manual without their own None guard.
    """
    if manual is None:
        return
    manual.mark_all_dormant()
