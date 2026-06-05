"""Encounter generator CLI.

Generates enemy stat blocks from genre pack data. When ``--world`` is provided
and the world has a ``creatures.yaml`` (now nested as ``regions.{region}.creatures``
post-2026-05-10 fold), samples creatures by tier from the bestiary. Otherwise
generates humanoid NPCs from genre rules.

Ported from ``crates/sidequest-encountergen/src/main.rs``.

Usage:
  python -m sidequest.cli.encountergen --genre-packs-path ./genre_packs --genre mutant_wasteland
  python -m sidequest.cli.encountergen --genre-packs-path ./genre_packs --genre mutant_wasteland --tier 2 --count 3
  python -m sidequest.cli.encountergen --genre-packs-path ./genre_packs --genre caverns_and_claudes --world caverns_sunden --tier 1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sidequest.cli.namegen.namegen import (
    OceanValues,
    TropeConnection,
    jitter_ocean,
    match_tropes,
    summarize_ocean,
)
from sidequest.genre import (
    Culture,
    GenrePack,
    NpcArchetype,
    load_genre_pack,
)
from sidequest.genre.models.character import spawnable_archetypes
from sidequest.genre.models.narrative import PowerTier
from sidequest.genre.names import build_from_culture

# Default HP base per level for humanoid enemies. Rust used
# ``pack.rules.class_hp_bases.get(&class).copied().unwrap_or(8)``; the
# Python rules model dropped ``class_hp_bases`` when ADR-078 replaced HP
# with Edge for runtime entities. The encountergen output stays B/X-shaped
# (content layer); the HP→Edge translation happens at the materializer.
DEFAULT_HP_BASE = 8


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass
class EnemyBlock:
    name: str
    class_: str
    race: str
    level: int
    tier_label: str
    role: str
    hp: int
    abilities: list[str]
    weaknesses: list[str]
    disposition: int
    personality: list[str]
    dialogue_quirks: list[str]
    inventory: list[str]
    stat_scores: dict[str, int]
    ocean: OceanValues
    ocean_summary: str
    trope_connections: list[TropeConnection]
    visual_prompt: str
    # Bestiary combat layer (story 90-1) — only set on the ruleset-module
    # path; None on the native / creatures.yaml paths, where the keys are
    # dropped from JSON output so the legacy shape is byte-identical.
    armor_class: int | None = None
    attack_bonus: int | None = None


@dataclass
class EncounterBlock:
    enemies: list[EnemyBlock] = field(default_factory=list)


def _enemy_block_to_dict(block: EnemyBlock) -> dict[str, Any]:
    """Serialize EnemyBlock — rename ``class_`` to ``class`` for JSON compat."""
    data = asdict(block)
    data["class"] = data.pop("class_")
    # Keep the legacy (native / creatures.yaml) output shape unchanged:
    # the bestiary combat keys only appear when the bestiary path set them.
    if data["armor_class"] is None:
        del data["armor_class"]
    if data["attack_bonus"] is None:
        del data["attack_bonus"]
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidequest-encountergen",
        description="Generate enemy encounter stat blocks from genre pack data",
    )
    p.add_argument(
        "--genre-packs-path",
        type=Path,
        default=os.environ.get("SIDEQUEST_CONTENT_PATH"),
        required="SIDEQUEST_CONTENT_PATH" not in os.environ,
        help="Path to the genre_packs/ directory. Also reads SIDEQUEST_CONTENT_PATH.",
    )
    p.add_argument(
        "--genre",
        default=os.environ.get("SIDEQUEST_GENRE"),
        required="SIDEQUEST_GENRE" not in os.environ,
        help="Genre slug (e.g., mutant_wasteland). Also reads SIDEQUEST_GENRE.",
    )
    p.add_argument(
        "--world",
        help=(
            "World slug. When set, checks for worlds/{world}/creatures.yaml and samples "
            "from creature definitions instead of generating humanoid NPCs."
        ),
    )
    p.add_argument(
        "--tier",
        type=int,
        help="Power tier (1-4, maps to level ranges). Random if omitted.",
    )
    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of enemies to generate. Defaults to 1.",
    )
    p.add_argument("--role", help='Enemy role hint (e.g., "ambush predator").')
    p.add_argument("--class", dest="class_", help="Character class. Random if omitted.")
    p.add_argument("--culture", help="Culture name for name generation. Random if omitted.")
    p.add_argument(
        "--archetype", help='Archetype name (e.g., "Wasteland Trader"). Random if omitted.'
    )
    p.add_argument(
        "--context",
        help='Context hint for the encounter (e.g., "guarding a bridge"). Flavors the visual prompt.',
    )
    return p


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def write_sidecar(block: EncounterBlock) -> None:
    """Write tool call records to the sidecar JSONL file for the orchestrator."""
    dir_ = os.environ.get("SIDEQUEST_TOOL_SIDECAR_DIR")
    session_id = os.environ.get("SIDEQUEST_TOOL_SESSION_ID")
    if not dir_ or not session_id:
        return

    sidecar_path = Path(dir_) / f"sidequest-tools-{session_id}.jsonl"
    Path(dir_).mkdir(parents=True, exist_ok=True)

    with sidecar_path.open("a") as f:
        for enemy in block.enemies:
            record = {
                "tool": "personality_event",
                "result": {
                    "npc": enemy.name,
                    "event_type": "introduced",
                    "description": f"enemy: {enemy.role} ({enemy.tier_label})",
                },
            }
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Tier helpers
# ---------------------------------------------------------------------------


def tier_to_level_range(tier: int) -> tuple[int, int]:
    """Map tier number (1-4) to a level range."""
    if tier == 1:
        return (1, 3)
    if tier == 2:
        return (4, 6)
    if tier == 3:
        return (7, 9)
    if tier == 4:
        return (10, 10)
    return (1, 3)


def find_power_tier(
    power_tiers: dict[str, list[PowerTier]], class_: str, level: int
) -> PowerTier | None:
    """Find the power tier entry for a given class and level."""
    tiers = power_tiers.get(class_)
    if not tiers:
        return None
    for t in tiers:
        if t.level_range[0] <= level <= t.level_range[1]:
            return t
    return None


# ---------------------------------------------------------------------------
# Creature YAML → EnemyBlock
# ---------------------------------------------------------------------------


def _collect_creatures_from_yaml(creatures_path: Path) -> list[dict[str, Any]]:
    """Return a flat list of creature dicts from a world's ``creatures.yaml``.

    Schema delta (2026-05-10 fold): the file is now keyed by region —
    ``regions.{region_id}.creatures: [...]``. Rust expected a top-level
    ``creatures:`` sequence; this loader flattens both shapes.
    """
    if not creatures_path.exists():
        return []
    try:
        doc = yaml.safe_load(creatures_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(
            f"sidequest-encountergen: failed to parse {creatures_path}: {e}",
            file=sys.stderr,
        )
        return []
    if not isinstance(doc, dict):
        return []

    out: list[dict[str, Any]] = []

    legacy = doc.get("creatures")
    if isinstance(legacy, list):
        out.extend(c for c in legacy if isinstance(c, dict))

    regions = doc.get("regions")
    if isinstance(regions, dict):
        for region in regions.values():
            if not isinstance(region, dict):
                continue
            region_creatures = region.get("creatures")
            if isinstance(region_creatures, list):
                out.extend(c for c in region_creatures if isinstance(c, dict))

    return out


def creature_to_enemy_block(creature: dict[str, Any], rng: random.Random) -> EnemyBlock:
    """Convert a creature definition from creatures.yaml into an EnemyBlock."""

    def str_field(key: str) -> str:
        value = creature.get(key)
        return value if isinstance(value, str) else ""

    def int_field(key: str, default: int) -> int:
        value = creature.get(key)
        if isinstance(value, bool):  # bool is a subclass of int in Python
            return default
        if isinstance(value, int):
            return value
        return default

    name = str_field("name")
    threat_level = int_field("threat_level", 1)
    hp = int_field("hp", 4)
    ac = int_field("ac", 10)
    damage = str_field("damage")
    morale = str_field("morale")

    abilities_raw = creature.get("abilities") or []
    abilities: list[str] = []
    if isinstance(abilities_raw, list):
        for ab in abilities_raw:
            if not isinstance(ab, dict):
                continue
            aname = ab.get("name")
            adesc = ab.get("description") or ""
            if not isinstance(aname, str) or not aname:
                continue
            adesc_short = adesc[:80] if isinstance(adesc, str) else ""
            abilities.append(f"{aname} — {adesc_short}")

    tags_raw = creature.get("tags") or []
    tags: list[str] = (
        [t for t in tags_raw if isinstance(t, str)] if isinstance(tags_raw, list) else []
    )

    loot_raw = creature.get("loot") or []
    inventory: list[str] = []
    if isinstance(loot_raw, list):
        for entry in loot_raw:
            if isinstance(entry, dict):
                desc = entry.get("description")
                if isinstance(desc, str):
                    inventory.append(desc)

    description = creature.get("description") or ""
    visual_prompt = description[:200] if isinstance(description, str) else ""

    role = f"{damage}, morale: {morale}" if damage else morale

    return EnemyBlock(
        name=name,
        class_="creature",
        race=tags[0] if tags else "beast",
        level=threat_level,
        tier_label=f"tier-{threat_level}",
        role=role,
        hp=hp,
        abilities=abilities,
        weaknesses=[f"AC {ac}"],
        disposition=-20,
        personality=[],
        dialogue_quirks=[],
        inventory=inventory,
        stat_scores={},
        ocean=OceanValues(
            openness=rng.uniform(1.0, 4.0),
            conscientiousness=rng.uniform(2.0, 5.0),
            extraversion=rng.uniform(2.0, 6.0),
            agreeableness=rng.uniform(1.0, 3.0),
            neuroticism=rng.uniform(4.0, 8.0),
        ),
        ocean_summary="feral and aggressive",
        trope_connections=[],
        visual_prompt=visual_prompt,
    )


# ---------------------------------------------------------------------------
# Pack bestiary → EnemyBlock (ruleset-module packs, story 90-1)
# ---------------------------------------------------------------------------


def generate_enemy_from_bestiary(
    pack: GenrePack,
    args: argparse.Namespace,
    rng: random.Random,
) -> EnemyBlock:
    """Generate an enemy from the pack-root bestiary (``ruleset != native``).

    The bestiary entry supplies the combat layer (level / hp / armor_class /
    attack_bonus, SRD-aligned per the bound ruleset); encountergen composes
    the narrative layers (OCEAN, visual prompt) the same way the
    creatures.yaml path does. Caller guarantees ``pack.bestiary`` is set.
    """
    assert pack.bestiary is not None  # guarded by main()'s fail-loud branch
    entries = pack.bestiary.entries

    tier = args.tier if args.tier is not None else rng.randint(1, 3)
    level_min, level_max = tier_to_level_range(tier)
    pool = [e for e in entries if level_min <= e.level <= level_max]
    if not pool:
        # Mirror the creatures.yaml sampling rule: an unpopulated tier falls
        # back to the full entry list (a sampling decision, not a config
        # fallback — the bestiary itself is validated non-empty at load).
        pool = list(entries)
    entry = rng.choice(pool)

    role = entry.role if entry.role else entry.name.lower()

    # Narrative layers stay encountergen's job (90-1 schema decision).
    parts: list[str] = [entry.description if entry.description else f"{entry.name}, {role}"]
    if args.context:
        parts.append(args.context)
    if pack.visual_style is not None:
        parts.append(pack.visual_style.positive_suffix)
    visual_prompt = ", ".join(p.strip().rstrip(",") for p in parts if p and p.strip())

    ocean = OceanValues(
        openness=rng.uniform(1.0, 4.0),
        conscientiousness=rng.uniform(2.0, 5.0),
        extraversion=rng.uniform(2.0, 6.0),
        agreeableness=rng.uniform(1.0, 3.0),
        neuroticism=rng.uniform(4.0, 8.0),
    )

    return EnemyBlock(
        name=entry.name,
        class_="creature",
        race=entry.tags[0] if entry.tags else "hostile",
        level=entry.level,
        tier_label=f"tier-{tier}",
        role=role,
        hp=entry.hp,
        abilities=list(entry.abilities),
        weaknesses=[],
        disposition=-20,
        personality=[],
        dialogue_quirks=[],
        inventory=[],
        stat_scores={},
        ocean=ocean,
        ocean_summary=summarize_ocean(ocean),
        trope_connections=[],
        visual_prompt=visual_prompt,
        armor_class=entry.armor_class,
        attack_bonus=entry.attack_bonus,
    )


# ---------------------------------------------------------------------------
# Abilities (class-tiered tables ported verbatim from Rust)
# ---------------------------------------------------------------------------


_CLASS_ABILITIES: dict[str, list[list[str]]] = {
    "scavenger": [
        ["Scrap Throw", "Quick Loot", "Improvised Trap"],
        ["Ambush", "Jury-Rig Weapon", "Escape Artist"],
        ["Salvage Mastery", "Trap Network", "Ghost Walk"],
        ["Vaultbreaker Strike", "Scrap Golem", "Perfect Ambush"],
    ],
    "mutant": [
        ["Toxic Spit", "Hardened Skin", "Feral Charge"],
        ["Acid Blood", "Regeneration", "Bioluminescent Flash"],
        ["Mutation Surge", "Toxic Cloud", "Adaptive Armor"],
        ["Apex Transformation", "Radioactive Aura", "Evolution Burst"],
    ],
    "pureblood": [
        ["First Aid", "Old-World Knowledge", "Steady Aim"],
        ["Field Surgery", "Tactical Analysis", "Precision Shot"],
        ["Command Presence", "Pre-War Tech Override", "Suppressing Fire"],
        ["Architect's Will", "Orbital Strike Beacon", "Civilization's Shield"],
    ],
    "synth": [
        ["Overclock", "Synthetic Resilience", "Scan"],
        ["Integrated Weapon", "Self-Repair", "EMP Pulse"],
        ["Combat Protocol", "System Override", "Drone Deploy"],
        ["Sovereign Mode", "Nanite Swarm", "Full System Integration"],
    ],
    "beastkin": [
        ["Feral Bite", "Pack Instinct", "Keen Senses"],
        ["Predator's Leap", "Territorial Roar", "Venom Strike"],
        ["Alpha Command", "Primal Fury", "Nature's Armor"],
        ["Apex Predator", "Pack Swarm", "Primal Lord's Presence"],
    ],
    "tinker": [
        ["Jury-Rig", "Shock Prod", "Smoke Bomb"],
        ["Turret Deploy", "Electrified Net", "Gadget Barrage"],
        ["Mech Suit Engage", "Tesla Coil", "Drone Swarm"],
        ["Forge Master's Arsenal", "Fabricator Beam", "Walking Workshop"],
    ],
}

_GENERIC_ABILITIES: list[list[str]] = [
    ["Strike", "Defend", "Retreat"],
    ["Power Strike", "Taunt", "Rally"],
    ["Devastating Blow", "Battle Cry", "Last Stand"],
    ["Ultimate Strike", "Overwhelming Force", "Unstoppable"],
]


def generate_abilities(
    class_: str,
    tier: int,
    archetype: NpcArchetype,
    rng: random.Random,
) -> list[str]:
    """Generate abilities from class + tier. Higher tiers unlock more powerful abilities."""
    table = _CLASS_ABILITIES.get(class_.lower(), _GENERIC_ABILITIES)
    tier_idx = max(0, min(len(table) - 1, tier - 1))

    abilities: list[str] = []
    for t_idx in range(tier_idx + 1):
        pool = table[t_idx]
        pick_count = 2 if t_idx == tier_idx else 1
        picked = 0
        for ability in pool:
            if picked >= pick_count:
                break
            if rng.randrange(10) < 7:  # 70% inclusion
                abilities.append(ability)
                picked += 1
        if t_idx == tier_idx and picked == 0:
            abilities.append(rng.choice(pool))

    if any(c.lower() == class_.lower() for c in archetype.typical_classes):
        abilities.append(f"{archetype.name}'s Instinct")

    return abilities


# ---------------------------------------------------------------------------
# Weaknesses
# ---------------------------------------------------------------------------


_CLASS_WEAKNESSES: dict[str, str] = {
    "scavenger": "low durability — light or no armor",
    "mutant": "radiation dependency — weakens in clean zones",
    "pureblood": "contamination vulnerability — no mutation resistance",
    "synth": "EMP vulnerability — stunned by electromagnetic pulses",
    "beastkin": "fire aversion — panics near open flame",
    "tinker": "gadget fragility — abilities break on critical failure",
}


def generate_weaknesses(class_: str, race: str, rng: random.Random) -> list[str]:
    weaknesses: list[str] = [_CLASS_WEAKNESSES.get(class_.lower(), "no special resistances")]

    if rng.randrange(2) == 0:
        race_lower = race.lower()
        if "mutant" in race_lower:
            weaknesses.append("unstable mutations — random debuff under stress")
        elif "synthetic" in race_lower:
            weaknesses.append("memory fragmentation — confused by paradoxes")
        elif "plant" in race_lower:
            weaknesses.append("drought vulnerability — weakened without water")
        elif "animal" in race_lower or "uplifted" in race_lower:
            weaknesses.append("pack instinct — morale breaks when isolated")

    return weaknesses


# ---------------------------------------------------------------------------
# Visual prompt
# ---------------------------------------------------------------------------


def build_visual_prompt(
    pack: GenrePack,
    class_: str,
    level: int,
    archetype: NpcArchetype,
    context: str | None,
) -> str:
    """Build an image generation prompt from power_tiers NPC description + visual_style."""
    parts: list[str] = []

    tier = find_power_tier(pack.power_tiers, class_, level)
    if tier is not None:
        parts.append(tier.npc if tier.npc else tier.player)
    else:
        parts.append(archetype.description)

    if context:
        parts.append(context)

    # Pack-level visual_style is optional (2026-05-29 directive — style lives
    # at world level). When the genre carries no pack-level style there is no
    # genre suffix to append; this is by-design absence, not a dropped config.
    if pack.visual_style is not None:
        parts.append(pack.visual_style.positive_suffix)

    cleaned = [p.strip().rstrip(",") for p in parts]
    return ", ".join(cleaned)


# ---------------------------------------------------------------------------
# Humanoid generation
# ---------------------------------------------------------------------------


def generate_enemy(
    pack: GenrePack,
    genre_dir: Path,
    args: argparse.Namespace,
    rng: random.Random,
) -> EnemyBlock:
    """Generate a humanoid enemy block from pack rules + archetypes."""
    corpus_dir = genre_dir / "corpus"
    corpus_fallbacks = [genre_dir.parent.parent / "corpus" / "shared"]

    # Class
    allowed_classes = pack.rules.allowed_classes
    if not allowed_classes:
        print(
            f"sidequest-encountergen: genre '{args.genre}' has no allowed_classes in rules.yaml",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.class_:
        match = next(
            (c for c in allowed_classes if c.lower() == args.class_.lower()),
            None,
        )
        if match is None:
            print(
                f"Class '{args.class_}' not found. Available: {', '.join(allowed_classes)}",
                file=sys.stderr,
            )
            sys.exit(1)
        class_ = match
    else:
        class_ = rng.choice(allowed_classes)

    # Race
    allowed_races = pack.rules.allowed_races
    if not allowed_races:
        print(
            f"sidequest-encountergen: genre '{args.genre}' has no allowed_races in rules.yaml",
            file=sys.stderr,
        )
        sys.exit(1)
    race = rng.choice(allowed_races)

    # Tier and level (tier 4 rare for random rolls)
    tier = args.tier if args.tier is not None else rng.randint(1, 3)
    level_min, level_max = tier_to_level_range(tier)
    level = rng.randint(level_min, level_max)

    # HP — Rust used class_hp_bases (dropped in Python rules per ADR-078).
    # Fall back to DEFAULT_HP_BASE; the materializer translates to EdgePool.
    hp = DEFAULT_HP_BASE * level

    # Archetype — world-over-genre resolution (GenrePack.effective_*), the SAME
    # resolution namegen + pregen.seed_manual use. Reading pack.archetypes raw
    # here was the perseus_cloud divergence (session 894) reaching this CLI:
    # genres that keep flavor in the world (epic-74) ship empty genre-tier
    # archetypes/cultures, so the raw read seeds zero encounters.
    archetypes, _ = pack.effective_archetypes(args.world)
    if not archetypes:
        world_clause = f" world '{args.world}'" if args.world else ""
        print(
            f"sidequest-encountergen: no archetypes for genre '{args.genre}'{world_clause} "
            "— archetypes.yaml is empty at both genre and world tiers",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.archetype:
        archetype = next(
            (a for a in archetypes if a.name.lower() == args.archetype.lower()),
            None,
        )
        if archetype is None:
            names = ", ".join(a.name for a in archetypes)
            print(
                f"Archetype '{args.archetype}' not found. Available: {names}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        spawnable = spawnable_archetypes(archetypes)
        if not spawnable:
            world_clause = f" world '{args.world}'" if args.world else ""
            print(
                f"sidequest-encountergen: no spawnable archetypes for genre "
                f"'{args.genre}'{world_clause} — all archetypes are named_individual "
                "(specific people who must not be randomly generated as enemies)",
                file=sys.stderr,
            )
            sys.exit(1)
        archetype = rng.choice(spawnable)

    # Culture + name — world-over-genre resolution (see archetype note above).
    cultures, _ = pack.effective_cultures(args.world)
    if not cultures:
        world_clause = f" world '{args.world}'" if args.world else ""
        print(
            f"sidequest-encountergen: no cultures for genre '{args.genre}'{world_clause} "
            "— cultures.yaml is empty at both genre and world tiers",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.culture:
        culture = next(
            (c for c in cultures if c.name.lower() == args.culture.lower()),
            None,
        )
        if culture is None:
            names = ", ".join(c.name for c in cultures)
            print(
                f"Culture '{args.culture}' not found. Available: {names}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        culture = rng.choice(cultures)

    name = _generate_name(culture, corpus_dir, rng, fallback_dirs=corpus_fallbacks)

    # Role
    role = args.role if args.role else archetype.name.lower()

    # Stat scores from archetype stat_ranges
    stat_scores: dict[str, int] = {}
    for stat_name in pack.rules.ability_score_names:
        rng_pair = archetype.stat_ranges.get(stat_name)
        if rng_pair and len(rng_pair) >= 2:
            stat_scores[stat_name] = rng.randint(int(rng_pair[0]), int(rng_pair[1]))
        else:
            stat_scores[stat_name] = rng.randint(8, 14)

    # OCEAN (jittered from archetype baseline)
    ocean = jitter_ocean(archetype, rng)
    ocean_summary = summarize_ocean(ocean)

    abilities = generate_abilities(class_, tier, archetype, rng)
    weaknesses = generate_weaknesses(class_, race, rng)
    trope_connections = match_tropes(pack.tropes, archetype, culture)

    pt = find_power_tier(pack.power_tiers, class_, level)
    tier_label = pt.label if pt is not None else f"tier-{tier}"

    visual_prompt = build_visual_prompt(pack, class_, level, archetype, args.context)

    return EnemyBlock(
        name=name,
        class_=class_,
        race=race,
        level=level,
        tier_label=tier_label,
        role=role,
        hp=hp,
        abilities=abilities,
        weaknesses=weaknesses,
        disposition=min(archetype.disposition_default, -10),  # enemies skew hostile
        personality=list(archetype.personality_traits),
        dialogue_quirks=list(archetype.dialogue_quirks),
        inventory=list(archetype.inventory_hints),
        stat_scores=stat_scores,
        ocean=ocean,
        ocean_summary=ocean_summary,
        trope_connections=trope_connections,
        visual_prompt=visual_prompt,
    )


def _generate_name(
    culture: Culture,
    corpus_dir: Path,
    rng: random.Random,
    fallback_dirs: list[Path] | None = None,
) -> str:
    generator = build_from_culture(culture, corpus_dir, rng, fallback_dirs=fallback_dirs)
    for _ in range(10):
        candidate = generator.generate_person()
        if not candidate:
            continue
        lower = candidate.lower()
        if lower.startswith("of ") or lower.startswith("the "):
            continue
        return candidate
    return generator.generate_person()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _emit(enemies: list[EnemyBlock]) -> int:
    """Print the encounter JSON to stdout + write the sidecar. Always 0."""
    block = EncounterBlock(enemies=enemies)
    out = {"enemies": [_enemy_block_to_dict(e) for e in block.enemies]}
    print(json.dumps(out, indent=2))
    write_sidecar(block)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    genre_dir = Path(args.genre_packs_path) / args.genre

    try:
        pack = load_genre_pack(genre_dir)
    except Exception as e:  # noqa: BLE001 — surface load errors to stderr like Rust
        print(f"Error loading genre pack: {e}", file=sys.stderr)
        return 1

    rng = random.Random()
    enemies: list[EnemyBlock] = []

    # World-level creatures.yaml → sample from bestiary
    creatures: list[dict[str, Any]] = []
    if args.world:
        creatures_path = genre_dir / "worlds" / args.world / "creatures.yaml"
        creatures = _collect_creatures_from_yaml(creatures_path)

    if creatures:
        if args.tier is not None:
            filtered = [c for c in creatures if c.get("threat_level") == args.tier]
            pool = filtered if filtered else creatures
        else:
            pool = creatures

        for _ in range(args.count):
            creature = rng.choice(pool)
            enemies.append(creature_to_enemy_block(creature, rng))

        return _emit(enemies)

    # Ruleset-module packs (ADR-117) deliberately drop allowed_classes —
    # enemies come from the pack-root bestiary instead (story 90-1). Fail
    # loud when the bestiary is absent: silently seeding an empty Monster
    # Manual pool was the 87-4 bug this branch retires.
    if pack.rules.ruleset != "native":
        if pack.bestiary is None:
            print(
                f"sidequest-encountergen: genre '{args.genre}' binds ruleset "
                f"'{pack.rules.ruleset}' but ships no bestiary.yaml at the pack "
                "root — ruleset-module packs REQUIRE a bestiary (90-1 fail-loud "
                "contract; author SRD-aligned combat stat blocks)",
                file=sys.stderr,
            )
            return 1
        for _ in range(args.count):
            enemies.append(generate_enemy_from_bestiary(pack, args, rng))
        return _emit(enemies)

    # Native packs: humanoid NPCs from rules.yaml allowed_classes
    for _ in range(args.count):
        enemies.append(generate_enemy(pack, genre_dir, args, rng))

    return _emit(enemies)


if __name__ == "__main__":
    sys.exit(main())
