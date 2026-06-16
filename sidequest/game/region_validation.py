"""Discovered-regions write-time validator + canonicalizer (Stories 45-16, 45-17).

**Story 45-16** added the rejection rule (``validate_region_name``):
Playtest 3 Felix observed ``(aside — narrator brief)`` registered as a
traversable region alongside legitimate rooms. Aside rounds were
leaking into the region graph because the narrator-driven write paths
appended ``result.location`` and patch entries without inspecting their
shape.

**Story 45-17** adds the canonicalization rule
(``canonicalize_region_name``): same playtest, separate symptom — the
same room appeared twice (``"The Crew Quarters"`` and ``"the crew
quarters"``) because the LLM emitted surface-variant spellings on
different turns and the dedup compared raw strings. Canonicalizing to
a slug at write lets the dedup catch case-only / punctuation-only
variants.

The two helpers compose: callers run ``validate_region_name`` first,
then (on the valid path) check ``canonicalize_region_name(new) in
existing_slugs`` before appending.

Rejection rule (conservative, conjunctive):

- Empty / whitespace-only after strip
- Leading bracket character ``(``, ``[``, ``{``, ``<`` after lstrip
  (parenthetical narrator commentary — the original Felix leak)
- Contains a newline (multi-line text is never a region name)
- Length > 80 characters after strip (proper region names are short
  noun phrases — longest in test fixtures is "Felix's Workshop" at 16)

Canonicalization rule:

- NFKD-fold (so accents, em-dashes, smart quotes don't fork the slug)
- ASCII-coerce (drop characters that fold away to nothing useful)
- Lowercase
- Collapse runs of non-alphanumeric to a single ``-``
- Strip leading/trailing ``-``

The canonical form is **stable** but **lossy** — round-tripping is not
expected. Display layers should keep the original surface form; the
slug is a comparison key, not a display string.
"""

from __future__ import annotations

import re
import unicodedata

# Cap chosen so legitimate proper-noun-phrase region names pass while
# narrator prose paragraphs are blocked. 80 chars matches the longest
# observed legitimate region in fixtures plus a healthy multiplier.
_MAX_REGION_NAME_LENGTH = 80

# Opening punctuation that signals parenthetical / structural commentary
# rather than a region name. Brace and angle-bracket included because
# narrator output occasionally emits ``{system note}`` or ``<aside>``
# style asides.
_BRACKET_PREFIXES = ("(", "[", "{", "<")

# Single regex run over the lowercased ASCII form — every run of one
# or more non-alphanumeric chars becomes one ``-``. ``strip("-")``
# afterwards trims ends so " A — B " → "a-b" not "-a-b-".
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Unicode separators that ASCII-coerce to nothing (the bytes are
# stripped) but should act as word boundaries. Mapping them to ASCII
# hyphen *before* the NFKD/ASCII pass means "Crew—Freighter" slugs to
# "crew-freighter" rather than "crewfreighter".
_UNICODE_SEPARATOR_TRANSLATION = str.maketrans(
    {
        "–": "-",  # en-dash
        "—": "-",  # em-dash
        "―": "-",  # horizontal bar
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        " ": " ",  # non-breaking space
        "…": " ",  # horizontal ellipsis
    }
)


def validate_region_name(name: str | None) -> tuple[bool, str | None]:
    """Return ``(is_valid, reason)`` for a candidate region entry.

    ``reason`` is ``None`` on the valid path. On the invalid path it is
    one of: ``empty``, ``bracketed``, ``multiline``, ``too_long``. The
    reason string is the value the caller passes as the ``reason``
    attribute on the ``region.entry_rejected`` OTEL span.
    """
    if name is None:
        return False, "empty"

    stripped = name.strip()
    if not stripped:
        return False, "empty"

    if "\n" in stripped or "\r" in stripped:
        return False, "multiline"

    if stripped.startswith(_BRACKET_PREFIXES):
        return False, "bracketed"

    if len(stripped) > _MAX_REGION_NAME_LENGTH:
        return False, "too_long"

    return True, None


def canonicalize_region_name(name: str) -> str:
    """Return a stable slug form of ``name`` for cross-write dedup.

    The slug is the comparison key the write seams use to detect
    surface-variant duplicates (``"The Crew Quarters"`` and
    ``"the crew quarters!"`` collapse to the same key). Display
    layers keep the original surface form — the slug is a key, not a
    display string.

    Examples:

    >>> canonicalize_region_name("The Crew Quarters — Freighter Unpaid Debt")
    'the-crew-quarters-freighter-unpaid-debt'
    >>> canonicalize_region_name("the  crew  quarters!")
    'the-crew-quarters'
    >>> canonicalize_region_name("Tood's Dôme")
    'tood-s-dome'

    Returns the empty string for blank / unfoldable input. Callers
    should run ``validate_region_name`` first to reject those before
    reaching the canonicalizer; the empty-slug return is a defensive
    fallback rather than a designed signal.
    """
    pre = name.translate(_UNICODE_SEPARATOR_TRANSLATION)
    nfkd = unicodedata.normalize("NFKD", pre)
    ascii_form = nfkd.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_form.lower()
    return _NON_ALNUM_RE.sub("-", lowered).strip("-")


__all__ = ["canonicalize_region_name", "validate_region_name"]
