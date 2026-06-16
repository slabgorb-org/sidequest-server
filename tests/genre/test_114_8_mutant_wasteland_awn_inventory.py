"""Story 114-8 (RED) — mutant_wasteland inventory is AWN-SRD-sourced, not bespoke.

ADR-145 (SRD-sourced inventory; supersedes the 2026-06-14 audit's "AWN is
derive-only" read): all four Without Number SRDs — including **AWN** — are
reproducible **verbatim** under Sine Nomine's free-use terms. mutant_wasteland
binds AWN (`pack.rules.ruleset == "awn"`, AWN combat = CWN verbatim), so its
genre-tier baseline gear must *be* AWN gear: mechanical envelope reproduced
verbatim from the AWN SRD equipment chapter and provenance-stamped
(`mode=verbatim, srd=awn, license=wn-free, srd_ref`). Per ADR-145 D2 every
genre-tier baseline item MUST carry provenance; per D1 a flavorful wasteland
name is a free *reskin* over the verbatim mechanics (still `mode=verbatim`).

Source data (AWN Free Edition SRD, Equipment chapter, pp.76-78):
  Armor              AC   Enc  TraumaTargetMod  Cost  TL
  Scrap Mail         15    2        +1          100    2     <- genre `scrap_armor`
  Scrap Plate        18    3        +1          300    2
  Ranged Weapon      Dmg     Mag  Enc  TraumaDie  TraumaRating  TL
  Shotgun            3d4      2    2     1d10         x3         2     <- genre `sawed_off`
  Light Pistol       1d6     15    1     1d8          x2         3
  Rifle              1d10+2   6    2     1d8          x3         2
  ...every AWN weapon lists a Trauma Die + Trauma Rating (SRD p.42).

What's broken today (the RED state these tests pin):
  * No catalog item carries `provenance` at all (hand-authored bespoke).
  * Weapons are bare `{dice}` with no `trauma_die`/`trauma_rating`.
  * `scrap_armor` has NO `armor_class` — it provides no mitigation (the named
    defect). Under AWN ascending-AC this is `armor_class: 15` (Scrap Mail).

Bespoke exemption: the five pre-war chargen artifacts (power_glove, datapad,
growth_wand, purifier, mystery_compass) are NOT AWN SRD gear; they are legitimate
`provenance.mode == "bespoke"` relics. The schema tests below exempt bespoke items
— a bespoke weapon need not carry a Trauma Die. (Placement of genre-tier bespoke
items is a separate ADR-145 D1/D3 question — see the session Delivery Findings.)
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_PACK_SLUG = "mutant_wasteland"

# The AWN Scrap Mail AC the genre `scrap_armor` reskins (SRD Equipment, p.77).
_AWN_SCRAP_MAIL_AC = 15


def _load_pack():
    try:
        return load_genre_pack(find_pack_path(_PACK_SLUG))
    except PackNotFound as exc:  # pragma: no cover - env-gated skip
        pytest.skip(str(exc))


def _is_bespoke(item) -> bool:
    """A genuinely-invented (non-SRD) item — exempt from the AWN schema asserts."""
    return item.provenance is not None and item.provenance.mode == "bespoke"


def _catalog(pack):
    assert pack.inventory is not None, "mutant_wasteland must ship a genre-tier inventory"
    return pack.inventory.item_catalog


# ---------------------------------------------------------------------------
# Guard — the pack loads and binds AWN (passes today; locks the precondition).
# ---------------------------------------------------------------------------


def test_pack_loads_clean_and_binds_awn():
    pack = _load_pack()
    assert pack.rules.ruleset == "awn", "mutant_wasteland must bind the AWN ruleset"
    assert pack.inventory is not None
    assert _catalog(pack), "genre inventory must carry an item_catalog"


# ---------------------------------------------------------------------------
# ADR-145 D2 — every genre-tier baseline item carries provenance.
# ---------------------------------------------------------------------------


def test_every_genre_catalog_item_carries_provenance():
    """RED: today not one mutant_wasteland item is provenance-stamped. ADR-145 D2
    requires every genre-tier baseline item to carry a provenance record (data,
    not a YAML comment) so the licensing audit / GM panel can read it."""
    pack = _load_pack()
    unstamped = [i.id for i in _catalog(pack) if i.provenance is None]
    assert not unstamped, (
        "every genre-tier mutant_wasteland item must carry provenance (ADR-145 D2); "
        f"unstamped: {unstamped}"
    )


def test_catalog_is_actually_awn_sourced():
    """RED: the baseline must be SRD-sourced, not hand-authored. At least the
    standard wasteland gear should be `mode=verbatim, srd=awn`. Today zero items
    are verbatim — the catalog is entirely bespoke."""
    pack = _load_pack()
    verbatim_awn = [
        i.id
        for i in _catalog(pack)
        if i.provenance is not None
        and i.provenance.mode == "verbatim"
        and i.provenance.srd == "awn"
    ]
    assert verbatim_awn, (
        "mutant_wasteland's genre baseline must be AWN-SRD-sourced "
        "(>=1 item with provenance mode=verbatim, srd=awn); the catalog is bespoke"
    )


def test_verbatim_items_are_awn_wn_free_with_srd_ref():
    """Correctness contract on whatever IS stamped verbatim: a verbatim item must
    name srd=awn, license=wn-free, and cite the AWN SRD in srd_ref (ADR-145
    D2/D4/D4b — sourcing boundary is the SRD document, never a commercial book)."""
    pack = _load_pack()
    for item in _catalog(pack):
        prov = item.provenance
        if prov is None or prov.mode != "verbatim":
            continue
        assert prov.srd == "awn", f"{item.id}: verbatim item must declare srd=awn"
        assert prov.license == "wn-free", f"{item.id}: AWN verbatim license must be wn-free"
        assert prov.srd_ref and "awn" in prov.srd_ref.lower(), (
            f"{item.id}: srd_ref must cite the AWN SRD, got {prov.srd_ref!r}"
        )


# ---------------------------------------------------------------------------
# Weapon schema — every AWN weapon carries a Trauma Die + Rating (SRD p.42).
# ---------------------------------------------------------------------------


def test_every_combat_weapon_carries_awn_trauma_fields():
    """RED: the title's "trauma die" requirement. Every AWN weapon lists a Trauma
    Die and a Trauma Rating multiplier; a verbatim/reskinned weapon must carry
    them on its DamageSpec. Bespoke artifacts are exempt. Today no weapon has a
    trauma_die."""
    pack = _load_pack()
    offenders: list[str] = []
    for item in _catalog(pack):
        if item.category != "weapon" or item.damage is None:
            continue
        if _is_bespoke(item):
            continue
        dmg = item.damage
        if dmg.trauma_die is None or dmg.trauma_rating <= 1:
            offenders.append(f"{item.id}(trauma_die={dmg.trauma_die}, rating={dmg.trauma_rating})")
    assert not offenders, (
        "every non-bespoke AWN weapon must carry a Trauma Die + Trauma Rating "
        f"(AWN SRD p.42); missing on: {offenders}"
    )


# ---------------------------------------------------------------------------
# Armor schema — AWN uses ascending AC; the named scrap_armor defect.
# ---------------------------------------------------------------------------


def test_every_armor_item_declares_an_armor_class():
    """RED: AWN armor is rated by ascending Armor Class. A non-bespoke armor item
    with no `armor_class` provides zero mitigation (the `equip_starting_armor`
    derivation can't read it) — a content gap. Today `scrap_armor` is the
    offender."""
    pack = _load_pack()
    no_ac = [
        i.id
        for i in _catalog(pack)
        if i.category == "armor" and not _is_bespoke(i) and i.armor_class is None
    ]
    assert not no_ac, f"every non-bespoke AWN armor item must declare armor_class; missing: {no_ac}"


def test_scrap_armor_is_the_awn_scrap_mail_reskin():
    """RED (the story's named deliverable): genre `scrap_armor` reproduces AWN
    Scrap Mail — AC 15 — verbatim-stamped (a flavor reskin keeps mode=verbatim per
    ADR-145 D1). Today `scrap_armor` has no armor_class and no provenance.

    (If a full ADR-145-D3 migration instead renames this to an `awn_*` baseline id
    reskinned at the world tier, that is a deviation from the title's named
    `scrap_armor` target and must be logged + reconciled by Dev.)"""
    pack = _load_pack()
    scrap = next((i for i in _catalog(pack) if i.id == "scrap_armor"), None)
    assert scrap is not None, "the story names genre `scrap_armor` as the target item"
    assert scrap.armor_class == _AWN_SCRAP_MAIL_AC, (
        f"scrap_armor must reproduce AWN Scrap Mail's AC {_AWN_SCRAP_MAIL_AC} "
        f"(SRD Equipment p.77); got armor_class={scrap.armor_class}"
    )
    assert scrap.provenance is not None and scrap.provenance.mode == "verbatim", (
        "scrap_armor's mechanics are the AWN Scrap Mail envelope — mode=verbatim "
        "(the wasteland name is a free reskin, ADR-145 D1)"
    )
    assert scrap.provenance.srd == "awn"
    assert scrap.provenance.license == "wn-free"
