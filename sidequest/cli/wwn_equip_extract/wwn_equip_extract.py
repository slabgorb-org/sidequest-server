"""WWN SRD equipment-extraction tool (story 114-3; refactored onto the shared
``wn_equip_extract_core`` in story 114-12).

Reads a Worlds Without Number SRD equipment-chapter (§3.0.0) text and emits canonical,
provenance-stamped ``CatalogItem`` records. The mechanical envelope (damage, AC, cost,
encumbrance, range_band/magazine) is reproduced *verbatim* from the SRD text; each item
carries an ``ItemProvenance`` of ``mode=verbatim, srd=<srd>, license=<license>,
srd_ref=<section>`` (ADR-145 D1/D2/D4).

WWN armor is single-AC and WWN melee has no Trauma column, so this CLI binds the shared
``parse_armor``/``parse_melee`` with ``has_soak=False``/``has_trauma=False``. The
ranged and general parsers are shared verbatim. All four sections resolve through the
single ``wn_equip_extract_core`` module (story 114-12 AC1).

No PDF library is a project dependency, so this tool reads a *pre-extracted text* file
at ``--srd-path`` (run ``pdftotext -layout`` on the SRD PDF first).

Usage:
  python -m sidequest.cli.wwn_equip_extract --srd-path ./wwn_equipment.txt --srd wwn
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from sidequest.cli.wn_equip_extract_core import (
    WN_LICENSE_CHOICES,
    WN_SRD_CHOICES,
    SectionSpec,
    parse_armor,
    parse_general,
    parse_melee,
    parse_ranged,
)
from sidequest.cli.wn_equip_extract_core import (
    extract_catalog as _core_extract_catalog,
)
from sidequest.genre.models.inventory import CatalogItem

# Tool version stamp written into provenance.extracted_by (ADR-145 D2). Pinned string —
# referenced by tests/server/test_cwn_inventory_wiring + catalog-item tests.
TOOL_VERSION = "wwn_equip_extract@114-3"

# (section-key, header, parser). WWN's four sections; armor/melee bind the no-extra-column
# variant of the shared parsers. The header doubles as the srd_ref body (ADR-145 D4b).
_SECTION_PARSERS: list[SectionSpec] = [
    ("armor", "§3.0.1 Armor", partial(parse_armor, has_soak=False)),
    ("melee", "§3.0.2 Melee Weapons", partial(parse_melee, has_trauma=False)),
    ("ranged", "§3.0.3 Ranged Weapons", parse_ranged),
    ("general", "§3.0.4 General Equipment", parse_general),
]


def extract_catalog(srd_text: str, *, srd: str, license: str = "wn-free") -> list[CatalogItem]:
    """Parse WWN SRD equipment-chapter text into provenance-stamped CatalogItems."""
    return _core_extract_catalog(
        srd_text,
        srd=srd,
        license=license,
        section_parsers=_SECTION_PARSERS,
        extracted_by=TOOL_VERSION,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidequest-wwn-equip-extract",
        description=(
            "Extract a Worlds Without Number SRD equipment chapter into provenance-stamped "
            "CatalogItems (ADR-145). Input is pre-extracted SRD chapter TEXT; run "
            "pdftotext -layout on the SRD PDF first."
        ),
    )
    # No hardcoded default (No Silent Fallbacks): the source path is REQUIRED.
    p.add_argument(
        "--srd-path",
        type=Path,
        required=True,
        help="Path to the pre-extracted SRD equipment-chapter text file.",
    )
    p.add_argument(
        "--srd",
        default="wwn",
        choices=WN_SRD_CHOICES,
        help="SRD slug stamped into provenance (one of %(choices)s). Defaults to wwn.",
    )
    p.add_argument(
        "--license",
        default="wn-free",
        choices=WN_LICENSE_CHOICES,
        help="Provenance license (one of %(choices)s). Verbatim requires 'wn-free' (ADR-145 D4).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve before use so the fail-loud message names the absolute path the operator
    # actually pointed at, and symlinks are canonicalized (CWE-59; 114-12 hardening).
    srd_path = Path(args.srd_path).resolve()
    if not srd_path.is_file():
        # Fail loud: never fall back to a hardcoded path or an empty catalog.
        print(
            f"sidequest-wwn-equip-extract: --srd-path {srd_path} does not exist "
            "(No Silent Fallbacks — provide the pre-extracted SRD chapter text).",
            file=sys.stderr,
        )
        return 1

    try:
        text = srd_path.read_text(encoding="utf-8")
        items = extract_catalog(text, srd=args.srd, license=args.license)
    except Exception as e:  # noqa: BLE001 — surface extraction errors to stderr
        print(f"sidequest-wwn-equip-extract: {e}", file=sys.stderr)
        return 1

    print(json.dumps([item.model_dump() for item in items], indent=2))
    return 0
