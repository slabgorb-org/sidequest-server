"""Single source of truth for slug generation across the reference surface.

Both the HTML renderer (`reference_renderer`) and the URL builder
(`reference_anchors`) import from here so the two surfaces cannot drift.

Algorithm: lowercase, replace any run of non-`[a-z0-9]` chars with a single
hyphen, strip leading/trailing hyphens. ASCII-only; non-ASCII chars become
hyphen separators. Empty input returns empty string.
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Non-ASCII chars become separators; runs collapse."""
    return _SLUG_RE.sub("-", text.lower()).strip("-")
