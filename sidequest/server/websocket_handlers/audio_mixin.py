"""Audio-dispatch mixin for the WebSocket session handler.

Extracted from ``websocket_session_handler`` as a mixin so the four audio
methods (backend construction + per-turn DJ dispatch + skip/dispatched span
emitters) move out of the 6.8k-line handler without changing a single call
site: ``WebSocketSessionHandler`` inherits this mixin, so ``self._audio_skip``
/ ``self._audio_dispatched`` resolve unchanged and external callers
(``connect.py``, the audio wiring tests) keep working via the MRO.

The OTEL tracer name is preserved (``sidequest.server.session_handler``) so
span sources do not rename — the audio-dispatch span and its watcher routes
are load-bearing for the GM panel (CLAUDE.md OTEL Observability Principle).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

from sidequest.audio.library_backend import LibraryBackend
from sidequest.genre.loader import GenreLoader
from sidequest.protocol.messages import AudioCueMessage, AudioCuePayload
from sidequest.server.audio_cue import build_audio_cue_payload
from sidequest.server.session_state import _AUDIO_INTERPRETER
from sidequest.telemetry.spans import (
    audio_backend_disabled_span,
    audio_backend_enabled_span,
    audio_dispatched_span,
    audio_skipped_span,
)

if TYPE_CHECKING:
    from sidequest.genre.models.pack import GenrePack
    from sidequest.server.session_state import _SessionData

logger = logging.getLogger(__name__)

# Preserve the original tracer name so OTEL span sources do not rename when
# these methods moved out of websocket_session_handler.py. Phase-3 principle.
tracer = trace.get_tracer("sidequest.server.session_handler")


class AudioDispatchMixin:
    """Per-session audio backend construction and per-turn DJ dispatch."""

    def _build_audio_backend(
        self,
        genre_slug: str,
        genre_pack: GenrePack,
    ) -> LibraryBackend | None:
        """Construct the per-session LibraryBackend, or None when the
        genre pack has no resolvable on-disk audio directory.

        Emits a watcher event when audio is disabled so the GM panel
        can tell whether a silent turn is because the narration had
        no cues or because audio is off entirely."""
        try:
            pack_dir = GenreLoader().find(genre_slug)
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash connect
            # Span emission replaces the prior direct ``_watcher_publish`` —
            # ``WatcherSpanProcessor`` re-emits via
            # ``SPAN_ROUTES[SPAN_AUDIO_BACKEND_DISABLED]``.
            with audio_backend_disabled_span(
                reason="pack_dir_missing",
                genre=genre_slug,
            ):
                logger.warning(
                    "audio.backend_skipped reason=pack_dir_missing genre=%s error=%s",
                    genre_slug,
                    exc,
                )
            return None

        audio_cfg = genre_pack.audio
        if audio_cfg is None:
            # Epic 74 — genre audio is optional (mechanics-only genre tier). No
            # genre audio config → no library backend. World-tier audio override
            # is a free-form file consumed elsewhere; this connect-time backend
            # reads the genre AudioConfig only. Disable loud-but-clean, same as
            # the empty-config path below (No Silent Fallbacks).
            with audio_backend_disabled_span(
                reason="no_audio_config",
                genre=genre_slug,
            ):
                logger.info(
                    "audio.backend_skipped reason=no_audio_config genre=%s",
                    genre_slug,
                )
            return None
        if not audio_cfg.mood_tracks and not audio_cfg.themes and not audio_cfg.sfx_library:
            with audio_backend_disabled_span(
                reason="empty_config",
                genre=genre_slug,
            ):
                logger.info(
                    "audio.backend_skipped reason=empty_config genre=%s",
                    genre_slug,
                )
            return None

        with audio_backend_enabled_span(
            genre=genre_slug,
            mood_count=len(audio_cfg.mood_tracks) + len(audio_cfg.themes),
            sfx_count=len(audio_cfg.sfx_library),
        ):
            logger.info(
                "audio.backend_ready genre=%s pack_dir=%s",
                genre_slug,
                pack_dir,
            )
        return LibraryBackend(audio_cfg, base_path=pack_dir)

    def _maybe_dispatch_audio(
        self,
        sd: _SessionData,
        result: object,
    ) -> AudioCueMessage | None:
        """Run the DJ: interpret narration → resolve tracks → return an
        AudioCueMessage, or None if any precondition fails. Best-effort;
        exceptions are caught and logged so audio never crashes a turn."""
        from sidequest.agents.orchestrator import NarrationTurnResult

        if not isinstance(result, NarrationTurnResult):
            return None
        if sd.audio_backend is None:
            self._audio_skip(sd, "no_audio_config")
            return None
        narration = (result.narration or "").strip()
        if not narration:
            self._audio_skip(sd, "no_narration")
            return None

        try:
            # Keep the span open across interpret + payload build so its
            # attributes can carry the *final* DJ decision (mood/track/
            # sfx). Playtest 2026-04-24 "sidequest.audio.dispatch span has
            # zero attributes — blind OTEL" — the prior impl opened the
            # span with no attributes and the GM panel couldn't tell why
            # the client was firing "Unable to decode audio data".
            with tracer.start_as_current_span("sidequest.audio.dispatch") as span:
                span.set_attribute("genre", sd.genre_slug)
                span.set_attribute("turn_number", sd.snapshot.turn_manager.interaction)
                cues = _AUDIO_INTERPRETER.interpret(
                    narration,
                    sd.audio_backend._config,  # type: ignore[attr-defined]
                )
                payload = build_audio_cue_payload(
                    cues,
                    audio_backend=sd.audio_backend,
                    genre_slug=sd.genre_slug,
                )
                # Emit the resolved cue shape so the GM panel can correlate
                # a turn's dispatch with the client-side decode errors.
                span.set_attribute("mood", payload.mood or "")
                span.set_attribute("music_track", payload.music_track or "")
                span.set_attribute("sfx_count", len(payload.sfx_triggers))
                if payload.sfx_triggers:
                    # Spans accept list attributes; truncate to keep the
                    # trace payload bounded even with long SFX batches.
                    span.set_attribute(
                        "sfx_triggers",
                        list(payload.sfx_triggers[:16]),
                    )
                span.set_attribute(
                    "reason",
                    "empty_cues"
                    if payload.mood is None and not payload.sfx_triggers
                    else "dispatched",
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash a turn
            logger.warning("audio.dispatch_failed error=%s", exc)
            self._audio_skip(sd, "error", extra={"error": type(exc).__name__})
            return None

        if payload.mood is None and not payload.sfx_triggers:
            self._audio_skip(sd, "empty_cues")
            return None

        self._audio_dispatched(sd, payload)
        return AudioCueMessage(
            payload=payload,
            player_id=sd.player_id,
        )

    def _audio_skip(
        self,
        sd: _SessionData,
        reason: str,
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits via
        # ``SPAN_ROUTES[SPAN_AUDIO_SKIPPED]``. ``extra`` is JSON-encoded
        # because OTEL drops dict attribute values; the route extract
        # returns the JSON string for dashboard parity.
        with audio_skipped_span(
            reason=reason,
            turn_number=sd.snapshot.turn_manager.interaction,
            extra=extra,
        ):
            pass

    def _audio_dispatched(
        self,
        sd: _SessionData,
        payload: AudioCuePayload,
    ) -> None:
        # Span emission replaces the prior direct ``_watcher_publish`` —
        # ``WatcherSpanProcessor`` re-emits via
        # ``SPAN_ROUTES[SPAN_AUDIO_DISPATCHED]``.
        with audio_dispatched_span(
            turn_number=sd.snapshot.turn_manager.interaction,
            mood=payload.mood or "",
            music_track=payload.music_track or "",
            sfx_count=len(payload.sfx_triggers),
        ):
            pass
