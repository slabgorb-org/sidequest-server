"""Shared Without Number equipment-extraction core (story 114-12, ADR-145).

The WWN (114-3) and CWN (114-5) extraction CLIs were ~95% duplicated. This module
factors the common machinery into ONE place:

* helpers — ``slugify`` / ``provenance`` / ``data_rows`` / ``row_context`` /
  ``split_trailing`` / ``slice_section`` / ``section_ref``;
* the licensing gate + the ``extract_catalog`` driver;
* the section parsers. ``parse_ranged`` and ``parse_general`` are byte-identical
  across the WN SRDs. ``parse_armor`` and ``parse_melee`` differ between WWN and CWN
  by exactly one optional column (CWN armor adds **Soak**, CWN melee adds **Trauma**),
  so they take a ``has_soak`` / ``has_trauma`` flag — ONE parser per section,
  selected with ``functools.partial`` in each CLI's ``_SECTION_PARSERS`` table, not a
  fork. CWN-only **cyberware** stays in the CWN CLI.

Each CLI remains a thin ``python -m`` entry point (the two entry points are pinned by
wiring tests; the shared core sits beneath them). This is an offline authoring tool —
text in, ``CatalogItem`` models out — so it emits no OTEL spans (not a runtime
subsystem).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator

from sidequest.genre.models.inventory import (
    _VERBATIM_LICENSES,
    CatalogItem,
    DamageSpec,
    ItemProvenance,
)

# argparse `choices` allowlists (114-12 hardening). A bogus --srd/--license value
# exits at parse time rather than minting a self-inconsistent provenance stamp
# (No Silent Fallbacks). The license set mirrors ItemProvenance.license's Literal.
WN_SRD_CHOICES = ("wwn", "cwn", "swn", "awn")
WN_LICENSE_CHOICES = ("wn-free", "ccby", "none", "na")

_SHOCK_RE = re.compile(r"^(?P<shock>\d+)/AC(?P<ac>\d+)$")
# Ranged damage may carry a flat bonus "NdM+B" (e.g. a slugthrower's 1d8+2). The
# DamageSpec.dice validator rejects a "+B" suffix, so we split here into dice + bonus
# rather than widening that validator (114-12 watch-item).
_RANGED_DAMAGE_RE = re.compile(r"^(?P<dice>\d+d\d+)(?:\+(?P<bonus>\d+))?$")
# CWN melee Trauma cell: "<die>/x<rating>/T<target>" (e.g. 1d10/x2/T6). An N/A cell is
# a verbatim "no Trauma rating" — emit no trauma fields, never an invented number.
_TRAUMA_RE = re.compile(r"^(?P<die>\d+d\d+)/x(?P<rating>\d+)/T(?P<target>\d+)$")

# Sentinel cells the SRD uses for "this column does not apply to this row": a no-Shock
# weapon prints "None", a no-magazine weapon "-", a no-soak armor / no-Trauma weapon
# likewise. These are VERBATIM "not applicable" → an absent mechanical field, never an
# invented number (the ADR-143 re-stat the verbatim binding forbids). Any OTHER
# non-conforming cell still fails loud (No Silent Fallbacks).
_NA_CELLS = frozenset({"None", "-", "—", "N/A", "n/a"})

# A section parser: (rows, srd, license, ref, extracted_by) -> items. armor/melee bind
# their has_soak/has_trauma flag via functools.partial, so the runtime call signature
# stays uniform.
SectionParser = Callable[..., list[CatalogItem]]
SectionSpec = tuple[str, str, SectionParser]


def section_ref(srd: str, header: str) -> str:
    """Build the srd_ref for a section, prefixed with the SRD slug (ADR-145 D4b).

    The section header (e.g. "§3.0.1 Armor") doubles as the ref body, so --srd cwn
    yields "CWN SRD §3.0.1 Armor", never a hardcoded "WWN SRD ...".
    """
    return f"{srd.upper()} SRD {header}"


def slugify(name: str, srd: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{srd}_{slug}"


def provenance(srd: str, license: str, srd_ref: str, extracted_by: str) -> ItemProvenance:
    return ItemProvenance(
        mode="verbatim",
        srd=srd,
        srd_ref=srd_ref,
        license=license,  # type: ignore[arg-type]  # validated by the model
        extracted_by=extracted_by,
    )


def data_rows(section: str, srd_ref: str) -> list[str]:
    """Yield the value rows of a section, dropping ONLY the column-header line.

    Drop exactly the first non-empty line (the "Name ..." column header) — NOT every
    line that starts with "Name" (a real item named "Nameplate" must survive; No Silent
    Fallbacks / no silent row drop). A LATER header-shaped line means the input is
    malformed and we fail loud rather than dropping a row.
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
def row_context(ref: str, row: str) -> Iterator[None]:
    """Re-raise any error from a row's parse/coercion with section+row context."""
    try:
        yield
    except ValueError as e:
        raise ValueError(f"{ref} row {row!r}: {e}") from e


def split_trailing(line: str, n: int) -> tuple[str, list[str]]:
    """Split a row into (name, last n whitespace-delimited tokens).

    Names contain spaces; the trailing columns are single tokens, so we split from the
    right. Fails loud if the row has too few columns.
    """
    parts = line.split()
    if len(parts) < n + 1:
        raise ValueError(f"row {line!r} has fewer than {n + 1} columns")
    name = " ".join(parts[:-n])
    return name, parts[-n:]


def slice_section(text: str, header: str, all_headers: list[str]) -> str:
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


def parse_armor(
    rows: list[str], srd: str, license: str, ref: str, extracted_by: str, *, has_soak: bool
) -> list[CatalogItem]:
    """Armor parser. WWN is single-AC (Name AC Cost Enc). CWN is dual-stat
    (Name AC Soak Cost Enc): the Soak column populates ``mitigation``; a '-'/None Soak
    cell is a verbatim 'no soak' → mitigation None, never an invented 0."""
    items: list[CatalogItem] = []
    for row in rows:
        with row_context(ref, row):
            if has_soak:
                name, (ac, soak, cost, enc) = split_trailing(row, 4)
                mitigation = None if soak in _NA_CELLS else int(soak)
            else:
                name, (ac, cost, enc) = split_trailing(row, 3)
                mitigation = None
            items.append(
                CatalogItem(
                    id=slugify(name, srd),
                    name=name,
                    description=name,
                    category="armor",
                    value=int(cost),
                    weight=float(enc),
                    armor_class=int(ac),
                    mitigation=mitigation,
                    provenance=provenance(srd, license, ref, extracted_by),
                )
            )
    return items


def parse_melee(
    rows: list[str], srd: str, license: str, ref: str, extracted_by: str, *, has_trauma: bool
) -> list[CatalogItem]:
    """Melee parser. WWN: Name Damage Shock Enc Cost Attr. CWN adds a Trauma column:
    Name Damage Shock Trauma Enc Cost Attr. A "<die>/x<rating>/T<target>" Trauma cell
    populates trauma_die/trauma_rating/trauma_target; an N/A cell emits no trauma
    fields (defaults preserved). Shock parses verbatim in both (X/ACY, or no-Shock)."""
    items: list[CatalogItem] = []
    for row in rows:
        with row_context(ref, row):
            if has_trauma:
                name, (dice, shock, trauma, enc, cost, _attr) = split_trailing(row, 6)
            else:
                name, (dice, shock, enc, cost, _attr) = split_trailing(row, 5)
                trauma = "None"
            damage_kwargs: dict[str, object] = {"dice": dice}
            if shock not in _NA_CELLS:
                m = _SHOCK_RE.match(shock)
                if not m:
                    raise ValueError(
                        f"shock {shock!r} is not 'X/ACY' or a no-Shock cell {sorted(_NA_CELLS)}"
                    )
                damage_kwargs["shock"] = int(m["shock"])
                damage_kwargs["shock_ac"] = int(m["ac"])
            if trauma not in _NA_CELLS:
                tm = _TRAUMA_RE.match(trauma)
                if not tm:
                    raise ValueError(
                        f"trauma {trauma!r} is not '<die>/x<rating>/T<target>' or a "
                        f"no-Trauma cell {sorted(_NA_CELLS)}"
                    )
                damage_kwargs["trauma_die"] = tm["die"]
                damage_kwargs["trauma_rating"] = int(tm["rating"])
                damage_kwargs["trauma_target"] = int(tm["target"])
            items.append(
                CatalogItem(
                    id=slugify(name, srd),
                    name=name,
                    description=name,
                    category="melee_weapon",
                    value=int(cost),
                    weight=float(enc),
                    damage=DamageSpec(**damage_kwargs),  # type: ignore[arg-type]
                    provenance=provenance(srd, license, ref, extracted_by),
                )
            )
    return items


def parse_ranged(
    rows: list[str], srd: str, license: str, ref: str, extracted_by: str
) -> list[CatalogItem]:
    """Ranged parser (shared, both SRDs). Columns: Name Damage Range Mag Enc Cost.
    Damage may be NdM or NdM+B — the flat bonus splits into ``damage.bonus`` (the dice
    validator rejects a +B suffix). A '-'/None Mag cell is a verbatim 'no magazine'."""
    items: list[CatalogItem] = []
    for row in rows:
        with row_context(ref, row):
            name, (dmg, range_band, mag, enc, cost) = split_trailing(row, 5)
            m = _RANGED_DAMAGE_RE.match(dmg)
            if not m:
                raise ValueError(f"ranged damage {dmg!r} is not NdM or NdM+B notation")
            bonus = int(m["bonus"]) if m["bonus"] else 0
            magazine = None if mag in _NA_CELLS else int(mag)
            items.append(
                CatalogItem(
                    id=slugify(name, srd),
                    name=name,
                    description=name,
                    category="ranged_weapon",
                    value=int(cost),
                    weight=float(enc),
                    range_band=range_band,
                    magazine=magazine,
                    damage=DamageSpec(dice=m["dice"], bonus=bonus),
                    provenance=provenance(srd, license, ref, extracted_by),
                )
            )
    return items


def parse_general(
    rows: list[str], srd: str, license: str, ref: str, extracted_by: str
) -> list[CatalogItem]:
    """General-equipment parser (shared, both SRDs). Columns: Name Cost Enc."""
    items: list[CatalogItem] = []
    for row in rows:
        with row_context(ref, row):
            name, (cost, enc) = split_trailing(row, 2)
            items.append(
                CatalogItem(
                    id=slugify(name, srd),
                    name=name,
                    description=name,
                    category="general",
                    value=int(cost),
                    weight=float(enc),
                    provenance=provenance(srd, license, ref, extracted_by),
                )
            )
    return items


def extract_catalog(
    srd_text: str,
    *,
    srd: str,
    license: str,
    section_parsers: list[SectionSpec],
    extracted_by: str,
) -> list[CatalogItem]:
    """Parse SRD equipment-chapter text into provenance-stamped CatalogItems.

    Pure, file-free core (text in → models out) so it is unit-testable without a PDF.
    Every emitted item is ``mode=verbatim`` (ADR-145 D1). The licensing invariant
    (ADR-145 D4): ``verbatim`` requires a license that permits verbatim reuse — fails
    loud otherwise (No Silent Fallbacks).
    """
    if license not in _VERBATIM_LICENSES:
        raise ValueError(
            f"refusing to emit verbatim items for srd={srd!r} under license={license!r}: "
            f"that license does not permit verbatim reuse (ADR-145 D4). Verbatim is "
            f"permitted only under: {sorted(_VERBATIM_LICENSES)}."
        )

    headers = [h for _, h, _ in section_parsers]
    items: list[CatalogItem] = []
    for _section, header, parser in section_parsers:
        if header not in srd_text:
            # Fail loud rather than silently emitting a partial catalog.
            raise ValueError(f"SRD text is missing expected section header {header!r}")
        body = slice_section(srd_text, header, headers)
        ref = section_ref(srd, header)
        items.extend(parser(data_rows(body, ref), srd, license, ref, extracted_by))
    return items
