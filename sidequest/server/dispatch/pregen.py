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
from typing import TYPE_CHECKING, Any

from sidequest.cli.encountergen.encountergen import main as encountergen_main
from sidequest.cli.namegen.namegen import main as namegen_main
from sidequest.genre import load_genre_pack
from sidequest.genre.models.archetype_constraints import ArchetypeConstraints
from sidequest.genre.models.character import spawnable_archetypes
from sidequest.telemetry.spans.pregen import SPAN_PREGEN_SEED_MANUAL
from sidequest.telemetry.spans.span import Span

if TYPE_CHECKING:
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.genre.models.bestiary import Bestiary

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


def _encounter_factions(data: dict[str, Any], bestiary: Bestiary | None) -> list[str]:
    """Union the source bestiary entries' ``factions`` onto a seeded encounter.

    Faction/zone-scoped eligibility (epic-157, ADR-059 amendment): the encounter
    inherits the faction tag(s) of the bestiary creatures it is built from, so
    Seam 1 (:func:`monster_manual_inject._npc_patches_for_encounters`) can scope
    it to its zone.

    The join key is the enemy **name**: ``encountergen.generate_enemy_from_bestiary``
    copies ``entry.name`` verbatim into each enemy and carries no creature_id, so
    the name (case-insensitive) is the only stable link back to the
    :class:`~sidequest.genre.models.bestiary.BestiaryEntry`. A native pack (no
    bestiary) or an unmatched enemy contributes nothing → empty list → eligible
    everywhere at runtime (the permissive predicate). Returned sorted for stable
    on-disk Manual JSON.
    """
    if bestiary is None:
        return []
    by_name = {entry.name.lower(): entry for entry in bestiary.entries}
    factions: set[str] = set()
    enemies = data.get("enemies") if isinstance(data, dict) else None
    if isinstance(enemies, list):
        for enemy in enemies:
            if not isinstance(enemy, dict):
                continue
            name = enemy.get("name")
            if not isinstance(name, str):
                continue
            entry = by_name.get(name.lower())
            if entry is not None:
                factions.update(entry.factions)
    return sorted(factions)


def _seed_authored_npcs(pack: Any, world: str, manual: MonsterManual) -> int:
    """Insert/refresh the world's authored ``npcs.yaml`` cast in ``manual``.

    Reads ``pack.worlds[world].authored_npcs`` (each an
    :class:`~sidequest.genre.models.authored_npc.AuthoredNpc`) and, for each:

    - **inserts** it (via :meth:`MonsterManual.add_npc` with ``exact=True``) when
      no Manual entry has its **exact** (case-insensitive) name, carrying
      ``location_tags`` through to ``ManualNpc.location_tags`` so placement-aware
      selection (:meth:`MonsterManual.available_at_location`) can surface it at
      the right location before it has ever been narrated;
    - **upserts** its ``location_tags`` onto an exact-name match when the tag
      *set* differs — a stale on-disk Manual (the wry_whimsy/oz recurrence)
      otherwise keeps its old/empty placement forever even after the author fixes
      the roster (M4). The set comparison is order-insensitive so a reordered
      YAML is not counted as a change.

    Dedup is **exact**, not the fuzzy substring :meth:`MonsterManual.find_npc_by_name`
    used for generated walk-ons: a canonical authored "Lion" must never be
    shadowed by — nor overwrite the placement of — a pre-seeded "Cowardly Lion".

    Returns the number of entries inserted **or** tag-refreshed — i.e. how many
    times the Manual changed, so the caller knows whether to persist.

    No Silent Fallbacks (H3): ``pack`` lacking ``worlds`` (a pack that failed to
    load → ``None``, or a stub) is the one tolerated no-op — the caller already
    warned on a load failure. A ``world`` key that *is* absent from a real
    ``worlds`` mapping is a config/wiring error and logs a WARNING. The roster is
    read via direct attribute access (``world_obj.authored_npcs``) so a future
    field rename surfaces loudly instead of masking as an empty roster. The
    outcome is logged on every successful read — including a count of 0 — so an
    empty roster is distinguishable from a read that never happened.
    """
    worlds = getattr(pack, "worlds", None)
    if worlds is None:
        # Pack failed to load or is a non-pack stub — nothing to seed. The
        # seed_manual caller already logged the load failure (No Silent
        # Fallbacks is satisfied upstream); a None pack here is not an error.
        return 0
    if not world:
        logger.warning("pregen.authored_seed_skipped (reason=no_world_slug)")
        return 0
    world_obj = worlds.get(world)
    if world_obj is None:
        logger.warning(
            "pregen.authored_seed_world_not_found (world=%s, known_worlds=%s)",
            world,
            sorted(worlds.keys()),
        )
        return 0
    # Direct access (not getattr-with-default): a real World always exposes
    # ``authored_npcs`` (default_factory=list). Masking a rename behind ``or []``
    # would silently drop the whole authored cast — the exact bug class H3 fixes.
    authored = world_obj.authored_npcs
    inserted = 0
    refreshed = 0
    for npc in authored:
        new_tags = list(npc.location_tags)
        # EXACT-name dedup (not the fuzzy find_npc_by_name): a canonical authored
        # NPC must never be shadowed by — nor mutate the placement of — a
        # substring-colliding walk-on ("Lion" vs a pre-seeded "Cowardly Lion").
        # The insert uses add_npc(exact=True) so its internal guard matches this
        # lookup exactly (a fuzzy guard would silently drop the exact-miss).
        existing = manual.find_npc_by_exact_name(npc.name)
        if existing is None:
            # Build the namegen-shaped ``data`` blob ``add_npc``/``_human_patch``
            # read (name/role/culture/ocean_summary). The authored NPC's prose
            # lives in history_seeds; we pass the role through so the "Other known
            # NPCs" line reads naturally.
            data: dict[str, Any] = {
                "name": npc.name,
                "role": npc.role or "",
                "culture": "",
            }
            if npc.appearance:
                data["ocean_summary"] = npc.appearance
            # ``authored=True`` (story 162-1): the flag is what protects a named
            # cast member from the accumulation cap (an authored insert at the
            # cap evicts a generated walk-on instead of being refused) and what
            # the pool-discard span counts for the V3 forensic.
            manual.add_npc(data, new_tags, exact=True, authored=True)
            inserted += 1
        else:
            changed = False
            if not existing.authored:
                # Legacy pools predate the ``authored`` flag (162-1): an entry
                # matching the authored cast by exact name IS authored — upsert
                # the flag so cap-eviction protection and the V3 discard
                # forensic cover pre-162-1 on-disk manuals too.
                existing.authored = True
                changed = True
            if set(existing.location_tags) != set(new_tags):
                # Order-insensitive dirty check: a re-authored YAML with the same
                # tags in a different order is NOT a change — comparing lists
                # directly would fire a spurious save + OTEL backfill span every
                # load. Assign the list so the authored order is preserved on a
                # real change.
                existing.location_tags = new_tags
                changed = True
            if changed:
                refreshed += 1
    logger.info(
        "pregen.authored_npcs_seeded (world=%s, inserted=%d, refreshed=%d, total_authored=%d)",
        world,
        inserted,
        refreshed,
        len(authored),
    )
    return inserted + refreshed


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
    tier 1 and tier 2 via encountergen, whose source depends on the bound
    ruleset: a ruleset-module pack (``wwn|cwn|swn|awn``) samples its authored
    ``bestiary.yaml`` and **fails loud** (``EncounterSeedError``) if seeding
    produces nothing; a native pack reads ``worlds/{world}/creatures.yaml`` (or
    falls back to humanoid ``allowed_classes`` NPCs) and keeps the legacy
    warning-only skip. Either way the ``pregen.seed_manual`` span fires with the
    outcome — including ``seed_error`` on a fail-loud — before the raise
    propagates (story 90-5).
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

    # ── Authored roster NPCs (placement-aware) ────────────────
    # The world's authored ``npcs.yaml`` cast (canonical companions, named
    # individuals) must participate in the Monster Manual's "nearby (not yet
    # met)" surfacing — otherwise they only exist as pre-loaded ``state.npcs``
    # and the Manual surfaces generic generated walk-ons instead (wry_whimsy/oz
    # bug, 2026-06-14). Each authored NPC carries its ``location_tags`` into the
    # Manual so placement-aware selection can match it to the right location
    # BEFORE it has ever been narrated.
    # _seed_authored_npcs logs its own outcome unconditionally (H3); we keep the
    # count to surface it on the seed span below so the GM panel sees the authored
    # cast entering the pool (No Silent Fallbacks — the seeding decision is a
    # first-class span attribute, not just a log line).
    authored_seeded = _seed_authored_npcs(pack, world, manual)

    # ── Encounters: tier 1 + tier 2 ───────────────────────────
    # Social, Composure-only packs (combat_encounters=False) have no combat —
    # skip encounter generation entirely so the Manual never holds B/X-style
    # combat enemies to inject (playtest 2026-06-01, blackthorn_moor).
    # A pack that failed to load (pack is None) skips encounter seeding entirely —
    # the no-culture fallback can still mint NPCs (the comment above at line ~322:
    # "falls back to no-culture branch on failure"), but without a pack we cannot
    # know the ruleset, so we disable combat encounter seeding for this run.
    # A pack that loaded but has no ``rules`` block is a configuration error
    # and fails loud (No Silent Fallbacks principle).
    if pack is not None and getattr(pack, "rules", None) is None:
        raise ValueError(
            "pregen.seed_manual: pack/rules missing — cannot resolve ruleset. A "
            "missing ruleset is a configuration error, not a silent 'dial' default "
            "(spec 2026-06-17 §1, No Silent Fallbacks)."
        )
    combat_encounters = (
        getattr(pack.rules, "combat_encounters", True) if pack is not None else False
    )
    ruleset = pack.rules.ruleset if pack is not None else None
    # Story 90-5 (item 3): a ruleset-module seeding failure must still be
    # OTEL-visible. Capture the failure message and BREAK instead of raising
    # mid-loop — the ``pregen.seed_manual`` span below fires with this
    # ``seed_error`` attribute BEFORE the raise, so the GM panel records the
    # seeding-failure decision (the old raise-in-loop emitted no seeding span).
    seed_error: str | None = None
    # Faction/zone-scoped eligibility (epic-157): resolve the world's bestiary
    # ONCE so each seeded encounter inherits the faction tag(s) of the creatures
    # it is built from (the union; see :func:`_encounter_factions`). None for a
    # pack that failed to load → encounters seed untagged (eligible everywhere).
    bestiary: Bestiary | None = None
    if combat_encounters and pack is not None:
        bestiary, _bestiary_source = pack.effective_bestiary(world)
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
                factions = _encounter_factions(data, bestiary)
                logger.info(
                    "pregen.encounter_generated (tier=%d, ruleset=%s, factions=%s)",
                    tier,
                    ruleset,
                    factions,
                )
                manual.add_encounter(data, tier, [], factions=factions)
            elif ruleset != "dial":
                # Story 90-1: a ruleset-module pack seeds from its bestiary or
                # fails LOUD — the old warning-only skip shipped silently-empty
                # encounter pools (No Silent Fallbacks). Native packs keep the
                # legacy warning-only behavior.
                seed_error = (
                    f"encounter seeding failed for ruleset-module pack "
                    f"'{genre}' (ruleset={ruleset}, world={world!r}, tier={tier}): "
                    "encountergen produced no output — check the pack's "
                    "bestiary.yaml (REQUIRED for ruleset-module packs) and the "
                    "pregen.encountergen_failed log line above"
                )
                break
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
            # Authored roster cast inserted/refreshed this seed (wry_whimsy/oz
            # fix): GM-panel proof the world's npcs.yaml companions entered the
            # Manual pool, not just the random-minted walk-ons.
            "authored_npcs_seeded": authored_seeded,
            "encounters_after": len(manual.encounters),
            "combat_encounters": combat_encounters,
            # Roster-only worlds: namegen minting was skipped because the
            # effective archetype pool holds only named_individual templates
            # (GM-panel proof the skip gate fired instead of 9× WARN spam).
            "namegen_skipped": no_spawnable_archetypes,
            # Story 90-5 (item 3): empty string on success, the loud failure
            # message on a ruleset-module seeding failure — the GM-panel signal
            # that distinguishes a healthy seed from a fail-loud one.
            "seed_error": seed_error or "",
        },
    ):
        pass

    # Story 90-5 (item 3): raise AFTER the span so the seeding-failure decision
    # is recorded, but BEFORE ``save()`` so a failed seed never persists a
    # silently-empty pool to disk (No Silent Fallbacks).
    if seed_error is not None:
        raise EncounterSeedError(seed_error)

    manual.save()
