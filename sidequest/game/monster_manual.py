"""Monster Manual — persistent pre-generated content pool (ADR-059).

Server-side GM prep: tool binaries generate NPCs and encounters before the session,
results are stored in a persistent JSON file per genre/world. The narrator prompt
receives names + brief descriptors via game_state injection. Full stat blocks stay
in the Manual for post-narration compound key lookup.

The narrator treats game_state as world truth and uses pool names naturally.
No XML casting tags, no meta-instructions. World data in the world data section.

Ported from ``crates/sidequest-game/src/monster_manual.rs``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Accumulation caps (story 162-1, spec D4). The shared MM cache previously grew
# without bound — flickering_reach reached 310 NPCs and glenross 1,153 (no cap on
# pregen re-seeding). These bound the pool so a runaway cannot recur; an add past
# the cap is dropped LOUDLY (a warning, never a silent drop). An authored insert
# at the NPC cap evicts a generated walk-on rather than being refused, so a named
# cast member is never crowded out by generated walk-ons (spec V3).
MAX_MANUAL_NPCS = 200
MAX_MANUAL_ENCOUNTERS = 100


@dataclass(frozen=True)
class ContentDiscard:
    """What a :meth:`MonsterManual.reconcile_content` discard dropped.

    Returned only when a *previously-stamped* pool was dropped on a content/seed
    mismatch (story 162-1, spec V1-V3). The counts feed the
    ``monster_manual.pool_discarded`` OTEL span so the GM panel sees that a
    stale, multi-clone-written pool was discarded and how many authored NPCs went
    with it (the "what deleted beneath_sunden's authored NPCs" forensic).
    """

    npcs_discarded: int
    encounters_discarded: int
    authored_discarded: int


def _tags_match_location(location_tags: list[str], loc_lower: str) -> bool:
    """Whether any placement tag overlaps the (already lowercased) location.

    A tag matches when it is a substring of the location OR the location is a
    substring of the tag — the same bidirectional, case-insensitive anchor
    match used for ``activated_location`` in :meth:`MonsterManual.format_nearby_npcs`.
    Tags are expected lowercase (authored that way) but are lowercased defensively.
    Blank tags never match.
    """
    for tag in location_tags:
        tag_lower = tag.lower().strip()
        if not tag_lower:
            continue
        if tag_lower in loc_lower or loc_lower in tag_lower:
            return True
    return False


class EntryState(StrEnum):
    """Lifecycle state for a Manual entry."""

    AVAILABLE = "available"
    """Pre-generated, not yet used in narration."""
    ACTIVE = "active"
    """Narrator has introduced them, currently in scene."""
    DORMANT = "dormant"
    """Used previously, not in current scene, can return."""


class ManualNpc(BaseModel):
    """A pre-generated NPC identity from sidequest-namegen."""

    model_config = {"extra": "forbid"}

    data: dict[str, Any]
    """Full namegen JSON output (name, OCEAN, personality, dialogue_quirks, etc.)."""

    name: str
    """Extracted name for quick reference / compound key."""

    role: str
    """Role (e.g., "wasteland trader", "tech cultist")."""

    culture: str
    """Culture/faction (e.g., "Scrapborn", "Vaultborn")."""

    location_tags: list[str] = Field(default_factory=list)
    """Lowercase biome/terrain/location substrings anchoring pre-authored placement.

    Consumed by :meth:`MonsterManual.format_nearby_npcs` (and the injection-seam
    mirror in ``monster_manual_inject``): an AVAILABLE NPC that carries tags is
    *placed* — it may only surface as "nearby (not yet met)" where one of its
    tags matches the current location (substring, case-insensitive, either
    direction — mirroring the ``activated_location`` anchor match). An NPC with
    no tags is *unplaced* and keeps the legacy behavior of being eligible
    everywhere (generated walk-ons). This is the fix for the wry_whimsy/oz bug
    where authored companions never appeared at the right spot because placement
    was ignored until an NPC had already been narrated.
    """

    state: EntryState = EntryState.AVAILABLE
    """Lifecycle state."""

    activated_location: str | None = None
    """Location where this NPC was first activated (introduced in narration).

    Used to anchor NPCs geographically — they don't follow the player everywhere.
    """

    factions: list[str] = Field(default_factory=list)
    """Faction/zone-scoped eligibility tag (epic-157, ADR-059 amendment).

    Each value is an exact world ``controlled_by`` faction slug or the reserved
    sentinel ``"*"`` (all zones). Default empty. Generated walk-ons are
    *origin-stamped* on activation (story 157-3), not authored — so this stays
    empty for the namegen pool until the walk-on is born in a region.
    """

    authored: bool = False
    """Whether this NPC came from a world's authored ``npcs.yaml`` cast.

    Story 162-1: the accumulation cap evicts a *generated* walk-on before an
    authored NPC (a named cast member is never crowded out), and
    :meth:`MonsterManual.reconcile_content` reports how many authored NPCs a
    discard dropped (spec V3 forensic). Epic-162's later stories fold this into a
    unified typed ``Origin``; for now it is a single boolean.
    """


class ManualEncounter(BaseModel):
    """A pre-generated encounter block from sidequest-encountergen."""

    model_config = {"extra": "forbid"}

    data: dict[str, Any]
    """Full encountergen JSON output (enemies array with stats, abilities, etc.)."""

    label: str
    """Summary label (e.g., "2x Salt Burrower (tier 2)")."""

    tier: int
    """Power tier (1-4)."""

    terrain_tags: list[str] = Field(default_factory=list)
    """Biome/terrain tags for future filtering."""

    factions: list[str] = Field(default_factory=list)
    """Faction/zone-scoped eligibility tag (epic-157, ADR-059 amendment).

    The union of the source bestiary entries' ``factions`` (stamped at seed
    time by :func:`sidequest.server.dispatch.pregen.seed_manual`). Each value
    is an exact world ``controlled_by`` faction slug or the reserved sentinel
    ``"*"``. Default empty → eligible everywhere at runtime (the permissive
    predicate); Seam 1 (:func:`monster_manual_inject._npc_patches_for_encounters`)
    drops a tagged-but-wrong-zone encounter.
    """

    state: EntryState = EntryState.AVAILABLE
    """Lifecycle state."""


class MonsterManual(BaseModel):
    """Persistent Monster Manual for a genre/world combination.

    Stored as JSON at ``~/.sidequest/manuals/{genre}_{world}.json``.
    Grows over play sessions — every generated entry persists.
    """

    model_config = {"extra": "forbid"}

    genre: str
    """Genre slug this manual belongs to."""

    world: str
    """World slug this manual belongs to."""

    npcs: list[ManualNpc] = Field(default_factory=list)
    """Pre-generated NPC entries available to this world."""

    encounters: list[ManualEncounter] = Field(default_factory=list)
    """Pre-generated encounter entries available to this world."""

    content_sha: str = ""
    """Content version the pool was derived under (story 162-1, spec D3).

    A hash of the world's effective bestiary (the creature roster the pool is
    seeded from). Empty on a legacy/never-stamped manual — :meth:`reconcile_content`
    *adopts* an unstamped pool without discarding, and only discards a
    *previously-stamped* pool whose ``content_sha`` changed (a different content
    checkout = a multi-clone writer). Set by the injection seam, not authored.
    """

    session_seed: str = ""
    """Per-session key so a NEW session re-derives its pool (derive-don't-cache).

    The session's ``SessionRoom`` slug. Reconnects within a session share it
    (pool preserved); a new session gets a new slug, so its
    :meth:`reconcile_content` sees a mismatch and re-derives — the pool is no
    longer a persistent cache that accumulates across sessions (the 1,153-NPC
    runaway). Empty on a legacy/never-stamped manual (adopted, not discarded).
    """

    # ── Persistence ────────────────────────────────────────────

    @staticmethod
    def _manuals_dir() -> Path:
        """Directory where Manual files are stored."""
        return Path.home() / ".sidequest" / "manuals"

    @staticmethod
    def _file_path(genre: str, world: str) -> Path:
        """File path for this genre/world Manual.

        Fails loud on a blank genre or world (story 162-1, spec D4). The old
        ``or ""`` caller path minted ``caverns_and_claudes_.json`` /
        ``heavy_metal_.json`` for sessions bound pre-world-resolution — an
        unattributable, shared, empty-slug key. Per No Silent Fallbacks a blank
        key never resolves to a file; the caller must supply a resolved world.
        """
        if not genre or not genre.strip():
            raise ValueError("MonsterManual requires a non-empty genre slug (got blank)")
        if not world or not world.strip():
            raise ValueError(
                f"MonsterManual requires a non-empty world slug for genre {genre!r} "
                "(got blank) — refusing to key the cache on an empty world"
            )
        return MonsterManual._manuals_dir() / f"{genre}_{world}.json"

    @classmethod
    def load(cls, genre: str, world: str) -> MonsterManual:
        """Load a Manual from disk. Returns a new empty Manual if file doesn't exist."""
        path = cls._file_path(genre, world)
        if not path.exists():
            return cls(genre=genre, world=world)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(
                "monster_manual.read_failed — starting fresh (path=%s, error=%s)",
                path,
                e,
            )
            return cls(genre=genre, world=world)
        try:
            return cls.model_validate_json(content)
        except ValueError as e:
            logger.warning(
                "monster_manual.load_failed — starting fresh (path=%s, error=%s)",
                path,
                e,
            )
            return cls(genre=genre, world=world)

    def save(self) -> None:
        """Save this Manual to disk."""
        # Validate the key FIRST — a blank genre/world raises before we create
        # the cache dir or write anything (story 162-1, No Silent Fallbacks).
        path = self._file_path(self.genre, self.world)
        directory = self._manuals_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("monster_manual.mkdir_failed (error=%s)", e)
            return
        try:
            json_text = self.model_dump_json(indent=2)
        except ValueError as e:
            logger.warning("monster_manual.serialize_failed (error=%s)", e)
            return
        try:
            path.write_text(json_text, encoding="utf-8")
        except OSError as e:
            logger.warning(
                "monster_manual.save_failed (path=%s, error=%s)",
                path,
                e,
            )

    # ── Lookup ──────────────────────────────────────────────────

    def get_npc(self, name: str, culture: str) -> ManualNpc | None:
        """Compound key lookup: find an NPC by (name, culture, world)."""
        name_lower = name.lower()
        culture_lower = culture.lower()
        for npc in self.npcs:
            if npc.name.lower() == name_lower and npc.culture.lower() == culture_lower:
                return npc
        return None

    def find_npc_by_name(self, name: str) -> ManualNpc | None:
        """Find an NPC by name alone (fuzzy — substring match)."""
        name_lower = name.lower()
        for npc in self.npcs:
            npc_lower = npc.name.lower()
            if npc_lower == name_lower or name_lower in npc_lower or npc_lower in name_lower:
                return npc
        return None

    def find_npc_by_exact_name(self, name: str) -> ManualNpc | None:
        """Find an NPC by exact (case-insensitive) full-name match.

        Unlike :meth:`find_npc_by_name`, this does NOT substring-match — so a
        canonical authored "Lion" is never confused with a walk-on "Cowardly
        Lion". Used by the authored-cast seeding path (:func:`~sidequest.server.
        dispatch.pregen._seed_authored_npcs`) where a substring collision must
        not shadow an authored NPC nor mutate the wrong entry's placement tags.
        """
        name_lower = name.lower()
        for npc in self.npcs:
            if npc.name.lower() == name_lower:
                return npc
        return None

    def find_enemy_by_name(self, name: str) -> tuple[dict, int] | None:
        """Find a pre-generated enemy across all encounters by name.

        Returns ``(enemy_dict, tier)`` for the first match, or ``None``.
        Case-insensitive, substring-fuzzy (mirrors ``find_npc_by_name``).
        Caller passes the result to ``_creature_patch_from_enemy`` so the
        promotion seam receives a real MM-derived stat block.
        """
        name_lower = name.lower()
        for enc in self.encounters:
            enemies_raw = enc.data.get("enemies") or []
            if not isinstance(enemies_raw, list):
                continue
            for enemy in enemies_raw:
                if not isinstance(enemy, dict):
                    continue
                enemy_name = enemy.get("name")
                if not isinstance(enemy_name, str):
                    continue
                enemy_lower = enemy_name.lower()
                if (
                    enemy_lower == name_lower
                    or name_lower in enemy_lower
                    or enemy_lower in name_lower
                ):
                    return enemy, enc.tier
        return None

    # ── Lifecycle ───────────────────────────────────────────────

    def mark_active(self, name: str, location: str, *, faction: str | None = None) -> None:
        """Mark an NPC as Active by name (case-insensitive, fuzzy).

        ``faction`` (epic-157 Seam 2, story 157-3): generated walk-on
        origin-stamp. When provided AND the matched entry carries no ``factions``
        yet, stamp it with the activating region's ``controlled_by`` faction — a
        walk-on born in Lilliput is a Lilliputian and cannot later resurface in
        Houyhnhnm-land. Only an EMPTY ``factions`` is stamped (a walk-on already
        born in a zone keeps its origin); a falsy ``faction`` (unzoned world /
        unowned region) never stamps, leaving the legacy activation untouched.
        """
        name_lower = name.lower()
        for npc in self.npcs:
            npc_lower = npc.name.lower()
            if npc_lower == name_lower or name_lower in npc_lower or npc_lower in name_lower:
                npc.state = EntryState.ACTIVE
                if npc.activated_location is None:
                    npc.activated_location = location
                if faction and not npc.factions:
                    npc.factions = [faction]
                return

    def mark_all_dormant(self) -> None:
        """Transition all Active entries to Dormant (call on location change)."""
        for npc in self.npcs:
            if npc.state == EntryState.ACTIVE:
                npc.state = EntryState.DORMANT
        for enc in self.encounters:
            if enc.state == EntryState.ACTIVE:
                enc.state = EntryState.DORMANT

    def available_npcs(self) -> list[ManualNpc]:
        """Available NPCs (not yet used in narration)."""
        return [n for n in self.npcs if n.state == EntryState.AVAILABLE]

    def available_encounters(self) -> list[ManualEncounter]:
        """Available encounters."""
        return [e for e in self.encounters if e.state == EntryState.AVAILABLE]

    def needs_seeding(self) -> bool:
        """Whether the Manual needs more Available entries."""
        return len(self.available_npcs()) < 4 or not self.available_encounters()

    def reconcile_content(self, *, content_sha: str, session_seed: str) -> ContentDiscard | None:
        """Discard the whole pool when the CONTENT version changed (story 162-1, D3).

        Derive-don't-cache. This genre+world-keyed cache is shared across sessions
        and ~4 clones with divergent content checkouts — the source of the
        beneath_sunden purge/reseed livelock and the barsoom foreign-bestiary
        bleed. Rather than name-match and targeted-purge stale entries (the two
        removed ``purge_*`` tourniquets), the pool carries the ``content_sha`` it
        was derived under and is discarded *wholesale* when the content changed, so
        staleness is impossible and a purge/reseed cycle cannot livelock.
        ``needs_seeding`` then re-fires and the pool re-derives.

        ``session_seed`` is *recorded* for per-world attribution (spec V1) — it is
        refreshed on every reconcile but does NOT trigger a discard. Content is the
        only staleness axis: a new session with the SAME content reuses the pool
        (accumulation is bounded by the caps, not by nuking the pool every
        session). Making session identity a discard key emptied a valid pool on
        every new session — a regression this deliberately avoids.

        Migration-aware (No Silent Fallbacks, without nuking legacy saves): an
        UNSTAMPED pool (``content_sha == ""`` — a pre-162-1 on-disk Manual or a
        freshly-loaded empty one) is *adopted*: stamped to the current content
        version WITHOUT discarding. A never-versioned pool is not evidence of a
        divergent-content writer. Only a *previously-stamped* pool whose
        ``content_sha`` changed is discarded.

        Returns a :class:`ContentDiscard` only when a stamped, non-empty pool was
        actually dropped (feeds the ``monster_manual.pool_discarded`` span);
        ``None`` on unchanged content, on adoption of an unstamped pool, or when
        the discarded pool held nothing. Pure apart from mutating ``self``; the
        caller persists + emits the OTEL span.
        """
        # Attribution is always refreshed; only content drives staleness.
        self.session_seed = session_seed
        stale = bool(self.content_sha) and self.content_sha != content_sha
        if not stale:
            # Unchanged content, or an unstamped pool being adopted — keep the
            # pool and stamp/refresh the content version.
            self.content_sha = content_sha
            return None

        npcs_discarded = len(self.npcs)
        encounters_discarded = len(self.encounters)
        authored_discarded = sum(1 for n in self.npcs if n.authored)

        # Content changed under a previously-stamped pool: a different content
        # checkout (multi-clone writer) — discard and re-derive. Re-stamp so the
        # next reconcile against the same content is stable (no livelock).
        self.content_sha = content_sha
        self.npcs = []
        self.encounters = []
        if npcs_discarded == 0 and encounters_discarded == 0:
            return None
        return ContentDiscard(
            npcs_discarded=npcs_discarded,
            encounters_discarded=encounters_discarded,
            authored_discarded=authored_discarded,
        )

    # ── Placement ───────────────────────────────────────────────

    def available_at_location(self, current_location: str) -> list[ManualNpc]:
        """AVAILABLE NPCs eligible to surface as "nearby (not yet met)" here.

        Selection precedence (the fix for the wry_whimsy/oz placement bug):

        - A *placed* NPC (non-empty ``location_tags``) surfaces ONLY when one of
          its tags matches ``current_location`` — substring, case-insensitive,
          either direction, mirroring the ``activated_location`` anchor match in
          :meth:`format_nearby_npcs`. Placed-but-non-matching NPCs are excluded.
        - An *unplaced* NPC (no ``location_tags``) keeps the legacy behavior of
          being eligible everywhere (generated walk-ons).

        Placed-and-matching NPCs are ordered ahead of unplaced ones so authored
        roster NPCs win the surfacing race against generic generated walk-ons.
        """
        loc_lower = (current_location or "").lower()
        placed: list[ManualNpc] = []
        unplaced: list[ManualNpc] = []
        for npc in self.npcs:
            if npc.state != EntryState.AVAILABLE:
                continue
            if not npc.location_tags:
                unplaced.append(npc)
                continue
            if loc_lower and _tags_match_location(npc.location_tags, loc_lower):
                placed.append(npc)
        return placed + unplaced

    # ── Formatting for game_state injection ────────────────────

    def format_nearby_npcs(self, current_location: str) -> str:
        """Format location-relevant NPCs for injection into the ``<game_state>`` section.

        Only includes NPCs that are:

        - Active at the current location (full profile with personality + speech)
        - Available but not yet encountered (name + role only, max 3)

        Dormant NPCs at other locations are omitted entirely — the narrator
        doesn't need the full world roster to narrate the current scene.

        A ``None``/blank ``current_location`` (pre-bind / pre-chargen turn) is
        tolerated: there is no location to match, so placed NPCs are gated out
        and only Active-anchorless + unplaced NPCs surface (No crash on
        ``None.lower()`` — M6, wry_whimsy/oz follow-up).
        """
        loc_lower = (current_location or "").lower()

        at_location: list[ManualNpc] = []
        for npc in self.npcs:
            if npc.state != EntryState.ACTIVE:
                continue
            if npc.activated_location is None:
                at_location.append(npc)
                continue
            anchor_lower = npc.activated_location.lower()
            if anchor_lower in loc_lower or loc_lower in anchor_lower:
                at_location.append(npc)

        available = self.available_at_location(current_location)[:3]

        if not at_location and not available:
            return ""

        lines: list[str] = []

        if at_location:
            lines.append("NPCs present at this location:")
            for npc in at_location:
                ocean_summary = npc.data.get("ocean_summary", "") or ""
                quirks_raw = npc.data.get("dialogue_quirks") or []
                quirks = [q for q in quirks_raw if isinstance(q, str)][:2]
                quirk_str = f"\n    Speech: {'; '.join(quirks)}" if quirks else ""
                lines.append(
                    f"  - {npc.name} ({npc.role}, {npc.culture}) — {ocean_summary}{quirk_str}"
                )

        if available:
            names = [f"{n.name} ({n.role})" for n in available]
            lines.append(f"Other known NPCs: {', '.join(names)}")

        return "\n".join(lines)

    def format_area_creatures(self, in_combat: bool) -> str:
        """Format encounters for injection into ``<game_state>``.

        When ``in_combat`` is true, includes full stat blocks (abilities + weaknesses)
        for all available encounters — the narrator needs them for combat resolution.

        When not in combat, includes only name + tier for at most 2 encounters.
        The narrator doesn't need 8 creature stat blocks to describe a marketplace.
        """
        available = self.available_encounters()
        if not available:
            return ""

        lines: list[str] = ["Hostile creatures in the area:"]
        limit = len(available) if in_combat else 2
        for enc in available[:limit]:
            enemies = enc.data.get("enemies") or []
            if not isinstance(enemies, list):
                continue
            for enemy in enemies:
                if not isinstance(enemy, dict):
                    continue
                name = enemy.get("name") or "Unknown"
                class_label = enemy.get("class") or ""
                tier_label = enemy.get("tier_label") or "?"
                hp = enemy.get("hp") or 0
                role = enemy.get("role") or ""
                lines.append(f"  - {name} ({class_label}, {tier_label}, HP {hp}) — {role}")
                if in_combat:
                    abilities_raw = enemy.get("abilities") or []
                    abilities = [a for a in abilities_raw if isinstance(a, str)][:3]
                    weaknesses_raw = enemy.get("weaknesses") or []
                    weaknesses = [w for w in weaknesses_raw if isinstance(w, str)][:2]
                    if abilities or weaknesses:
                        lines.append(
                            f"    Abilities: {', '.join(abilities)}."
                            f" Weakness: {', '.join(weaknesses)}."
                        )

        return "\n".join(lines)

    # ── Insertion ───────────────────────────────────────────────

    def add_npc(
        self,
        data: dict[str, Any],
        location_tags: list[str],
        *,
        exact: bool = False,
        authored: bool = False,
    ) -> None:
        """Add a pre-generated NPC from namegen JSON output.

        Dedup defaults to the fuzzy :meth:`find_npc_by_name` (substring) — the
        legacy behavior for generated walk-ons. Pass ``exact=True`` to dedup on
        full name only (:meth:`find_npc_by_exact_name`); the authored-cast path
        uses this so a canonical NPC whose name is a substring of an existing
        entry (e.g. "Lion" vs "Cowardly Lion") is still inserted instead of being
        silently swallowed by the fuzzy guard.

        ``authored`` (story 162-1) marks an NPC that came from a world's authored
        ``npcs.yaml`` cast. It governs the accumulation cap: past
        :data:`MAX_MANUAL_NPCS`, a generated walk-on is dropped LOUDLY, but an
        authored NPC instead *evicts* the oldest generated walk-on so a named cast
        member is never crowded out (spec V3). A cap hit is logged, never silent.
        """
        name = str(data.get("name") or "")
        role = str(data.get("role") or "")
        culture = str(data.get("culture") or "")

        match = self.find_npc_by_exact_name(name) if exact else self.find_npc_by_name(name)
        if match is not None:
            return

        if len(self.npcs) >= MAX_MANUAL_NPCS and not self._make_room_for_npc(
            authored=authored, incoming=name
        ):
            return

        self.npcs.append(
            ManualNpc(
                data=data,
                name=name,
                role=role,
                culture=culture,
                location_tags=location_tags,
                state=EntryState.AVAILABLE,
                activated_location=None,
                authored=authored,
            )
        )

    def _make_room_for_npc(self, *, authored: bool, incoming: str) -> bool:
        """Enforce the NPC accumulation cap. Returns True if there is room to add.

        A generated walk-on at the cap is refused (dropped loudly). An authored
        NPC evicts a generated walk-on to make room, so a named cast member is
        never crowded out; only if the pool is ALL authored is an authored insert
        refused. Never a silent drop (story 162-1, D4).

        Eviction prefers an AVAILABLE walk-on (never activated — nothing in play
        references it) and only falls back to the oldest activated one when every
        generated entry is in play. An ACTIVE walk-on is anchored to a location
        and projected into narration (Diamonds and Coal — a walk-on the players
        engaged is a diamond in the making); silently vanishing it mid-scene is
        the bug class this Manual exists to prevent.
        """
        if not authored:
            logger.warning(
                "monster_manual.npc_cap_reached — dropping generated NPC %r (cap=%d)",
                incoming,
                MAX_MANUAL_NPCS,
            )
            return False
        evict_at: int | None = None
        for i, existing in enumerate(self.npcs):
            if existing.authored:
                continue
            if existing.state == EntryState.AVAILABLE:
                evict_at = i
                break
            if evict_at is None:
                evict_at = i
        if evict_at is not None:
            logger.warning(
                "monster_manual.npc_cap_evicted — authored %r evicts generated %r (cap=%d)",
                incoming,
                self.npcs[evict_at].name,
                MAX_MANUAL_NPCS,
            )
            del self.npcs[evict_at]
            return True
        logger.warning(
            "monster_manual.npc_cap_reached_all_authored — dropping authored NPC %r (cap=%d)",
            incoming,
            MAX_MANUAL_NPCS,
        )
        return False

    def add_encounter(
        self,
        data: dict[str, Any],
        tier: int,
        terrain_tags: list[str],
        factions: list[str] | None = None,
    ) -> None:
        """Add a pre-generated encounter from encountergen JSON output.

        ``factions`` (epic-157) is the union of the source bestiary entries'
        faction tags, stamped by the seeding path so Seam 1 can scope the
        encounter to its zone. Defaults to empty (unzoned worlds / native
        packs with no bestiary) — empty means eligible everywhere at runtime.
        """
        enemies_raw = data.get("enemies") or []
        enemy_names: list[str] = []
        if isinstance(enemies_raw, list):
            for enemy in enemies_raw:
                if isinstance(enemy, dict):
                    name = enemy.get("name")
                    if isinstance(name, str):
                        enemy_names.append(name)

        label = (
            f"encounter (tier {tier})"
            if not enemy_names
            else f"{', '.join(enemy_names)} (tier {tier})"
        )

        if len(self.encounters) >= MAX_MANUAL_ENCOUNTERS:
            # Accumulation cap (story 162-1, D4) — drop LOUDLY, never silently.
            logger.warning(
                "monster_manual.encounter_cap_reached — dropping encounter %r (cap=%d)",
                label,
                MAX_MANUAL_ENCOUNTERS,
            )
            return

        self.encounters.append(
            ManualEncounter(
                data=data,
                label=label,
                tier=tier,
                terrain_tags=terrain_tags,
                factions=list(factions or []),
                state=EntryState.AVAILABLE,
            )
        )
