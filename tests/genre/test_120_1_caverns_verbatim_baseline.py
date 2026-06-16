"""Story 120-1 (RED) — caverns_and_claudes (WWN) genre baseline goes 100% verbatim.

ADR-145 D3 + epic 120: an SRD-bound pack's genre-tier item catalog is the rulebook,
so every genre item must be ``mode=verbatim`` sourced from the WWN SRD. caverns_and_claudes
today carries 31 UNPROVENANCED legacy items (no ``provenance`` block at all) alongside 68
already-verbatim WWN items. This story sources each unprovenanced item verbatim at the genre
tier where a WWN analog exists, and MOVES genuinely-unique items (plot devices with no WWN
analog) to the world tier as ``mode=bespoke`` (the mutant_wasteland precedent).

Target pattern: elemental_harmony + heavy_metal, both already 100% WWN-verbatim.

Driven against the REAL pack through ``load_genre_pack`` / ``resolve_inventory`` so green
proves the production content complies, not a fixture. This story does NOT touch the validator
(that is 120-3); these tests assert the *content* reaches the verbatim baseline and that the
chargen kits still resolve through the world-replaces-genre trap (ADR-140).

RED today:
  * ``test_caverns_genre_baseline_has_no_unprovenanced_items`` — the 31 legacy items.
  * ``test_caverns_genre_baseline_is_fully_wwn_verbatim`` — same 31 (plus shape checks).
GREEN-guard (must not regress through the migration):
  * ``test_caverns_pack_loads_clean`` — the real loader gate still accepts the pack.
  * ``test_caverns_genre_baseline_carries_no_bespoke`` — moved items go to the WORLD tier,
    never marked bespoke at the genre tier (would trip the 114-14 validator anyway).
  * ``test_caverns_kits_resolve_no_dangling_ids`` — renaming/moving items must not leave a
    starting_equipment id that no longer resolves (empty-kit trap).
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

SLUG = "caverns_and_claudes"


def _load():
    try:
        return load_genre_pack(find_pack_path(SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def test_caverns_pack_loads_clean() -> None:
    """Wiring gate: the real loader (which runs the 114-14 genre-baseline validator)
    accepts caverns_and_claudes and it ships a genre-tier inventory. Guards that the
    verbatim migration / world-tier moves don't break the load (e.g. a world bespoke
    item is legal; a genre bespoke item is not)."""
    pack = _load()
    assert pack.inventory is not None, f"{SLUG} must ship a genre-tier inventory.yaml"
    assert pack.inventory.item_catalog, f"{SLUG} genre item_catalog must be non-empty"


def test_caverns_genre_baseline_has_no_unprovenanced_items() -> None:
    """RED: today 31 legacy caverns items carry NO ``provenance`` block. After the sweep
    every item REMAINING at the genre tier must carry provenance (the no-analog plot
    devices are moved OFF the genre tier to the world tier, not left unprovenanced)."""
    pack = _load()
    assert pack.inventory is not None
    unprovenanced = [item.id for item in pack.inventory.item_catalog if item.provenance is None]
    assert not unprovenanced, (
        f"{SLUG} genre baseline must carry NO unprovenanced items (ADR-145 D3): every "
        f"genre item is SRD-sourced verbatim or moved to the world tier. "
        f"{len(unprovenanced)} unprovenanced: {unprovenanced}"
    )


def test_caverns_genre_baseline_is_fully_wwn_verbatim() -> None:
    """RED: the genre baseline must be 100% WWN-verbatim — every item ``mode=verbatim``,
    ``srd=wwn``, ``license=wn-free``, with a non-empty ``srd_ref`` that cites the WWN SRD.
    Mirrors the elemental_harmony / heavy_metal target pattern."""
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
        if prov.srd != "wwn":
            offenders.append(f"{item.id}: srd={prov.srd!r} (want 'wwn')")
        if prov.license != "wn-free":
            offenders.append(f"{item.id}: license={prov.license!r} (want 'wn-free')")
        if not (prov.srd_ref and "wwn" in prov.srd_ref.lower()):
            offenders.append(f"{item.id}: srd_ref={prov.srd_ref!r} (must cite the WWN SRD)")
    assert not offenders, (
        f"{SLUG} genre baseline must be 100% WWN-verbatim ({len(offenders)} offenders): "
        + "; ".join(offenders)
    )


def test_caverns_genre_baseline_carries_no_bespoke() -> None:
    """Guard (GREEN now, must stay GREEN): genuinely-unique plot devices move to the WORLD
    tier as ``mode=bespoke`` — the genre tier must never carry a bespoke item (ADR-145 D3;
    the 114-14 validator would reject the load anyway). Catches a wrong-direction migration
    that marks plot devices bespoke at the genre tier instead of relocating them."""
    pack = _load()
    assert pack.inventory is not None
    bespoke = [
        item.id
        for item in pack.inventory.item_catalog
        if item.provenance is not None and item.provenance.mode == "bespoke"
    ]
    assert not bespoke, (
        f"{SLUG} genre baseline must carry NO mode=bespoke items — relocate plot devices to "
        f"worlds/<world>/inventory.yaml (ADR-145 D3). Found: {bespoke}"
    )


def test_caverns_kits_resolve_no_dangling_ids() -> None:
    """Kit-trap guard (ADR-140), GREEN now and must stay GREEN: the verbatim sweep renames
    legacy ids (e.g. ``torch`` -> ``wwn_torch``) and may move items to the world tier. For
    EVERY caverns world (and the pure genre baseline), ``resolve_inventory`` must produce a
    catalog in which every ``starting_equipment`` id resolves, and each declared class kit
    is non-empty. A world inventory.yaml that drops starting_equipment/gold/currency, or a
    rename that misses starting_equipment, ships empty/broken kits — this catches both."""
    pack = _load()
    # None == pure genre baseline; each world slug exercises the world-replaces-genre merge.
    world_slugs: list[str | None] = [None, *pack.worlds.keys()]
    for world_slug in world_slugs:
        resolved = resolve_inventory(pack, world_slug)
        label = world_slug or "<genre-baseline>"
        assert resolved is not None, f"{SLUG}/{label}: resolve_inventory must return a config"
        catalog_ids = {item.id for item in resolved.item_catalog}
        for klass, ids in resolved.starting_equipment.items():
            assert ids, f"{SLUG}/{label}: class {klass!r} ships an EMPTY starting kit"
            dangling = [item_id for item_id in ids if item_id not in catalog_ids]
            assert not dangling, (
                f"{SLUG}/{label}: class {klass!r} kit references ids that do not resolve in the "
                f"merged catalog (rename/move left them dangling): {dangling}"
            )
