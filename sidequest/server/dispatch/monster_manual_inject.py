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

import logging
from typing import TYPE_CHECKING, Any

from sidequest.game.monster_manual import EntryState, MonsterManual
from sidequest.game.session import NpcPatch, WorldStatePatch
from sidequest.genre.names.generator import sanitize_display_name
from sidequest.telemetry.spans import Span
from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_INJECTED

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_handler import _SessionData

logger = logging.getLogger(__name__)


# Cap on Available NPCs surfaced into the snapshot when no anchor is
# active at the current location. Mirrors the Rust
# ``format_nearby_npcs`` "Other known NPCs" slice (top 3).
_AVAILABLE_NPC_INJECT_LIMIT = 3
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

    manual = MonsterManual.load(sd.genre_slug, sd.world_slug or "")
    pack = sd.genre_pack
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
    sd.monster_manual = manual
    return manual


def _npc_patches_for_available_humans(
    manual: MonsterManual, current_location: str
) -> list[NpcPatch]:
    """Build patches for Active-at-location + top-N Available humans.

    Mirrors :meth:`MonsterManual.format_nearby_npcs` selection logic:

    - Active NPCs whose ``activated_location`` overlaps ``current_location``
      (substring either direction) — full-profile patch, stamped with
      the explicit anchor location.
    - First :data:`_AVAILABLE_NPC_INJECT_LIMIT` Available NPCs — name-only
      patch stamped with the party's ``current_location`` so the
      projection layer's ``in_same_zone()`` matches them.

    Dormant NPCs are skipped — same exclusion as the Rust formatter.

    Playtest 2026-05-11 regression: prior versions left ``location=None``
    on every patch, which silently masked every co-located target from
    ``in_same_zone()`` and gave the narrator ``npcs_present=0`` for the
    whole dive. Blank ``current_location`` (pre-bind / pre-chargen) is
    kept as None — there's no meaningful zone to stamp.
    """
    loc_lower = (current_location or "").lower()
    fallback_location = current_location or None

    patches: list[NpcPatch] = []

    for npc in manual.npcs:
        if npc.state != EntryState.ACTIVE:
            continue
        anchor = npc.activated_location
        if anchor is None:
            patches.append(_human_patch(npc, location=fallback_location))
            continue
        anchor_lower = anchor.lower()
        if loc_lower and (anchor_lower in loc_lower or loc_lower in anchor_lower):
            patches.append(_human_patch(npc, location=anchor))

    available = [n for n in manual.npcs if n.state == EntryState.AVAILABLE][
        :_AVAILABLE_NPC_INJECT_LIMIT
    ]
    for npc in available:
        patches.append(_human_patch(npc, location=fallback_location))

    return patches


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
    manual: MonsterManual, in_combat: bool, current_location: str
) -> list[NpcPatch]:
    """Build creature patches from Available encounters.

    In combat: every Available encounter's enemy roster lands in
    ``snap.npcs`` so the narrator (and Sebastien's GM panel) sees the
    real creatures the encounter intends.  Out of combat: cap at the
    first :data:`_OUT_OF_COMBAT_ENCOUNTER_LIMIT` encounters to avoid
    materializing eight monsters around a calm scene.

    Stamps ``location=current_location`` on every creature patch so the
    projection layer's ``in_same_zone()`` matches them (playtest
    2026-05-11). Blank ``current_location`` keeps ``location=None`` —
    there's no meaningful zone to bind to.
    """
    available = manual.available_encounters()
    if not available:
        return []
    limit = len(available) if in_combat else _OUT_OF_COMBAT_ENCOUNTER_LIMIT
    creature_location = current_location or None
    patches: list[NpcPatch] = []
    for encounter in available[:limit]:
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
    rules = getattr(getattr(sd, "genre_pack", None), "rules", None)
    combat_encounters = getattr(rules, "combat_encounters", True)

    all_patches: list[NpcPatch] = []
    if manual is not None:
        human_patches = _npc_patches_for_available_humans(manual, current_location)
        creature_patches = (
            _npc_patches_for_encounters(manual, in_combat, current_location)
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

        with Span.open(
            SPAN_MONSTER_MANUAL_INJECTED,
            {
                "available_npcs": len(manual.available_npcs()),
                "available_encounters": len(manual.available_encounters()),
                "total_npcs": len(manual.npcs),
                "total_encounters": len(manual.encounters),
                "npcs_injected": len(human_patches),
                "creatures_injected": len(creature_patches),
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
        all_patches = all_patches + _npc_patches_for_room_binding(sd, room_id, current_location)

    if not all_patches:
        return 0

    patch = WorldStatePatch(npcs_present=all_patches)
    snapshot.apply_world_patch(patch)
    return len(all_patches)


def mark_active_from_narration(
    manual: MonsterManual, narration: str, current_location: str
) -> list[str]:
    """Scan narration for Available Manual NPC names and mark Active.

    Returns the list of NPC names activated this pass. Mirrors the Rust
    pattern at ``dispatch/mod.rs:1671-1695``: case-sensitive substring
    match against the cleaned narration text (the Python ``result.narration``
    is already the post-strip equivalent of Rust's ``clean_narration``).
    """
    if not narration:
        return []
    activated: list[str] = []
    for npc in manual.npcs:
        if npc.state != EntryState.AVAILABLE:
            continue
        if npc.name and npc.name in narration:
            activated.append(npc.name)
    for name in activated:
        manual.mark_active(name, current_location)
        logger.info("monster_manual.npc_activated name=%r location=%r", name, current_location)
    return activated


def mark_all_dormant(manual: MonsterManual | None) -> None:
    """Transition all Active Manual entries to Dormant.

    Thin wrapper over :meth:`MonsterManual.mark_all_dormant` so call
    sites can pass an optional Manual without their own None guard.
    """
    if manual is None:
        return
    manual.mark_all_dormant()
