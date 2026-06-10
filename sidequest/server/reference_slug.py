"""Single source of truth for slug generation across the reference surface.

Both the HTML renderer (`reference_renderer`) and the URL builder
(`reference_anchors`) import from here so the two surfaces cannot drift.

Algorithm: NFKD-fold non-ASCII (the shared :func:`fold_to_ascii` core, Story
101-8), lowercase, replace any run of non-`[a-z0-9]` chars with a single hyphen,
strip leading/trailing hyphens. Diacritics fold to their base letter (``café`` →
``cafe``) rather than becoming hyphen separators; ASCII input is unchanged.
Empty input returns empty string.
"""

from __future__ import annotations

import re

from sidequest.server.slug_fold import fold_to_ascii

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Diacritics NFKD-fold to base letters; runs collapse."""
    return _SLUG_RE.sub("-", fold_to_ascii(text).lower()).strip("-")
