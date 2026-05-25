"""Unit tests for SessionHandler audio dispatch plumbing.

Extended in Task 4 with _maybe_dispatch_audio coverage.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sidequest.audio.library_backend import LibraryBackend
from sidequest.server.session_handler import _SessionData


def test_session_data_has_audio_backend_field() -> None:
    names = {f.name for f in fields(_SessionData)}
    assert "audio_backend" in names, (
        "SessionHandler needs per-session audio_backend to keep "
        "ThemeRotator cooldown state across turns."
    )


@pytest.fixture
def fake_audio_pack_dir(tmp_path: Path) -> Path:
    """A minimal on-disk genre pack structure with one mood track and one SFX."""
    audio_dir = tmp_path / "audio"
    (audio_dir / "music" / "tension").mkdir(parents=True)
    (audio_dir / "sfx").mkdir(parents=True)
    (audio_dir / "music" / "tension" / "a.ogg").touch()
    (audio_dir / "sfx" / "door_creak.ogg").touch()
    return tmp_path


def _minimal_audio_config():
    """Build an AudioConfig instance with just enough to exercise resolve()."""
    from sidequest.genre.models.audio import (
        AudioConfig,
        MixerConfig,
        MoodTrack,
    )

    return AudioConfig(
        mood_tracks={
            "tension": [MoodTrack(path="audio/music/tension/a.ogg", title="Tension", bpm=90)],
        },
        sfx_library={"door_creak": ["audio/sfx/door_creak.ogg"]},
        mixer=MixerConfig(music_volume=1.0, sfx_volume=1.0, crossfade_default_ms=3000),
    )


def test_library_backend_resolves_mood_track_under_pack_dir(
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.audio.models import AudioCue, AudioLane, MoodCategory

    backend = LibraryBackend(_minimal_audio_config(), base_path=fake_audio_pack_dir)
    cue = AudioCue(lane=AudioLane.MUSIC, mood=MoodCategory.TENSION, intensity=0.5)
    resolved = backend.resolve(cue)
    assert resolved is not None
    # post-2026-05-12: ``LibraryBackend.resolve`` returns a locator string
    # (URL or absolute filesystem path) so URL-shaped inputs aren't
    # corrupted by pathlib slash-collapse.
    assert resolved == str((fake_audio_pack_dir / "audio/music/tension/a.ogg").resolve())


def test_build_audio_backend_returns_library_backend_for_configured_pack(
    monkeypatch: pytest.MonkeyPatch,
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.genre.loader import GenreLoader
    from sidequest.server.session_handler import WebSocketSessionHandler

    monkeypatch.setattr(
        GenreLoader,
        "find",
        lambda self, code: fake_audio_pack_dir,
    )
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)
    pack = MagicMock()
    pack.audio = _minimal_audio_config()

    backend = handler._build_audio_backend("test_genre", pack)

    assert isinstance(backend, LibraryBackend)
    assert backend.base_path == fake_audio_pack_dir


def test_build_audio_backend_returns_none_when_config_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from sidequest.genre.loader import GenreLoader
    from sidequest.genre.models.audio import AudioConfig, MixerConfig
    from sidequest.server.session_handler import WebSocketSessionHandler

    monkeypatch.setattr(GenreLoader, "find", lambda self, code: tmp_path)
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)
    pack = MagicMock()
    pack.audio = AudioConfig(
        mixer=MixerConfig(music_volume=1.0, sfx_volume=1.0, crossfade_default_ms=3000),
    )

    backend = handler._build_audio_backend("empty", pack)

    assert backend is None


# ---------------------------------------------------------------------------
# _maybe_dispatch_audio — Task 4
# ---------------------------------------------------------------------------


def _dispatcher_fixture(audio_backend: LibraryBackend | None):
    """Build a throwaway _SessionData for direct dispatcher calls."""
    sd = _SessionData.__new__(_SessionData)
    sd.audio_backend = audio_backend
    sd.player_id = "p-1"
    sd.genre_slug = "test_genre"
    sd.world_slug = "test_world"
    sd.snapshot = MagicMock()
    sd.snapshot.turn_manager.interaction = 5
    return sd


def _narration_result(narration: str):
    from sidequest.agents.orchestrator import NarrationTurnResult

    return NarrationTurnResult(
        narration=narration,
        agent_name="test",
        agent_duration_ms=0,
        token_count_in=0,
        token_count_out=0,
        is_degraded=False,
        prompt_tier="delta",
    )


def test_maybe_dispatch_audio_returns_message_on_mood_hit(
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.protocol.enums import MessageType
    from sidequest.protocol.messages import AudioCueMessage
    from sidequest.server.session_handler import WebSocketSessionHandler

    backend = LibraryBackend(
        _minimal_audio_config(),
        base_path=fake_audio_pack_dir,
    )
    sd = _dispatcher_fixture(backend)
    result = _narration_result("The dungeon falls silent. Tension coils through every shadow.")
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)

    msg = handler._maybe_dispatch_audio(sd, result)

    assert isinstance(msg, AudioCueMessage)
    assert msg.type == MessageType.AUDIO_CUE
    assert msg.payload.mood == "tension"
    # Playtest 2026-04-24: server prefixes pack-relative paths so the
    # client fetches from a fully-qualified asset URL rather than the
    # Vite dev root. Default asset base is the CDN (R2); the local
    # mount form is exercised under SIDEQUEST_ASSET_BASE_URL=local.
    assert msg.payload.music_track == (
        "https://cdn.slabgorb.com/genre_packs/test_genre/audio/music/tension/a.ogg"
    )
    assert msg.player_id == "p-1"


def test_maybe_dispatch_audio_returns_none_when_backend_absent() -> None:
    from sidequest.server.session_handler import WebSocketSessionHandler

    sd = _dispatcher_fixture(None)
    result = _narration_result("Tension coils through every shadow.")
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)

    assert handler._maybe_dispatch_audio(sd, result) is None


def test_maybe_dispatch_audio_returns_none_on_empty_narration(
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.server.session_handler import WebSocketSessionHandler

    backend = LibraryBackend(
        _minimal_audio_config(),
        base_path=fake_audio_pack_dir,
    )
    sd = _dispatcher_fixture(backend)
    result = _narration_result("   ")
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)

    assert handler._maybe_dispatch_audio(sd, result) is None


def test_maybe_dispatch_audio_returns_none_when_cues_empty(
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.server.session_handler import WebSocketSessionHandler

    backend = LibraryBackend(
        _minimal_audio_config(),
        base_path=fake_audio_pack_dir,
    )
    sd = _dispatcher_fixture(backend)
    result = _narration_result("You walk along the path.")
    handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)

    assert handler._maybe_dispatch_audio(sd, result) is None


def test_maybe_dispatch_audio_swallows_exceptions_from_interpreter(
    fake_audio_pack_dir: Path,
) -> None:
    from sidequest.server import session_handler as sh

    backend = LibraryBackend(
        _minimal_audio_config(),
        base_path=fake_audio_pack_dir,
    )
    sd = _dispatcher_fixture(backend)
    result = _narration_result("Tension.")
    handler = sh.WebSocketSessionHandler.__new__(sh.WebSocketSessionHandler)

    boom = MagicMock(side_effect=RuntimeError("interpreter blew up"))
    # AudioInterpreter moved to the session_state leaf module (64-6); patch the
    # canonical class so the shared _AUDIO_INTERPRETER instance picks it up.
    with patch("sidequest.audio.interpreter.AudioInterpreter.interpret", boom):
        assert handler._maybe_dispatch_audio(sd, result) is None


def test_maybe_dispatch_audio_span_carries_dj_decision_attributes(
    fake_audio_pack_dir: Path,
) -> None:
    """sidequest.audio.dispatch span carries the resolved DJ decision.

    Regression for playtest 2026-04-24 "sidequest.audio.dispatch span
    has zero attributes — blind OTEL". Without attributes the GM panel
    can't correlate a turn's dispatch with the client-side "Unable to
    decode audio data" errors.
    """
    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.server.session_handler import WebSocketSessionHandler
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        backend = LibraryBackend(
            _minimal_audio_config(),
            base_path=fake_audio_pack_dir,
        )
        sd = _dispatcher_fixture(backend)
        result = _narration_result("The dungeon falls silent. Tension coils through every shadow.")
        handler = WebSocketSessionHandler.__new__(WebSocketSessionHandler)

        handler._maybe_dispatch_audio(sd, result)

        dispatch_spans = [
            s for s in exporter.get_finished_spans() if s.name == "sidequest.audio.dispatch"
        ]
        assert len(dispatch_spans) == 1
        attrs = dispatch_spans[0].attributes
        assert attrs["genre"] == "test_genre"
        assert attrs["turn_number"] == 5
        # mood hit — reason=dispatched, mood + music_track populated.
        assert attrs["reason"] == "dispatched"
        assert attrs["mood"] == "tension"
        assert attrs["music_track"] == (
            "https://cdn.slabgorb.com/genre_packs/test_genre/audio/music/tension/a.ogg"
        )
    finally:
        processor.shutdown()
