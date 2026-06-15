"""Story 114-7 (RED) — space_opera inventory is SWN-SRD-sourced, de-triplicated, TL-tagged.

space_opera binds SWN (`pack.rules.ruleset == "swn"`). The 2026-06-14 inventory
audit found the worst duplication of any pack: the three worlds — aureate_span,
coyote_star, perseus_cloud — ship **byte-identical** `inventory.yaml` files
(md5 ``80a030e3…``, 278 lines each), with **zero** world-distinct gear, **zero**
``tech_level`` tags, and a hand-authored bespoke catalog (blaster_sidearm,
vibroblade, …) carrying no ``provenance`` at all.

This story applies the ADR-145 D3 fix already shipped for the WWN/CWN/AWN siblings
(114-4 / 114-5 / 114-8): hoist a single SWN **genre-tier baseline** catalog that the
three worlds inherit, leaving each world a thin, distinct override.

**LICENSING — the load-bearing correction.** The story context (written from the
2026-06-14 audit) frames SWN as *derive-only*. ADR-145 D4 **explicitly supersedes
that audit read**: Sine Nomine's standing Without Number free-use policy covers
**all four** WN SRDs — WWN, CWN, SWN, AWN — and every one resolves to *reproduce
verbatim* (`mode=verbatim`, `srd=swn`, `license=wn-free`). The `derived` mode in the
schema is reserved for a *future* non-WN ruleset, NOT the WN line (ADR-145 lines
152-153, 189-190, 329; the AWN precedent test ``test_114_8_…`` says the same in its
header). These tests therefore pin **verbatim**, matching the three sibling stories
and the code-enforced ``_VERBATIM_LICENSES`` / D3 loader validator — see the
session Delivery Findings + Design Deviations for the conflict log.

What's broken today (the RED state these tests pin):
  * No genre-tier ``inventory.yaml`` exists at all → ``pack.inventory is None``
    (the shared SWN core has never been hoisted).
  * The three world catalogs are identical (no de-triplication, no distinct gear).
  * No catalog item carries ``provenance`` or ``tech_level``.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.inventory_resolve import resolve_inventory
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_PACK_SLUG = "space_opera"
_WORLDS = ("aureate_span", "coyote_star", "perseus_cloud")


def _load_pack():
    try:
        return load_genre_pack(find_pack_path(_PACK_SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def _genre_catalog(pack):
    assert pack.inventory is not None, (
        "space_opera must ship a genre-tier SWN baseline inventory.yaml — the hoisted "
        "shared core every world inherits (ADR-145 D3); today there is none"
    )
    return pack.inventory.item_catalog


def _is_bespoke(item) -> bool:
    """A genuinely-invented (non-SRD) item — exempt from the SWN schema asserts."""
    return item.provenance is not None and item.provenance.mode == "bespoke"


def _world_inventory(pack, slug):
    world = pack.worlds.get(slug)
    return world.inventory if world is not None else None


def _world_ids(pack, slug) -> set[str]:
    inv = _world_inventory(pack, slug)
    return {item.id for item in inv.item_catalog} if inv is not None else set()


# ---------------------------------------------------------------------------
# Guard — the pack loads and binds SWN (passes today; locks the precondition).
# ---------------------------------------------------------------------------


def test_pack_loads_clean_and_binds_swn():
    pack = _load_pack()
    assert pack.rules.ruleset == "swn", "space_opera must bind the SWN ruleset"


# ---------------------------------------------------------------------------
# Clause 2 — hoist the shared SWN core to a genre-tier baseline (ADR-145 D3).
# ---------------------------------------------------------------------------


def test_genre_tier_swn_baseline_exists():
    """RED: today space_opera has no genre-tier inventory.yaml — ``pack.inventory``
    is None. The shared SWN core must be hoisted to the genre tier so the three
    worlds inherit it (ADR-145 D3; the same hoist 114-4/114-5/114-8 performed)."""
    pack = _load_pack()
    catalog = _genre_catalog(pack)
    assert catalog, "the genre baseline must carry a non-empty item_catalog (the SWN core)"


# ---------------------------------------------------------------------------
# ADR-145 D2 — every genre-tier baseline item carries provenance.
# ---------------------------------------------------------------------------


def test_every_genre_baseline_item_carries_provenance():
    """RED: today not one space_opera item is provenance-stamped. ADR-145 D2 requires
    every genre-tier baseline item to carry a provenance record (data, not a YAML
    comment) so the licensing audit / GM panel can read it."""
    pack = _load_pack()
    unstamped = [i.id for i in _genre_catalog(pack) if i.provenance is None]
    assert not unstamped, (
        "every genre-tier space_opera item must carry provenance (ADR-145 D2); "
        f"unstamped: {unstamped}"
    )


# ---------------------------------------------------------------------------
# ADR-145 D4 — the SWN baseline is VERBATIM, not derived (the conflict pin).
# ---------------------------------------------------------------------------


def test_genre_baseline_is_swn_verbatim_sourced():
    """RED: the baseline must be SWN-SRD-sourced verbatim. ADR-145 D4 settles that
    SWN (like all four WN SRDs) reproduces verbatim under wn-free terms — NOT the
    audit's superseded 'derive-only' read. At least the standard sci-fi gear should
    be ``mode=verbatim, srd=swn``. Today zero items are verbatim (catalog is bespoke
    flavor with no provenance)."""
    pack = _load_pack()
    verbatim_swn = [
        i.id
        for i in _genre_catalog(pack)
        if i.provenance is not None
        and i.provenance.mode == "verbatim"
        and i.provenance.srd == "swn"
    ]
    assert verbatim_swn, (
        "space_opera's genre baseline must be SWN-SRD-sourced verbatim "
        "(>=1 item with provenance mode=verbatim, srd=swn); the catalog is bespoke"
    )


def test_no_genre_baseline_item_uses_derived_mode():
    """RED-correctness guard against implementing the superseded audit read. ADR-145
    D4: the WN line (incl. SWN) reproduces *verbatim*; ``derived`` is reserved for a
    future non-WN ruleset whose license forbids verbatim reuse. A genre-tier SWN item
    stamped ``mode=derived`` would be the audit's stale framing — reject it."""
    pack = _load_pack()
    derived = [
        i.id
        for i in _genre_catalog(pack)
        if i.provenance is not None and i.provenance.mode == "derived"
    ]
    assert not derived, (
        "SWN is verbatim-reproducible under wn-free terms (ADR-145 D4); a genre-tier "
        f"item stamped mode=derived applies the superseded 'derive-only' audit read: {derived}"
    )


def test_genre_baseline_has_no_bespoke_items():
    """ADR-145 D3 / D3 loader validator: a WN pack's genre catalog IS the SRD rulebook
    — bespoke (invented) gear is a world-tier privilege. (``load_genre_pack`` already
    raises ``PackError`` on a genre-tier bespoke item; this asserts the contract
    explicitly so a regression names the offender.)"""
    pack = _load_pack()
    bespoke = [i.id for i in _genre_catalog(pack) if _is_bespoke(i)]
    assert not bespoke, (
        "genre-tier baseline must carry no bespoke items — move invented gear to the "
        f"world tier (ADR-145 D3); offenders: {bespoke}"
    )


def test_verbatim_items_are_swn_wn_free_with_srd_ref():
    """Correctness contract on whatever IS stamped verbatim: a verbatim item must name
    srd=swn, license=wn-free, and cite the SWN SRD in srd_ref (ADR-145 D2/D4)."""
    pack = _load_pack()
    for item in _genre_catalog(pack):
        prov = item.provenance
        if prov is None or prov.mode != "verbatim":
            continue
        assert prov.srd == "swn", f"{item.id}: verbatim item must declare srd=swn"
        assert prov.license == "wn-free", f"{item.id}: SWN verbatim license must be wn-free"
        assert prov.srd_ref and "swn" in prov.srd_ref.lower(), (
            f"{item.id}: srd_ref must cite the SWN SRD, got {prov.srd_ref!r}"
        )


# ---------------------------------------------------------------------------
# Clause 4 — backfill SWN tech_level tags (the SRD rates gear by Tech Level).
# ---------------------------------------------------------------------------


def test_baseline_backfills_tech_level_tags():
    """RED: the title's "backfill SWN tech levels". SWN rates equipment by Tech Level;
    today zero items carry ``tech_level``. The hoisted baseline must populate it."""
    pack = _load_pack()
    tagged = [i.id for i in _genre_catalog(pack) if i.tech_level is not None]
    assert tagged, "the SWN baseline must backfill tech_level tags (clause 4); zero today"


def test_every_swn_weapon_and_armor_declares_tech_level():
    """Every SWN weapon and armor entry lists a Tech Level in the SRD; a non-bespoke
    weapon/armor missing ``tech_level`` is an incomplete SWN record."""
    pack = _load_pack()
    missing = [
        i.id
        for i in _genre_catalog(pack)
        if i.category in ("weapon", "armor") and not _is_bespoke(i) and i.tech_level is None
    ]
    assert not missing, (
        f"every non-bespoke SWN weapon/armor must carry a tech_level (SRD TL); missing: {missing}"
    )


# ---------------------------------------------------------------------------
# Clause 1 — de-triplicate: the three world catalogs are no longer identical.
# ---------------------------------------------------------------------------


def test_world_catalogs_are_no_longer_triplicated():
    """RED: the three world inventory.yaml are byte-identical today (md5 80a030e3).
    After de-triplication each world ships a DISTINCT thin override over the shared
    genre baseline — the three world-authored id sets must not all be identical."""
    pack = _load_pack()
    id_sets = {slug: tuple(sorted(_world_ids(pack, slug))) for slug in _WORLDS}
    distinct = set(id_sets.values())
    assert len(distinct) > 1, (
        "the three space_opera world catalogs must not be identical after de-triplication "
        f"(clause 1); got identical world-authored id sets: {id_sets}"
    )


# ---------------------------------------------------------------------------
# Clause 3 — each world ships its own world-distinct gear.
# ---------------------------------------------------------------------------


def test_each_world_has_distinct_gear():
    """RED: today every world has zero world-specific gear (all three identical). Each
    world must contribute >=1 item id NOT present in the other two worlds — its own
    flavor gear layered over the shared SWN core."""
    pack = _load_pack()
    per_world = {slug: _world_ids(pack, slug) for slug in _WORLDS}
    for slug in _WORLDS:
        others: set[str] = set().union(*(per_world[o] for o in _WORLDS if o != slug))
        own = per_world[slug] - others
        assert own, (
            f"world {slug!r} must ship >=1 distinct item not in the other worlds "
            f"(clause 3, world-distinct gear); got world ids {sorted(per_world[slug])}"
        )


# ---------------------------------------------------------------------------
# WIRING — the hoisted baseline is non-droppable and actually reaches each world.
# ---------------------------------------------------------------------------


def test_resolve_inventory_merges_swn_baseline_into_each_world():
    """WIRING: the genre baseline is non-droppable (ADR-145 D3). The resolved catalog
    each world's chargen/loadout actually reads (``resolve_inventory``) must union the
    SWN genre baseline ids with the world's distinct gear. Proves the baseline is wired
    into the resolver, not merely sitting in a YAML file."""
    pack = _load_pack()
    baseline_ids = {i.id for i in _genre_catalog(pack)}
    assert baseline_ids, "precondition: a non-empty SWN genre baseline must exist"
    for slug in _WORLDS:
        resolved = resolve_inventory(pack, slug)
        assert resolved is not None, f"resolve_inventory must return a catalog for world {slug!r}"
        resolved_ids = {i.id for i in resolved.item_catalog}
        missing = baseline_ids - resolved_ids
        assert not missing, (
            f"world {slug!r} resolved catalog must include the non-droppable SWN baseline "
            f"(genre baseline ∪ world override); missing baseline ids: {sorted(missing)}"
        )
