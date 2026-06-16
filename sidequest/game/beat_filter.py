"""Per-class beat filter — single source of truth for 'what can the player do this turn?'.

Per spec docs/superpowers/specs/2026-05-08-cnc-bx-class-beats-morale-design.md §4.3
and 2026-05-06 magic-system spec §3.5 (memorization wiring, story 47-10).

The filter resolves three independent gates:

  1. class_filter on each beat (None = universal; non-empty = whitelist)
  2. class_def.encounter_beat_choices intersection (per-class whitelist)
  3. cast_spell resource gates:
     a. slot gate — spell_slots_remaining >= 1.0
     b. prepared-list gate — actor has at least one spell prepared at any
        level (story 47-10 dual-plugin pivot)

The slot and prepared-list gates fail independently, with distinct OTEL
reasons so the GM panel can tell "Mage out of slots" from "Mage didn't
memorize anything this morning". The two gates are NOT collapsed into one.
"""

from __future__ import annotations

from sidequest.genre.error import PackError
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import BeatDef, ConfrontationDef


def _has_any_prepared(prepared_spells: dict[int, list[str]] | None) -> bool:
    """True iff the actor has at least one spell prepared at any level."""
    if not prepared_spells:
        return False
    return any(spells for spells in prepared_spells.values())


def beats_available_for(
    confrontation: ConfrontationDef,
    class_def: ClassDef,
    spell_slots_remaining: float,
    prepared_spells: dict[int, list[str]] | None = None,
) -> list[BeatDef]:
    """Return the BeatDefs the given class can select this turn.

    ``prepared_spells`` (story 47-10 addition) is optional for backward
    compatibility — when omitted (or None), the prepared-list gate is
    skipped and behavior matches the pre-47-10 contract. Existing
    callers (narrator.py, orchestrator.py) continue to work; new
    callers should pass it.
    """
    if not class_def.encounter_beat_choices:
        raise PackError(f"class {class_def.display_name!r} has empty encounter_beat_choices")

    pool: list[BeatDef] = []
    for beat in confrontation.beats:
        # Gate 1 — class_filter whitelist. A non-empty class_filter restricts
        # the beat to the listed classes; class restriction is enforced HERE.
        if beat.class_filter is not None and class_def.display_name not in beat.class_filter:
            continue
        # Universal beats (class_filter is None) are available to every class
        # without per-class ``encounter_beat_choices`` enumeration — this is
        # the documented "class_filter None = universal" semantics. The
        # ``encounter_beat_choices`` whitelist (gate 2 below) curates only
        # class-specific beats; applying it to universals filtered out every
        # non-combat confrontation's beats (chase/negotiation/standoff are all
        # universal and no class enumerates them), starving the UI of choices.
        if beat.class_filter is None:
            pool.append(beat)
            continue
        # Gate 2 — per-class whitelist for class-specific beats.
        if beat.id not in class_def.encounter_beat_choices:
            continue
        if beat.id == "cast_spell":
            if spell_slots_remaining < 1.0:
                continue
            # Prepared-list gate runs only when the caller opts in by
            # passing prepared_spells. Backward-compat: existing callers
            # that don't pass the param skip this gate and rely on the
            # slot gate alone.
            if prepared_spells is not None and not _has_any_prepared(prepared_spells):
                continue
        pool.append(beat)
    return pool


def cast_spell_rejection_reason(
    confrontation: ConfrontationDef,
    class_def: ClassDef,
    spell_slots_remaining: float,
    prepared_spells: dict[int, list[str]] | None = None,
) -> str | None:
    """Why was cast_spell filtered out for this actor?

    Returns one of:
      - ``None`` — cast_spell was selectable (no rejection), OR ``prepared_spells``
        was omitted (backward-compat caller — gate is dormant)
      - ``"no_slots"`` — slot bar at zero; rest required
      - ``"unprepared"`` — caller passed a non-None ``prepared_spells`` and the
        actor has no spells prepared at any level
      - ``"class"`` — class isn't allowed cast_spell at all (Fighter/Thief)
      - ``"absent"`` — beat isn't in this confrontation's pool

    Used by OTEL emitters to stamp distinct decision values on the
    confrontation.beat_filter span — the GM panel reads them to tell
    "out of slots" from "didn't prep" without parsing prose.
    """
    cast_beat = next((b for b in confrontation.beats if b.id == "cast_spell"), None)
    if cast_beat is None:
        return "absent"
    if cast_beat.class_filter is not None and class_def.display_name not in cast_beat.class_filter:
        return "class"
    if "cast_spell" not in (class_def.encounter_beat_choices or []):
        return "class"
    if spell_slots_remaining < 1.0:
        return "no_slots"
    # Symmetric with beats_available_for: when prepared_spells is omitted
    # (None — backward-compat callers), the prepared-list gate is skipped
    # and cast_spell is considered selectable. Only callers that explicitly
    # pass an empty/non-empty dict trigger the unprepared check.
    if prepared_spells is not None and not _has_any_prepared(prepared_spells):
        return "unprepared"
    return None
