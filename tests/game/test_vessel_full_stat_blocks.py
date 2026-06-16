"""Full vessel stat blocks — VesselTags parses ``speed`` and ``mount_slots``
(Story 86-5, Plan 5: content remap + calibration).

Story 53-2 shipped composure-only parsing; 86-2 added ``armor``. ``speed:N`` and
``mount_slots:N`` were authored on every rig in ``inventory.yaml`` but the parser
*intentionally ignored them* ("read by other subsystems" — the module docstring).
Plan 5 (rules.yaml line 476: "full vessel stat blocks ... are 86-5") promotes them
to first-class parsed fields so the calibration layer and the mount_slot → CWN
weapon remap have real numbers to work with.

These are RED until Dev extends ``VesselTags`` / ``parse_vessel_tags`` in
``sidequest/game/vessel_tags.py``:
  * add ``speed: int`` and ``mount_slots: int`` to the model;
  * parse the ``speed:N`` / ``mount_slots:N`` tags (today they fall through the
    ``for tag in tags`` loop unmatched);
  * fail loud on malformed / negative values per "No Silent Fallbacks" — exactly
    as composure/armor already do (:class:`InvalidVesselTagsError`).

The "full stat block" reading of AC1 is deliberate: a rig that moves and mounts
weapons MUST declare speed and mount_slots. A vessel missing either is a content
bug and must fail loud, not silently default — the same contract the parser
already enforces for ``composure``/``composure_max``.

Pattern precedent: ``tests/game/test_rig_armor_reduction.py`` (the 86-2 armor-tag
parse + fail-loud tests this file mirrors for speed/mount_slots).
"""

from __future__ import annotations

import pytest

from sidequest.game.vessel_tags import (
    InvalidVesselTagsError,
    parse_vessel_tags,
)
from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path


def _vessel_item(
    *,
    item_id: str = "rig_tier_1_prospect",
    composure: int = 4,
    composure_max: int = 4,
    armor: int = 0,
    speed: int | None = 3,
    mount_slots: int | None = 1,
    extra_tags: list[str] | None = None,
) -> dict:
    """A vessel inventory item dict with a full stat block in its tags.

    ``speed`` / ``mount_slots`` of ``None`` omit that tag entirely so the
    fail-loud-on-missing tests can exercise the omission path.
    """
    tags = [
        "vessel",
        "rig",
        f"composure:{composure}",
        f"composure_max:{composure_max}",
        f"armor:{armor}",
    ]
    if speed is not None:
        tags.append(f"speed:{speed}")
    if mount_slots is not None:
        tags.append(f"mount_slots:{mount_slots}")
    if extra_tags:
        tags.extend(extra_tags)
    return {"id": item_id, "name": "Test Rig", "category": "vessel", "tags": tags}


# ---------------------------------------------------------------------------
# AC1 — speed and mount_slots are parsed into the typed VesselTags
# ---------------------------------------------------------------------------


def test_parse_vessel_tags_extracts_speed() -> None:
    """``speed:N`` lands on ``VesselTags.speed`` (today the tag is ignored)."""
    tags = parse_vessel_tags(_vessel_item(speed=5))
    assert tags.speed == 5, (
        "VesselTags must parse the speed:N tag into a first-class .speed field "
        "(Story 86-5 full stat block); it is silently ignored today"
    )


def test_parse_vessel_tags_extracts_mount_slots() -> None:
    """``mount_slots:N`` lands on ``VesselTags.mount_slots``."""
    tags = parse_vessel_tags(_vessel_item(mount_slots=3))
    assert tags.mount_slots == 3, (
        "VesselTags must parse the mount_slots:N tag into a first-class "
        ".mount_slots field (Story 86-5); it is silently ignored today"
    )


# ---------------------------------------------------------------------------
# AC1 — fail loud (No Silent Fallbacks) on malformed / missing / negative
# ---------------------------------------------------------------------------


def test_non_integer_speed_fails_loud() -> None:
    """A non-integer ``speed`` value raises, never silently drops the tag."""
    item = _vessel_item()
    item["tags"] = [t for t in item["tags"] if not t.startswith("speed:")]
    item["tags"].append("speed:fast")
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(item)


def test_non_integer_mount_slots_fails_loud() -> None:
    """A non-integer ``mount_slots`` value raises."""
    item = _vessel_item()
    item["tags"] = [t for t in item["tags"] if not t.startswith("mount_slots:")]
    item["tags"].append("mount_slots:lots")
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(item)


def test_negative_speed_fails_loud() -> None:
    """Speed cannot be negative — a parked rig is speed 0, never -1."""
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item(speed=-1))


def test_negative_mount_slots_fails_loud() -> None:
    """Mount slots cannot be negative."""
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item(mount_slots=-1))


def test_missing_speed_fails_loud() -> None:
    """A vessel with no ``speed:N`` tag is an incomplete stat block → fail loud.

    'Full vessel stat blocks' (rules.yaml line 476) means speed is mandatory on a
    rig, not an optional extra that silently defaults. Mirrors the existing
    required-field contract for composure/composure_max.
    """
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item(speed=None))


def test_missing_mount_slots_fails_loud() -> None:
    """A vessel with no ``mount_slots:N`` tag is an incomplete stat block."""
    with pytest.raises(InvalidVesselTagsError):
        parse_vessel_tags(_vessel_item(mount_slots=None))


def test_zero_mount_slots_is_valid() -> None:
    """A stripped rig with no hardpoints is legal (0 slots) — the lower bound is
    >= 0, not >= 1. Guards against an over-strict ``> 0`` rule that would reject a
    legitimately weaponless chassis."""
    tags = parse_vessel_tags(_vessel_item(mount_slots=0))
    assert tags.mount_slots == 0


# ---------------------------------------------------------------------------
# Wiring — the REAL road_warrior rigs parse to a full stat block
# ---------------------------------------------------------------------------


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _road_warrior_vessel_items() -> list[dict]:
    """Load the real road_warrior rig vessels and return their raw item dicts.

    Reads the YAML directly (not through the typed model) because the parser
    contract is dict-in — this is exactly the shape the chargen loadout flow
    hands to ``parse_vessel_tags``.

    Epic 120 (story 120-2): the bespoke rig vessels have no CWN SRD analog and were
    relocated off the now-100%-CWN-verbatim genre baseline to the_circuit's world
    inventory (ADR-145 D3). That world file is where they ship and where chargen
    resolves them (world-replaces-genre, ADR-140), so read it here.
    """
    import yaml

    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    inv_path = find_pack_path("road_warrior") / "worlds" / "the_circuit" / "inventory.yaml"
    catalog = yaml.safe_load(inv_path.read_text())["item_catalog"]
    vessels = [it for it in catalog if "vessel" in (it.get("tags") or [])]
    assert vessels, "road_warrior/the_circuit must ship at least one vessel item"
    return vessels


def test_real_road_warrior_rigs_expose_full_stat_block() -> None:
    """Every shipped road_warrior rig parses to a full stat block with a positive
    speed and at least one mount slot — the content side of AC1 / AC2 calibration.
    Routes through the production parser so it is RED until speed/mount_slots are
    first-class."""
    for item in _road_warrior_vessel_items():
        parsed = parse_vessel_tags(item)
        assert parsed.speed > 0, f"{item['id']} must declare a positive speed"
        assert parsed.mount_slots >= 1, (
            f"{item['id']} must declare at least one mount slot (every tier ships "
            f"1..5 per the rig_composure_spec table)"
        )
