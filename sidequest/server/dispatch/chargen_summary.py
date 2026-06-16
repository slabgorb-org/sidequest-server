"""Confirmation-phase summary rendering for the character builder.

Port of ``sidequest-server/src/dispatch/chargen_summary.rs``.

Until 2026-04-09 this lived inside ``CharacterBuilder.to_scene_message`` in
the ``sidequest.game`` package (and the equivalent in the Rust source). That
was the wrong home: the builder is a state machine, and a faithful
confirmation summary needs inputs the builder does not own — specifically
the **lobby-provided player name** (there is no chargen scene for it in
genres like ``caverns_and_claudes``) and the **genre pack's
``starting_equipment`` table** (resolved from ``inventory.yaml``, not from
scene effects).

Keeping summary rendering inside the builder silently dropped those two
fields during the Thessa playtest bug on 2026-04-09. Moving it here
co-locates rendering with the data it requires: the server-side dispatch
layer already holds the ``GenrePack`` and the lobby name at every chargen
call site.

The builder stays responsible for the state machine; this module is the
view. New summary fields go here, not in the builder.
"""

from __future__ import annotations

from enum import StrEnum

from opentelemetry import trace

from sidequest.game.builder import CharacterBuilder, humanize_snake_case
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import RulesConfig
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
)

# Canonical chargen field keys used by ``chargen_field_labels`` lookups in
# rules.yaml. Genre packs can override any subset; unspecified keys fall
# through to the default labels below (which preserve the pre-existing
# fantasy-pack vocabulary). Documented here, not duplicated in every
# pack — packs opt in to renaming.
DEFAULT_CHARGEN_FIELD_LABELS: dict[str, str] = {
    "name": "Name",
    "race": "Race",
    "class": "Class",
    "personality": "Personality",
    "pronouns": "Pronouns",
    "stats": "Stats",
    "mutation": "Mutation",
    "affinity": "Affinity",
    "rig": "Rig",
    "rig_trait": "Rig Trait",
    "equipment": "Equipment",
    "backstory": "Backstory",
}


# Sentence punctuation that distinguishes freeform prose (the_story flow,
# autogen-table rolls) from tokenish chargen identifiers ("Outsystem-arrived",
# "old-soldier"). Tokens don't contain these characters; prose almost always
# does. The pipe is from ``_apply_story``'s ``" | "`` background+description
# join — preserves the separator on output rather than mangling it.
_PROSE_MARKERS = frozenset(".,!?|;:")


def _looks_like_prose(value: str) -> bool:
    """True if ``value`` reads as freeform prose rather than a chargen token.

    Used by the backstory rendering branch to decide whether to apply
    ``humanize_display`` (token-style: kebab-case → Title Case) or
    pass through as-is (prose). Sentence punctuation is the discriminator.
    """
    return any(ch in _PROSE_MARKERS for ch in value)


def humanize_display(value: str) -> str:
    """Title-case a chargen identifier value for player-facing display.

    Pack YAML stores trait/affinity/background tokens as either snake_case
    (``natural_armor``), kebab-case (``trouble-magnet``), or
    Pascal-with-hyphen (``Outsystem-arrived``). Players see these on the
    character preview row alongside TitleCase fields like ``Engineer's
    Multitool`` — Playtest 2026-04-30 flagged the casing inconsistency
    ("Personality: trouble-magnet" next to "Origin: Coreworlder") as a
    "raw enum keys leaking through" smell.

    Splits on both ``-`` and ``_``, capitalizes each token, joins with
    spaces. Idempotent on already-Title-cased strings (``Coreworlder`` →
    ``Coreworlder``); a single capitalized acronym word like ``HVAC`` gets
    rendered as ``Hvac`` — acceptable for the chargen identifier vocab,
    where acronyms aren't expected.
    """
    if not value:
        return value
    # Replace separators with space, then title-case each token. Preserve
    # multi-word values that already use spaces (``quietly grieving``) by
    # treating space as another separator — collapses runs cleanly.
    normalized = value.replace("-", " ").replace("_", " ")
    return " ".join(word.capitalize() for word in normalized.split() if word)


def field_label(rules: RulesConfig, key: str) -> str:
    """Return the display label for a chargen field.

    Lookup precedence:

    1. ``rules.chargen_field_labels[key]`` — per-pack override.
    2. Legacy ``rules.race_label`` / ``rules.class_label`` for ``race``
       and ``class`` — preserves the older one-off fields without
       forcing packs to migrate.
    3. ``DEFAULT_CHARGEN_FIELD_LABELS[key]`` — canonical fantasy label.

    Unknown keys (i.e. keys not in the default map) return the key
    as-is, preserving the title-cased convention used elsewhere.
    """
    override = rules.chargen_field_labels.get(key)
    if override:
        return override
    if key == "race" and rules.race_label:
        return rules.race_label
    if key == "class" and rules.class_label:
        return rules.class_label
    return DEFAULT_CHARGEN_FIELD_LABELS.get(key, key)


class _NameSource(StrEnum):
    """Which source produced the Name line in the rendered summary."""

    NAME_SCENE = "name_scene"
    """A freeform name-entry scene in the builder (e.g. mutant_wasteland)."""
    LOBBY = "lobby"
    """The lobby-provided name passed via the ``connect`` payload."""
    NONE = "none"
    """No name available from either source."""


class _EquipmentSource(StrEnum):
    """Which source produced the Equipment line in the rendered summary."""

    SCENE_ITEM_HINTS = "scene_item_hints"
    """Accumulated ``item_hint`` mechanical effects from scene choices."""
    PACK_STARTING_EQUIPMENT = "pack_starting_equipment"
    """Looked up from ``pack.inventory.starting_equipment[class]``."""
    MERGED = "merged"
    """Both sources contributed (scene hints merged onto the class loadout)."""
    NONE = "none"
    """Neither source produced any equipment."""


def render_confirmation_summary(
    builder: CharacterBuilder,
    pack: GenrePack,
    lobby_name: str | None,
    player_id: str,
) -> CharacterCreationMessage:
    """Render the Confirmation-phase summary message for a builder.

    Port of ``render_confirmation_summary`` in ``chargen_summary.rs``.

    Pulls fields from three sources:

    1. **Builder state** — pronouns, stats, race/class hints, mutation/rig
       traits, backstory, and the name-entry-scene name (if the genre has
       one).
    2. **Lobby name** — fallback for the Name line when no name-entry scene
       exists. The precedence (scene > lobby) matches the precedence used at
       ``build()`` time in the dispatch handler.
    3. **Genre pack inventory** — ``starting_equipment[class]`` resolved via
       either the accumulated ``class_hint`` or, if absent, the genre's
       ``default_class`` from ``rules.yaml``. Item IDs are mapped to display
       names through ``pack.inventory.item_catalog`` when possible.

    Emits an OTEL event ``character_creation.confirmation_rendered``
    recording which sources fired, so the GM panel can catch silent
    regressions (empty Name line, missing Equipment line, etc.).
    """
    assert builder.is_confirmation(), (
        "render_confirmation_summary called outside Confirmation phase"
    )

    acc = builder.accumulated()
    rules = builder.rules
    parts: list[str] = []
    # Ordered preview dict — mirror of ``parts`` so the UI can render a
    # structured character-sheet preview without parsing the joined
    # summary string. Keys are the *resolved display labels* (already
    # routed through ``field_label`` / ``chargen_field_labels``); the UI
    # renders them verbatim.
    preview: dict[str, str] = {}

    def _add(key: str, value: str) -> None:
        label = field_label(rules, key)
        parts.append(f"{label}: {value}")
        preview[label] = value

    # --- Name (scene > lobby > omit) --------------------------------------
    scene_name = builder.character_name()
    if scene_name is not None:
        name_source = _NameSource.NAME_SCENE
        resolved_name: str | None = scene_name
    else:
        trimmed_lobby = lobby_name.strip() if lobby_name else ""
        if trimmed_lobby:
            name_source = _NameSource.LOBBY
            resolved_name = trimmed_lobby
        else:
            name_source = _NameSource.NONE
            resolved_name = None
    if resolved_name is not None:
        _add("name", resolved_name)

    # --- Race / Class / Personality ---------------------------------------
    # Only show fields the chargen actually accumulated. Genres like
    # caverns_and_claudes deliberately omit race/class scenes — we don't lie
    # with "Unknown" for fields the genre doesn't define.
    if acc.race_hint is not None:
        _add("race", acc.race_hint)

    if acc.class_hint is not None:
        _add("class", acc.class_hint)
    elif (default_class := builder.default_class()) is not None:
        # If the genre has a default_class in rules.yaml (e.g. caverns
        # default_class: Delver), show it on the summary so the player sees
        # what class their equipment will be loaded for.
        _add("class", default_class)

    if acc.personality_trait is not None:
        _add("personality", humanize_display(acc.personality_trait))

    if acc.pronoun_hint is not None:
        _add("pronouns", acc.pronoun_hint)

    # --- Stats ------------------------------------------------------------
    rolled = builder.rolled_stats()
    if rolled is not None:
        stat_line = "  ".join(f"{name} {val}" for name, val in rolled)
        _add("stats", stat_line)

    if acc.mutation_hint is not None:
        _add("mutation", humanize_display(acc.mutation_hint))
    if acc.affinity_hint is not None:
        _add("affinity", humanize_display(acc.affinity_hint))
    if acc.rig_type_hint is not None:
        _add("rig", humanize_display(acc.rig_type_hint))
    if acc.rig_trait is not None:
        _add("rig_trait", humanize_display(acc.rig_trait))

    # --- Equipment (merge scene hints with pack starting equipment) -------
    # Resolve the class used for the starting_equipment lookup the same way
    # the dispatch handler's confirmation branch does at build time:
    # prefer an explicit class_hint, otherwise fall back to the genre's
    # default_class from rules.yaml. This keeps the *preview* and the
    # *actual wired character* in sync by construction — no drift.
    lookup_class: str | None = acc.class_hint or builder.default_class()

    equipment_ids: list[str] = []
    used_scene_hints = False
    used_pack_starting = False

    if pack.inventory is not None and lookup_class is not None:
        class_lower = lookup_class.lower()
        for key, loadout in pack.inventory.starting_equipment.items():
            if key.lower() == class_lower:
                equipment_ids.extend(loadout)
                used_pack_starting = bool(loadout)
                break

    if acc.item_hints:
        for hint in acc.item_hints:
            if hint not in equipment_ids:
                equipment_ids.append(hint)
        used_scene_hints = True

    if used_pack_starting and used_scene_hints:
        equipment_source = _EquipmentSource.MERGED
    elif used_pack_starting:
        equipment_source = _EquipmentSource.PACK_STARTING_EQUIPMENT
    elif used_scene_hints:
        equipment_source = _EquipmentSource.SCENE_ITEM_HINTS
    else:
        equipment_source = _EquipmentSource.NONE

    if equipment_ids:
        display_items = [_resolve_item_display_name(pack, item_id) for item_id in equipment_ids]
        _add("equipment", ", ".join(display_items))

    # Backstory display source preference (added 2026-04-30, Parsley
    # playtest BUG-LOW): genres like space_opera/coyote_star use
    # ``MechanicalEffects.background`` as a routing tag set by the
    # *origin* scene ("Outsystem-arrived"), not as the chosen
    # backstory hook ("Someone Went Into the Drift"). The accumulator
    # detects drive-shaped scenes (relationship/goals/emotional_state
    # without race/class/mutation/rig hints) and records the choice
    # label there as ``acc.backstory_label``. Prefer it when present;
    # fall back to ``acc.background`` for genres like mutant_wasteland
    # where ``background`` IS the meaningful label ("Vault Dweller").
    backstory_source: str | None = None
    if acc.backstory_label is not None:
        backstory_source = acc.backstory_label
    elif acc.background is not None:
        backstory_source = acc.background

    if backstory_source is not None:
        # Backstory keeps its leading blank line in the joined summary
        # (visual separation from the stat block) but is added to the
        # preview dict normally. Humanize the raw token so kebab-case
        # YAML values like "Outsystem-arrived" or "old-soldier" display
        # as "Outsystem Arrived" / "Old Soldier" instead of leaking
        # routing tags into the player-facing summary. But: the_story
        # flow (caverns_and_claudes) routes freeform prose AND/OR
        # autogen-table prose into ``acc.background`` — applying
        # ``humanize_display`` to a sentence like "Two delves under his
        # belt. Came back from the second…" capitalizes every word and
        # mangles intent. Detect prose by sentence punctuation; only
        # humanize tokenish values. The_story also concatenates
        # background + description with " | " (no dedicated description
        # field on MechanicalEffects); split it back to a stacked
        # display so players see two paragraphs instead of a pipe.
        backstory_label = field_label(rules, "backstory")
        if _looks_like_prose(backstory_source):
            paragraphs = [seg.strip() for seg in backstory_source.split(" | ") if seg.strip()]
            background_display = "\n".join(paragraphs)
        else:
            background_display = humanize_display(backstory_source)
        parts.append(f"\n{backstory_label}: {background_display}")
        preview[backstory_label] = background_display

    summary = "\n".join(parts)

    # --- Lie-detector telemetry -------------------------------------------
    # Records which sources fired so the GM panel can catch silent drops
    # (e.g. the 2026-04-09 Thessa bug: name_source=none, equipment_source=none
    # despite a lobby name being present and the pack defining a Delver
    # loadout).
    trace.get_current_span().add_event(
        "character_creation.confirmation_rendered",
        {
            "event": "confirmation_rendered",
            "name_source": name_source.value,
            "has_name": resolved_name is not None,
            "equipment_source": equipment_source.value,
            "equipment_count": len(equipment_ids),
            "lookup_class": lookup_class or "",
            "has_rolled_stats": rolled is not None,
            "player_id": player_id,
        },
    )

    payload = CharacterCreationPayload(
        phase="confirmation",
        scene_index=None,
        total_scenes=builder.total_scenes(),
        summary=summary,
        # Structured mirror of the joined ``summary`` for the React
        # client. Keys are the genre-resolved display labels (e.g.
        # "Origin" instead of "Race" for the Tea & Murder pack), values
        # are the raw field strings. UI iterates this dict to render
        # the "Your Character" preview without parsing the summary
        # blob; the legacy ``summary`` is still emitted as a fallback
        # for any client that pre-dates this field.
        character_preview=preview if preview else None,
    )
    return CharacterCreationMessage(payload=payload, player_id=player_id)


def _resolve_item_display_name(pack: GenrePack, item_id: str) -> str:
    """Map a starting-equipment item ID to a display name via
    ``pack.inventory.item_catalog``, falling back to Title-Cased snake_case
    if the catalog has no entry.
    """
    if pack.inventory is not None:
        for entry in pack.inventory.item_catalog:
            if entry.id == item_id and entry.name:
                return entry.name
    return humanize_snake_case(item_id)


__all__ = ["render_confirmation_summary"]
