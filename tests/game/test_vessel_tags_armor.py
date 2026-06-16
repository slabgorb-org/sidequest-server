"""Story 86-2 (AC3): VesselTags must parse the ``armor:N`` tag.

Story 53-2 shipped composure-only vessel parsing — ``armor:N`` /
``speed:N`` / ``mount_slots:N`` were present in road_warrior content
tags but explicitly *ignored* (see ``vessel_tags.py`` module docstring).
CWN Vehicle Combat §2.4.8 makes Armor mechanically load-bearing: every
hit is reduced by the vehicle's Armor rating. Plan 2 wires it in.

These tests are RED until Dev extends ``VesselTags`` with an ``armor``
field and ``parse_vessel_tags`` reads the ``armor:N`` tag (defaulting to
0 when absent, so composure-only legacy items keep loading).

Faithful-port + No-Silent-Fallbacks contract:
  - ``armor:N`` present  → parsed to ``VesselTags.armor == N``
  - ``armor`` tag absent → ``VesselTags.armor == 0`` (back-compat default)
  - ``armor:<non-int>``  → ``InvalidVesselTagsError`` (fail loud, no silent 0)
  - negative armor       → ``InvalidVesselTagsError``
  - duplicate ``armor``  → ``InvalidVesselTagsError`` (matches composure dup guard)
"""

from __future__ import annotations

import pytest

from sidequest.game.vessel_tags import InvalidVesselTagsError, parse_vessel_tags


def _vessel_item(*extra_tags: str, item_id: str = "rig_tier_2_road_captain") -> dict:
    """A minimally-valid vessel item dict carrying composure + extra tags."""
    return {
        "id": item_id,
        # 86-5: speed + mount_slots are now required (full stat block).
        "tags": [
            "vessel",
            "rig",
            "composure:6",
            "composure_max:6",
            "speed:4",
            "mount_slots:2",
            *extra_tags,
        ],
    }


def test_armor_tag_is_parsed() -> None:
    """A vessel with ``armor:3`` parses to ``VesselTags.armor == 3`` — the
    rating CWN subtracts from every incoming hit (§2.4.8)."""
    tags = parse_vessel_tags(_vessel_item("armor:3"))
    assert tags.armor == 3


def test_armor_defaults_to_zero_when_tag_absent() -> None:
    """A composure-only legacy vessel (no ``armor`` tag) must still parse,
    with ``armor`` defaulting to 0 — Story 53-2 items predate the tag and
    must keep loading (back-compat, not a silent fallback: explicit 0)."""
    tags = parse_vessel_tags(_vessel_item())
    assert tags.armor == 0


def test_zero_armor_tag_parses_to_zero() -> None:
    """An explicit ``armor:0`` (road_warrior tier-1 rigs ship this) parses
    to 0 — distinct from 'absent' but mechanically equal."""
    tags = parse_vessel_tags(_vessel_item("armor:0"))
    assert tags.armor == 0


def test_non_integer_armor_fails_loud() -> None:
    """``armor:heavy`` is a content bug; the parser must raise rather than
    silently treat it as 0 (No Silent Fallbacks)."""
    with pytest.raises(InvalidVesselTagsError) as exc:
        parse_vessel_tags(_vessel_item("armor:heavy"))
    assert exc.value.item_id == "rig_tier_2_road_captain"


def test_negative_armor_fails_loud() -> None:
    """Armor cannot be negative — a negative rating would *add* damage on
    reduction. Fail loud at parse time."""
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item("armor:-2"))


def test_duplicate_armor_tag_fails_loud() -> None:
    """Two ``armor:N`` tags is ambiguous content — match the existing
    duplicate-composure guard and raise rather than last-wins."""
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item("armor:1", "armor:4"))
