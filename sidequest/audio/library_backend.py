"""LibraryBackend — resolves AudioCues to file paths in genre pack audio dirs.

Story 5-4: The DJ, not the radio. Picks tracks but does NOT do playback.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from sidequest.audio.models import AudioCue, AudioLane, AudioResult
from sidequest.audio.protocol import AudioBackend
from sidequest.audio.rotator import ThemeRotator
from sidequest.genre.models import AudioConfig
from sidequest.genre.models.audio import MAX_ALIAS_HOPS
from sidequest.telemetry.spans.audio import (
    mood_alias_failed_span,
    mood_alias_resolved_span,
)

logger = logging.getLogger(__name__)

# ADR-033 Pillar 3: the music fallback mood. The interpreter already treats
# "exploration" as the universal fallback (interpreter.py classifies to it
# when no keyword matches); the track-selection fallback reuses that
# convention rather than introducing a separate config field.
DEFAULT_FALLBACK_MOOD = "exploration"


def resolve_mood_to_track_key(mood: str, cfg: AudioConfig) -> str | None:
    """Resolve ``mood`` to a key present in ``cfg.mood_tracks`` (ADR-033 Step 3).

    - Direct hit (``mood`` already a mood_tracks key): returned unchanged,
      no span, no log — the common path stays silent.
    - Declared alias: the chain is load-validated good (see
      ``AudioConfig._validate_mood_aliases``), so it resolves to a real
      mood_tracks key. Emits ``music.mood_alias_resolved``.
    - Undeclared unknown mood (not a track, not an alias — e.g. a novel
      string from the narrator/encounter): NOT silent. Emits
      ``music.mood_alias_failed`` (reason=broken_chain), logs a WARNING,
      and falls back to ``DEFAULT_FALLBACK_MOOD`` when that mood has tracks.
      Returns ``None`` only when even the default has no tracks (caller
      then returns no cue — still observed by the failed span).

    The chain walk stays defensively bounded even though load validation
    already guarantees declared chains are acyclic and shallow.
    """
    if mood in cfg.mood_tracks:
        return mood

    if mood in cfg.mood_aliases:
        start = time.perf_counter()
        seen = {mood}
        cur = cfg.mood_aliases[mood]
        depth = 1
        while cur not in cfg.mood_tracks:
            # Load validation rejects cycles/over-depth/broken declared
            # chains, so this defensive break should be unreachable in a
            # validated pack — but never spin or silently misresolve.
            if cur in seen or cur not in cfg.mood_aliases or depth >= MAX_ALIAS_HOPS:
                return _fallback(mood, "broken_chain", cfg)
            seen.add(cur)
            cur = cfg.mood_aliases[cur]
            depth += 1
        latency_ms = (time.perf_counter() - start) * 1000.0
        with mood_alias_resolved_span(
            mood_name=mood,
            resolved_to=cur,
            chain_depth=depth,
            latency_ms=latency_ms,
        ):
            pass
        return cur

    return _fallback(mood, "broken_chain", cfg)


def _fallback(mood: str, reason: str, cfg: AudioConfig) -> str | None:
    """Emit the failed span + WARNING and return the default mood (or None)."""
    fallback = DEFAULT_FALLBACK_MOOD if DEFAULT_FALLBACK_MOOD in cfg.mood_tracks else ""
    with mood_alias_failed_span(mood_name=mood, reason=reason, fallback_mood=fallback):
        pass
    logger.warning(
        "music mood %r did not resolve to a track (%s); falling back to %r",
        mood,
        reason,
        fallback or "<none available>",
    )
    return fallback or None


def _materialize_chosen(base_path: Path, chosen: str) -> str:
    """Return the chosen library entry as a resource locator string.

    Post genre-pack load (``sidequest.genre.loader._resolve_audio_urls``),
    paths on ``AudioConfig`` are already absolute CDN/local URLs — pass
    those through unchanged. Pre-load fixtures (and tests that bypass the
    loader) carry relative paths; resolve those against ``base_path`` so
    callers get an absolute filesystem path.

    Returning the raw URL string for the eager-resolved case is mandatory
    because ``pathlib.Path("https://x/y")`` collapses ``//`` to ``/`` —
    the playtest 2026-05-11 doubled-URL regression came from feeding URLs
    through ``base / chosen``.
    """
    if chosen.startswith(("http://", "https://")):
        return chosen
    return str((base_path / chosen).resolve())


class LibraryBackend(AudioBackend):
    """Resolves AudioCues to file paths from a genre pack's audio directory."""

    def __init__(self, audio_config: AudioConfig, base_path: Path) -> None:
        self._config = audio_config
        self._base_path = base_path
        self._rotator = ThemeRotator()

    @property
    def name(self) -> str:
        return "library"

    @property
    def base_path(self) -> Path:
        return self._base_path

    def resolve(self, cue: AudioCue) -> str | None:
        """Map an AudioCue to a resource locator string, or None if unresolvable.

        The returned string is either an absolute URL (eager-resolved
        production shape) or an absolute filesystem path (pre-load
        fixture shape). Callers route URLs through unchanged and strip
        the base prefix from filesystem paths before the asset-URL seam.
        """
        if cue.lane == AudioLane.MUSIC:
            return self._resolve_music(cue)
        if cue.lane == AudioLane.SFX:
            return self._resolve_sfx(cue)
        return None

    def _resolve_music(self, cue: AudioCue) -> str | None:
        if cue.mood is None:
            return None

        # Check theme families first
        theme_variations = []
        for theme in self._config.themes:
            mood_val = cue.mood.value if hasattr(cue.mood, "value") else cue.mood
            if theme.mood == mood_val:
                theme_variations.extend(theme.variations)

        if theme_variations:
            chosen_path = self._rotator.pick(
                theme_variations,
                intensity=cue.intensity,
                mood=cue.mood,
            )
            if chosen_path is None:
                return None
            return _materialize_chosen(self._base_path, chosen_path)

        # Fall back to mood_tracks — via the ADR-033 Pillar 3 alias chain.
        mood_val = cue.mood.value if hasattr(cue.mood, "value") else cue.mood
        resolved_mood = resolve_mood_to_track_key(mood_val, self._config)
        if resolved_mood is None:
            return None
        tracks = self._config.mood_tracks.get(resolved_mood, [])
        if not tracks:
            return None
        chosen_path = self._rotator.pick(tracks, intensity=cue.intensity, mood=cue.mood)
        if chosen_path is None:
            return None
        return _materialize_chosen(self._base_path, chosen_path)

    def _resolve_sfx(self, cue: AudioCue) -> str | None:
        if cue.sfx_id is None:
            return None
        variants = self._config.sfx_library.get(cue.sfx_id, [])
        if not variants:
            return None
        chosen = random.choice(variants)
        return _materialize_chosen(self._base_path, chosen)

    def list_tracks(self, lane: str) -> list[Path]:
        """List available tracks for a lane, excluding missing files."""
        if lane == "music":
            paths = []
            for tracks in self._config.mood_tracks.values():
                for track in tracks:
                    p = (self._base_path / track.path).resolve()
                    if p.exists():
                        paths.append(p)
            return paths
        if lane == "sfx":
            paths = []
            for variants in self._config.sfx_library.values():
                for variant in variants:
                    p = (self._base_path / variant).resolve()
                    if p.exists():
                        paths.append(p)
            return paths
        return []

    async def play(self, cue: AudioCue) -> AudioResult:
        """Resolve cue and return an AudioResult. Raises FileNotFoundError if unresolvable.

        ``AudioResult.audio_path`` is typed ``Path`` — this method is the
        local-disk playback surface (test/CLI only post-ADR-095, when
        playback moved to the UI). For URL-shaped resolution results the
        Path coercion is lossy, so this method is not called from the
        production wire path.
        """
        resolved = self.resolve(cue)
        if resolved is None:
            raise FileNotFoundError(f"No audio file for cue: {cue}")
        return AudioResult(
            audio_path=Path(resolved),
            duration_ms=0,
            lane=cue.lane,
            cue=cue,
            source="library",
        )

    async def warm_up(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def supports_lane(self, lane: AudioLane) -> bool:
        return lane in (AudioLane.MUSIC, AudioLane.SFX)
