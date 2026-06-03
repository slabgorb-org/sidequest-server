"""Single-seam URL builder for player-facing media assets.

Toggled by the SIDEQUEST_ASSET_BASE_URL env var:

* unset / not present  -> https://cdn.slabgorb.com (production default)
* "" or "local"        -> local-serve fallback (/genre/* and /renders/*)
* any other URL        -> use that URL as the prefix

The local-serve fallback exists for offline dev and rollback. It maps the
two top-level R2 prefixes back onto the existing static mounts:

* genre_packs/<rest>  -> /genre/<rest>  (mounted by app.py against
  $SIDEQUEST_GENRE_PACKS)
* artifacts/<rest>    -> /renders/<rest>  (legacy back-compat for
  pre-migration artifacts written under ~/.sidequest/)

Per CLAUDE.md no-silent-fallbacks rule: a relative_path with an unknown
top-level prefix raises in local mode. CDN mode tolerates anything (the
404 is the lie detector).
"""

from __future__ import annotations

import os
import re
from typing import Final

from sidequest.telemetry.spans.asset_url import asset_url_resolved_span

_DEFAULT_BASE: Final[str] = "https://cdn.slabgorb.com"

_LOCAL_PREFIX_MAP: Final[dict[str, str]] = {
    "genre_packs/": "/genre/",
    "artifacts/": "/renders/artifacts/",
}


def _local_path_for(relative: str) -> str:
    for prefix, replacement in _LOCAL_PREFIX_MAP.items():
        if relative.startswith(prefix):
            return replacement + relative[len(prefix) :]
    raise ValueError(
        f"unknown asset prefix in local mode: {relative!r} "
        f"(expected one of {sorted(_LOCAL_PREFIX_MAP)})"
    )


def resolve_asset_url(relative_path: str) -> str:
    """Convert a content-relative path to the URL the UI should fetch.

    Examples (default config):
      "genre_packs/cav/audio/music/combat.ogg"
        -> "https://cdn.slabgorb.com/genre_packs/cav/audio/music/combat.ogg"
      "artifacts/dungeon/0d8e/portraits/abc.png"
        -> "https://cdn.slabgorb.com/artifacts/dungeon/0d8e/portraits/abc.png"
    """
    rel = relative_path.lstrip("/")
    base = os.environ.get("SIDEQUEST_ASSET_BASE_URL", _DEFAULT_BASE)
    if base in ("", "local"):
        url = _local_path_for(rel)
        mode = "local"
    else:
        url = f"{base.rstrip('/')}/{rel}"
        mode = "cdn"

    with asset_url_resolved_span(relative_path=rel, base_url=base or "", mode=mode):
        pass
    return url


# Matches a CSS url() token, capturing the (optional) quote char and the inner
# path. Only genre-pack-relative paths are rewritten; everything else (data:,
# absolute http(s), unrelated relative paths) is left byte-for-byte intact.
_CSS_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"""url\(\s*(?P<q>['"]?)(?P<path>[^'")]+)(?P=q)\s*\)"""
)


def rewrite_theme_css_asset_urls(css: str) -> str:
    """Resolve genre-pack-relative ``url()``s in injected theme CSS.

    Genre ``client_theme.css`` authors font (and other asset) ``url()``s as
    either the local-mount form ``/genre/...`` or the content-relative form
    ``genre_packs/...``. Routing them through :func:`resolve_asset_url` makes
    them honor the same single seam as images/audio — absolute CDN in prod,
    ``/genre/...`` in offline-local mode — instead of being served raw off the
    UI origin (where the CDN bytes never reach R2).

    Foreign ``url()``s (``data:``, absolute ``http(s)://``, or relative paths
    that are not genre-pack assets) are left untouched.
    """

    def _replace(m: re.Match[str]) -> str:
        quote = m.group("q")
        path = m.group("path").strip()
        if path.startswith("/genre/"):
            key = "genre_packs/" + path[len("/genre/") :]
        elif path.startswith("genre_packs/"):
            key = path
        else:
            return m.group(0)  # not a genre asset — leave intact
        return f"url({quote}{resolve_asset_url(key)}{quote})"

    return _CSS_URL_RE.sub(_replace, css)
