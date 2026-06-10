"""Shared non-ASCII normalization core for slug derivation (Story 101-8).

The single source of truth for how SideQuest folds non-ASCII characters when
building slugs. Every slug surface — the portrait/player rule
(:func:`sidequest.server.utils.slugify_player_name`), the reference/POI rule
(:func:`sidequest.server.reference_slug.slugify`), the daemon's render-side file
namer (``sidequest_daemon.media.catalogs._slugify_name``), and the render script
(``scripts.render_common.slugify``) — applies THIS fold as its first step, then
layers its own separator policy on top. The separator policies differ by surface
(portraits use ``_``, reference uses ``-``) and are intentionally preserved so
ASCII slugs are byte-for-byte unchanged; only the non-ASCII handling is unified.

**Canonical decision (ratified by Keith, 2026-06-10):** stdlib NFKD fold. We
``unicodedata.normalize("NFKD", …)`` to decompose precomposed letters into a base
letter plus combining marks, then drop the combining marks — so ``é→e``, ``á→a``,
``ą→a``, ``ñ→n``. No transliteration dependency (``anyascii``/``unidecode``) is
added.

**Documented limitation:** NFKD does not decompose stand-alone letters that carry
no combining mark — ``ł`` (U+0142), ``ø``, ``þ``, Cyrillic, etc. Those survive the
fold unchanged here and are dropped by each surface's ``[^a-z0-9…]`` filter
(``Łódź`` → ``odz``). Swapping in a full transliteration library is a deliberate
future step, not a silent default.

**This is distinct from the evropi blank-portrait incident** (a stale
``r2_manifest.json`` in a server clone, fixed separately) — that was a manifest
staleness bug, not a slug-derivation bug. Do not conflate the two.
"""

from __future__ import annotations

import unicodedata


def fold_to_ascii(text: str) -> str:
    """NFKD-fold ``text``: decompose precomposed letters and strip combining marks.

    Diacritics fold to their base letter (``café`` → ``cafe``). Characters with no
    NFKD decomposition (``ł``, ``ø``, ``þ``, Cyrillic) pass through unchanged here;
    each caller's existing ``[^a-z0-9…]`` filter drops them. Returns the folded
    string WITHOUT lowercasing or separator handling — callers apply those.
    """
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
