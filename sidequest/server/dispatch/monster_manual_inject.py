"""Monster Manual injection seam — port of ADR-059 per-turn wiring.

This is the doctrine-divergent leaf of the Monster Manual port. The Rust
version (``crates/sidequest-server/src/dispatch/mod.rs:643-681``) appended
``format_nearby_npcs`` and ``format_area_creatures`` text directly to the
narrator's ``state_summary``. Python deviates: per ``project_narrator_
gaslighting_doctrine.md``, we materialize Manual entries into ``snap.npcs``
as runtime ``Npc`` records via :class:`NpcPatch` / :class:`WorldStatePatch`
so the narrator sees them as world truth — never as "available list" prose.

Lifecycle:

1. :func:`ensure_loaded` — idempotent lazy load + seed.  Mirrors Rust
   ``MonsterManual::load`` + ``pregen::seed_manual`` at session-bind. The
   Manual lives on :class:`_SessionData` across the session; Rust re-loaded
   from disk on every turn — Python keeps it in memory and saves at turn
   end (same effective on-disk state, fewer JSON parses).
2. :func:`inject` — builds the per-turn :class:`WorldStatePatch` from the
   Manual's location-filtered Available pool and applies it to the
   snapshot.  Emits the ``monster_manual.injected`` OTEL span (Rust parity).
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
from sidequest.game.monster_manual import EntryState, MonsterManual
from sidequest.game.session import NpcPatch, WorldStatePatch
from sidequest.genre.names.generator import sanitize_display_name
from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.monster_manual import (
    SPAN_MONSTER_MANUAL_AUTHORED_BACKFILL,
    SPAN_MONSTER_MANUAL_INJECTED,
    SPAN_MONSTER_MANUAL_POOL_DISCARDED,
    SPAN_MONSTER_MANUAL_REGION_POPULATION,
)
from sidequest.telemetry.spans.zone_eligibility import SPAN_ZONE_ELIGIBILITY_FILTERED

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
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


def _content_sha_for(pack: Any, world: str) -> str:
    """Content version the MM pool is derived under (story 162-1, spec D3).

    A stable hash of the world's effective bestiary — the creature roster the
    pool is seeded from, and the axis along which clones' content checkouts
    diverge (foreign-bestiary bleed, roster churn). When the roster changes the
    sha changes and :meth:`MonsterManual.reconcile_content` discards the
    now-stale pool. A pack with no effective bestiary (a minimal stub, or a
    native pack) hashes to a stable empty-roster digest — sha256 always yields a
    64-char digest, so the key never silently collapses to an empty string (No
    Silent Fallbacks). ``effective_bestiary`` may be absent on a stub pack; a
    real GenrePack always exposes it.
    """
    entries: list[str] = []
    # ``pack`` is duck-typed (a real GenrePack or a test stub) — keep the accessor
    # dynamic so the (Bestiary | None, str) unpack type-checks.
    effective_bestiary = getattr(pack, "effective_bestiary", None) if pack is not None else None
    if callable(effective_bestiary):
        resolved: Any = effective_bestiary(world)
        bestiary, _source = resolved
        if bestiary is not None:
            for entry in bestiary.entries:
                entries.append(f"{entry.name}:{entry.hp}:{entry.level}:{entry.armor_class}")
    payload = "|".join(sorted(entries)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _session_seed_for(sd: _SessionData) -> str:
    """Per-session key so a NEW session re-derives its pool (story 162-1, D3).

    The ``SessionRoom`` slug uniquely identifies the session: reconnects within a
    session share it (pool preserved), a new session gets a new slug (its
    reconcile sees a mismatch and re-derives). Falls back to the world slug when
    no room is bound (pre-room construction / synthetic paths) — still a stable
    non-empty key, never a blank that would collapse every session onto one pool.
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
    # foreign-bestiary bleed. Key the pool on (content_sha, session_seed) and
    # DISCARD it wholesale on a mismatch, REPLACING the two removed targeted purge
    # tourniquets: a different content checkout (multi-clone) or a new session
    # re-derives from scratch, so a stale pool can never be reused or livelock a
    # purge/reseed cycle. reconcile_content adopts a legacy/unstamped pool without
    # discarding (no nuking pre-162-1 saves). Emit the forensic span on a real
    # discard so the GM panel sees it (OTEL Observability, spec V1-V3).
    # Reconcile only with a pack in hand: without one the effective bestiary is
    # unreadable, and judging staleness on evidence we don't have would discard a
    # validly-stamped pool on a transient packless load (the empty-roster digest
    # would masquerade as a content change). Same conservative posture as the
    # removed purges' None-bestiary guard — no evidence, no discard.
    if pack is not None:
        content_sha = _content_sha_for(pack, sd.world_slug)
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


def _npc_patches_for_available_humans(
    manual: MonsterManual, current_location: str
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

    # Collect all Active-at-location matches first, then cap — so the cap drops
    # the tail deterministically (manual.npcs order) and we can report how many
    # were elided rather than silently swallowing them (No Silent Fallbacks).
    active_patches: list[NpcPatch] = []
    for npc in manual.npcs:
        if npc.state != EntryState.ACTIVE:
            continue
        anchor = npc.activated_location
        if anchor is None:
            active_patches.append(_human_patch(npc, location=fallback_location))
            continue
        anchor_lower = anchor.lower()
        if loc_lower and (anchor_lower in loc_lower or loc_lower in anchor_lower):
            active_patches.append(_human_patch(npc, location=anchor))

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
        patches.append(_human_patch(npc, location=fallback_location))

    return patches, active_capped, available_placed_matched, available_placed_eligible


def _human_patch(npc: Any, *, location: str | None) -> NpcPatch:
    """Build an :class:`NpcPatch` for a human Manual NPC.

    Pulls flavor fields (personality summary, dialogue quirks) from the
    namegen ``data`` blob but does NOT set creature fields — the
    materializer defaults disposition to 0 (neutral) for non-creature
    patches. ``location`` is the zone the projection should bind the NPC
    to so ``in_same_zone()`` can match them; ``None`` is reserved for the
    pre-bind / pre-chargen case where no meaningful zone exists yet.
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

    return NpcPatch(
        name=npc.name,
        description=description,
        personality=personality,
        role=npc.role or None,
        location=location,
        # Story 72-3: Monster Manual authorship marker (ADR-059).
        manual_origin=True,
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
    """Materialize Manual entries into ``snapshot.npcs``.

    Returns the count of patches applied. Idempotent across turns: NPCs
    already in ``snapshot.npcs`` with the same name are merged
    (:meth:`GameSnapshot._merge_npc_patch`) rather than duplicated.

    Emits :data:`SPAN_MONSTER_MANUAL_INJECTED` with the same attribute
    shape as the Rust span so the existing GM-panel dashboard reads it
    without changes.

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

    all_patches: list[NpcPatch] = []
    active_capped = 0
    available_placed_matched = 0
    available_placed_eligible = 0
    if manual is not None:
        (
            human_patches,
            active_capped,
            available_placed_matched,
            available_placed_eligible,
        ) = _npc_patches_for_available_humans(manual, current_location)
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
        all_patches = human_patches + creature_patches

        # Playtest 2026-05-11 lie-detector: count how many patches actually
        # land with a bound location. Pre-fix this was always 0 (every patch
        # had location=None) which silently masked every NPC from
        # ``in_same_zone()``. Post-fix this matches ``len(all_patches)`` whenever
        # ``current_location`` is meaningful.
        patches_with_location = sum(1 for p in all_patches if p.location)

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
    if room_id and combat_encounters:
        authored = _npc_patches_for_room_binding(sd, room_id, current_location)
        all_patches = all_patches + authored
        # Story 153-x (ADR-106 region population): inject the region's frozen
        # procedural roster, region-stamped so Task 6 can seat by region id.
        # De-duped by name so an authored creature ALWAYS wins over its
        # procedural counterpart (authored content dominates; No Silent Fallbacks).
        authored_names = {p.name for p in authored}
        region_pop = _npc_patches_for_region_population(
            sd, room_id, current_location=current_location, in_combat=in_combat
        )
        all_patches = all_patches + [p for p in region_pop if p.name not in authored_names]

    if not all_patches:
        return 0

    patch = WorldStatePatch(npcs_present=all_patches)
    snapshot.apply_world_patch(patch)
    return len(all_patches)


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
