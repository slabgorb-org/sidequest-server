"""CWN SRD equipment-extraction tool (story 114-5, ADR-145).

The Cities Without Number analogue of the WWN extraction tool (story 114-3). Reads
a Cities Without Number SRD equipment-chapter (§3.0.0) text and emits canonical,
provenance-stamped ``CatalogItem`` records. The mechanical envelope (damage, AC,
cost, encumbrance, range_band/magazine, and — CWN's net-new extra — cyberware
``system_strain``) is reproduced *verbatim* from the SRD text; each item carries an
``ItemProvenance`` of ``mode=verbatim, srd=cwn, license=wn-free, srd_ref=<section>``
(ADR-145 D1/D2/D4).

Licensing (ADR-145 D4): CWN is part of the Without Number line, which resolves to
**reproduce verbatim** under the WN free-use basis — exactly like WWN. The
``wn-free`` license is therefore the only one that permits verbatim emission;
``ccby``/``none``/``na`` fail loud (No Silent Fallbacks).

CWN's net-new section over the WWN tool is **§3.0.5 Cyberware**, whose real SRD
column layout is ``Name | Cost | Location | Concealment | System Strain | Effect``
(no TL column). Its defining mechanical extra is ``system_strain`` — a non-negative
**number** that raises the character's permanent System-Strain floor on install.
It is often **fractional**: CWN prices its most common chrome (Cybereyes, Cyberears,
Skillplug Jack I) at 0.25 and (Viper Sting, Skillplug Jack II) at 0.5, so the value
is a ``float`` (an int would truncate 0.25→0 and make the iconic chrome "free" — a
verbatim-fidelity violation; Keith's ruling 2026-06-14). Location (Body/Head/Skin/
Sensory/Nerve/Limb) and Concealment (Obvious/Sight/Touch/Medical) grades are carried
in ``tags``; the Effect text becomes the item ``description``.

This module deliberately mirrors ``sidequest.cli.wwn_equip_extract`` (same CLI
contract, same verbatim/NA/fail-loud parsing discipline). The four shared sections
use the WN-family common item schema (ADR-145 D4: "the four WN SRDs share one item
schema"). Factoring a shared ``_wn_equip_core`` out of the two extractors is a
worthwhile DRY follow-up (logged as a Delivery Finding) — kept separate here to
avoid touching the shipped WWN tool mid-story.

No PDF library is a project dependency, so this tool reads a *pre-extracted text*
file at ``--srd-path`` (run ``pdftotext -layout`` on the SRD PDF first).

Usage:
  python -m sidequest.cli.cwn_equip_extract --srd-path ./cwn_equipment.txt --srd cwn
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from sidequest.genre.models.inventory import (
    _VERBATIM_LICENSES,
    CatalogItem,
    DamageSpec,
    ItemProvenance,
)

# Tool version stamp written into provenance.extracted_by (ADR-145 D2).
TOOL_VERSION = "cwn_equip_extract@114-5"

# SRD section labels per category. The "<SRD> SRD §..." prefix is templated from
# the --srd slug (ADR-145 D4b: srd_ref must name the SRD document and match the
# item's srd slug — so --srd cwn yields "CWN SRD §...").
_SECTION_LABELS = {
    "armor": "§3.0.1 Armor",
    "melee": "§3.0.2 Melee Weapons",
    "ranged": "§3.0.3 Ranged Weapons",
    "general": "§3.0.4 General Equipment",
    "cyberware": "§3.0.5 Cyberware",
}


def _section_ref(srd: str, section: str) -> str:
    """Build the srd_ref for a section, prefixed with the SRD slug (ADR-145 D4b)."""
    return f"{srd.upper()} SRD {_SECTION_LABELS[section]}"


_SHOCK_RE = re.compile(r"^(?P<shock>\d+)/AC(?P<ac>\d+)$")

# Sentinel cells the SRD uses for "this column does not apply to this row": a
# weapon with no Shock rating prints "None", a ranged weapon with no magazine
# prints "-". These are VERBATIM "not applicable", not a missing value — mapped to
# an absent mechanical field (shock omitted / magazine None), never an invented
# number. Any OTHER non-conforming cell still fails loud (No Silent Fallbacks).
_NA_CELLS = frozenset({"None", "-", "—", "N/A", "n/a"})

# A plain non-negative number cell — a positive integer (Cost) or a decimal
# (System Strain, e.g. "0.25"). Roman-numeral name grades ("II") and grade words
# ("Sensory") do NOT match, so the FIRST match in a cyberware row is the Cost and
# the SECOND is the System Strain (ADR-145 D4; the §3.6.7 column layout).
_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _is_numeric(token: str) -> bool:
    return bool(_NUMERIC_RE.match(token))


def _slugify(name: str, srd: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{srd}_{slug}"


def _provenance(srd: str, license: str, srd_ref: str) -> ItemProvenance:
    return ItemProvenance(
        mode="verbatim",
        srd=srd,
        srd_ref=srd_ref,
        license=license,  # type: ignore[arg-type]  # validated by the model
        extracted_by=TOOL_VERSION,
    )


def _data_rows(section: str, srd_ref: str) -> list[str]:
    """Yield the value rows of a section, dropping ONLY the column-header line.

    Drop exactly the first non-empty line (the "Name ..." column header) — NOT
    every line that starts with "Name" (a real item named "Nameplate" must
    survive; No Silent Fallbacks / no silent row drop). A LATER header-shaped line
    means the input is malformed and we fail loud rather than dropping a row.
    """
    rows: list[str] = []
    header_seen = False
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not header_seen:
            header_seen = True
            continue
        if line.split()[0] == "Name":
            raise ValueError(
                f"{srd_ref}: unexpected second header-like row {line!r} in data body "
                "(malformed section — refusing to silently drop rows)"
            )
        rows.append(line)
    return rows


@contextlib.contextmanager
def _row_context(ref: str, row: str) -> Iterator[None]:
    """Re-raise any error from a row's parse/coercion with section+row context."""
    try:
        yield
    except ValueError as e:
        raise ValueError(f"{ref} row {row!r}: {e}") from e


def _split_trailing(line: str, n: int) -> tuple[str, list[str]]:
    """Split a row into (name, last n whitespace-delimited tokens).

    Names contain spaces; the trailing columns are single tokens, so we split
    from the right. Fails loud if the row has too few columns.
    """
    parts = line.split()
    if len(parts) < n + 1:
        raise ValueError(f"row {line!r} has fewer than {n + 1} columns")
    name = " ".join(parts[:-n])
    return name, parts[-n:]


def _parse_armor(rows: list[str], srd: str, license: str) -> list[CatalogItem]:
    # Columns: Name  AC  Cost  Enc
    items: list[CatalogItem] = []
    ref = _section_ref(srd, "armor")
    for row in rows:
        with _row_context(ref, row):
            name, (ac, cost, enc) = _split_trailing(row, 3)
            items.append(
                CatalogItem(
                    id=_slugify(name, srd),
                    name=name,
                    description=name,
                    category="armor",
                    value=int(cost),
                    weight=float(enc),
                    armor_class=int(ac),
                    provenance=_provenance(srd, license, ref),
                )
            )
    return items


def _parse_melee(rows: list[str], srd: str, license: str) -> list[CatalogItem]:
    # Columns: Name  Damage  Shock  Enc  Cost  Attribute
    items: list[CatalogItem] = []
    ref = _section_ref(srd, "melee")
    for row in rows:
        with _row_context(ref, row):
            name, (dice, shock, enc, cost, _attr) = _split_trailing(row, 5)
            if shock in _NA_CELLS:
                damage = DamageSpec(dice=dice)
            else:
                m = _SHOCK_RE.match(shock)
                if not m:
                    raise ValueError(
                        f"shock {shock!r} is not 'X/ACY' or a no-Shock cell {sorted(_NA_CELLS)}"
                    )
                damage = DamageSpec(dice=dice, shock=int(m["shock"]), shock_ac=int(m["ac"]))
            items.append(
                CatalogItem(
                    id=_slugify(name, srd),
                    name=name,
                    description=name,
                    category="melee_weapon",
                    value=int(cost),
                    weight=float(enc),
                    damage=damage,
                    provenance=_provenance(srd, license, ref),
                )
            )
    return items


def _parse_ranged(rows: list[str], srd: str, license: str) -> list[CatalogItem]:
    # Columns: Name  Damage  Range  Mag  Enc  Cost
    items: list[CatalogItem] = []
    ref = _section_ref(srd, "ranged")
    for row in rows:
        with _row_context(ref, row):
            name, (dice, range_band, mag, enc, cost) = _split_trailing(row, 5)
            magazine = None if mag in _NA_CELLS else int(mag)
            items.append(
                CatalogItem(
                    id=_slugify(name, srd),
                    name=name,
                    description=name,
                    category="ranged_weapon",
                    value=int(cost),
                    weight=float(enc),
                    range_band=range_band,
                    magazine=magazine,
                    damage=DamageSpec(dice=dice),
                    provenance=_provenance(srd, license, ref),
                )
            )
    return items


def _parse_general(rows: list[str], srd: str, license: str) -> list[CatalogItem]:
    # Columns: Name  Cost  Enc
    items: list[CatalogItem] = []
    ref = _section_ref(srd, "general")
    for row in rows:
        with _row_context(ref, row):
            name, (cost, enc) = _split_trailing(row, 2)
            items.append(
                CatalogItem(
                    id=_slugify(name, srd),
                    name=name,
                    description=name,
                    category="general",
                    value=int(cost),
                    weight=float(enc),
                    provenance=_provenance(srd, license, ref),
                )
            )
    return items


def _parse_cyberware(rows: list[str], srd: str, license: str) -> list[CatalogItem]:
    # Columns: Name  Cost  Location  Concealment  System Strain  Effect
    # CWN's net-new section (real §3.6.7 layout — no TL column). The Name may carry
    # spaces and a Roman-numeral grade ("Enhanced Reflexes II"); the FIRST arabic
    # number is the Cost and the SECOND is the System Strain. Location/Concealment
    # are single grade words carried in tags; the remaining tokens are the Effect
    # (the item description). system_strain is a non-negative FLOAT reproduced
    # verbatim (CWN prices common chrome at 0.25/0.5). The CatalogItem ge=0
    # validator rejects a negative cell; any malformed row fails loud via the row
    # context (No Silent Fallbacks — never silently drop a column).
    items: list[CatalogItem] = []
    ref = _section_ref(srd, "cyberware")
    for row in rows:
        with _row_context(ref, row):
            tokens = row.split()
            cost_idx = next((i for i, t in enumerate(tokens) if _is_numeric(t)), None)
            if cost_idx is None:
                raise ValueError("no numeric Cost column (expected Name Cost ...)")
            if cost_idx == 0:
                raise ValueError("missing Name before the Cost column")
            # After Cost: Location, Concealment, then the numeric System Strain,
            # then ≥1 Effect token. A row shorter than that is malformed.
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
                    id=_slugify(name, srd),
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
                    provenance=_provenance(srd, license, ref),
                )
            )
    return items


# (section-key, header, parser). Each header is matched in text; the section-key
# resolves the templated srd_ref (ADR-145 D4b). CWN adds Cyberware over WWN's four.
_SECTION_PARSERS = [
    ("armor", "§3.0.1 Armor", _parse_armor),
    ("melee", "§3.0.2 Melee Weapons", _parse_melee),
    ("ranged", "§3.0.3 Ranged Weapons", _parse_ranged),
    ("general", "§3.0.4 General Equipment", _parse_general),
    ("cyberware", "§3.0.5 Cyberware", _parse_cyberware),
]


def _slice_section(text: str, header: str, all_headers: list[str]) -> str:
    """Return the body of ``header`` up to the next section header."""
    start = text.index(header) + len(header)
    end = len(text)
    for other in all_headers:
        if other == header:
            continue
        idx = text.find(other)
        if idx != -1 and start <= idx < end:
            end = idx
    return text[start:end]


def extract_catalog(srd_text: str, *, srd: str, license: str = "wn-free") -> list[CatalogItem]:
    """Parse CWN SRD equipment-chapter text into provenance-stamped CatalogItems.

    Pure, file-free core (text in → models out) so it is unit-testable without a
    PDF. Every emitted item is ``mode=verbatim`` (ADR-145 D1: the WN baseline is
    verbatim, never bespoke).

    ADR-145 D4 licensing invariant: ``verbatim`` requires a license that permits
    verbatim reuse. Fails loud otherwise — No Silent Fallbacks.
    """
    if license not in _VERBATIM_LICENSES:
        raise ValueError(
            f"refusing to emit verbatim items for srd={srd!r} under license={license!r}: "
            f"that license does not permit verbatim reuse (ADR-145 D4). Verbatim is "
            f"permitted only under: {sorted(_VERBATIM_LICENSES)}."
        )

    headers = [h for _, h, _ in _SECTION_PARSERS]
    items: list[CatalogItem] = []
    for section, header, parser in _SECTION_PARSERS:
        if header not in srd_text:
            # Fail loud rather than silently emitting a partial catalog.
            raise ValueError(f"SRD text is missing expected section header {header!r}")
        body = _slice_section(srd_text, header, headers)
        items.extend(parser(_data_rows(body, _section_ref(srd, section)), srd, license))
    return items


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
        help="SRD slug stamped into provenance (e.g. cwn). Defaults to cwn.",
    )
    p.add_argument(
        "--license",
        default="wn-free",
        help="Provenance license. Verbatim requires 'wn-free' (ADR-145 D4).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    srd_path = Path(args.srd_path)
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
