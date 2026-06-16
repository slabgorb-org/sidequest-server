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
        from sidequest.server.dispatch.pregen import seed_manual

        try:
            seed_manual(
                genre_packs_path=source_dir.parent,
                genre=sd.genre_slug,
                world=sd.world_slug or "",
                manual=manual,
            )
        except Exception as exc:  # noqa: BLE001
            # Don't crash the turn on a pregen failure — the narrator
            # can still run with whatever the Manual already had on disk.
            # OTEL fires below regardless so the GM panel sees the seed
            # attempt and its outcome.
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
    )


def inject(
    sd: _SessionData,
    snapshot: GameSnapshot,
    *,
    current_location: str,
    in_combat: bool,
) -> int:
    """Materialize Manual entries into ``snapshot.npcs``.

    Returns the count of patches applied. Idempotent across turns: NPCs
    already in ``snapshot.npcs`` with the same name are merged
    (:meth:`GameSnapshot._merge_npc_patch`) rather than duplicated.

    Emits :data:`SPAN_MONSTER_MANUAL_INJECTED` with the same attribute
    shape as the Rust span so the existing GM-panel dashboard reads it
    without changes.
    """
    manual = sd.monster_manual
    if manual is None:
        return 0

    human_patches = _npc_patches_for_available_humans(manual, current_location)
    creature_patches = _npc_patches_for_encounters(manual, in_combat, current_location)
    all_patches = human_patches + creature_patches

    available_npcs = len(manual.available_npcs())
    available_encounters = len(manual.available_encounters())

    # Playtest 2026-05-11 lie-detector: count how many patches actually
    # land with a bound location. Pre-fix this was always 0 (every patch
    # had location=None) which silently masked every NPC from
    # ``in_same_zone()``. Post-fix this matches ``len(all_patches)`` whenever
    # ``current_location`` is meaningful.
    patches_with_location = sum(1 for p in all_patches if p.location)

    with Span.open(
        SPAN_MONSTER_MANUAL_INJECTED,
        {
            "available_npcs": available_npcs,
            "available_encounters": available_encounters,
            "total_npcs": len(manual.npcs),
            "total_encounters": len(manual.encounters),
            "npcs_injected": len(human_patches),
            "creatures_injected": len(creature_patches),
            "patches_with_location": patches_with_location,
            "in_combat": bool(in_combat),
            "location": current_location or "",
        },
    ):
        pass

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
