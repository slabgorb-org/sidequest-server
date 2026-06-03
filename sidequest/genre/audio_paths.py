"""Single rule for turning an audio.yaml relative path into a served URL.

A track ``path`` whose first segment is ``assets/`` lives in the shared
``genre_packs/assets/`` bucket (the "bucket o' music", mirroring
``genre_packs/assets/fonts/``) and resolves WITHOUT a pack slug. Any other
relative path is pack-local and resolves under ``genre_packs/<slug>/``.

This is the one place the convention is defined; the genre loader's eager
resolution and the turn-time audio_cue prefixer both call it, so the rule
cannot drift between them (per the 2026-05-10 playtest that found audio was
the one media path bypassing the asset_urls seam).
"""

from __future__ import annotations

from sidequest.server.asset_urls import resolve_asset_url

SHARED_PREFIX = "assets/"


def resolve_audio_relpath(rel: str, *, genre_slug: str) -> str:
    """Resolve an audio.yaml relative path to a full asset URL.

    Absolute URLs (``http://``, ``https://``) and server-absolute paths
    (``/...``) pass through untouched.
    """
    if not rel:
        return rel
    if rel.startswith(("http://", "https://", "/")):
        return rel
    if rel.startswith(SHARED_PREFIX):
        return resolve_asset_url(f"genre_packs/{rel}", scope="shared")
    return resolve_asset_url(f"genre_packs/{genre_slug}/{rel}", scope="pack")
