"""WWN SRD equipment-extraction tool (story 114-3, ADR-145).

Reads a Without Number SRD equipment-chapter (§3.0.0) text and emits canonical,
provenance-stamped ``CatalogItem`` records. The mechanical envelope (damage, AC,
cost, encumbrance, range_band/magazine) is reproduced *verbatim* from the SRD
text; each item carries an ``ItemProvenance`` of ``mode=verbatim, srd=<srd>,
license=<license>, srd_ref=<section>`` (ADR-145 D1/D2/D4).

Sourcing boundary (ADR-145 D4b): the input is the bare SRD equipment chapter
*text only*. There is no code path that ingests a commercial rulebook, and every
emitted ``srd_ref`` names the SRD document, never a paid book.

Licensing invariant (ADR-145 D4): the tool refuses to emit a ``verbatim`` item
under a license that does not permit verbatim reuse. The Without Number SRDs are
``wn-free`` (verbatim-permitted); ``ccby``/``none``/``na`` do not permit verbatim
reuse and the tool fails loud (No Silent Fallbacks).

No PDF library is a project dependency, so this tool reads a *pre-extracted text*
file at ``--srd-path``. The PDF→text step (e.g. ``pdftotext -layout``) is a
documented preprocessing call, kept out of this tool so the parser stays
file-format-free and unit-testable.

Usage:
  python -m sidequest.cli.wwn_equip_extract --srd-path ./wwn_equipment.txt --srd wwn
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
TOOL_VERSION = "wwn_equip_extract@114-3"

# SRD section labels per category. The "<SRD> SRD §..." prefix is templated from
# the --srd slug (ADR-145 D4b: srd_ref must name the SRD document, and must match
# the item's srd slug — so --srd cwn yields "CWN SRD §...", never "WWN SRD §...").
_SECTION_LABELS = {
    "armor": "§3.0.1 Armor",
    "melee": "§3.0.2 Melee Weapons",
    "ranged": "§3.0.3 Ranged Weapons",
    "general": "§3.0.4 General Equipment",
}


def _section_ref(srd: str, section: str) -> str:
    """Build the srd_ref for a section, prefixed with the SRD slug (ADR-145 D4b)."""
    return f"{srd.upper()} SRD {_SECTION_LABELS[section]}"


_SHOCK_RE = re.compile(r"^(?P<shock>\d+)/AC(?P<ac>\d+)$")

# Sentinel cells the WWN SRD uses for "this column does not apply to this row":
# a weapon with no Shock rating (e.g. Blackjack, Club, Bow, Throwing Blade) prints
# "None" in the Shock column, and a ranged weapon with no magazine prints "-".
# These are VERBATIM "not applicable", not a missing value — the row is complete,
# the cell is genuinely empty. We map them to an absent mechanical field (shock
# omitted / magazine None), never to an invented number (that would be the ADR-143
# re-stat the verbatim binding exists to forbid). Any OTHER non-conforming cell
# still fails loud via the section parsers (No Silent Fallbacks).
_NA_CELLS = frozenset({"None", "-", "—", "N/A", "n/a"})


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

    The documented SRD layout always leads each section body with its column
    header ("Name  AC  Cost  Enc"). We drop exactly that first non-empty line —
    NOT every line that happens to start with "Name" (a real item named
    "Nameplate" must survive; No Silent Fallbacks / no silent row drop). If a
    LATER line still looks like a header row, the input is malformed and we fail
    loud rather than silently dropping a row.
    """
    rows: list[str] = []
    header_seen = False
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not header_seen:
            # First non-empty line is the column header — consume it.
            header_seen = True
            continue
        # A header-shaped line ("Name" as its own first token) appearing in the
        # data body means the section was mis-sliced or the layout drifted.
        if line.split()[0] == "Name":
            raise ValueError(
                f"{srd_ref}: unexpected second header-like row {line!r} in data body "
                "(malformed section — refusing to silently drop rows)"
            )
        rows.append(line)
    return rows


@contextlib.contextmanager
def _row_context(ref: str, row: str) -> Iterator[None]:
    """Re-raise any error from a row's parse/coercion with section+row context.

    Real WN equipment tables use "—", "varies", "*" for N/A cells; an int()/float()
    on those raises a context-free "invalid literal" message. Wrapping each row
    so the operator can locate the offending row across ~90 entries (No Silent
    Fallbacks: fail loud AND attributable).
    """
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
            # A "None"/"-" Shock cell is a verbatim "no Shock rating" (Blackjack,
            # Club, Throwing Blade, etc.) — emit the weapon with no shock fields
            # rather than inventing a number or dropping the row.
            if shock in _NA_CELLS:
                damage = DamageSpec(dice=dice)
            else:
                m = _SHOCK_RE.match(shock)
                if not m:
                    raise ValueError(f"shock {shock!r} is not 'X/ACY' or a no-Shock cell {sorted(_NA_CELLS)}")
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
            # WWN ranged weapons reload per-shot and have no magazine capacity;
            # the SRD prints "-" there. A "-"/"None" magazine cell is a verbatim
            # "no magazine", emitted as an absent field — never an invented count.
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


# (section-key, header, parser). Order does not matter; each header is matched
# in text. The section-key resolves the templated srd_ref (ADR-145 D4b).
_SECTION_PARSERS = [
    ("armor", "§3.0.1 Armor", _parse_armor),
    ("melee", "§3.0.2 Melee Weapons", _parse_melee),
    ("ranged", "§3.0.3 Ranged Weapons", _parse_ranged),
    ("general", "§3.0.4 General Equipment", _parse_general),
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
    """Parse SRD equipment-chapter text into provenance-stamped CatalogItems.

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
        prog="sidequest-wwn-equip-extract",
        description=(
            "Extract a Without Number SRD equipment chapter into provenance-stamped "
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
        help="SRD slug stamped into provenance (e.g. wwn). Defaults to wwn.",
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
