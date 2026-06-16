"""Story 120-2 (RED) — road_warrior (CWN) genre baseline goes fully verbatim.

ADR-145 D3 + epic 120: an SRD-bound pack's genre-tier item catalog is the rulebook,
so every genre item must be ``mode=verbatim`` sourced from the CWN SRD. road_warrior
today carries 25 UNPROVENANCED legacy items (no ``provenance`` block at all) — rig parts,
mount weapons, survival gear, and the five rig-tier vessels — alongside 66 already-verbatim
CWN items (the 114-5 extraction). This story sources each unprovenanced item verbatim at the
genre tier where a CWN analog exists, and MOVES genuinely-unique items with NO CWN analog
(the rig-tier vessels, road-warrior-specific gear) to the world tier
(``worlds/the_circuit/inventory.yaml``, the file 114-14 created) as honest ``mode=bespoke``.

Sibling of 120-1 (caverns_and_claudes / WWN); target pattern: elemental_harmony + heavy_metal,
both already 100% WWN-verbatim. Driven against the REAL pack through ``load_genre_pack`` /
``resolve_inventory`` so green proves the production content complies, not a fixture. This
story does NOT touch the validator (that is 120-3); these tests assert the *content* reaches
the verbatim baseline and that the chargen kits still resolve through the world-replaces-genre
trap (ADR-140).

RED today:
  * ``test_road_warrior_genre_baseline_has_no_unprovenanced_items`` — the 25 legacy items.
  * ``test_road_warrior_genre_baseline_is_fully_cwn_verbatim`` — same 25 (plus shape checks).
GREEN-guard (must not regress through the migration):
  * ``test_road_warrior_pack_loads_clean`` — the real loader gate still accepts the pack.
  * ``test_road_warrior_genre_baseline_carries_no_bespoke`` — moved items go to the WORLD
    tier, never marked bespoke at the genre tier (would trip the 114-14 validator anyway).
  * ``test_road_warrior_circuit_kits_resolve_no_dangling_ids`` — the kit trap. EVERY rig-tier
    vessel that moves to the world tier (notably ``rig_tier_1_prospect``, referenced by every
    class kit) must be re-added to the world catalog, and any verbatim rename must update
    ``starting_equipment`` — otherwise the kit ships a dangling id.
  * ``test_road_warrior_circuit_world_items_are_provenanced`` — the moved uniques land as
    honest ``mode=bespoke`` (provenanced), not left unprovenanced at the world tier.

DEVIATION from the 120-1 template (logged in the session under TEA): the kit-trap test here
is scoped to road_warrior's WORLDS only and deliberately EXCLUDES the bare genre baseline
(``world_slug=None``). 114-14 moved road_warrior's personal weapons (pistol/tire_iron/chain/
sawed_off_shotgun) to the_circuit, but the genre-tier ``starting_equipment`` still references
them, so ``resolve_inventory(pack, None)`` already dangles those four ids — a pre-existing
114-14 condition unrelated to this sweep. road_warrior is played via the_circuit; that merged
catalog is the real chargen path and is the one this guard pins. (See the 120-1 caverns test
for the ``[None, *worlds]`` form, which is correct there because caverns' genre baseline is
self-contained.)
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

SLUG = "road_warrior"


def _load():
    try:
        return load_genre_pack(find_pack_path(SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def test_road_warrior_pack_loads_clean() -> None:
    """Wiring gate: the real loader (which runs the 114-14 genre-baseline validator)
    accepts road_warrior and it ships a genre-tier inventory. Guards that the verbatim
    migration / world-tier moves don't break the load (e.g. a world bespoke item is legal;
    a genre bespoke item is not)."""
    pack = _load()
    assert pack.inventory is not None, f"{SLUG} must ship a genre-tier inventory.yaml"
    assert pack.inventory.item_catalog, f"{SLUG} genre item_catalog must be non-empty"


def test_road_warrior_genre_baseline_has_no_unprovenanced_items() -> None:
    """RED: today 25 legacy road_warrior items (rig parts, mount weapons, survival gear,
    rig-tier vessels) carry NO ``provenance`` block. After the sweep every item REMAINING at
    the genre tier must carry provenance (the no-analog vessels/gear are moved OFF the genre
    tier to the world tier, not left unprovenanced)."""
    pack = _load()
    assert pack.inventory is not None
    unprovenanced = [item.id for item in pack.inventory.item_catalog if item.provenance is None]
    assert not unprovenanced, (
        f"{SLUG} genre baseline must carry NO unprovenanced items (ADR-145 D3): every "
        f"genre item is SRD-sourced verbatim or moved to the world tier. "
        f"{len(unprovenanced)} unprovenanced: {unprovenanced}"
    )


def test_road_warrior_genre_baseline_is_fully_cwn_verbatim() -> None:
    """RED: the genre baseline must be 100% CWN-verbatim — every item ``mode=verbatim``,
    ``srd=cwn``, ``license=wn-free``, with a non-empty ``srd_ref`` that cites the CWN SRD.
    Mirrors the elemental_harmony / heavy_metal target pattern (WWN there, CWN here)."""
    pack = _load()
    assert pack.inventory is not None
    offenders: list[str] = []
    for item in pack.inventory.item_catalog:
        prov = item.provenance
        if prov is None:
            offenders.append(f"{item.id}: unprovenanced")
            continue
        if prov.mode != "verbatim":
            offenders.append(f"{item.id}: mode={prov.mode!r} (want 'verbatim')")
            continue
        if prov.srd != "cwn":
            offenders.append(f"{item.id}: srd={prov.srd!r} (want 'cwn')")
        if prov.license != "wn-free":
            offenders.append(f"{item.id}: license={prov.license!r} (want 'wn-free')")
        if not (prov.srd_ref and "cwn" in prov.srd_ref.lower()):
            offenders.append(f"{item.id}: srd_ref={prov.srd_ref!r} (must cite the CWN SRD)")
    assert not offenders, (
        f"{SLUG} genre baseline must be 100% CWN-verbatim ({len(offenders)} offenders): "
        + "; ".join(offenders)
    )


def test_road_warrior_genre_baseline_carries_no_bespoke() -> None:
    """Guard (GREEN now, must stay GREEN): genuinely-unique rig vessels / road gear move to
    the WORLD tier as ``mode=bespoke`` — the genre tier must never carry a bespoke item
    (ADR-145 D3; the 114-14 validator would reject the load anyway). Catches a wrong-direction
    migration that marks the no-analog items bespoke at the genre tier instead of relocating
    them."""
    pack = _load()
    assert pack.inventory is not None
    bespoke = [
        item.id
        for item in pack.inventory.item_catalog
        if item.provenance is not None and item.provenance.mode == "bespoke"
    ]
    assert not bespoke, (
        f"{SLUG} genre baseline must carry NO mode=bespoke items — relocate rig vessels / "
        f"road gear to worlds/<world>/inventory.yaml (ADR-145 D3). Found: {bespoke}"
    )


def test_road_warrior_circuit_kits_resolve_no_dangling_ids() -> None:
    """Kit-trap guard (ADR-140), GREEN now and must stay GREEN. The verbatim sweep renames
    legacy ids (e.g. ``medkit`` -> ``cwn_medkit``) and moves the rig-tier vessels to the world
    tier. ``rig_tier_1_prospect`` is referenced by EVERY class kit, so when it moves off the
    genre tier the_circuit must re-add it to its world catalog or every kit dangles. For EACH
    road_warrior world, ``resolve_inventory`` must produce a merged catalog in which every
    ``starting_equipment`` id resolves and each declared class kit is non-empty.

    Scoped to worlds (not the bare genre baseline): road_warrior's genre-tier kits reference
    the_circuit-owned weapons that 114-14 moved to the world tier, so the pure genre baseline
    legitimately dangles those — see this module's docstring. road_warrior is played via a
    world; that merged catalog is the real chargen path."""
    pack = _load()
    assert pack.worlds, f"{SLUG} must ship at least one world for chargen to resolve through"
    for world_slug in pack.worlds:
        resolved = resolve_inventory(pack, world_slug)
        assert resolved is not None, f"{SLUG}/{world_slug}: resolve_inventory must return a config"
        catalog_ids = {item.id for item in resolved.item_catalog}
        assert resolved.starting_equipment, (
            f"{SLUG}/{world_slug}: resolve_inventory produced NO class kits — the loop "
            f"below would pass vacuously with zero kits to check"
        )
        for klass, ids in resolved.starting_equipment.items():
            assert ids, f"{SLUG}/{world_slug}: class {klass!r} ships an EMPTY starting kit"
            dangling = [item_id for item_id in ids if item_id not in catalog_ids]
            assert not dangling, (
                f"{SLUG}/{world_slug}: class {klass!r} kit references ids that do not resolve "
                f"in the merged catalog (rename/move left them dangling): {dangling}"
            )


def test_road_warrior_circuit_world_items_are_provenanced() -> None:
    """Honest-bespoke guard (GREEN now, must stay GREEN): the rig-tier vessels and road gear
    with no CWN analog MOVE to the world tier as honest ``mode=bespoke`` (story 120-2). A world
    item left UNPROVENANCED would be a dishonest move — the item escaped the genre-tier
    verbatim sweep by relocating without declaring where its mechanics came from. Every item a
    road_warrior world declares in its own inventory catalog must carry a provenance block."""
    pack = _load()
    offenders: list[str] = []
    for world_slug, world in pack.worlds.items():
        if world.inventory is None:
            continue
        for item in world.inventory.item_catalog:
            if item.provenance is None:
                offenders.append(f"{world_slug}/{item.id}")
    assert not offenders, (
        f"{SLUG} world-tier inventory items must be provenanced — moved no-analog items land "
        f"as honest mode=bespoke, never unprovenanced (story 120-2). Unprovenanced: {offenders}"
    )
