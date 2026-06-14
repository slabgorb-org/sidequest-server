"""RED-phase tests for the CWN SRD equipment-extraction tool (story 114-5).

Contract under test (ADR-145, mirroring story 114-3's WWN tool):

* A CLI tool (house pattern of ``encountergen``/``namegen``/``wwn_equip_extract``:
  a ``sidequest.cli.cwn_equip_extract`` package with ``build_parser()`` +
  ``main(argv) -> int`` and a ``python -m`` entry point) reads the Cities Without
  Number SRD equipment chapter (§3.0.0) text and emits canonical ``CatalogItem``
  records.
* Each emitted item is provenance-stamped ``mode=verbatim, srd=cwn,
  srd_ref=<section>, license=wn-free`` (ADR-145 D4: **CWN is verbatim under the
  Without Number free-use basis, exactly like WWN** — "verbatim … is the mode for
  all four WN SRDs"; the epic's "CWN likely CC0 (verify)" parenthetical is settled
  by the governing ADR-145 D4 to ``wn-free``).
* CWN's net-new section is **Cyberware**, whose defining mechanical extra is a
  typed **system_strain** (ADR-145 D4 lists "system_strain for cyberware" as the
  WN-schema category extra). Each extracted cyberware item carries
  ``category="cyberware"`` and a typed ``system_strain: int`` reproduced verbatim.
* The licensing invariant (ADR-145 D4): the tool refuses to emit a ``verbatim``
  item under a license that does not permit verbatim reuse (``none``/``na``/``ccby``).
* The SRD source path is a required, configurable argument that fails LOUD when
  missing (No Silent Fallbacks).

NO real SRD PDF is a test dependency. All parsing runs against the synthetic text
fixture at ``tests/fixtures/cwn_srd/equipment_chapter.txt``.

Dev note (test design, not implementation mandate): these tests require a
``sidequest.cli.cwn_equip_extract`` entry point with the cyberware/system_strain
capability. They do NOT require duplicating the WWN armor/melee/ranged/general
parsers — sharing a Without-Number extraction core between the wwn and cwn CLIs is
encouraged (DRY); the tests only pin the CWN entry point's behavior. The Armor/
Melee/Ranged/General fixture rows use the shared WN column layout (ADR-145 D4);
the real CWN armor schema (dual AC / soak) is a Dev concern against the real SRD
text and is intentionally not pinned here.

These tests are RED until story 114-5 lands the tool + the ``system_strain``
schema delta. The import of ``sidequest.cli.cwn_equip_extract`` is the primary RED
signal (module does not exist yet).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# These imports are the primary RED signal: the module does not exist yet.
from sidequest.cli.cwn_equip_extract.cwn_equip_extract import (
    build_parser,
    extract_catalog,
    main,
)

from sidequest.genre.models.inventory import CatalogItem

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cwn_srd" / "equipment_chapter.txt"


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
    assert "Armor" in text
    assert "Melee Weapons" in text
    assert "Ranged Weapons" in text
    assert "General Equipment" in text
    assert "Cyberware" in text  # CWN's net-new section


# ---------------------------------------------------------------------------
# Extraction contract — verbatim CWN provenance
# ---------------------------------------------------------------------------


def test_extract_emits_all_fixture_items() -> None:
    """Five sections yield their rows: 2 armor + 2 melee + 2 ranged + 2 gear + 2 cyberware = 10."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    assert len(items) == 10


def test_every_item_is_provenance_stamped_verbatim_cwn() -> None:
    """ADR-145 D4: every CWN baseline item carries mode=verbatim, srd=cwn, license=wn-free."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    assert items
    for it in items:
        assert it.provenance is not None, f"{it.id} missing provenance"
        assert it.provenance.mode == "verbatim"
        assert it.provenance.srd == "cwn"
        assert it.provenance.license == "wn-free"
        assert it.provenance.srd_ref, f"{it.id} missing srd_ref"
        assert it.provenance.extracted_by, f"{it.id} missing extracted_by version stamp"


def test_srd_ref_names_cwn_not_wwn() -> None:
    """ADR-145 D4b: srd_ref names the CWN SRD, never WWN (the slug templates the ref)."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    for it in items:
        assert it.provenance is not None
        ref = it.provenance.srd_ref or ""
        assert "SRD" in ref, f"{it.id} srd_ref {ref!r} does not name the SRD document"
        assert "CWN" in ref, f"{it.id} srd_ref {ref!r} does not name CWN"
        assert "WWN" not in ref, f"{it.id} srd_ref {ref!r} still hardcodes WWN"


# ---------------------------------------------------------------------------
# Cyberware section — the typed system_strain (the story's net-new mechanic)
# ---------------------------------------------------------------------------


def test_cyberware_section_emits_typed_system_strain() -> None:
    """A cyberware row carries category=cyberware and a typed system_strain int,
    reproduced verbatim from the SRD (ADR-145 D4: 'system_strain for cyberware')."""
    items = _by_id(extract_catalog(_read_fixture(), srd="cwn"))
    reflexes = next(it for it in items.values() if "Wired Reflexes" in it.name)
    assert reflexes.category == "cyberware"
    assert reflexes.system_strain == 2  # verbatim strain cost from the fixture
    assert reflexes.tech_level == 4  # TL carried verbatim
    assert reflexes.value == 5000  # cost verbatim


def test_cyberware_system_strain_is_int_not_prose() -> None:
    """The defining defect this story fixes: system_strain must be a real typed int,
    not free-form prose (the audit found cyberware cost living in a lore string)."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    cyber = [it for it in items if it.category == "cyberware"]
    assert len(cyber) == 2, f"expected 2 cyberware items, got {len(cyber)}"
    for it in cyber:
        assert isinstance(it.system_strain, int), (
            f"{it.id} system_strain {it.system_strain!r} is not a typed int"
        )
        assert not isinstance(it.system_strain, bool)  # bool is an int subclass — exclude it


def test_non_cyberware_items_have_no_system_strain() -> None:
    """system_strain is a cyberware-only extra — weapons/armor/gear leave it None."""
    items = extract_catalog(_read_fixture(), srd="cwn")
    for it in items:
        if it.category != "cyberware":
            assert it.system_strain is None, (
                f"{it.id} ({it.category}) should not carry system_strain"
            )


# ---------------------------------------------------------------------------
# Licensing invariant (ADR-145 D4) — CWN is wn-free; refuse non-permitting
# ---------------------------------------------------------------------------


def test_cwn_happy_path_wn_free_permits_verbatim() -> None:
    """CWN under wn-free is the happy path (ADR-145 D4) — verbatim is permitted."""
    items = extract_catalog(_read_fixture(), srd="cwn", license="wn-free")
    assert items
    assert all(it.provenance and it.provenance.license == "wn-free" for it in items)


@pytest.mark.parametrize("bad_license", ["none", "na", "ccby"])
def test_refuses_verbatim_under_non_permitting_license(bad_license: str) -> None:
    """ADR-145 D4: the tool must refuse to emit a verbatim item under a license that
    does not permit verbatim reuse. Fail loud (ValueError), not a silent skip."""
    with pytest.raises(ValueError) as exc:
        extract_catalog(_read_fixture(), srd="cwn", license=bad_license)
    msg = str(exc.value).lower()
    assert "verbatim" in msg or "license" in msg


# ---------------------------------------------------------------------------
# No Silent Fallbacks — missing source path must fail LOUD
# ---------------------------------------------------------------------------


def test_missing_source_path_fails_loud(tmp_path: Path) -> None:
    """A nonexistent --srd-path must error (non-zero), never a silent empty catalog."""
    missing = tmp_path / "does_not_exist.txt"
    rc = main(["--srd-path", str(missing), "--srd", "cwn"])
    assert rc != 0


def test_source_path_is_required() -> None:
    """--srd-path has no hardcoded default; argparse must require it."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--srd", "cwn"])  # no --srd-path


# ---------------------------------------------------------------------------
# Wiring test (mandatory) — tool is reachable as a real CLI entry point
# ---------------------------------------------------------------------------


def test_cli_entrypoint_reachable() -> None:
    """python -m sidequest.cli.cwn_equip_extract runs end-to-end against the fixture
    and exits 0 — proving the tool is wired as a real entry point (house pattern:
    wwn_equip_extract/__main__.py), not an orphan function."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sidequest.cli.cwn_equip_extract",
            "--srd-path",
            str(FIXTURE),
            "--srd",
            "cwn",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # Parse the emitted catalog and assert COUNT + category presence (a substring
    # grep would pass even if items silently vanished; the structural check catches it).
    catalog = json.loads(result.stdout)
    assert len(catalog) == 10, f"expected 10 items, got {len(catalog)}"
    categories = {item["category"] for item in catalog}
    assert "cyberware" in categories
    # Every emitted item carries verbatim / cwn / wn-free provenance.
    assert all(item["provenance"]["mode"] == "verbatim" for item in catalog)
    assert all(item["provenance"]["srd"] == "cwn" for item in catalog)
    assert all(item["provenance"]["license"] == "wn-free" for item in catalog)
    # The cyberware items carry a typed integer system_strain in the JSON.
    cyber = [item for item in catalog if item["category"] == "cyberware"]
    assert cyber, "no cyberware items emitted"
    assert all(isinstance(item["system_strain"], int) for item in cyber)
