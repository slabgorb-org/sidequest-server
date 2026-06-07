"""Server-side pre-generation for the Monster Manual (ADR-059).

Invokes the ``namegen`` and ``encountergen`` CLIs to populate a
:class:`MonsterManual` with NPC and encounter blocks. Same invocation
pattern as the Rust era ``dispatch::pregen`` module — but in-process
via :func:`sidequest.cli.namegen.main` and
:func:`sidequest.cli.encountergen.main` rather than subprocess fork-exec.

Translation delta from ``crates/sidequest-server/src/dispatch/pregen.rs``:

* Rust shelled out to compiled binaries discovered via
  ``AppState::namegen_binary_path`` / ``encountergen_binary_path``. The
  Python equivalents are modules in the same package, so we capture
  stdout in-process. The sidecar JSONL contract (env vars
  ``SIDEQUEST_TOOL_SIDECAR_DIR`` and ``SIDEQUEST_TOOL_SESSION_ID``) is
  unaffected — both CLI mains honour them whether invoked in-process or
  as a subprocess.
* Rust selected ``rand::rng()``; Python uses a local ``random.Random``
  so the seeding pass is hermetic if a future caller injects a seed.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import random
from pathlib import Path
from typing import TYPE_CHECKING

from sidequest.cli.encountergen.encountergen import main as encountergen_main
from sidequest.cli.namegen.namegen import main as namegen_main
from sidequest.genre import load_genre_pack
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.character import spawnable_archetypes
from sidequest.telemetry.spans.pregen import SPAN_PREGEN_SEED_MANUAL
from sidequest.telemetry.spans.span import Span

if TYPE_CHECKING:
    from sidequest.game.monster_manual import MonsterManual

logger = logging.getLogger(__name__)


class EncounterSeedError(RuntimeError):
    """Encounter seeding failed for a ruleset-module pack (story 90-1).

    A ``ruleset: wwn|cwn|swn|awn`` pack must seed its Monster Manual
    encounters pool from the pack bestiary or fail LOUD — the old
    warning-only skip shipped worlds with silently-empty pools (87-4:
    evropi 49 NPCs / 0 encounters). Native packs keep the warning-only
    behavior (their generation path is unchanged by 90-1).
    """


NPCS_PER_CULTURE = 3
"""How many NPCs to generate per culture during seeding (Rust parity)."""

DEFAULT_NPC_FALLBACK_COUNT = NPCS_PER_CULTURE * 3
"""When a pack has no cultures, generate this many faction-less NPCs."""

ENCOUNTER_TIERS = (1, 2)
"""Tiers to pre-generate (Rust parity: tier 1 + tier 2, 2 enemies each)."""

ENCOUNTERS_PER_TIER = 2


def _select_diverse_pairings(
    constraints: ArchetypeConstraints,
    count: int,
    rng: random.Random,
) -> list[tuple[str, str, str]]:
    """Pick ``count`` diverse ``(jungian, rpg_role, npc_role)`` triples.

    Weighting matches the Rust version: 60% common, 30% uncommon, 10% rare.
    """
    common_count = math.ceil(count * 0.6)
    uncommon_count = math.ceil(count * 0.3)
    rare_count = max(0, count - common_count - uncommon_count)

    def sample(pool: list[list[str]], n: int) -> list[tuple[str, str]]:
        if not pool:
            return []
        return [
            (pool[rng.randrange(len(pool))][0], pool[rng.randrange(len(pool))][1]) for _ in range(n)
        ]

    pairs: list[tuple[str, str]] = []
    pairs.extend(sample(constraints.valid_pairings.common, common_count))
    pairs.extend(sample(constraints.valid_pairings.uncommon, uncommon_count))
    pairs.extend(sample(constraints.valid_pairings.rare, rare_count))

    npc_roles = constraints.npc_roles_available
    if not npc_roles:
        return [(j, r, "") for j, r in pairs]
    return [(j, r, npc_roles[i % len(npc_roles)]) for i, (j, r) in enumerate(pairs)]


def _run_cli_capturing_json(
    cli_main: object,
    argv: list[str],
    *,
    label: str,
) -> dict[str, object] | None:
    """Invoke a CLI main(argv) in-process and parse its stdout as JSON.

    Returns ``None`` (with a warning logged) on non-zero exit, JSON parse
    failure, or unhandled exception. Mirrors the Rust ``output.status.success()``
    + ``serde_json::from_slice`` pattern.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cli_main(argv)  # type: ignore[operator]
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # noqa: BLE001 — surface unexpected failures to the log
        logger.warning("pregen.%s_failed (error=%s)", label, e)
        return None
    if rc != 0:
        logger.warning("pregen.%s_failed (exit_code=%s)", label, rc)
        return None
    raw = buf.getvalue()
    if not raw.strip():
        logger.warning("pregen.%s_empty_output", label)
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("pregen.%s_invalid_json (error=%s)", label, e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("pregen.%s_invalid_shape (type=%s)", label, type(parsed).__name__)
        return None
    return parsed


def _generate_npc(
    genre_packs_path: Path,
    genre: str,
    *,
    culture: str | None,
    axes: tuple[str, str, str] | None,
    world: str | None,
) -> dict[str, object] | None:
    """Invoke sidequest-namegen and return parsed JSON, or None on failure."""
    argv: list[str] = [
        "--genre-packs-path",
        str(genre_packs_path),
        "--genre",
        genre,
    ]
    if culture:
        argv += ["--culture", culture]
    if axes is not None:
        jungian, rpg_role, npc_role = axes
        if jungian:
            argv += ["--jungian", jungian]
        if rpg_role:
            argv += ["--rpg-role", rpg_role]
        if npc_role:
            argv += ["--npc-role", npc_role]
    if world:
        argv += ["--world", world]

    return _run_cli_capturing_json(namegen_main, argv, label="namegen")


def _generate_encounter(
    genre_packs_path: Path,
    genre: str,
    world: str,
    *,
    tier: int | None,
    count: int,
) -> dict[str, object] | None:
    """Invoke sidequest-encountergen and return parsed JSON, or None on failure."""
    argv: list[str] = [
        "--genre-packs-path",
        str(genre_packs_path),
        "--genre",
        genre,
        "--count",
        str(count),
    ]
    if world:
        argv += ["--world", world]
    if tier is not None:
        argv += ["--tier", str(tier)]

    return _run_cli_capturing_json(encountergen_main, argv, label="encountergen")


def seed_manual(
    *,
    genre_packs_path: Path,
    genre: str,
    world: str,
    manual: MonsterManual,
    rng: random.Random | None = None,
) -> None:
    """Seed a :class:`MonsterManual` with NPCs and encounters from the tool CLIs.

    Examines the genre pack's cultures and generates 3 NPCs per culture for
    **all** of the world's declared cultures (no cap — story 72-11; a world
    declaring N cultures seeds N × 3 NPCs). Generates 2 encounter blocks at
    tier 1 and tier 2. When ``world`` is set, encountergen reads
    ``worlds/{world}/creatures.yaml`` for creature definitions; otherwise
    falls back to humanoid NPCs from rules.yaml.
    """
    rng = rng if rng is not None else random.Random()

    npcs_before = len(manual.npcs)
    encounters_before = len(manual.encounters)

    # ── Load pack for cultures + constraints ──────────────────
    genre_dir = genre_packs_path / genre
    try:
        pack = load_genre_pack(genre_dir)
    except Exception as e:  # noqa: BLE001 — falls back to no-culture branch on failure
        logger.warning("pregen.pack_load_failed (genre=%s, error=%s)", genre, e)
        pack = None

    cultures: list[str] = []
    cultures_source = "none"
    effective_culture_count = 0
    constraints: ArchetypeConstraints | None = None
    if pack is not None:
        # World-over-genre resolution (the SAME rule namegen uses): a world
        # that declares its own cultures REPLACES the genre set. Reading
        # ``pack.cultures`` raw here handed genre culture names to a name
        # generator that validates against the WORLD set, so perseus_cloud
        # seeding failed every time and seeded 0 NPCs (session 894).
        effective, cultures_source = pack.effective_cultures(world)
        # Seed ALL the world's cultures — no cap. The world author's declared
        # culture list is the bound (72-11): the old ``[:MAX_CULTURES]`` (=4)
        # slice silently dropped any culture past the fourth, so coyote_star's
        # fifth culture (voidborn) never seeded an NPC.
        effective_culture_count = len(effective)
        cultures = [c.name for c in effective]
        constraints = pack.archetype_constraints

    # Roster-only worlds (playtest 2026-06-07, blackthorn_moor): a world
    # whose effective archetype pool contains ONLY ``named_individual``
    # templates (a murder-mystery cast of specific people) has nothing
    # namegen may random-mint — every invocation would fail with
    # "no spawnable archetypes" (exit 1), once per NPC slot (9× WARN spam).
    # Detect the empty mint pool ONCE here and skip namegen cleanly,
    # mirroring the ``pregen.encounters_skipped`` gate below. The world
    # survives on its authored roster NPCs by design.
    no_spawnable_archetypes = False
    if pack is not None:
        effective_archetypes, _archetypes_source = pack.effective_archetypes(world)
        no_spawnable_archetypes = not spawnable_archetypes(effective_archetypes)

    # ── NPCs: 3 per culture (Rust parity) ─────────────────────
    npc_count = len(cultures) * NPCS_PER_CULTURE if cultures else DEFAULT_NPC_FALLBACK_COUNT
    pairings: list[tuple[str, str, str]] | None = (
        _select_diverse_pairings(constraints, npc_count, rng) if constraints is not None else None
    )
    world_opt = world if world else None

    if no_spawnable_archetypes:
        logger.info(
            "pregen.namegen_skipped (genre=%s, world=%s, reason=no_spawnable_archetypes)",
            genre,
            world,
        )
    elif not cultures:
        for i in range(npc_count):
            axes = pairings[i] if pairings is not None and i < len(pairings) else None
            data = _generate_npc(
                genre_packs_path,
                genre,
                culture=None,
                axes=axes,
                world=world_opt,
            )
            if data is not None:
                logger.info(
                    "pregen.npc_generated (name=%s, jungian=%s, rpg_role=%s, npc_role=%s)",
                    data.get("name") or "?",
                    (axes[0] if axes else ""),
                    (axes[1] if axes else ""),
                    (axes[2] if axes else ""),
                )
                manual.add_npc(data, [])
    else:
        for ci, culture in enumerate(cultures):
            for j in range(NPCS_PER_CULTURE):
                idx = ci * NPCS_PER_CULTURE + j
                axes = pairings[idx] if pairings is not None and idx < len(pairings) else None
                data = _generate_npc(
                    genre_packs_path,
                    genre,
                    culture=culture,
                    axes=axes,
                    world=world_opt,
                )
                if data is not None:
                    logger.info(
                        "pregen.npc_generated (name=%s, culture=%s, jungian=%s, rpg_role=%s, npc_role=%s)",
                        data.get("name") or "?",
                        culture,
                        (axes[0] if axes else ""),
                        (axes[1] if axes else ""),
                        (axes[2] if axes else ""),
                    )
                    manual.add_npc(data, [])

    # ── Encounters: tier 1 + tier 2 ───────────────────────────
    # Social, Composure-only packs (combat_encounters=False) have no combat —
    # skip encounter generation entirely so the Manual never holds B/X-style
    # combat enemies to inject (playtest 2026-06-01, blackthorn_moor). A pack
    # that failed to load defaults to combat-enabled (the model default), since
    # the no-culture fallback path is the legacy combat behavior.
    combat_encounters = getattr(getattr(pack, "rules", None), "combat_encounters", True)
    ruleset = getattr(getattr(pack, "rules", None), "ruleset", "native") if pack else "native"
    if combat_encounters:
        for tier in ENCOUNTER_TIERS:
            data = _generate_encounter(
                genre_packs_path,
                genre,
                world,
                tier=tier,
                count=ENCOUNTERS_PER_TIER,
            )
            if data is not None:
                logger.info("pregen.encounter_generated (tier=%d, ruleset=%s)", tier, ruleset)
                manual.add_encounter(data, tier, [])
            elif ruleset != "native":
                # Story 90-1: a ruleset-module pack seeds from its bestiary or
                # fails LOUD — the old warning-only skip shipped silently-empty
                # encounter pools (No Silent Fallbacks). Native packs keep the
                # legacy warning-only behavior.
                raise EncounterSeedError(
                    f"encounter seeding failed for ruleset-module pack "
                    f"'{genre}' (ruleset={ruleset}, world={world!r}, tier={tier}): "
                    "encountergen produced no output — check the pack's "
                    "bestiary.yaml (REQUIRED for ruleset-module packs) and the "
                    "pregen.encountergen_failed log line above"
                )
    else:
        logger.info(
            "pregen.encounters_skipped (genre=%s, world=%s, reason=combat_encounters=false)",
            genre,
            world,
        )

    npcs_after = len(manual.npcs)
    logger.info(
        "pregen.seed_manual_complete (npcs_before=%d, npcs_after=%d, "
        "encounters_before=%d, encounters_after=%d)",
        npcs_before,
        npcs_after,
        encounters_before,
        len(manual.encounters),
    )

    # OTEL so the GM panel can confirm Monster-Manual seeding actually fired
    # AND consulted the world layer — ``cultures_source=world`` + ``npcs_after>0``
    # is the proof the perseus_cloud seeding gap is closed (0 NPCs seeded was
    # invisible until forensics; the span makes it a first-class signal).
    with Span.open(
        SPAN_PREGEN_SEED_MANUAL,
        {
            "genre": genre,
            "world": world or "",
            # Story 90-1 (AC4): which ruleset path seeded the encounters pool —
            # ruleset-module packs draw from the pack bestiary, native packs
            # from allowed_classes. GM-panel proof the right path fired.
            "ruleset": ruleset,
            "cultures_source": cultures_source,
            # ``effective_culture_count`` is the world's TRUE culture count
            # (pre-seed); ``culture_count`` is how many were actually seeded.
            # They diverge only if a culture was dropped — so any silent
            # truncation is visible on the GM panel instead of invisible (72-11).
            "effective_culture_count": effective_culture_count,
            "culture_count": len(cultures),
            "npcs_before": npcs_before,
            "npcs_after": npcs_after,
            "encounters_after": len(manual.encounters),
            "combat_encounters": combat_encounters,
            # Roster-only worlds: namegen minting was skipped because the
            # effective archetype pool holds only named_individual templates
            # (GM-panel proof the skip gate fired instead of 9× WARN spam).
            "namegen_skipped": no_spawnable_archetypes,
        },
    ):
        pass

    manual.save()
