"""road_warrior vessel calibration + mount_slot → CWN weapon remap (Story 86-5,
Plan 5 — the epic's final integration gate).

rules.yaml line 476 scopes this story explicitly: "full vessel stat blocks,
mount_slot → CWN weapon remap, and lethality calibration are 86-5". This file is
the calibration guard for that scope, loaded against the REAL road_warrior pack:

  * AC2 — every shipped rig parses cleanly and its stat block is *calibrated*:
    monotonic across tiers and matching the canonical ``rig_composure_spec`` table
    in rules.yaml (composure_max 4/6/8/10/12, mount_slots 1/2/3/4/5).
  * AC1 — the mounted rig weapons get real CWN vehicle-weapon damage (the
    "mount_slot → CWN weapon remap"). Before Plan 5 every ``mounted``+``rig``
    weapon shipped ``damage: None`` (inventory.yaml comment: "Rig/mounted weapons
    ... stay damage-less — rig combat is Plan 2"); Plan 5 gave them mechanical
    backing so a gunner manning a mount slot rolls real damage instead of
    improvised prose.

GREEN as of 86-5 / 120-2: speed/mount_slots are first-class parsed fields
(``vessel_tags.py``, cf. ``test_vessel_full_stat_blocks.py``) and every mounted rig
weapon now lives in ``worlds/the_circuit/inventory.yaml`` with a CWN ``damage`` block.

The damage assertions are deliberately *structural* — they require a well-formed
``damage`` block with NdM dice, never a specific die size. Per the design spec
("faithful SRD port, do not redesign", D4) the exact vehicle-weapon numbers are
Keith's crunch call; the test enforces that the backing EXISTS, not what it is.

Pattern precedent: ``tests/genre/test_road_warrior_loads_cwn.py`` (real-pack load
+ structured-field assertions, never prose-grep).
"""

from __future__ import annotations

import pytest
import yaml

from sidequest.game.vessel_tags import parse_vessel_tags
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path
from tests.genre.test_resolution_mode import load_pack

# Canonical rig_composure_spec table (rules.yaml §rig_composure_spec):
#   tier: (composure_max, mount_slots)
SPEC_TABLE = {1: (4, 1), 2: (6, 2), 3: (8, 3), 4: (10, 4), 5: (12, 5)}


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _circuit_inventory() -> dict:
    """Raw the_circuit WORLD inventory dict (item_catalog + starting_equipment).

    Epic 120 (story 120-2): road_warrior's bespoke rig vessels and mount weapons
    have no CWN SRD analog, so they were relocated OFF the genre-tier baseline
    (which is now 100% CWN-verbatim) to ``worlds/the_circuit/inventory.yaml`` as
    mode=bespoke (ADR-145 D3). the_circuit is road_warrior's play world, so its
    world inventory is where the vessels/mount weapons now ship and where the
    chargen kits resolve them (world-replaces-genre, ADR-140)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    inv_path = find_pack_path("road_warrior") / "worlds" / "the_circuit" / "inventory.yaml"
    return yaml.safe_load(inv_path.read_text())


def _vessel_dicts() -> list[dict]:
    catalog = _circuit_inventory()["item_catalog"]
    return [it for it in catalog if "vessel" in (it.get("tags") or [])]


def _tier_of(item: dict) -> int:
    """Extract the tier integer from a ``tier-N`` tag. Fails loud if absent —
    a vessel with no tier tag is a content bug that would make the calibration
    table assertions pass vacuously."""
    for tag in item.get("tags", []):
        if isinstance(tag, str) and tag.startswith("tier-"):
            return int(tag.split("-", 1)[1])
    raise AssertionError(f"vessel {item.get('id')!r} has no tier-N tag")


def _load_typed():
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk")
    try:
        return load_pack("road_warrior")
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


# ---------------------------------------------------------------------------
# AC2 — every vessel parses (fail-loud calibration; no silent content bug)
# ---------------------------------------------------------------------------


def test_every_vessel_item_parses_cleanly() -> None:
    """Every ``category: vessel`` item parses through the production parser with a
    full stat block. GREEN: speed/mount_slots are first-class required fields in the
    parser (Story 86-5) — a vessel missing either tag fails loud via
    ``InvalidVesselTagsError``."""
    vessels = _vessel_dicts()
    assert len(vessels) >= 5, (
        f"road_warrior ships a 5-tier rig ladder; found {len(vessels)} vessel items"
    )
    for item in vessels:
        parsed = parse_vessel_tags(item)  # raises InvalidVesselTagsError on any bug
        assert parsed.composure_max > 0
        assert parsed.speed > 0
        assert parsed.mount_slots >= 0


# ---------------------------------------------------------------------------
# AC2 — stat block is monotonic across the tier ladder
# ---------------------------------------------------------------------------


def test_vessel_stats_are_monotonic_across_tiers() -> None:
    """Higher tier ⇒ a strictly better hull and never-worse speed/armor/mounts.

    A non-monotonic ladder (tier 3 slower than tier 2, say) is a calibration
    error that makes advancement feel broken to a mechanics-first player. Routes
    every value through the production parser."""
    by_tier = sorted(_vessel_dicts(), key=_tier_of)
    tiers = [_tier_of(v) for v in by_tier]
    assert tiers == sorted(set(tiers)), f"tier tags must be unique + ordered; got {tiers}"

    prev = None
    for item in by_tier:
        cur = parse_vessel_tags(item)
        if prev is not None:
            assert cur.composure_max > prev.composure_max, (
                f"composure_max must strictly increase by tier; {item['id']} "
                f"({cur.composure_max}) <= previous ({prev.composure_max})"
            )
            assert cur.speed >= prev.speed, f"{item['id']} speed regressed vs lower tier"
            assert cur.armor >= prev.armor, f"{item['id']} armor regressed vs lower tier"
            assert cur.mount_slots >= prev.mount_slots, (
                f"{item['id']} mount_slots regressed vs lower tier"
            )
        prev = cur


def test_vessel_composure_and_mount_slots_match_spec_table() -> None:
    """Each tier's parsed composure_max + mount_slots equals the canonical
    rig_composure_spec table — the content must not drift from the documented
    progression the narrator and UI quote to the player."""
    for item in _vessel_dicts():
        tier = _tier_of(item)
        assert tier in SPEC_TABLE, f"unexpected rig tier {tier} on {item['id']}"
        parsed = parse_vessel_tags(item)
        want_composure, want_slots = SPEC_TABLE[tier]
        assert parsed.composure_max == want_composure, (
            f"tier {tier} ({item['id']}) composure_max must be {want_composure} "
            f"per rig_composure_spec; got {parsed.composure_max}"
        )
        assert parsed.mount_slots == want_slots, (
            f"tier {tier} ({item['id']}) mount_slots must be {want_slots} per "
            f"rig_composure_spec; got {parsed.mount_slots}"
        )


# ---------------------------------------------------------------------------
# AC1 — mount_slot → CWN vehicle weapon remap: mounted rig weapons get damage
# ---------------------------------------------------------------------------


def _mounted_rig_weapons_typed(pack) -> list:
    # Epic 120: mount weapons are bespoke (no CWN analog) and now ship at the world
    # tier (worlds/the_circuit/inventory.yaml), not the genre baseline. Read the
    # typed the_circuit world catalog — the tier they live at post-sweep.
    world = pack.worlds.get("the_circuit")
    assert world is not None and world.inventory is not None, (
        "road_warrior/the_circuit must ship a world inventory catalog"
    )
    out = []
    for it in world.inventory.item_catalog:
        tags = set(it.tags or [])
        if it.category == "weapon" and {"mounted", "rig"} <= tags:
            out.append(it)
    return out


def test_mounted_rig_weapons_exist() -> None:
    """Guard: the pack actually ships mounted rig weapons, so the remap test below
    can't pass vacuously on an empty list."""
    pack = _load_typed()
    mounted = _mounted_rig_weapons_typed(pack)
    assert len(mounted) >= 3, (
        f"road_warrior must ship its mounted rig-weapon set (mounted_gun, "
        f"flame_rig, harpoon_gun, ...); found {[w.id for w in mounted]}"
    )


def test_mounted_rig_weapons_carry_vehicle_damage() -> None:
    """Every mounted rig weapon carries a well-formed CWN damage block — the
    mount_slot → CWN weapon remap (AC1). A gunner manning a mount slot must roll
    real damage, not improvised prose. Structural only: asserts NdM dice exist,
    never a specific die size (faithful-port decision D4 leaves the numbers to
    Keith). GREEN as of 86-5: every mounted rig weapon in the_circuit's inventory
    carries a CWN ``damage`` block."""
    pack = _load_typed()
    undamaged = [
        w.id for w in _mounted_rig_weapons_typed(pack) if w.damage is None or not w.damage.dice
    ]
    assert not undamaged, (
        f"mounted rig weapons must carry a CWN vehicle-weapon damage block "
        f"(Story 86-5 mount_slot → CWN weapon remap); still damage-less: {undamaged}"
    )


# ---------------------------------------------------------------------------
# AC1/AC2 — starting loadouts respect mount-slot capacity
# ---------------------------------------------------------------------------


def test_starting_mounted_weapons_fit_in_starting_rig_slots() -> None:
    """No class is handed more mounted rig weapons than its starting rig has slots.

    A loadout that over-fills the mount slots is an un-equippable content bug.
    Cross-references starting_equipment against the parsed mount_slots of the
    granted rig.

    Resolves against the MERGED genre+world catalog (``resolve_inventory(pack,
    "the_circuit")``), not the_circuit's world inventory alone. Post-120-2 the kits
    mix world-tier bespoke rigs/mount-weapons with genre-tier ``cwn_*`` verbatim
    gear; reading the world catalog by itself silently dropped every ``cwn_*`` id to
    ``{}`` (via ``.get(i, {})``), so this guard ran against a catalog that wasn't the
    real chargen path. The merged catalog is what chargen actually resolves
    (world-replaces-genre for kits, union for items — ADR-140 / ADR-145 D3)."""
    pack = _load_typed()
    resolved = resolve_inventory(pack, "the_circuit")
    assert resolved is not None, "road_warrior/the_circuit must resolve an inventory config"
    catalog = {it.id: it for it in resolved.item_catalog}
    starting = resolved.starting_equipment
    assert starting, "road_warrior/the_circuit must define starting_equipment"

    for class_name, item_ids in starting.items():
        missing = [i for i in item_ids if i not in catalog]
        assert not missing, (
            f"{class_name} kit references ids absent from the merged the_circuit "
            f"catalog (no silent skip): {missing}"
        )
        rig_ids = [i for i in item_ids if "vessel" in (catalog[i].tags or [])]
        assert len(rig_ids) == 1, f"{class_name} must start with exactly one rig; got {rig_ids}"
        # parse_vessel_tags() takes a plain dict (it has an isinstance(item, dict)
        # guard), so dump the typed CatalogItem to a dict. CatalogItem carries no
        # field aliases, so id/tags survive model_dump() unchanged.
        slots = parse_vessel_tags(catalog[rig_ids[0]].model_dump()).mount_slots
        mounted = [
            i
            for i in item_ids
            if {"mounted", "rig"} <= set(catalog[i].tags or []) and catalog[i].category == "weapon"
        ]
        assert len(mounted) <= slots, (
            f"{class_name} starts with {len(mounted)} mounted rig weapons {mounted} "
            f"but its rig {rig_ids[0]} has only {slots} mount slot(s)"
        )
