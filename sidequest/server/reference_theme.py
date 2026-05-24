"""Per-pack theme loader for reference pages (Story 63-4 Task 18).

Loads ``theme.yaml`` from a pack directory and returns a ``ReferenceTheme``
dataclass with the palette, fonts, archetype, and dinkus glyphs that the
reference renderer needs to emit chrome.

Missing required fields raise ``MissingThemeFieldError`` LOUD — no silent
fallback per SideQuest doctrine. Every missing-field path emits an
``sidequest.reference.theme_missing`` ERROR span so the GM panel can see
chrome-render failures without grepping logs.
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
