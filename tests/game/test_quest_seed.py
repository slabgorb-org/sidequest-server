"""RED tests — Story 77-1 — Seed-at-creation quest spine (ADR-137 Option A).

At session init the engine must derive a campaign spine — one ``quest_anchor``
+ a ``quest_log`` entry + ``active_stakes`` — from the PC's ``drive`` /
``calling_label`` so every session starts non-empty from turn 1, instead of
running on pure narrator improvisation (the wry_whimsy/oz turn-13 failure:
``quest_log: {}``, ``quest_anchors: []``, ``active_stakes: ""``).

Contract under test (TEA-defined for Dev):

* ``sidequest.game.quest_seed.seed_quest_spine(snapshot, character)`` mutates
  the snapshot in place.
* **Seed source** is ``character.drive``, falling back to
  ``character.calling_label``.
* **Populated source** → ``quest_log`` has >=1 entry, ``quest_anchors`` has >=1
  anchor, ``active_stakes`` is non-empty and *derived from* the seed source
  (the source text is referenced in the seeded content). Emits a
  ``quest.seeded_at_creation`` span with ``has_stakes=True``, non-empty
  ``quest_id`` / ``anchor_id``, ``source_drive`` == the source, and a
  ``severity`` that is NOT "warning".
* **Empty source** (both drive AND calling_label blank — the prose-pack case)
  → the seed does NOT fabricate a spine (all three fields stay empty), but it
  MUST degrade LOUDLY: emit exactly one ``quest.seeded_at_creation`` span with
  ``severity="warning"``, ``has_stakes=False``, ``source_drive=""``. Never a
  silent no-op (CLAUDE.md "No Silent Fallbacks", the story's headline rule).

The ``quest.seeded_at_creation`` span is the ONLY span this story adds; the
other epic spans (``quest.created`` / ``quest.updated`` / ``quest.anchor.added``
/ ``stakes.set``) belong to 77-2 / 77-3 and are out of scope here.

``otel_capture`` is the in-memory span exporter fixture (tests/game/conftest.py).
"""

from __future__ import annotations

import pytest

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore

# Import under test — RED: the module does not exist yet, so collection fails
# loudly until Dev creates sidequest/game/quest_seed.py.
from sidequest.game.quest_seed import seed_quest_spine
from sidequest.game.session import GameSnapshot

SPAN_NAME = "quest.seeded_at_creation"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_character(
    *, drive: str = "", calling_label: str = "", name: str = "Dorothy"
) -> Character:
    """A minimally-valid PC. ``backstory`` cannot be blank (Character validator)."""
    return Character(
        core=CreatureCore(name=name, description="A traveler far from home", personality="curious"),
        backstory="Swept up by a cyclone and dropped somewhere strange.",
        char_class="Wanderer",
        race="Human",
        drive=drive,
        calling_label=calling_label,
    )


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _only_span(otel_capture, name: str):
    spans = _spans_named(otel_capture, name)
    assert len(spans) == 1, f"expected exactly one {name!r} span, got {len(spans)}"
    return spans[0]


# ---------------------------------------------------------------------------
# Span constant wiring (matches the room_entry_otel precedent) — the GM panel
# can only surface the seed if the span is a registered, routed constant.
# ---------------------------------------------------------------------------


def test_seeded_at_creation_span_constant_is_exported_and_routed() -> None:
    from sidequest.telemetry.spans import SPAN_QUEST_SEEDED_AT_CREATION, SPAN_ROUTES

    assert SPAN_QUEST_SEEDED_AT_CREATION == SPAN_NAME
    assert SPAN_QUEST_SEEDED_AT_CREATION in SPAN_ROUTES, (
        "span must be routed so the GM panel surfaces it, not flat-only"
    )


# ---------------------------------------------------------------------------
# AC-2 (mirror / happy path): populated drive seeds all three fields and fires
# a clean (non-warning) span.
# ---------------------------------------------------------------------------


def test_populated_drive_seeds_all_three_fields() -> None:
    snap = GameSnapshot()
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    assert len(snap.quest_log) >= 1, "quest_log must hold the seeded quest"
    assert len(snap.quest_anchors) >= 1, "quest_anchors must hold the seeded anchor"
    assert snap.active_stakes != "", "active_stakes must be set from the drive"


def test_populated_drive_stakes_are_derived_from_the_drive() -> None:
    """'Derived from drive' — not an arbitrary constant. The drive text must
    surface in the seeded content (active_stakes or a quest_log value)."""
    snap = GameSnapshot()
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    # Story 77-2: quest_log values are QuestEntry now — pull their text fields.
    quest_text = [f"{e.title} {e.objective} {e.status}" for e in snap.quest_log.values()]
    seeded_text = " ".join([snap.active_stakes, *quest_text]).lower()
    assert "kansas" in seeded_text, (
        "seeded spine must reference the PC's drive, not a generic placeholder"
    )


def test_populated_drive_span_fires_without_warning(otel_capture) -> None:
    snap = GameSnapshot()
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert attrs.get("severity") != "warning", "populated drive must not warn"
    assert attrs.get("has_stakes") is True
    assert attrs.get("source_drive") == "Get home to Kansas"
    assert attrs.get("quest_id"), "quest_id attribute must be non-empty on a real seed"
    assert attrs.get("anchor_id"), "anchor_id attribute must be non-empty on a real seed"


def test_calling_label_used_when_drive_empty() -> None:
    """Seed source is drive *or* calling_label — a PC with only a calling
    still gets a spine and must NOT take the empty/loud-degrade path."""
    snap = GameSnapshot()
    char = _make_character(drive="", calling_label="Sworn to find the lost heir")

    seed_quest_spine(snap, char)

    assert len(snap.quest_anchors) >= 1
    assert snap.active_stakes != ""


def test_calling_label_seed_span_is_not_a_warning(otel_capture) -> None:
    snap = GameSnapshot()
    char = _make_character(drive="", calling_label="Sworn to find the lost heir")

    seed_quest_spine(snap, char)

    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert attrs.get("severity") != "warning"
    assert attrs.get("has_stakes") is True
    assert attrs.get("source_drive") == "Sworn to find the lost heir"


# ---------------------------------------------------------------------------
# AC-1 + AC-2: empty drive AND empty calling — the prose-pack case. No
# fabrication, but a LOUD warning span. No Silent Fallbacks.
# ---------------------------------------------------------------------------


def test_empty_drive_does_not_fabricate_a_spine() -> None:
    snap = GameSnapshot()
    char = _make_character(drive="", calling_label="")

    seed_quest_spine(snap, char)

    assert snap.quest_log == {}, "must not invent a quest when there's nothing to seed from"
    assert snap.quest_anchors == [], "must not invent an anchor"
    assert snap.active_stakes == "", "must not invent stakes"


def test_empty_drive_degrades_loudly_with_warning_span(otel_capture) -> None:
    snap = GameSnapshot()
    char = _make_character(drive="", calling_label="")

    seed_quest_spine(snap, char)

    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert attrs.get("severity") == "warning", (
        "empty seed must carry a WARNING-severity attribute (No Silent Fallbacks)"
    )
    assert attrs.get("has_stakes") is False
    assert attrs.get("source_drive") == ""


def test_empty_drive_path_is_never_silent(otel_capture) -> None:
    """The empty path must still emit exactly one span — never a quiet skip.
    This is the lie-detector tell that the seed ran but had nothing to seed."""
    snap = GameSnapshot()
    char = _make_character(drive="", calling_label="")

    seed_quest_spine(snap, char)

    assert len(_spans_named(otel_capture, SPAN_NAME)) == 1, (
        "exactly one quest.seeded_at_creation span on the empty path — not zero (silent), not many"
    )


# ---------------------------------------------------------------------------
# Out-of-scope guard: this story adds ONLY the one span. The 77-2/77-3 spans
# must NOT be emitted by the creation-time seed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden", ["quest.created", "quest.updated", "quest.anchor.added", "stakes.set"]
)
def test_seed_does_not_emit_out_of_scope_spans(otel_capture, forbidden: str) -> None:
    snap = GameSnapshot()
    seed_quest_spine(snap, _make_character(drive="Get home to Kansas"))

    assert _spans_named(otel_capture, forbidden) == [], (
        f"{forbidden} belongs to story 77-2/77-3, not the creation-time seed"
    )


# ---------------------------------------------------------------------------
# Rework (Review RT1, HIGH finding): fill-not-clobber. The chargen seam runs
# AFTER materialize_from_genre_pack, which sets snapshot.active_stakes from the
# FRESH opening chapter (world_materialization.py:315-316) — live on ~10 worlds
# incl. flickering_reach (history.yaml:107) and annees_folles. The seed must
# FILL an empty spine, never OVERWRITE a world-authored one. When a spine is
# already authored (active_stakes non-empty), the seed defers: preserves the
# authored stakes, creates no seed quest/anchor, and still emits one
# non-warning span (defer is observable, never silent).
# ---------------------------------------------------------------------------

_AUTHORED_STAKES = "The blood-debt comes due at dusk, and the well runs dry by dawn."


def test_world_authored_active_stakes_is_not_clobbered() -> None:
    """A world's opening chapter already set active_stakes; a PC with a drive
    must NOT overwrite it with a generic drive/calling string."""
    snap = GameSnapshot()
    snap.active_stakes = _AUTHORED_STAKES  # as materialize_from_genre_pack would
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    assert snap.active_stakes == _AUTHORED_STAKES, (
        "seed must not clobber world-authored active_stakes (Diamonds and Coal)"
    )


def test_authored_spine_skips_seed_quest_and_anchor() -> None:
    """When the world already authored a spine (active_stakes set), the seed
    defers entirely — it does not graft a competing seed quest/anchor on top."""
    snap = GameSnapshot()
    snap.active_stakes = _AUTHORED_STAKES
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    assert "seed_drive" not in snap.quest_log, "must not add a seed quest over an authored spine"
    assert "seed_drive_anchor" not in snap.quest_anchors, (
        "must not add a seed anchor over an authored spine"
    )


def test_defer_path_emits_one_non_warning_span(otel_capture) -> None:
    """Deferring to an authored spine is a success, not a failure: emit exactly
    one quest.seeded_at_creation span, non-warning, never a silent skip."""
    snap = GameSnapshot()
    snap.active_stakes = _AUTHORED_STAKES
    char = _make_character(drive="Get home to Kansas")

    seed_quest_spine(snap, char)

    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert attrs.get("severity") != "warning", (
        "an authored spine is not an empty-seed failure — must not warn"
    )
    # Defer is distinguishable on the GM panel: a spine exists (has_stakes True)
    # but the seed did not originate it from the drive (source_drive empty).
    assert attrs.get("has_stakes") is True
    assert attrs.get("source_drive") == ""


# ---------------------------------------------------------------------------
# Story 153-19 — Oddity 3: active_stakes shows a raw CLASS NAME.
#
# Playtest (shattered_accord + burning_peace, both WWN) surfaced
# ``active_stakes: "Channeler"`` → the Confrontation panel rendered
# **Stakes: Channeler**, a meaningless bare class identifier.
#
# Root: ``seed_quest_spine`` falls back to ``character.calling_label`` when
# ``drive`` is empty (the 77-1 contract — see
# ``test_calling_label_used_when_drive_empty`` above, which stays GREEN because
# *there* the calling_label is a drive-like phrase). But ``calling_label`` is a
# DISPLAY-FLAVOR label that the Character model says is "Empty when the label IS
# the archetype" (character.py:165-173) — for these WWN PCs it was populated with
# the bare class/calling ("Channeler") that equals ``char_class``. Seeding that
# as stakes produces the oddity.
#
# Contract (AC-3, TEA-defined for Dev): when the ONLY available seed source is a
# ``calling_label`` that is a bare class identifier (equals ``char_class``,
# case-insensitively), the seed must NOT write the class name into
# ``active_stakes``. With no real drive and no meaningful flavor calling, there
# is nothing to seed — so the seed takes the existing LOUD-degrade path (empty
# spine + ``severity="warning"`` span), never a silent class-name fabrication
# (CLAUDE.md "No Silent Fallbacks"). A genuinely flavorful calling_label that
# differs from the class name is unaffected (the 77-1 contract holds).
# ---------------------------------------------------------------------------


def _wwn_class_name_character(*, name: str = "Kantos", char_class: str = "Channeler") -> Character:
    """A WWN PC whose only identity label is the bare class/calling name — the
    shattered_accord/burning_peace repro: empty drive, calling_label == char_class."""
    return Character(
        core=CreatureCore(
            name=name,
            description="A red-skinned channeler far from the dead sea bottoms",
            personality="driven",
        ),
        backstory="Woke beneath two moons with the High Art still burning in the blood.",
        char_class=char_class,
        race="Human",
        drive="",
        calling_label=char_class,  # the bug: flavor label populated with the bare class
    )


def test_bare_class_name_calling_label_is_not_seeded_as_stakes() -> None:
    """AC-3: active_stakes must never be the PC's bare class name."""
    snap = GameSnapshot()
    char = _wwn_class_name_character(char_class="Channeler")

    seed_quest_spine(snap, char)

    assert snap.active_stakes.casefold() != "channeler", (
        "active_stakes was seeded with the bare class name 'Channeler' — the "
        "Confrontation panel would render 'Stakes: Channeler' (153-19 oddity 3)."
    )
    assert snap.active_stakes.strip().casefold() != char.char_class.strip().casefold(), (
        "active_stakes equals the PC's char_class — a class identifier is not a "
        "stakes description (AC-3)."
    )
    # And it must not have laundered the class name into a seed quest entry either.
    quest_text = " ".join(f"{e.title} {e.objective}" for e in snap.quest_log.values()).casefold()
    assert "channeler" not in quest_text, (
        "the bare class name leaked into the seeded quest spine, not just active_stakes."
    )


def test_class_name_only_calling_degrades_loudly_not_silently(otel_capture) -> None:
    """With no real drive and only a class-name calling, the seed has nothing
    meaningful to seed from. It must degrade LOUDLY (empty spine + warning span),
    exactly like the empty-source path — never silently invent a class-name stake.
    """
    snap = GameSnapshot()
    char = _wwn_class_name_character(char_class="Channeler")

    seed_quest_spine(snap, char)

    # No fabrication.
    assert snap.active_stakes == "", "must not fabricate stakes from a bare class name"
    assert snap.quest_log == {}, "must not invent a quest from a bare class name"
    assert snap.quest_anchors == [], "must not invent an anchor from a bare class name"

    # But LOUD — exactly one span, warning severity, has_stakes False.
    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert attrs.get("severity") == "warning", (
        "a class-name-only calling is a no-meaningful-source case — it must carry "
        "a WARNING-severity span (No Silent Fallbacks), not seed silently."
    )
    assert attrs.get("has_stakes") is False


def test_flavorful_calling_label_differing_from_class_still_seeds() -> None:
    """Guard the 77-1 contract: a calling_label that is a real flavor phrase
    (NOT the bare class name) must still seed a spine. The 153-19 fix narrows
    ONLY the class-name-equals-calling case, it does not disable the calling
    fallback wholesale."""
    snap = GameSnapshot()
    char = Character(
        core=CreatureCore(name="Kantos", description="d", personality="p"),
        backstory="Woke beneath two moons.",
        char_class="Channeler",
        race="Human",
        drive="",
        calling_label="Sworn to free the captive princess of Helium",
    )

    seed_quest_spine(snap, char)

    assert snap.active_stakes != "", (
        "a genuinely flavorful calling_label must still seed active_stakes — the "
        "153-19 fix must not break the 77-1 calling fallback for real phrases."
    )
    assert "helium" in snap.active_stakes.casefold()
