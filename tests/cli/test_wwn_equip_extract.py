"""RED-phase tests for the WWN SRD equipment-extraction tool (story 114-3).

Contract under test (ADR-145):

* A CLI tool (house pattern of ``encountergen``/``namegen``: a
  ``sidequest.cli.wwn_equip_extract`` package with ``build_parser()`` +
  ``main(argv) -> int`` and a ``python -m`` entry point) reads the WWN SRD
  equipment chapter (§3.0.0) text and emits canonical ``CatalogItem`` records.
* Each emitted item is provenance-stamped ``mode=verbatim, srd=wwn,
  srd_ref=<section>, license=wn-free`` (ADR-145 D2/D4), with the mechanical
  envelope (damage, AC, cost, encumbrance, range_band/magazine for ranged)
  populated *verbatim* from the SRD text.
* ``CatalogItem`` grows the D2 schema delta: a nested ``ItemProvenance`` model
  plus ``tech_level`` / ``range_band`` / ``magazine`` — under the existing
  strict ``extra="forbid"`` config.
* The SRD source path is a required, configurable argument that fails LOUD when
  missing (No Silent Fallbacks — never a hardcoded home-dir fallback, never a
  silent empty catalog).
* The licensing invariant (ADR-145 D4): the tool refuses to emit a
  ``verbatim`` item under a license that does not permit verbatim reuse.

NO real SRD PDF is a test dependency. All parsing runs against the synthetic
text fixture at ``tests/fixtures/wwn_srd/equipment_chapter.txt`` which mirrors
the SRD §3.0.0 table layout with invented, schema-shaped rows.

These tests are RED until story 114-3 lands the tool + the CatalogItem schema
delta. They are expected to fail at import (module does not exist) and on the
new fields/model not existing — that is the intended red signal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# These imports are the primary RED signal: the module/symbols do not exist yet.
from sidequest.cli.wwn_equip_extract.wwn_equip_extract import (
    build_parser,
    extract_catalog,
    main,
)
from sidequest.genre.models.inventory import CatalogItem, ItemProvenance

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wwn_srd" / "equipment_chapter.txt"


def _by_id(items: list[CatalogItem]) -> dict[str, CatalogItem]:
    return {it.id: it for it in items}


def _read_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture sanity — guard against a fixture bug masquerading as a real failure
# ---------------------------------------------------------------------------


def test_fixture_exists_and_has_sections() -> None:
    """If this fails the fixture is broken — not the tool. Keeps the RED signal honest."""
    text = _read_fixture()
    assert "§3.0.0 Equipment" in text
    assert "Melee Weapons" in text
    assert "Ranged Weapons" in text
    assert "Armor" in text
    assert "General Equipment" in text


# ---------------------------------------------------------------------------
# Schema delta on CatalogItem (ADR-145 D2)
# ---------------------------------------------------------------------------


def test_item_provenance_model_shape() -> None:
    """ItemProvenance is a strict nested model with mode/srd/srd_ref/license/extracted_by."""
    prov = ItemProvenance(
        mode="verbatim",
        srd="wwn",
        srd_ref="WWN SRD §3.0.1 Armor",
        license="wn-free",
        extracted_by="wwn_equip_extract@114-3",
    )
    assert prov.mode == "verbatim"
    assert prov.srd == "wwn"
    assert prov.license == "wn-free"


def test_item_provenance_is_strict() -> None:
    """extra='forbid' — provenance is first-class data, not loose metadata (ADR-145 D2)."""
    with pytest.raises(ValidationError):
        ItemProvenance(mode="verbatim", srd="wwn", bogus_field="x")  # type: ignore[call-arg]


def test_item_provenance_verbatim_requires_permitting_license() -> None:
    """HARDENING 1 (ADR-145 D4): the verbatim⇒permitting-license invariant is
    structural on ItemProvenance — no construction path can mint a verbatim
    record under a non-permitting license, not just the extraction tool."""
    with pytest.raises(ValidationError):
        ItemProvenance(mode="verbatim", srd="wwn", license="na")
    with pytest.raises(ValidationError):
        ItemProvenance(mode="verbatim", srd="wwn", license="ccby")
    # Non-verbatim modes are unconstrained by the license invariant.
    bespoke = ItemProvenance(mode="bespoke", license="na")
    assert bespoke.mode == "bespoke"


def test_catalog_item_accepts_new_fields() -> None:
    """CatalogItem grows provenance + tech_level/range_band/magazine, still extra='forbid'."""
    item = CatalogItem(
        id="wwn_test_sling_carbine",
        name="Test Sling Carbine",
        description="ranged",
        category="ranged_weapon",
        tech_level=4,
        range_band="pistol",
        magazine=6,
        provenance=ItemProvenance(
            mode="verbatim", srd="wwn", srd_ref="WWN SRD §3.0.3 Ranged Weapons", license="wn-free"
        ),
    )
    assert item.tech_level == 4
    assert item.range_band == "pistol"
    assert item.magazine == 6
    assert item.provenance is not None
    assert item.provenance.mode == "verbatim"


def test_catalog_item_still_forbids_unknown_fields() -> None:
    """The schema delta must NOT relax extra='forbid'."""
    with pytest.raises(ValidationError):
        CatalogItem(id="x", name="x", description="x", category="x", not_a_real_field=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Extraction contract — verbatim mechanical envelope from the fixture text
# ---------------------------------------------------------------------------


def test_extract_emits_all_fixture_items() -> None:
    """The four sections yield their rows: 2 armor + 2 melee + 2 ranged + 2 gear = 8."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    assert len(items) == 8


def test_every_item_is_provenance_stamped_verbatim_wwn() -> None:
    """ADR-145 D1/D4: every baseline item carries mode=verbatim, srd=wwn, license=wn-free."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    for it in items:
        assert it.provenance is not None, f"{it.id} missing provenance"
        assert it.provenance.mode == "verbatim"
        assert it.provenance.srd == "wwn"
        assert it.provenance.license == "wn-free"
        assert it.provenance.srd_ref, f"{it.id} missing srd_ref"
        assert it.provenance.extracted_by, f"{it.id} missing extracted_by version stamp"


def test_srd_ref_points_into_srd_document_not_commercial_book() -> None:
    """ADR-145 D4b: srd_ref must name the SRD, never a commercial book."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    for it in items:
        assert it.provenance is not None
        ref = it.provenance.srd_ref or ""
        assert "SRD" in ref, f"{it.id} srd_ref {ref!r} does not name the SRD document"


def test_melee_weapon_mechanics_verbatim() -> None:
    """Melee row carries damage dice verbatim and is shaped as a weapon."""
    items = _by_id(extract_catalog(_read_fixture(), srd="wwn"))
    cudgel = next(it for it in items.values() if "Iron Cudgel" in it.name)
    assert cudgel.damage is not None
    assert cudgel.damage.dice == "1d8"
    # Shock "2/AC15" from the fixture → shock=2, shock_ac=15 (verbatim).
    assert cudgel.damage.shock == 2
    assert cudgel.damage.shock_ac == 15
    assert cudgel.value == 12  # cost verbatim
    assert cudgel.weight == 1.0  # encumbrance verbatim


def test_ranged_weapon_mechanics_verbatim() -> None:
    """Ranged row carries damage + range_band + magazine verbatim."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    carbine = next(it for it in items if "Sling Carbine" in it.name)
    assert carbine.damage is not None
    assert carbine.damage.dice == "1d6"
    assert carbine.range_band == "pistol"
    assert carbine.magazine == 6
    assert carbine.value == 40


def test_armor_mechanics_verbatim() -> None:
    """Armor row carries armor_class + cost + encumbrance verbatim."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    plate = next(it for it in items if "Plate Harness" in it.name)
    assert plate.armor_class == 16
    assert plate.value == 250
    assert plate.weight == 3.0
    assert plate.damage is None  # armor is not a weapon


def test_general_gear_has_no_combat_envelope() -> None:
    """General equipment carries cost/encumbrance but no damage/AC."""
    items = extract_catalog(_read_fixture(), srd="wwn")
    rope = next(it for it in items if "Hemp Rope" in it.name)
    assert rope.damage is None
    assert rope.armor_class is None
    assert rope.value == 4
    assert rope.weight == 1.0


# ---------------------------------------------------------------------------
# Verbatim-integrity defects (review fixes)
# ---------------------------------------------------------------------------


def test_no_silent_row_drop_for_item_named_like_header() -> None:
    """MAJOR 1: only the FIRST line is the column header. A real item whose name
    starts with the header token ("Nameplate") must NOT be silently dropped."""
    text = (
        "§3.0.0 Equipment\n\n"
        "§3.0.1 Armor\n"
        "Name                AC    Cost    Enc\n"
        "Nameplate Cuirass   14    30      2\n"
        "Test Plate Harness  16    250     3\n\n"
        "§3.0.2 Melee Weapons\n"
        "Name                Damage   Shock      Enc   Cost   Attribute\n"
        "Test Iron Cudgel    1d8      2/AC15     1     12     Str\n\n"
        "§3.0.3 Ranged Weapons\n"
        "Name                Damage   Range      Mag   Enc   Cost\n"
        "Test Sling Carbine  1d6      pistol     6     1     40\n\n"
        "§3.0.4 General Equipment\n"
        "Name                Cost    Enc\n"
        "Test Hemp Rope 50ft 4       1\n"
    )
    items = extract_catalog(text, srd="wwn")
    names = {it.name for it in items}
    assert "Nameplate Cuirass" in names, "item named like the header was silently dropped"
    assert "Test Plate Harness" in names
    # Both armor rows survive (header consumed exactly once).
    armor = [it for it in items if it.category == "armor"]
    assert len(armor) == 2


def test_non_numeric_cell_raises_with_row_context() -> None:
    """MAJOR 2: a non-numeric cost cell (real SRDs use '—'/'varies') must raise
    an error that names the offending row + section, not a context-free
    'invalid literal for int()'."""
    text = (
        "§3.0.0 Equipment\n\n"
        "§3.0.1 Armor\n"
        "Name                AC    Cost    Enc\n"
        "Test Padded Vest    13    20      1\n\n"
        "§3.0.2 Melee Weapons\n"
        "Name                Damage   Shock      Enc   Cost   Attribute\n"
        "Test Iron Cudgel    1d8      2/AC15     1     12     Str\n\n"
        "§3.0.3 Ranged Weapons\n"
        "Name                Damage   Range      Mag   Enc   Cost\n"
        "Test Sling Carbine  1d6      pistol     6     1     40\n\n"
        "§3.0.4 General Equipment\n"
        "Name                Cost    Enc\n"
        "Test Mystery Crate  varies  1\n"
    )
    with pytest.raises(ValueError) as exc:
        extract_catalog(text, srd="wwn")
    msg = str(exc.value)
    assert "Test Mystery Crate" in msg, f"error did not name the row: {msg!r}"
    assert "varies" in msg, f"error did not surface the bad cell: {msg!r}"


def test_srd_ref_prefix_tracks_srd_slug() -> None:
    """MAJOR 3: srd_ref is templated from --srd, not hardcoded 'WWN SRD'. With
    --srd cwn the refs name CWN and never WWN (generalizes for 114-5)."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    assert items
    for it in items:
        assert it.provenance is not None
        ref = it.provenance.srd_ref or ""
        assert "CWN" in ref, f"{it.id} srd_ref {ref!r} does not name CWN"
        assert "WWN" not in ref, f"{it.id} srd_ref {ref!r} still hardcodes WWN"
        assert it.provenance.srd == "cwn"


# ---------------------------------------------------------------------------
# Licensing invariant (ADR-145 D4) — refuse verbatim under a non-permitting license
# ---------------------------------------------------------------------------


def test_refuses_verbatim_under_non_permitting_license() -> None:
    """ADR-145 D4: the tool must refuse to emit a verbatim item under a license
    that does not permit verbatim reuse (e.g. Fate's ccby / a 'none' basis).

    Dev note: pinned to ValueError as the codebase's fail-loud type (cf. the
    DamageSpec validators in inventory.py). If 114-3 raises a more specific
    extraction error, widen this to that type — not to a bare Exception."""
    with pytest.raises(ValueError) as exc:
        extract_catalog(_read_fixture(), srd="wwn", license="none")
    assert "verbatim" in str(exc.value).lower() or "license" in str(exc.value).lower()


def test_wwn_happy_path_license_permits_verbatim() -> None:
    """WWN under wn-free is the happy path — verbatim is permitted, no raise."""
    items = extract_catalog(_read_fixture(), srd="wwn", license="wn-free")
    assert items
    assert all(it.provenance and it.provenance.license == "wn-free" for it in items)


# ---------------------------------------------------------------------------
# No Silent Fallbacks — missing source path must fail LOUD
# ---------------------------------------------------------------------------


def test_missing_source_path_fails_loud(tmp_path: Path) -> None:
    """A nonexistent --srd-path must error (non-zero / raise), never produce an
    empty catalog via a silent fallback to a hardcoded home-dir path."""
    missing = tmp_path / "does_not_exist.txt"
    rc = main(["--srd-path", str(missing), "--srd", "wwn"])
    assert rc != 0


def test_source_path_is_required() -> None:
    """--srd-path has no hardcoded default; argparse must require it."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--srd", "wwn"])  # no --srd-path


# ---------------------------------------------------------------------------
# Wiring test (mandatory) — tool is reachable as a real CLI entry point
# ---------------------------------------------------------------------------


def test_cli_entrypoint_reachable() -> None:
    """python -m sidequest.cli.wwn_equip_extract runs end-to-end against the
    fixture and exits 0 — proving the tool is wired as a real entry point
    (house pattern: encountergen/namegen __main__.py), not an orphan function."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sidequest.cli.wwn_equip_extract",
            "--srd-path",
            str(FIXTURE),
            "--srd",
            "wwn",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # HARDENING 2: parse the emitted catalog and assert COUNT + per-category
    # presence. A substring grep would pass even if 7 of 8 items silently
    # vanished (MAJOR 1's failure mode); the structural assertion catches it.
    catalog = json.loads(result.stdout)
    assert len(catalog) == 8, f"expected 8 items, got {len(catalog)}"
    categories = {item["category"] for item in catalog}
    assert categories == {"armor", "melee_weapon", "ranged_weapon", "general"}
    # Every emitted item carries verbatim / wn-free provenance.
    assert all(item["provenance"]["mode"] == "verbatim" for item in catalog)
    assert all(item["provenance"]["license"] == "wn-free" for item in catalog)
