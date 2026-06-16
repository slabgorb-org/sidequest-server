"""CWN SRD equipment-extraction tool (story 114-5; refactored onto the shared
``wn_equip_extract_core`` in story 114-12).

The Cities Without Number analogue of the WWN tool. Reads a CWN SRD equipment-chapter
(§3.0.0) text and emits canonical, provenance-stamped ``CatalogItem`` records, verbatim
from the SRD (ADR-145 D1/D2/D4). Licensing (ADR-145 D4): CWN is part of the Without
Number line — ``wn-free``, verbatim-permitted.

CWN diverges from WWN by adding columns to two shared sections and one whole section:

* **Armor** is dual-stat (Name AC **Soak** Cost Enc) — the Soak column populates
  ``mitigation`` (binds the shared ``parse_armor`` with ``has_soak=True``).
* **Melee** adds a **Trauma** column (Name Damage Shock **Trauma** Enc Cost Attr) —
  populating trauma_die/trauma_rating/trauma_target (binds ``parse_melee`` with
  ``has_trauma=True``).
* **Cyberware** (§3.0.5) is CWN-only; its defining extra is a typed, often-fractional
  ``system_strain`` (float — CWN prices common chrome at 0.25/0.5; an int would
  truncate 0.25→0 and make the implant "free", a verbatim-fidelity violation per
  Keith's 2026-06-14 ruling). It stays in this module (story 114-12 AC1: "CWN-only
  cyberware stays CWN-side").

Ranged and general parsing are shared verbatim with WWN through the core module.

No PDF library is a project dependency, so this tool reads a *pre-extracted text* file
at ``--srd-path`` (run ``pdftotext -layout`` on the SRD PDF first).

Usage:
  python -m sidequest.cli.cwn_equip_extract --srd-path ./cwn_equipment.txt --srd cwn
"""

from __future__ import annotations

import argparse
import json
import re
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
    provenance,
    row_context,
    slugify,
)
from sidequest.cli.wn_equip_extract_core import (
    extract_catalog as _core_extract_catalog,
)
from sidequest.genre.models.inventory import CatalogItem

# Tool version stamp written into provenance.extracted_by (ADR-145 D2). Pinned string —
# referenced by tests/server/test_cwn_inventory_wiring + catalog-item tests.
TOOL_VERSION = "cwn_equip_extract@114-5"

# A plain non-negative number cell — a positive integer (Cost) or a decimal (System
# Strain, e.g. "0.25"). Roman-numeral name grades ("II") and grade words ("Sensory") do
# NOT match, so the FIRST match in a cyberware row is the Cost and the SECOND is the
# System Strain (ADR-145 D4). Cyberware is CWN-only, so this stays local to the CWN CLI.
_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _is_numeric(token: str) -> bool:
    return bool(_NUMERIC_RE.match(token))


def parse_cyberware(
    rows: list[str], srd: str, license: str, ref: str, extracted_by: str
) -> list[CatalogItem]:
    # Columns: Name  Cost  Location  Concealment  System Strain  Effect (real §3.6.7
    # layout — no TL column). The Name may carry spaces and a Roman-numeral grade
    # ("Enhanced Reflexes II"); the FIRST arabic number is the Cost and the SECOND is
    # the System Strain. Location/Concealment are single grade words carried in tags;
    # the remaining tokens are the Effect (the item description). system_strain is a
    # non-negative FLOAT reproduced verbatim. Any malformed row fails loud via the row
    # context (No Silent Fallbacks — never silently drop a column).
    items: list[CatalogItem] = []
    for row in rows:
        with row_context(ref, row):
            tokens = row.split()
            cost_idx = next((i for i, t in enumerate(tokens) if _is_numeric(t)), None)
            if cost_idx is None:
                raise ValueError("no numeric Cost column (expected Name Cost ...)")
            if cost_idx == 0:
                raise ValueError("missing Name before the Cost column")
            # After Cost: Location, Concealment, then the numeric System Strain, then ≥1
            # Effect token. A row shorter than that is malformed.
            if len(tokens) < cost_idx + 5:
                raise ValueError("too few columns for Name|Cost|Location|Concealment|Strain|Effect")
            name = " ".join(tokens[:cost_idx])
            cost = int(tokens[cost_idx])
            location = tokens[cost_idx + 1]
            concealment = tokens[cost_idx + 2]
            strain_token = tokens[cost_idx + 3]
            if not _is_numeric(strain_token):
                raise ValueError(
                    f"System Strain cell {strain_token!r} is not a non-negative number"
                )
            effect = " ".join(tokens[cost_idx + 4 :])
            items.append(
                CatalogItem(
                    id=slugify(name, srd),
                    name=name,
                    description=effect,
                    category="cyberware",
                    value=cost,
                    system_strain=float(strain_token),
                    tags=[
                        "cyberware",
                        f"location:{location.lower()}",
                        f"concealment:{concealment.lower()}",
                    ],
                    provenance=provenance(srd, license, ref, extracted_by),
                )
            )
    return items


# (section-key, header, parser). CWN binds the dual-AC/Trauma variants of the shared
# armor/melee parsers and adds its own cyberware section. The header doubles as the
# srd_ref body (ADR-145 D4b).
_SECTION_PARSERS: list[SectionSpec] = [
    ("armor", "§3.0.1 Armor", partial(parse_armor, has_soak=True)),
    ("melee", "§3.0.2 Melee Weapons", partial(parse_melee, has_trauma=True)),
    ("ranged", "§3.0.3 Ranged Weapons", parse_ranged),
    ("general", "§3.0.4 General Equipment", parse_general),
    ("cyberware", "§3.0.5 Cyberware", parse_cyberware),
]


def extract_catalog(srd_text: str, *, srd: str, license: str = "wn-free") -> list[CatalogItem]:
    """Parse CWN SRD equipment-chapter text into provenance-stamped CatalogItems."""
    return _core_extract_catalog(
        srd_text,
        srd=srd,
        license=license,
        section_parsers=_SECTION_PARSERS,
        extracted_by=TOOL_VERSION,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sidequest-cwn-equip-extract",
        description=(
            "Extract a Cities Without Number SRD equipment chapter into "
            "provenance-stamped CatalogItems (ADR-145). Input is pre-extracted SRD "
            "chapter TEXT; run pdftotext -layout on the SRD PDF first."
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
        default="cwn",
        choices=WN_SRD_CHOICES,
        help="SRD slug stamped into provenance (one of %(choices)s). Defaults to cwn.",
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
            f"sidequest-cwn-equip-extract: --srd-path {srd_path} does not exist "
            "(No Silent Fallbacks — provide the pre-extracted SRD chapter text).",
            file=sys.stderr,
        )
        return 1

    try:
        text = srd_path.read_text(encoding="utf-8")
        items = extract_catalog(text, srd=args.srd, license=args.license)
    except Exception as e:  # noqa: BLE001 — surface extraction errors to stderr
        print(f"sidequest-cwn-equip-extract: {e}", file=sys.stderr)
        return 1

    print(json.dumps([item.model_dump() for item in items], indent=2))
    return 0
