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
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
    """Biome/terrain/location tags for future filtering."""

    state: EntryState = EntryState.AVAILABLE
    """Lifecycle state."""

    activated_location: str | None = None
    """Location where this NPC was first activated (introduced in narration).

    Used to anchor NPCs geographically — they don't follow the player everywhere.
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

    # ── Persistence ────────────────────────────────────────────

    @staticmethod
    def _manuals_dir() -> Path:
        """Directory where Manual files are stored."""
        return Path.home() / ".sidequest" / "manuals"

    @staticmethod
    def _file_path(genre: str, world: str) -> Path:
        """File path for this genre/world Manual."""
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
        directory = self._manuals_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("monster_manual.mkdir_failed (error=%s)", e)
            return
        path = self._file_path(self.genre, self.world)
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

    # ── Lifecycle ───────────────────────────────────────────────

    def mark_active(self, name: str, location: str) -> None:
        """Mark an NPC as Active by name (case-insensitive, fuzzy)."""
        name_lower = name.lower()
        for npc in self.npcs:
            npc_lower = npc.name.lower()
            if npc_lower == name_lower or name_lower in npc_lower or npc_lower in name_lower:
                npc.state = EntryState.ACTIVE
                if npc.activated_location is None:
                    npc.activated_location = location
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

    # ── Formatting for game_state injection ────────────────────

    def format_nearby_npcs(self, current_location: str) -> str:
        """Format location-relevant NPCs for injection into the ``<game_state>`` section.

        Only includes NPCs that are:

        - Active at the current location (full profile with personality + speech)
        - Available but not yet encountered (name + role only, max 3)

        Dormant NPCs at other locations are omitted entirely — the narrator
        doesn't need the full world roster to narrate the current scene.
        """
        loc_lower = current_location.lower()

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

        available = [n for n in self.npcs if n.state == EntryState.AVAILABLE][:3]

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

    def add_npc(self, data: dict[str, Any], location_tags: list[str]) -> None:
        """Add a pre-generated NPC from namegen JSON output."""
        name = str(data.get("name") or "")
        role = str(data.get("role") or "")
        culture = str(data.get("culture") or "")

        if self.find_npc_by_name(name) is not None:
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
            )
        )

    def add_encounter(
        self,
        data: dict[str, Any],
        tier: int,
        terrain_tags: list[str],
    ) -> None:
        """Add a pre-generated encounter from encountergen JSON output."""
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

        self.encounters.append(
            ManualEncounter(
                data=data,
                label=label,
                tier=tier,
                terrain_tags=terrain_tags,
                state=EntryState.AVAILABLE,
            )
        )
