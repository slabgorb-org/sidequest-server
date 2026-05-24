"""Per-pack theme loader and chrome metadata for reference pages.

Two responsibilities:

1. **Theme loader** (Story 63-4 Task 18) — ``load_reference_theme`` reads
   ``theme.yaml`` from a pack dir and returns a ``ReferenceTheme`` with
   palette, fonts, archetype, and dinkus glyphs. Missing fields raise
   ``MissingThemeFieldError`` LOUD with an ``sidequest.reference.theme_missing``
   ERROR span — no silent fallback.

2. **Chrome metadata** (Story 63-7 Tasks B + C) — ``PACK_LABELS``,
   ``PACK_BLURBS``, ``PACK_EPIGRAPHS``, ``PACK_TOC``, ``TOC_TO_FILES`` are
   Python ports of the design-bundle constants at
   ``docs/design-bundles/2026-05-23-lore-and-rules/project/app.jsx``
   (``PACK_META`` at lines 12-67, ``PACK_TOC`` at lines 183-218). Pack
   entries the bundle covers are ported verbatim; packs the bundle does
   not cover (elemental_harmony, pulp_noir, road_warrior, spaghetti_western,
   tea_and_murder) get on-genre Python-authored entries here. Adding a new
   live pack means adding an entry to all four constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sidequest.telemetry.spans.reference import reference_theme_missing_span


class MissingThemeFieldError(Exception):
    """Raised when theme.yaml lacks a required chrome field. Loud, no fallback."""


@dataclass(frozen=True)
class ReferenceTheme:
    """Per-pack theme tokens consumed by reference-page chrome."""

    archetype: str
    palette_primary: str
    palette_accent: str
    palette_background: str
    web_font_family: str
    display_font_family: str
    dinkus_light: str
    dinkus_medium: str
    dinkus_heavy: str


def _require_str(value: Any, key_path: str, pack: str) -> str:
    """Return ``value`` as a non-empty string or raise MissingThemeFieldError loud.

    Emits a ``sidequest.reference.theme_missing`` ERROR span with the pack
    name and missing-field key path so the GM panel can see chrome failures.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        with reference_theme_missing_span(pack=pack, field=key_path):
            raise MissingThemeFieldError(
                f"theme.yaml missing required field {key_path!r} for pack {pack!r}"
            )
    return str(value)


def load_reference_theme(pack_dir: Path) -> ReferenceTheme:
    """Load ``<pack_dir>/theme.yaml`` and return a ReferenceTheme.

    Raises ``MissingThemeFieldError`` if the file is absent, unparseable, or
    missing any of: archetype, primary, accent, background, web_font_family,
    display_font_family, dinkus.glyph.{light,medium,heavy}.
    """
    pack = pack_dir.name
    theme_path = pack_dir / "theme.yaml"
    if not theme_path.is_file():
        with reference_theme_missing_span(pack=pack, field="theme.yaml"):
            raise MissingThemeFieldError(f"theme.yaml not found for pack {pack!r}")
    with theme_path.open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            # Honor the docstring contract: every missing/broken theme.yaml
            # path surfaces as MissingThemeFieldError, not the raw yaml error.
            with reference_theme_missing_span(pack=pack, field="theme.yaml"):
                raise MissingThemeFieldError(
                    f"theme.yaml for pack {pack!r} is malformed: {exc}"
                ) from exc
    glyph = (data.get("dinkus") or {}).get("glyph") or {}
    return ReferenceTheme(
        archetype=_require_str(data.get("archetype"), "archetype", pack),
        palette_primary=_require_str(data.get("primary"), "primary", pack),
        palette_accent=_require_str(data.get("accent"), "accent", pack),
        palette_background=_require_str(data.get("background"), "background", pack),
        web_font_family=_require_str(data.get("web_font_family"), "web_font_family", pack),
        display_font_family=_require_str(
            data.get("display_font_family"), "display_font_family", pack
        ),
        dinkus_light=_require_str(glyph.get("light"), "dinkus.glyph.light", pack),
        dinkus_medium=_require_str(glyph.get("medium"), "dinkus.glyph.medium", pack),
        dinkus_heavy=_require_str(glyph.get("heavy"), "dinkus.glyph.heavy", pack),
    )


# ---------------------------------------------------------------------------
# Chrome metadata (Story 63-7 Tasks B + C)
# ---------------------------------------------------------------------------
#
# Python ports of the design-bundle constants at
# ``docs/design-bundles/2026-05-23-lore-and-rules/project/app.jsx``
# (``PACK_META`` at lines 12-67, ``PACK_TOC`` at lines 183-218). The
# bundle covers six packs (heavy_metal, space_opera, victoria,
# mutant_wasteland, caverns_and_claudes, neon_dystopia); five live packs
# (elemental_harmony, pulp_noir, road_warrior, spaghetti_western,
# tea_and_murder) are not in the bundle and have on-genre Python-authored
# entries here.
#
# A pack absent from these constants triggers the documented unknown-pack
# fallback (see ``DEFAULT_TOC`` and ``reference_toc_missing_span``) — that
# is a loud-failure path, not a silent default, and is reserved for
# genuine drift (new pack added to content without chrome metadata).


# Pack display labels (title-case for hero "{label} · Lore & Rules" line).
PACK_LABELS: dict[str, str] = {
    # Ported from app.jsx PACK_META.{pack}.label.
    "heavy_metal": "Heavy Metal",
    "space_opera": "Space Opera",
    "victoria": "Victoria",
    "mutant_wasteland": "Mutant Wasteland",
    "caverns_and_claudes": "Caverns & Claudes",
    "neon_dystopia": "Neon Dystopia",
    # Authored locally — not in the design bundle.
    "elemental_harmony": "Elemental Harmony",
    "pulp_noir": "Pulp Noir",
    "road_warrior": "Road Warrior",
    "spaghetti_western": "Spaghetti Western",
    "tea_and_murder": "Tea and Murder",
}


# One-line kicker shown above the hero title — used as a fallback when
# the world's ``lore.world.description`` field is missing/empty.
PACK_BLURBS: dict[str, str] = {
    # Ported from app.jsx PACK_META.{pack}.blurb.
    "heavy_metal": "for the houses that are ending",
    "space_opera": "a crew that shouldn't work but somehow does",
    "victoria": "a scandal about to catch fire",
    "mutant_wasteland": "after the radiation, before the answer",
    "caverns_and_claudes": "a torch, a map, and a long way down",
    "neon_dystopia": "the future works exactly as designed",
    # Authored locally — short, on-genre, kept in the same one-clause shape.
    "elemental_harmony": "the four bonds remember every promise",
    "pulp_noir": "the case is open and the rain hasn't stopped",
    "road_warrior": "the tank reads empty and the horizon doesn't",
    "spaghetti_western": "a long ride, a quiet town, an open grave",
    "tea_and_murder": "the kettle is on and someone won't see breakfast",
}


# Hero epigraph — body + attribution, both inserted into the
# ``<div class="hero-epigraph"><span class="attrib">…</span></div>``
# block. The bundle's CSS treats body and attrib distinctly, so the
# two-field shape is load-bearing.
PACK_EPIGRAPHS: dict[str, dict[str, str]] = {
    # Ported from app.jsx PACK_META.{pack}.epigraph.
    "heavy_metal": {
        "body": (
            "Magic in this world is never studied and cast. It is bargained "
            "for, inherited, or paid for — always paid for — in blood, soul, "
            "years, names, flesh, or memory. Every working is a withdrawal "
            "from a ledger, and every ledger has an account-holder."
        ),
        "attrib": "On the metaphysics of obligation",
    },
    "space_opera": {
        "body": (
            "You're standing on the bridge of a ship that shouldn't be yours, "
            "staring at a jump point that leads somewhere dangerous, with a "
            "crew that trusts you more than you trust yourself."
        ),
        "attrib": "On the core vibe",
    },
    "victoria": {
        "body": (
            "Someone in this house is lying. The question is whether finding "
            "the truth will save you or destroy everything you've built."
        ),
        "attrib": "On the core vibe",
    },
    "mutant_wasteland": {
        "body": (
            "The world ended once and is in no hurry to do it again. What "
            "survives is paid for in salvage, mutation, and the long memory "
            "of land that remembers its wounds."
        ),
        "attrib": "On what is left",
    },
    "caverns_and_claudes": {
        "body": (
            "The hamlet sleeps badly. Three dungeon mouths gape at the "
            "borders of a single quiet village, and the torchlight only "
            "carries so far. Press a door and something old answers."
        ),
        "attrib": "On what waits below",
    },
    "neon_dystopia": {
        "body": (
            "The grid never sleeps. Every transaction is observed. Every "
            "observation is monetized. You are the product, the protocol, "
            "and the patch the system was waiting for."
        ),
        "attrib": "On the optimized state",
    },
    # Authored locally — same body + attrib shape, two-sentence ceiling
    # to match the bundle's economy.
    "elemental_harmony": {
        "body": (
            "Fire remembers being lit. Water remembers being still. Earth "
            "remembers being held. Air remembers being breathed. The world "
            "is a long correspondence between elements who knew each other "
            "before there were names."
        ),
        "attrib": "On the four bonds",
    },
    "pulp_noir": {
        "body": (
            "The door opens, the city looks in, and the rain follows. "
            "Everyone has a price, a story, and a way out of the room; "
            "no two of them line up."
        ),
        "attrib": "On the open case",
    },
    "road_warrior": {
        "body": (
            "There is no road home because there is no home. There is the "
            "tank, the engine, the horizon, and the next town that may or "
            "may not still be a town. The first three are all you can trust."
        ),
        "attrib": "On the long drive",
    },
    "spaghetti_western": {
        "body": (
            "A man rides into town. The town has been waiting for him. "
            "Whether the town knows that yet is the only thing left to "
            "discover."
        ),
        "attrib": "On the slow arrival",
    },
    "tea_and_murder": {
        "body": (
            "The garden is in full bloom, the kettle is on, and a body lies "
            "in the morning room. Everyone has an alibi, an opinion, and a "
            "very particular way of taking their tea."
        ),
        "attrib": "On the well-set table",
    },
}


# Per-pack table of contents — ordered list of section descriptors.
# Each entry is ``{"num": str, "id": str, "label": str}``:
# - ``num``: Roman numeral or other display prefix (rendered in ``.toc-num``)
# - ``id``: matches the ``<section id="…">`` wrapper in the rendered body
# - ``label``: displayed text of the TOC link
#
# Unknown packs fall through to ``DEFAULT_TOC`` AND fire the
# ``sidequest.reference.toc_missing`` ERROR span — never silent.
PACK_TOC: dict[str, list[dict[str, str]]] = {
    # Ported from app.jsx PACK_TOC (lines 183-218).
    "heavy_metal": [
        {"num": "I", "id": "reckoning", "label": "The Reckoning"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
        {"num": "III", "id": "edge", "label": "The Edge"},
        {"num": "IV", "id": "confrontations", "label": "Confrontations"},
        {"num": "V", "id": "affinities", "label": "Affinities"},
        {"num": "VI", "id": "power-tiers", "label": "Power Tiers"},
        {"num": "VII", "id": "inventory", "label": "Inventory"},
        {"num": "VIII", "id": "vocab", "label": "Beat Vocabulary"},
    ],
    "space_opera": [
        {"num": "I", "id": "reckoning", "label": "The Cul-de-Sac"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
        {"num": "III", "id": "confrontations", "label": "Confrontations"},
        {"num": "IV", "id": "affinities", "label": "Affinities"},
        {"num": "V", "id": "power-tiers", "label": "Power Tiers"},
        {"num": "VI", "id": "inventory", "label": "Inventory"},
    ],
    "victoria": [
        {"num": "I", "id": "reckoning", "label": "The House"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "mutant_wasteland": [
        {"num": "I", "id": "reckoning", "label": "The Reach"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "caverns_and_claudes": [
        {"num": "I", "id": "reckoning", "label": "The Hamlet"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "neon_dystopia": [
        {"num": "I", "id": "reckoning", "label": "The Grid"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    # Authored locally — minimal 2-item TOC matching the bundle's minor-pack
    # shape (heavy_metal and space_opera are the only "deep" TOCs). When a
    # pack earns deeper sections, extend its entry here.
    "elemental_harmony": [
        {"num": "I", "id": "reckoning", "label": "The Bonds"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "pulp_noir": [
        {"num": "I", "id": "reckoning", "label": "The Case"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "road_warrior": [
        {"num": "I", "id": "reckoning", "label": "The Road"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "spaghetti_western": [
        {"num": "I", "id": "reckoning", "label": "The Town"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
    "tea_and_murder": [
        {"num": "I", "id": "reckoning", "label": "The House"},
        {"num": "II", "id": "bearing", "label": "Bearing & Make"},
    ],
}


# Plan-mandated 2-item default when ``PACK_TOC`` has no entry for the
# requested pack. Per plan line 2780 + AC10, this path also fires the
# ``sidequest.reference.toc_missing`` ERROR span so the GM panel surfaces
# the gap — silent fallback is forbidden.
DEFAULT_TOC: list[dict[str, str]] = [
    {"num": "I", "id": "reckoning", "label": "The World"},
    {"num": "II", "id": "bearing", "label": "Bearing & Make"},
]


# Section-id → file-stem mapping. Maps each ``PACK_TOC`` entry's ``id``
# to the list of YAML file stems whose rendered content belongs in that
# section. The reference renderer wraps the concatenated file renders
# in a single ``<section id="{toc.id}">…</section>`` so the TOC link
# resolves to the correct anchor.
#
# Missing files in a pack → the section renders empty (no entries
# dropped from the TOC). File stems not referenced here render at the
# end of the page in their own ``<section class="file">`` wrappers so
# content is never lost — see ``_section_for_stem`` in
# ``reference_renderer.py``.
TOC_TO_FILES: dict[str, list[str]] = {
    "reckoning": ["lore", "world", "history"],
    "bearing": [
        "archetypes",
        "classes",
        "cultures",
        "progression",
    ],
    "edge": ["rules"],
    "confrontations": ["rules", "tropes"],
    "affinities": ["magic"],
    "power-tiers": ["power_tiers"],
    "inventory": ["inventory", "equipment_tables"],
    "vocab": ["beat_vocabulary"],
}
