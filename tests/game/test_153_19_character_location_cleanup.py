"""RED tests — Story 153-19 — Oddity 2: stale "Adventurer" placeholder key in
``character_locations``.

Playtest (barsoom, heavy_metal/WWN — board lines 356-362, CONFIRM/DO-NOT-RE-FILE,
matches 150-11/150-12) surfaced ``character_locations`` carrying BOTH a stale
default ``Adventurer`` key AND the real character key (``Kantos``) after chargen.

Mechanism (confirmed in code):
* ``world_materialization._apply_chapter`` seeds
  ``character_locations[ch.core.name] = chapter.location`` for every character in
  ``snap.characters`` — and a FRESH chapter that authors a nameless protagonist
  gets the ``"Adventurer"`` default name (``_apply_character``, line 339). So
  ``character_locations["Adventurer"]`` is written.
* ``chargen_mixin._chargen_confirmation`` then does
  ``materialized.characters = [character]`` (chargen-built PC) and backfills
  ``character_locations[real_name]`` — but NEVER prunes the now-orphaned
  ``"Adventurer"`` key. Both keys coexist in the exposed snapshot.

Contract (AC-2, TEA-defined for Dev):

* ``sidequest.game.world_materialization.prune_orphan_character_locations(snapshot)
  -> int`` removes every ``character_locations`` key that does NOT match a current
  character's ``core.name``; returns the number of keys pruned. Mutates in place.
* It emits exactly one ``character_locations.orphan_pruned`` span on EVERY call —
  including when nothing is pruned (``pruned_count=0``) — so the GM panel sees the
  cleanup ran and never a silent skip (CLAUDE.md "No Silent Fallbacks" + OTEL
  Observability Principle). The span is a registered, routed constant.

The wiring proof (the cleanup actually runs at chargen finalization) lives in
``tests/server/test_153_19_character_location_cleanup_wiring.py``.

``otel_capture`` is the in-memory span exporter fixture (tests/game/conftest.py).
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot

# Import under test — RED: the function does not exist yet, so collection fails
# loudly until Dev adds prune_orphan_character_locations to world_materialization.
from sidequest.game.world_materialization import prune_orphan_character_locations

SPAN_NAME = "character_locations.orphan_pruned"

_PLACEHOLDER = "Adventurer"  # world_materialization._apply_character default name


def _pc(name: str = "Kantos") -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="A red-skinned warrior far from the dead sea bottoms",
            personality="resolute",
        ),
        backstory="Woke beneath two moons with no memory of the crossing.",
        char_class="Fighter",
        race="Human",
    )


def _spans_named(otel_capture, name: str) -> list:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _only_span(otel_capture, name: str):
    spans = _spans_named(otel_capture, name)
    assert len(spans) == 1, f"expected exactly one {name!r} span, got {len(spans)}"
    return spans[0]


# ---------------------------------------------------------------------------
# Span constant wiring — the GM panel can only surface the cleanup if the span
# is a registered, routed constant (mirrors the 77-1 quest-seed precedent).
# ---------------------------------------------------------------------------


def test_orphan_pruned_span_constant_is_exported_and_routed() -> None:
    from sidequest.telemetry.spans import SPAN_CHARACTER_LOCATIONS_PRUNED, SPAN_ROUTES

    assert SPAN_CHARACTER_LOCATIONS_PRUNED == SPAN_NAME
    assert SPAN_CHARACTER_LOCATIONS_PRUNED in SPAN_ROUTES, (
        "span must be routed so the GM panel surfaces the cleanup, not flat-only"
    )


# ---------------------------------------------------------------------------
# AC-2: the orphan placeholder key is pruned, the real key survives.
# ---------------------------------------------------------------------------


def test_prune_removes_orphan_placeholder_key_keeps_real_key() -> None:
    snap = GameSnapshot(characters=[_pc("Kantos")])
    # Post-finalization state: the chapter seeded the placeholder's location and
    # the real PC's, but the placeholder Character was discarded.
    snap.character_locations = {_PLACEHOLDER: "Helium", "Kantos": "Helium"}

    pruned = prune_orphan_character_locations(snap)

    assert _PLACEHOLDER not in snap.character_locations, (
        "the stale 'Adventurer' placeholder key survived in character_locations (153-19 oddity 2)."
    )
    assert snap.character_locations == {"Kantos": "Helium"}, (
        "prune must keep exactly the real character's key and drop every orphan."
    )
    assert pruned == 1, f"prune must report 1 orphan removed; got {pruned}"


def test_prune_keeps_all_keys_for_multiple_real_characters() -> None:
    """No over-pruning: every key that matches a current character is kept."""
    snap = GameSnapshot(characters=[_pc("Kantos"), _pc("Tars")])
    snap.character_locations = {
        _PLACEHOLDER: "Helium",
        "Kantos": "Helium",
        "Tars": "Thark Encampment",
    }

    pruned = prune_orphan_character_locations(snap)

    assert snap.character_locations == {"Kantos": "Helium", "Tars": "Thark Encampment"}
    assert pruned == 1


# ---------------------------------------------------------------------------
# No Silent Fallbacks: the cleanup must be observable even on the no-op path.
# ---------------------------------------------------------------------------


def test_prune_emits_span_even_when_nothing_to_prune(otel_capture) -> None:
    snap = GameSnapshot(characters=[_pc("Kantos")])
    snap.character_locations = {"Kantos": "Helium"}  # already clean

    pruned = prune_orphan_character_locations(snap)

    assert pruned == 0, "a clean dict prunes nothing"
    assert snap.character_locations == {"Kantos": "Helium"}, "clean dict untouched"
    span = _only_span(otel_capture, SPAN_NAME)
    assert int(dict(span.attributes or {}).get("pruned_count", -1)) == 0, (
        "the no-op path must STILL emit one span (pruned_count=0) — the cleanup "
        "ran and found nothing, never a silent skip (No Silent Fallbacks)."
    )


def test_prune_span_reports_count_and_orphan_keys(otel_capture) -> None:
    snap = GameSnapshot(characters=[_pc("Kantos")])
    snap.character_locations = {_PLACEHOLDER: "Helium", "Kantos": "Helium"}

    prune_orphan_character_locations(snap)

    span = _only_span(otel_capture, SPAN_NAME)
    attrs = dict(span.attributes or {})
    assert int(attrs.get("pruned_count", -1)) == 1, (
        "span must report the count of orphan keys pruned for GM-panel visibility."
    )
