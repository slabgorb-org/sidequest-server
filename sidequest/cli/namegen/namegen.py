"""NPC identity generator.

Generates a complete NPC block from genre pack data: culture-appropriate name
(Markov chains + mad-libs patterns), archetype personality, OCEAN profile,
dialogue quirks, inventory hints, and trope connections.

Called by the narrator agent when introducing new NPCs.

Usage:
  python -m sidequest.cli.namegen --genre-packs-path ./genre_packs --genre mutant_wasteland
  python -m sidequest.cli.namegen --genre-packs-path ./genre_packs --genre mutant_wasteland --culture Scrapborn --gender female
  python -m sidequest.cli.namegen --genre-packs-path ./genre_packs --genre mutant_wasteland --archetype "Wasteland Trader" --role mechanic
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sidequest.genre import (
    Culture,
    GenrePack,
    NpcArchetype,
    TropeDefinition,
    load_genre_pack,
)
from sidequest.genre.archetype import ResolutionSource, resolve_archetype
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.archetype_funnels import ArchetypeFunnels
from sidequest.genre.models.character import spawnable_archetypes
from sidequest.genre.models.npc_traits import NpcTrait
from sidequest.genre.names import build_from_culture
from sidequest.genre.names.generator import has_stem_collision
from sidequest.telemetry.spans import (
    SPAN_NAMEGEN_FAIL_LOUD,
    SPAN_NAMEGEN_STEM_COLLISION,
    Span,
)

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass
class OceanValues:
    openness: float
    conscientiousness: float
    extraversion: float
    agreeableness: float
    neuroticism: float


@dataclass
class TropeConnection:
    trope: str
    category: str
    connection: str


@dataclass
class NpcBlock:
    name: str
    pronouns: str
    gender: str
    culture: str
    faction: str
    faction_description: str
    archetype: str
    role: str
    appearance: str
    personality: list[str]
    dialogue_quirks: list[str]
    history: str
    ocean: OceanValues
    ocean_summary: str
    disposition: int
    inventory: list[str]
    stat_ranges: dict[str, list[int]]
    trope_connections: list[TropeConnection]
    jungian_id: str
    rpg_role_id: str
    npc_role_id: str | None
    resolved_archetype: str
    resolution_source: str
    spawn_quirks: list[str]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidequest-namegen",
        description="Generate a complete NPC identity from genre pack data",
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
    p.add_argument("--culture", help="Culture name (e.g., Scrapborn). Random if omitted.")
    p.add_argument(
        "--archetype", help='Archetype name (e.g., "Wasteland Trader"). Random if omitted.'
    )
    p.add_argument("--gender", help="Gender: male, female, nonbinary. Random if omitted.")
    p.add_argument("--role", help="Role override. Defaults to archetype name.")
    p.add_argument("--description", help="Physical description hints to layer on top of archetype.")
    p.add_argument(
        "--jungian", help="Jungian archetype axis (e.g. sage, hero, outlaw). Random if omitted."
    )
    p.add_argument(
        "--rpg-role",
        dest="rpg_role",
        help="RPG role axis (e.g. healer, tank, stealth). Random if omitted.",
    )
    p.add_argument(
        "--npc-role",
        dest="npc_role",
        help="NPC narrative role (e.g. mentor, mook). Random if omitted.",
    )
    p.add_argument(
        "--world", help="World slug for funnel resolution. If omitted, skips world funnels."
    )
    return p


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def write_sidecar(npc: NpcBlock) -> None:
    """Write a tool call record to the sidecar JSONL file for the orchestrator."""
    dir_ = os.environ.get("SIDEQUEST_TOOL_SIDECAR_DIR")
    session_id = os.environ.get("SIDEQUEST_TOOL_SESSION_ID")
    if not dir_ or not session_id:
        return

    sidecar_path = Path(dir_) / f"sidequest-tools-{session_id}.jsonl"
    Path(dir_).mkdir(parents=True, exist_ok=True)

    record = {
        "tool": "personality_event",
        "result": {
            "npc": npc.name,
            "event_type": "introduced",
            "description": f"{npc.role} ({npc.archetype})",
        },
    }

    with sidecar_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Axis / archetype resolution
# ---------------------------------------------------------------------------


def select_weighted_pairing(
    constraints: ArchetypeConstraints, rng: random.Random
) -> tuple[str, str]:
    """Pick a [jungian, rpg_role] pair using weighted randomness (60/30/10)."""
    roll = rng.random()
    if roll < 0.6:
        pool = constraints.valid_pairings.common
        fallbacks = [
            constraints.valid_pairings.uncommon,
            constraints.valid_pairings.rare,
        ]
    elif roll < 0.9:
        pool = constraints.valid_pairings.uncommon
        fallbacks = [
            constraints.valid_pairings.common,
            constraints.valid_pairings.rare,
        ]
    else:
        pool = constraints.valid_pairings.rare
        fallbacks = [
            constraints.valid_pairings.common,
            constraints.valid_pairings.uncommon,
        ]

    chosen_pool = pool if pool else next((p for p in fallbacks if p), None)
    if not chosen_pool:
        print("No valid pairings found in archetype_constraints", file=sys.stderr)
        sys.exit(1)

    pair = rng.choice(chosen_pool)
    return pair[0], pair[1]


def resolve_axes(
    pack: GenrePack, args: argparse.Namespace, rng: random.Random
) -> tuple[str, str, str | None, str, str]:
    """Resolve three-axis archetype selection through the constraint/funnel pipeline.

    Returns (jungian_id, rpg_role_id, npc_role_id, resolved_name, resolution_source).
    """
    base = pack.base_archetypes
    constraints: ArchetypeConstraints | None = pack.archetype_constraints
    if base is None or constraints is None:
        return legacy_axis_fallback(pack, args, rng)

    if args.jungian and args.rpg_role:
        jungian_id, rpg_role_id = args.jungian, args.rpg_role
    elif args.jungian:
        candidates = [
            p
            for p in (
                list(constraints.valid_pairings.common)
                + list(constraints.valid_pairings.uncommon)
                + list(constraints.valid_pairings.rare)
            )
            if p[0] == args.jungian
        ]
        if not candidates:
            print(f"No valid RPG role pairings found for jungian '{args.jungian}'", file=sys.stderr)
            sys.exit(1)
        pair = rng.choice(candidates)
        jungian_id, rpg_role_id = args.jungian, pair[1]
    elif args.rpg_role:
        candidates = [
            p
            for p in (
                list(constraints.valid_pairings.common)
                + list(constraints.valid_pairings.uncommon)
                + list(constraints.valid_pairings.rare)
            )
            if p[1] == args.rpg_role
        ]
        if not candidates:
            print(
                f"No valid Jungian pairings found for rpg_role '{args.rpg_role}'", file=sys.stderr
            )
            sys.exit(1)
        pair = rng.choice(candidates)
        jungian_id, rpg_role_id = pair[0], args.rpg_role
    else:
        jungian_id, rpg_role_id = select_weighted_pairing(constraints, rng)

    if args.npc_role:
        npc_role_id: str | None = args.npc_role
    elif constraints.npc_roles_available:
        npc_role_id = rng.choice(constraints.npc_roles_available)
    else:
        npc_role_id = None

    funnels: ArchetypeFunnels | None = None
    if args.world:
        world = pack.worlds.get(args.world)
        if world is not None:
            funnels = world.archetype_funnels

    try:
        result = resolve_archetype(
            jungian_id,
            rpg_role_id,
            base,
            constraints,
            funnels,
            args.genre,
            args.world,
        )
    except Exception as e:
        print(f"Archetype resolution failed: {e}", file=sys.stderr)
        sys.exit(1)

    return jungian_id, rpg_role_id, npc_role_id, result.resolved.name, result.source.value


def legacy_axis_fallback(
    pack: GenrePack, args: argparse.Namespace, rng: random.Random
) -> tuple[str, str, str | None, str, str]:
    """Populate axis fields from the old-style archetype selection."""
    # World-over-genre resolution (the SAME rule the main path uses at
    # generate_npc_block): a world that declares its own archetypes REPLACES
    # the genre set. Reading ``pack.archetypes`` raw here validated explicit
    # ``--archetype`` requests and random spawns against the GENRE tier even
    # when ``--world`` was passed — a tier mismatch with every other archetype
    # consumer (playtest 2026-06-07, blackthorn_moor).
    effective, _source = pack.effective_archetypes(args.world)
    if args.archetype:
        archetype = next(
            (a for a in effective if a.name.lower() == args.archetype.lower()),
            None,
        )
        if archetype is None:
            available = ", ".join(a.name for a in effective)
            print(
                f"Archetype '{args.archetype}' not found. Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        spawnable = spawnable_archetypes(effective)
        if not spawnable:
            world_clause = f" world '{args.world}'" if args.world else ""
            print(
                f"sidequest-namegen: no spawnable archetypes for genre '{args.genre}'"
                f"{world_clause} — all archetypes are named_individual (specific people)",
                file=sys.stderr,
            )
            sys.exit(1)
        archetype = rng.choice(spawnable)

    return (
        args.jungian or "",
        args.rpg_role or "",
        args.npc_role,
        archetype.name,
        ResolutionSource.genre_fallback.value,
    )


# ---------------------------------------------------------------------------
# Quirk selection
# ---------------------------------------------------------------------------


def select_quirk(traits: list[NpcTrait], jungian_id: str | None, rng: random.Random) -> str | None:
    """Weighted random selection from a trait pool.

    Traits with `jungian_affinity` matching the NPC's jungian_id get 3x weight.
    """
    if not traits:
        return None

    weights = [3.0 if jungian_id and jungian_id in t.jungian_affinity else 1.0 for t in traits]
    total = sum(weights)
    roll = rng.random() * total
    for t, w in zip(traits, weights, strict=True):
        roll -= w
        if roll <= 0.0:
            return t.trait_name
    return traits[-1].trait_name


def select_quirk_subset(quirks: list[str], count: int, rng: random.Random) -> list[str]:
    """Return a random subset of `count` quirks (or all of them, shuffled, if fewer)."""
    pool = list(quirks)
    rng.shuffle(pool)
    return pool[:count]


# ---------------------------------------------------------------------------
# OCEAN
# ---------------------------------------------------------------------------


def jitter_ocean_from_axes(
    jungian_id: str,
    pack: GenrePack,
    fallback_archetype: NpcArchetype,
    rng: random.Random,
) -> OceanValues:
    """Jitter OCEAN using the Jungian archetype's range-based tendencies."""
    tendencies = None
    base = pack.base_archetypes
    if base is not None:
        jungian = next((j for j in base.jungian if j.id == jungian_id), None)
        if jungian is not None:
            tendencies = jungian.ocean_tendencies

    if tendencies is None:
        return jitter_ocean(fallback_archetype, rng)

    def sample(rng_range: list[float]) -> float:
        base_v = rng.uniform(rng_range[0], rng_range[1])
        jitter = rng.uniform(-0.5, 0.5)
        return round(max(0.0, min(10.0, base_v + jitter)) * 10.0) / 10.0

    return OceanValues(
        openness=sample(tendencies.openness),
        conscientiousness=sample(tendencies.conscientiousness),
        extraversion=sample(tendencies.extraversion),
        agreeableness=sample(tendencies.agreeableness),
        neuroticism=sample(tendencies.neuroticism),
    )


def jitter_ocean(archetype: NpcArchetype, rng: random.Random) -> OceanValues:
    o = archetype.ocean
    if o is not None:
        base = (o.openness, o.conscientiousness, o.extraversion, o.agreeableness, o.neuroticism)
    else:
        base = (5.0, 5.0, 5.0, 5.0, 5.0)

    def j(v: float) -> float:
        jitter = rng.uniform(-1.5, 1.5)
        return round(max(0.0, min(10.0, v + jitter)) * 10.0) / 10.0

    return OceanValues(
        openness=j(base[0]),
        conscientiousness=j(base[1]),
        extraversion=j(base[2]),
        agreeableness=j(base[3]),
        neuroticism=j(base[4]),
    )


def summarize_ocean(o: OceanValues) -> str:
    def label(v: float, low: str, mid: str, high: str) -> str:
        if v < 4.0:
            return low
        if v > 7.0:
            return high
        return mid

    parts = [
        label(
            o.openness,
            "conventional and practical",
            "balanced between tradition and novelty",
            "curious and imaginative",
        ),
        label(
            o.conscientiousness,
            "spontaneous and flexible",
            "moderately organized",
            "meticulous and disciplined",
        ),
        label(o.extraversion, "reserved and quiet", "selectively social", "outgoing and talkative"),
        label(o.agreeableness, "blunt and competitive", "pragmatic", "warm and cooperative"),
        label(
            o.neuroticism,
            "emotionally steady",
            "occasionally anxious",
            "easily stressed and reactive",
        ),
    ]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


HISTORY_TEMPLATES = [
    "Once served as a {role} in {faction} territory before {event}.",
    "Grew up in the {faction} settlements. Left after {event}.",
    "Claims to have been {role} for years, but something about the story doesn't add up.",
    "Arrived from the wastes with nothing. Earned {faction} trust through {deed}.",
    "Former {alt_role} who switched trades after {event}.",
    "Born into {faction} culture. Never left the region. Knows every path and every grudge.",
]

HISTORY_EVENTS = [
    "a bad trade went wrong",
    "their settlement was raided",
    "a mutation changed everything",
    "a drought drove them out",
    "they found something in the ruins they won't talk about",
    "a feud with another faction",
    "the water turned bad",
    "they lost someone important",
    "an Ancient device activated nearby",
    "a pack of beasts destroyed their homestead",
]

HISTORY_DEEDS = [
    "hard work and silence",
    "a timely warning about raiders",
    "fixing something nobody else could",
    "sharing water during the drought",
    "standing their ground when it mattered",
    "knowing where the good salvage was",
    "patching up the wounded after the last raid",
]


def generate_history(faction: str, role: str, archetype: NpcArchetype, rng: random.Random) -> str:
    template = rng.choice(HISTORY_TEMPLATES)
    event = rng.choice(HISTORY_EVENTS)
    deed = rng.choice(HISTORY_DEEDS)
    alt_role = archetype.typical_classes[0].lower() if archetype.typical_classes else "drifter"
    return (
        template.replace("{role}", role)
        .replace("{faction}", faction)
        .replace("{event}", event)
        .replace("{deed}", deed)
        .replace("{alt_role}", alt_role)
    )


# ---------------------------------------------------------------------------
# Trope matching
# ---------------------------------------------------------------------------


def match_tropes(
    tropes: list[TropeDefinition], archetype: NpcArchetype, culture: Culture
) -> list[TropeConnection]:
    npc_tags: set[str] = set()
    for cls in archetype.typical_classes:
        npc_tags.add(cls.lower())
    for trait in archetype.personality_traits:
        npc_tags.add(trait.lower())
    npc_tags.add(culture.name.lower())
    for word in archetype.name.lower().split():
        npc_tags.add(word)

    out: list[TropeConnection] = []
    for trope in tropes:
        trope_tags = {t.lower() for t in trope.tags}
        overlap = sorted(npc_tags & trope_tags)
        if overlap:
            out.append(
                TropeConnection(
                    trope=trope.name,
                    category=trope.category,
                    connection=f"linked via: {', '.join(overlap)}",
                )
            )
    return out


# ---------------------------------------------------------------------------
# NPC generation
# ---------------------------------------------------------------------------


def _empty_pool_message(kind: str, file_basename: str, genre: str, world: str | None) -> str:
    world_clause = f" world '{world}'" if world else ""
    world_path_hint = (
        f" and, if --world is passed, genre_packs/{genre}/worlds/<slug>/{file_basename}."
        if not world
        else f" Check genre_packs/{genre}/worlds/{world}/{file_basename}."
    )
    return (
        f"sidequest-namegen: no {kind} available for genre '{genre}'{world_clause} — "
        f"{file_basename} is empty at both genre and world tiers. "
        f"Check genre_packs/{genre}/{file_basename}.{world_path_hint}"
    )


def generate_npc(
    pack: GenrePack,
    genre_dir: Path,
    args: argparse.Namespace,
    rng: random.Random,
) -> NpcBlock:
    corpus_dir = genre_dir / "corpus"
    corpus_fallbacks = [genre_dir.parent.parent / "corpus" / "shared"]

    # Shared world-over-genre resolution (GenrePack.effective_*). The SAME
    # helper Monster-Manual seeding (pregen.seed_manual) uses, so a seeded NPC's
    # culture tag always resolves against the set the name generator validates
    # against — preventing the perseus_cloud divergence (session 894) from
    # recurring if either call site is edited.
    effective_cultures, cultures_source = pack.effective_cultures(args.world)
    effective_archetypes, archetypes_source = pack.effective_archetypes(args.world)

    if not effective_cultures:
        print(
            _empty_pool_message("cultures", "cultures.yaml", args.genre, args.world),
            file=sys.stderr,
        )
        sys.exit(2)
    if not effective_archetypes:
        print(
            _empty_pool_message("archetypes", "archetypes.yaml", args.genre, args.world),
            file=sys.stderr,
        )
        sys.exit(2)

    print(
        f"namegen.tier_sources cultures={cultures_source} "
        f"archetypes={archetypes_source} (world={args.world or '<none>'})",
        file=sys.stderr,
    )

    if args.culture:
        culture = next(
            (c for c in effective_cultures if c.name.lower() == args.culture.lower()),
            None,
        )
        if culture is None:
            available = ", ".join(c.name for c in effective_cultures)
            print(
                f"Culture '{args.culture}' not found. Available: {available}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        culture = rng.choice(effective_cultures)

    jungian_id, rpg_role_id, npc_role_id, resolved_archetype, resolution_source = resolve_axes(
        pack, args, rng
    )

    archetype = next(
        (a for a in effective_archetypes if a.name.lower() == resolved_archetype.lower()),
        None,
    )
    if archetype is None and args.archetype:
        archetype = next(
            (a for a in effective_archetypes if a.name.lower() == args.archetype.lower()),
            None,
        )
    if archetype is None:
        spawnable = spawnable_archetypes(effective_archetypes)
        if not spawnable:
            print(
                _empty_pool_message(
                    "spawnable archetypes", "archetypes.yaml", args.genre, args.world
                ),
                file=sys.stderr,
            )
            sys.exit(2)
        archetype = rng.choice(spawnable)

    generator = build_from_culture(culture, corpus_dir, rng, fallback_dirs=corpus_fallbacks)
    name = ""
    stem_collision_count = 0
    for attempt_index in range(10):
        candidate = generator.generate_person()
        lower = candidate.lower()
        if not candidate or lower.startswith("of ") or lower.startswith("the "):
            continue
        if has_stem_collision(candidate):
            tokens = candidate.split()
            with Span.open(
                SPAN_NAMEGEN_STEM_COLLISION,
                {
                    "culture": culture.name,
                    "candidate": candidate,
                    "prefix_stem": tokens[0] if tokens else "",
                    "suffix_stem": tokens[-1] if len(tokens) > 1 else "",
                    "attempt_index": attempt_index,
                },
            ):
                pass
            stem_collision_count += 1
            continue
        name = candidate
        break
    if not name:
        # Stem-collision exhaustion (Story 45-28): fail loud rather than
        # fall back to a degenerate name. The original "Frandrew Andrew"
        # bug came from exactly this fallback silently winning.
        if stem_collision_count > 0:
            with Span.open(
                SPAN_NAMEGEN_FAIL_LOUD,
                {
                    "culture": culture.name,
                    "reason": "stem_collision_exhausted",
                },
            ):
                pass
            raise ValueError(
                f"namegen exhausted 10 attempts for culture '{culture.name}': "
                f"every generated name exhibited stem-collision artifacts. "
                f"Corpus may be too thin or too uniform."
            )
        # of/the exhaustion (pre-existing behaviour preserved — out of
        # scope for 45-28; if this becomes a real failure mode, surface
        # via a separate story rather than scope-creeping this one).
        name = generator.generate_person()

    gender = args.gender or rng.choice(["male", "female", "nonbinary"])
    pronouns = {"male": "he/him", "female": "she/her"}.get(gender, "they/them")

    if jungian_id:
        ocean = jitter_ocean_from_axes(jungian_id, pack, archetype, rng)
    else:
        ocean = jitter_ocean(archetype, rng)
    ocean_summary = summarize_ocean(ocean)

    role = args.role or archetype.name.lower()

    appearance = ""
    if args.description:
        appearance = f"{args.description}. "
    appearance += archetype.description

    history = generate_history(culture.name, role, archetype, rng)

    trope_connections = match_tropes(pack.tropes, archetype, culture)

    dialogue_quirks = select_quirk_subset(archetype.dialogue_quirks, 3, rng)

    spawn_quirks: list[str] = []
    db = pack.npc_traits
    if db is not None:
        jungian_ref = jungian_id if jungian_id else None
        q = select_quirk(db.personality, jungian_ref, rng)
        if q:
            spawn_quirks.append(q)
        pool = db.physical if rng.random() < 0.5 else db.behavioral
        q = select_quirk(pool, None, rng)
        if q:
            spawn_quirks.append(q)

    return NpcBlock(
        name=name,
        pronouns=pronouns,
        gender=gender,
        culture=culture.name,
        faction=culture.name,
        faction_description=culture.description,
        archetype=resolved_archetype,
        role=role,
        appearance=appearance,
        personality=list(archetype.personality_traits),
        dialogue_quirks=dialogue_quirks,
        history=history,
        ocean=ocean,
        ocean_summary=ocean_summary,
        disposition=archetype.disposition_default,
        inventory=list(archetype.inventory_hints),
        stat_ranges=dict(archetype.stat_ranges),
        trope_connections=trope_connections,
        jungian_id=jungian_id,
        rpg_role_id=rpg_role_id,
        npc_role_id=npc_role_id,
        resolved_archetype=resolved_archetype,
        resolution_source=resolution_source,
        spawn_quirks=spawn_quirks,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    genre_dir = args.genre_packs_path / args.genre
    try:
        pack = load_genre_pack(genre_dir)
    except Exception as e:
        print(f"Error loading genre pack: {e}", file=sys.stderr)
        return 1

    rng = random.Random()
    npc = generate_npc(pack, genre_dir, args, rng)

    print(json.dumps(asdict(npc), indent=2))
    write_sidecar(npc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
