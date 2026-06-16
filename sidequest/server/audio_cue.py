"""Build AUDIO_CUE wire payload from AudioInterpreter output."""

from __future__ import annotations

from pathlib import Path

from sidequest.audio.library_backend import LibraryBackend
from sidequest.audio.models import AudioCue, AudioLane
from sidequest.protocol.messages import AudioCuePayload
from sidequest.server.asset_urls import resolve_asset_url


def build_audio_cue_payload(
    cues: list[AudioCue],
    *,
    audio_backend: LibraryBackend | None = None,
    genre_slug: str | None = None,
) -> AudioCuePayload:
    """Convert AudioInterpreter output to an AudioCuePayload wire object.

    Args:
        cues: AudioCue list from AudioInterpreter.interpret().
        audio_backend: Optional LibraryBackend used to resolve each cue to a
            file path. When provided, music_track and each sfx_triggers
            entry are library-relative paths (relative to
            ``audio_backend.base_path``). When absent, music_track stays
            ``None`` and sfx_triggers carry the raw sfx_id.
        genre_slug: Optional genre pack slug. When provided, each resolved
            pack-relative path is routed through ``resolve_asset_url`` so
            the URL targets either the CDN (default) or the local
            ``/genre/{slug}/`` mount when ``SIDEQUEST_ASSET_BASE_URL=local``.
            Without this, a turn would emit ``audio/music/foo.ogg`` and the
            client would fetch it from the Vite dev-server root, 404ing as
            "Unable to decode audio data" (playtest 2026-04-24). Routing
            through the asset_urls seam matches renders, room files, and
            the genre loader; previously, audio_cue was the only media
            path that hand-rolled its own prefix and bypassed the seam,
            so even with R2 sync live, audio always pointed at the local
            mount and silently 404'd (playtest 2026-05-10). Leave ``None``
            for tests / CLI consumers that don't live behind the HTTP mount.

    Returns:
        Fully-populated AudioCuePayload. All fields default to
        empty/None when no matching cues exist.
    """
    mood: str | None = None
    music_track: str | None = None
    sfx_triggers: list[str] = []

    def _maybe_prefix(relative: str) -> str:
        """Resolve a pack-relative path through the ``asset_urls`` seam so
        the client fetches it from the configured asset base (CDN by
        default, local mount when ``SIDEQUEST_ASSET_BASE_URL=local``).
        Paths that are already absolute URLs or server-absolute pass
        through untouched so callers that pre-wrap don't double-prefix.
        """
        if not relative:
            return relative
        if relative.startswith(("http://", "https://", "/")):
            return relative
        if genre_slug is None:
            return relative
        return resolve_asset_url(f"genre_packs/{genre_slug}/{relative}")

    for cue in cues:
        if cue.lane == AudioLane.MUSIC and cue.mood is not None:
            mood = cue.mood
            if audio_backend is not None:
                resolved = _relative_to_backend(audio_backend, cue)
                music_track = _maybe_prefix(resolved) if resolved is not None else None
        elif cue.lane == AudioLane.SFX and cue.sfx_id is not None:
            entry = cue.sfx_id
            if audio_backend is not None:
                rel = _relative_to_backend(audio_backend, cue)
                if rel is not None:
                    entry = _maybe_prefix(rel)
            sfx_triggers.append(entry)

    return AudioCuePayload(
        mood=mood,
        music_track=music_track,
        sfx_triggers=sfx_triggers,
    )


def _relative_to_backend(backend: LibraryBackend, cue: AudioCue) -> str | None:
    """Resolve a cue through the backend and return the wire-shape locator.

    ``backend.resolve(cue)`` returns either an absolute URL (post genre-load,
    the production shape per ``sidequest.genre.loader._resolve_audio_urls``)
    or an absolute filesystem path (pre-load fixtures and tests that bypass
    the loader). URLs flow through unchanged so ``_maybe_prefix``'s
    startswith check correctly passes them through; filesystem paths get
    their ``base_path`` prefix stripped so ``_maybe_prefix`` can re-prefix
    through ``resolve_asset_url``.

    The playtest 2026-05-11 doubled-URL regression came from
    ``str((base / "https://x").resolve().relative_to(base))`` producing
    ``"https:/x"`` (single slash, pathlib normalization) — that string
    failed the ``startswith("https://")`` gate and got prepended again.
    """
    resolved = backend.resolve(cue)
    if resolved is None:
        return None
    if resolved.startswith(("http://", "https://")):
        return resolved
    base = backend.base_path
    try:
        return str(Path(resolved).relative_to(base))
    except ValueError:
        return resolved
