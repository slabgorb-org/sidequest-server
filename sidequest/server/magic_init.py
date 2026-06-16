"""Per-session magic-state initialization.

Phase 4 cut-point requires that, at chargen confirmation, a Coyote-Star
session lands with ``snapshot.magic_state`` populated and the freshly
built character already added to the ledger so per-character bars
(sanity / notice / vitality) can mutate from the very first turn.

This module owns the wire path:

    genre pack source_dir + world_slug + character.core.name
        ↓
    load_world_magic(genre_yaml, world_yaml)  →  WorldMagicConfig
        ↓
    MagicState.from_config(config) + add_character(character_id)
        ↓
    snapshot.magic_state

The function fails closed: any LoaderError is logged loud (CLAUDE.md
"No Silent Fallbacks") and ``snapshot.magic_state`` is left at its
prior value (typically ``None``). It does NOT raise — chargen has
already produced a character, and refusing to confirm because magic
config is malformed would orphan the commit.

Worlds without a ``magic.yaml`` (the common case for genres that don't
model magic) skip silently — the absence of the file is a deliberate
authoring decision, not a config error.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from sidequest.game.session import GameSnapshot
from sidequest.genre.magic_loader import LoaderError, load_world_magic
from sidequest.genre.models.character import ClassDef
from sidequest.magic.confrontations import (
    ConfrontationLoaderError,
    load_confrontations,
)
from sidequest.magic.models import LedgerBarSpec
from sidequest.magic.state import BarKey, LedgerBar, MagicState
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


def _load_class_def(genre_pack_source_dir: Path | None, character_class: str) -> ClassDef | None:
    """Load a single ClassDef by display_name from the genre pack's classes.yaml.

    Returns None if classes.yaml is absent or the class isn't found. Lookup
    uses display_name (case-sensitive) to match the chargen-emitted
    character_class string ("Mage", "Cleric", "Fighter", ...).
    """
    if genre_pack_source_dir is None:
        return None
    classes_yaml = genre_pack_source_dir / "classes.yaml"
    if not classes_yaml.exists():
        return None

    raw = yaml.safe_load(classes_yaml.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        return None
    for entry in raw:
        try:
            cd = ClassDef.model_validate(entry)
        except (ValidationError, TypeError) as exc:
            # Loud-fail per CLAUDE.md "No Silent Fallbacks": malformed
            # class entries surface as a warning so authoring bugs don't
            # masquerade as "class not found" lookup misses. Story 47-7
            # adds the watcher event so the GM panel surfaces it live.
            entry_id = entry.get("id", "?") if isinstance(entry, dict) else "?"
            logger.warning(
                "magic.init_class_def_invalid genre_pack=%s entry_id=%s error=%s",
                genre_pack_source_dir,
                entry_id,
                exc,
            )
            _watcher_publish(
                "magic.init_class_def_invalid",
                {
                    "genre_pack_source_dir": str(genre_pack_source_dir),
                    "entry_id": entry_id,
                    "error": str(exc),
                },
                component="magic",
                severity="warning",
            )
            continue
        if cd.display_name == character_class:
            return cd
    return None


def init_magic_state_for_session(
    *,
    snapshot: GameSnapshot,
    genre_pack_source_dir: Path | None,
    world_slug: str,
    character_id: str,
    character_class: str | None = None,
) -> bool:
    """Populate ``snapshot.magic_state`` for the freshly chargen'd character.

    ``character_class`` (display-cased: "Mage", "Cleric", "Fighter", "Thief",
    "Delver") is required when the world's magic.yaml ships a character-
    scope bar with class-keyed ``starts_at_chargen`` (B/X-style class-aware
    spell slot allocation, caverns_sunden 2026-05-07). Worlds with only
    scalar specs (coyote_star) work without it. A class-keyed bar with
    no ``character_class`` raises ValueError inside ``add_character``
    rather than silently falling back.

    Returns True iff a magic state was loaded and assigned. Returns False
    when:
      - ``genre_pack_source_dir`` is None (pack loaded from a non-disk
        source — magic loader requires file paths)
      - genre or world ``magic.yaml`` is absent (this world has no magic)
      - loader raised LoaderError (logged at ERROR; snapshot untouched)
    """
    if genre_pack_source_dir is None:
        _watcher_publish(
            "magic.init_skipped",
            {
                "world_slug": world_slug,
                "actor": character_id,
                "reason": "no_genre_pack_source_dir",
            },
            component="magic",
            severity="info",
        )
        return False

    genre_magic = genre_pack_source_dir / "magic.yaml"
    world_magic = genre_pack_source_dir / "worlds" / world_slug / "magic.yaml"

    if not genre_magic.exists() or not world_magic.exists():
        # No magic config for this world — expected, common (e.g. genres
        # that don't model magic at all). Surface to the GM panel so
        # "subsystem invisible" never reads as "subsystem broken" — per
        # the OTEL Observability Principle (CLAUDE.md), the panel needs
        # to confirm engagement OR justified non-engagement, not silence.
        _watcher_publish(
            "magic.init_skipped",
            {
                "world_slug": world_slug,
                "actor": character_id,
                "reason": "no_magic_yaml",
                "genre_magic_exists": genre_magic.exists(),
                "world_magic_exists": world_magic.exists(),
            },
            component="magic",
            severity="info",
        )
        return False

    try:
        config = load_world_magic(genre_yaml=genre_magic, world_yaml=world_magic)
    except LoaderError as exc:
        # Authoring bug — log loud per CLAUDE.md, don't crash chargen.
        logger.error(
            "magic.init_failed world=%s genre_yaml=%s world_yaml=%s error=%s",
            world_slug,
            genre_magic,
            world_magic,
            exc,
        )
        _watcher_publish(
            "magic.init_failed",
            {
                "world_slug": world_slug,
                "actor": character_id,
                "genre_yaml": str(genre_magic),
                "world_yaml": str(world_magic),
                "error": str(exc),
            },
            component="magic",
            severity="error",
        )
        return False

    # Pingpong 2026-04-30 ("Magic system parse_error: unknown actor:
    # 'Linus'; call add_character first"): in 4P MP each player's
    # chargen confirmation calls this function; pre-fix every call did
    # ``MagicState.from_config(config)`` which built a NEW state with
    # only the current ``character_id`` and assigned it to
    # ``snapshot.magic_state``, wiping prior committers. With four
    # sequential commits Charlie → Snoopy → Linus → Lucy, only Lucy
    # ended up in the ledger; the next turn's narrator referenced
    # Linus by name, the magic parser tried to apply a working against
    # actor 'Linus', the per-character bars weren't there, and the
    # parser raised ``unknown actor: 'Linus'; call add_character first``.
    # Fix: idempotent on the snapshot — if a magic state already
    # exists for this slug, REUSE it and just register the new
    # character. Build a fresh state only on the first commit (when
    # ``snapshot.magic_state is None``). Mirrors the same MP-aware
    # idempotence the lore seeders rely on (DuplicateLoreId guard) and
    # the canonical-snapshot model (room-owned shared world state).
    if snapshot.magic_state is None:
        state = MagicState.from_config(config)
        # Phase 5 (Story 47-3): on first commit, also load the world's
        # named magic confrontations. The file is optional — worlds
        # without ``confrontations.yaml`` simply have an empty list, so
        # the auto-fire evaluator (called inside ``apply_magic_working``)
        # is a no-op for them.
        #
        # When the file exists but fails to load (malformed YAML, missing
        # branch, schema error), this function follows the same
        # graceful-degrade pattern as the magic.yaml LoaderError catch
        # above: log at ERROR + emit a watcher event, then proceed with
        # ``state.confrontations = []``. This is a deliberate design
        # decision (chargen has already produced a character; refusing
        # to confirm would orphan the commit), NOT compliance with
        # CLAUDE.md "no silent fallback" — the subsystem visibly
        # degrades, which is a fallback. The watcher event surfaces the
        # degradation to the GM panel so it is not invisible. A
        # follow-up story should consider promoting this to a hard
        # failure once chargen rollback is wired.
        confrontations_yaml = genre_pack_source_dir / "worlds" / world_slug / "confrontations.yaml"
        if confrontations_yaml.exists():
            try:
                state.confrontations = load_confrontations(confrontations_yaml)
            except ConfrontationLoaderError as conf_exc:
                # Explicit reset — the comment above promises
                # ``state.confrontations = []`` on this path; defends
                # against any future code path that pre-populates the
                # field on ``MagicState.from_config`` (Westley round 2
                # comment-analyzer finding: comment claimed an explicit
                # assignment that did not exist).
                state.confrontations = []
                logger.error(
                    "magic.confrontations_init_failed world=%s yaml=%s error=%s",
                    world_slug,
                    confrontations_yaml,
                    conf_exc,
                )
                _watcher_publish(
                    "state_transition",
                    {
                        "field": "magic_state",
                        "op": "confrontations_load_failed",
                        "world_slug": world_slug,
                        "yaml": str(confrontations_yaml),
                        "error": str(conf_exc),
                    },
                    component="magic",
                    severity="error",
                )
        snapshot.magic_state = state
        first_commit = True
    else:
        state = snapshot.magic_state
        first_commit = False
    state.add_character(character_id, character_class=character_class)

    # Story 47-10 — seed learned_v1 data layer for casters.
    # When the world activates learned_v1 AND the actor's ClassDef declares
    # magic_config, populate known_spells from the matching spell catalog
    # and create per-level slot bars. The data layer is a no-op for non-
    # casters and for worlds that don't activate learned_v1.
    if "learned_v1" in config.active_plugins and character_class is not None:
        class_def = _load_class_def(genre_pack_source_dir, character_class)
        if class_def is not None and class_def.magic_config is not None:
            tradition = class_def.magic_config.tradition
            # Catalogs are keyed by file stem (e.g. "arcane_l1") in
            # WorldMagicConfig.spell_catalogs. v1 ships L1 only — collect
            # all catalogs whose tradition matches this class so future
            # L2+ catalogs aggregate naturally.
            matching = [
                c for c in (config.spell_catalogs or {}).values() if c.tradition == tradition
            ]
            catalog = matching[0] if matching else None
            if catalog is None:
                logger.warning(
                    "magic.init_no_catalog world=%s actor=%s tradition=%s — "
                    "class declares magic_config but no %s catalog loaded",
                    world_slug,
                    character_id,
                    tradition,
                    tradition,
                )
                _watcher_publish(
                    "magic.init_no_catalog",
                    {
                        "world_slug": world_slug,
                        "actor": character_id,
                        "class": character_class,
                        "tradition": tradition,
                        "class_id": class_def.id,
                    },
                    component="magic",
                    severity="warning",
                )
            else:
                # Spellbook seed: the actor "knows" every spell in the
                # catalog. starting_known_spells in the spec is advisory
                # for now — we give the player the full list to prepare
                # from. Future stories may narrow this on chargen.
                known_spell_ids = [s.id for s in catalog.spells]
                # Class level — chargen lands characters at L1 by default.
                # Future progression hooks can pass a higher level via the
                # GameSnapshot character record.
                seed_learned_v1_state(
                    state,
                    actor=character_id,
                    class_def=class_def,
                    class_level=1,
                    chosen_known_spells=known_spell_ids,
                )

    plugins = list(config.active_plugins)
    bar_count = len(state.ledger)
    logger.info(
        "magic.init world=%s actor=%s class=%s plugins=%s bars=%d first_commit=%s",
        world_slug,
        character_id,
        character_class,
        plugins,
        bar_count,
        first_commit,
    )
    _watcher_publish(
        "magic.init",
        {
            "world_slug": world_slug,
            "actor": character_id,
            "class": character_class,
            "plugins": plugins,
            "bars": bar_count,
            "first_commit": first_commit,
        },
        component="magic",
        severity="info",
    )
    # Loud-fallback guard (CLAUDE.md "No Silent Fallbacks"): innate_v1
    # is the per-character bar producer; if it's active but no character
    # bars instantiated for this actor, the world's ``ledger_bars`` list
    # is missing per-character spec entries. Emit an explicit warning so
    # the GM panel and dev logs surface the misconfig instead of
    # silently shipping a Mage/Cleric with no sanity bar.
    if "innate_v1" in plugins:
        actor_prefix = f"character|{character_id}|"
        actor_bar_count = sum(1 for k in state.ledger if k.startswith(actor_prefix))
        if actor_bar_count == 0:
            logger.warning(
                "magic.init_no_actor_bars world=%s actor=%s plugins=%s — "
                "innate_v1 is active but world ledger_bars defines no "
                "character-scope bars for this actor",
                world_slug,
                character_id,
                plugins,
            )
            _watcher_publish(
                "magic.init_no_actor_bars",
                {
                    "world_slug": world_slug,
                    "actor": character_id,
                    "class": character_class,
                    "plugins": plugins,
                    "world_yaml": str(world_magic),
                },
                component="magic",
                severity="warning",
            )
    return True


def seed_learned_v1_state(
    state: MagicState,
    *,
    actor: str,
    class_def: ClassDef,
    class_level: int,
    chosen_known_spells: list[str],
) -> None:
    """Per-actor learned_v1 seed: known_spells + per-level slot bars.

    Called from init_magic_state_for_session for any class with
    magic_access == 'learned_v1'. Slot-table lookup uses string-keyed
    dicts (YAML 1.1/JSON-safe) and selects the highest entry <= class_level.
    """
    if class_def.magic_config is None:
        raise ValueError(f"class {class_def.id!r} declares learned_v1 but has no magic_config")

    for sid in chosen_known_spells:
        state.learn_spell(actor, sid)

    # Initialize prepared_spells[actor] as an empty dict so the
    # prepared-list gate (story 47-10 AC4) and context block (AC7) can
    # distinguish "never prepared" (empty dict) from "no MagicState for
    # this actor" (key absent). The actor will populate this via the
    # learned_ops.prepare op at a safe site.
    state.prepared_spells.setdefault(actor, {})

    # Pick the slot row for this class_level: largest key <= class_level.
    slot_table = class_def.magic_config.slots_by_class_level
    eligible = sorted(int(k) for k in slot_table if int(k) <= class_level)
    if not eligible:
        return  # class_level too low; no slots yet (e.g. cleric L1 has no L1 slots in some tables)
    row = slot_table[str(eligible[-1])]

    for spell_level_str, max_slots in row.items():
        spell_level = int(spell_level_str)
        bar_id = f"slots_l{spell_level}"
        spec = LedgerBarSpec(
            id=bar_id,
            scope="character",
            direction="down",
            range=(0.0, float(max_slots)),
            threshold_low=0.0,
            consequence_on_low_cross=f"out of L{spell_level} slots until rest",
            starts_at_chargen=float(max_slots),
        )
        _key = BarKey(scope="character", owner_id=actor, bar_id=bar_id)
        state.ledger[f"character|{actor}|{bar_id}"] = LedgerBar(spec=spec, value=float(max_slots))
