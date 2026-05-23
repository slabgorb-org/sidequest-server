"""RED tests for Story 50-9 — mood_aliases alias-chain fallback (ADR-033 Pillar 3 Steps 1-3).

These tests pin the BEHAVIOR described by the story ACs against the real
production seams:

  - Track selection: ``LibraryBackend._resolve_music`` (sidequest/audio/library_backend.py)
  - Mood classification: ``AudioInterpreter.interpret`` (sidequest/audio/interpreter.py)
  - Load-time validation: building ``AudioConfig`` from pack data
  - OTEL: ``music.mood_alias_resolved`` / ``music.mood_alias_failed`` spans

Current state (the bug): ``_resolve_music`` does
``self._config.mood_tracks.get(mood_val, [])`` and ``if not tracks: return None``
— a SILENT fallback. ``mood_aliases`` is parsed but never consumed. Every alias
test below therefore fails RED until the chain is wired.

Default-fallback key: the codebase already treats ``"exploration"`` as the
universal music fallback mood (interpreter.py:267-269 hardcodes it when no
keyword matches). These tests reuse that existing convention as the
"configured default mood" of AC-1/AC-5 rather than inventing a new config
field. See the TEA deviation note in the session file.

AC-2/AC-1 reconciliation (Dev): the original RED suite asserted a *declared*
broken/cyclic alias both fails at LOAD (AC-2) and falls back at RUNTIME
(AC-1/3/5) — mutually unsatisfiable. AC-2 (story context, higher authority)
governs: a declared alias chain that does not terminate in a mood_tracks key
within 5 hops is rejected loudly at pack load. The only mood that reaches
runtime unresolved is therefore an *undeclared* unknown string (novel
narrator/encounter mood), which triggers the failed-span + WARNING + default
fallback. ``loop_detected`` / ``depth_exceeded`` are surfaced in the loud
LOAD error, not a runtime span. See the Dev deviation + blocking Conflict
finding in the session file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from sidequest.audio.interpreter import AudioInterpreter
from sidequest.audio.library_backend import LibraryBackend
from sidequest.audio.models import AudioCue, AudioLane
from sidequest.genre.models.audio import AudioConfig

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )


@pytest.fixture
def otel_capture() -> Iterator[InMemorySpanExporter]:
    """In-memory OTEL exporter for span assertions.

    Matches the local-fixture pattern used across the suite (e.g.
    tests/magic/test_innate_v1_cast_resolution.py).
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _audio_config(
    *,
    mood_tracks: dict[str, list[str]],
    mood_aliases: dict[str, str] | None = None,
    mood_keywords: dict[str, list[str]] | None = None,
) -> AudioConfig:
    """Build a minimal valid AudioConfig.

    ``mood_tracks`` values are filenames; each becomes a single MoodTrack
    whose ``title`` equals the mood key so resolved paths are identifiable.
    """
    return AudioConfig.model_validate(
        {
            "mood_tracks": {
                mood: [{"path": fname, "title": mood, "bpm": 100} for fname in fnames]
                for mood, fnames in mood_tracks.items()
            },
            "mixer": {
                "music_volume": 0.2,
                "sfx_volume": 0.2,
                "crossfade_default_ms": 2000,
            },
            "mood_aliases": mood_aliases or {},
            "mood_keywords": mood_keywords or {},
        }
    )


def _backend(cfg: AudioConfig, tmp_path: Path) -> LibraryBackend:
    return LibraryBackend(cfg, tmp_path)


def _music_cue(mood: str) -> AudioCue:
    return AudioCue(lane=AudioLane.MUSIC, mood=mood, intensity=0.5)


def _alias_spans(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


# --------------------------------------------------------------------------
# AC-1 — Alias-chain implementation in MusicDirector track selection
# --------------------------------------------------------------------------


def test_direct_hit_resolves_without_touching_aliases(tmp_path: Path) -> None:
    """Regression guard: a mood that IS in mood_tracks resolves directly."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"]},
        mood_aliases={"court": "tension"},
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("tension"))
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg")


def test_single_hop_alias_resolves_to_target_track(tmp_path: Path) -> None:
    """AC-1: 'court' is not a mood_track but aliases to 'tension' which is."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"], "exploration": ["explore.ogg"]},
        mood_aliases={"court": "tension"},
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("court"))
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg"), (
        f"alias 'court'->'tension' must yield the tension track, got {resolved!r}"
    )


def test_two_hop_alias_chain_resolves(tmp_path: Path) -> None:
    """AC-1: mood -> alias1 -> alias2 -> track."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"], "exploration": ["explore.ogg"]},
        mood_aliases={"duel": "standoff", "standoff": "tension"},
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("duel"))
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg")


def test_undeclared_unknown_mood_falls_back_to_default_not_silent_none(
    tmp_path: Path,
) -> None:
    """AC-1/AC-5: a mood that is neither a mood_track nor a *declared* alias
    (a novel string from the narrator/encounter) falls back to the
    configured default mood ('exploration'), NEVER a silent None — even when
    an unrelated valid alias map is present.

    Reconciled from the original 'broken declared alias -> runtime fallback'
    shape: AC-2 makes a *declared* broken alias a LOAD failure, so the only
    mood that reaches runtime unresolved is an undeclared one. See the Dev
    deviation + blocking Conflict finding in the session file.
    """
    cfg = _audio_config(
        mood_tracks={"exploration": ["explore.ogg"], "tension": ["tension_a.ogg"]},
        mood_aliases={"court": "tension"},  # valid, unrelated to the cue
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("uncharted_mood"))
    assert resolved is not None, "unknown mood must NOT silently resolve to None"
    assert resolved.endswith("explore.ogg"), (
        "unknown mood must fall back to the default 'exploration' track"
    )


def test_overdeep_declared_chain_rejected_at_load() -> None:
    """AC-2 (reconciled): a chain longer than the 5-hop guard is a broken
    pack — rejected loudly at LOAD (depth_exceeded), not silently walked at
    runtime."""
    with pytest.raises(Exception, match=r"(?i)alias|depth|hop") as exc:
        _audio_config(
            mood_tracks={"exploration": ["explore.ogg"], "tension": ["tension_a.ogg"]},
            mood_aliases={
                "m0": "m1",
                "m1": "m2",
                "m2": "m3",
                "m3": "m4",
                "m4": "m5",
                "m5": "m6",
                "m6": "tension",
            },
        )
    assert "m0" in str(exc.value)


def test_circular_declared_chain_rejected_at_load_m1_m2() -> None:
    """AC-2 (reconciled): m1 -> m2 -> m1 is a broken pack — rejected at LOAD
    (loop_detected). Distinct fixture from the a/b cycle test for breadth."""
    with pytest.raises(Exception, match=r"(?i)alias|cycle|circular|loop"):
        _audio_config(
            mood_tracks={"exploration": ["explore.ogg"]},
            mood_aliases={"m1": "m2", "m2": "m1"},
        )


# --------------------------------------------------------------------------
# AC-2 — Load-time validity: broken alias fails loudly at pack load
# --------------------------------------------------------------------------


def test_valid_aliases_load_cleanly() -> None:
    """AC-2: heavy_metal-style aliases (all targets are real mood_tracks)
    must construct without error."""
    cfg = _audio_config(
        mood_tracks={
            "exploration": ["e.ogg"],
            "tension": ["t.ogg"],
            "ritual": ["r.ogg"],
            "sorrow": ["s.ogg"],
        },
        mood_aliases={
            "pact": "ritual",
            "working": "ritual",
            "procession": "sorrow",
            "court": "tension",
        },
    )
    assert set(cfg.mood_aliases) == {"pact", "working", "procession", "court"}


def test_broken_alias_target_fails_loudly_at_load() -> None:
    """AC-2: an alias whose target is neither a mood_tracks key nor another
    valid alias must raise loudly at construction, naming the offender.
    No silent substitution or default-filling at load time."""
    with pytest.raises(Exception, match=r"(?i)alias") as exc:
        _audio_config(
            mood_tracks={"tension": ["t.ogg"]},
            mood_aliases={"court": "does_not_exist"},
        )
    msg = str(exc.value)
    assert "court" in msg and "does_not_exist" in msg, (
        f"load-time error must identify the broken alias and its target; got: {msg!r}"
    )


def test_circular_alias_chain_rejected_at_load() -> None:
    """AC-2: a circular alias chain is a broken pack — reject at load."""
    with pytest.raises(Exception, match=r"(?i)alias|cycle|circular|loop"):
        _audio_config(
            mood_tracks={"tension": ["t.ogg"]},
            mood_aliases={"a": "b", "b": "a"},
        )


# --------------------------------------------------------------------------
# AC-3 — OTEL observability of alias resolution
# --------------------------------------------------------------------------


def test_successful_alias_resolution_emits_resolved_span(tmp_path: Path, otel_capture) -> None:
    """AC-3: music.mood_alias_resolved with mood_name, resolved_to, chain_depth."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"]},
        mood_aliases={"duel": "standoff", "standoff": "tension"},
    )
    _backend(cfg, tmp_path).resolve(_music_cue("duel"))

    spans = _alias_spans(otel_capture, "music.mood_alias_resolved")
    assert len(spans) == 1, "exactly one resolved span expected"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("mood_name") == "duel"
    assert attrs.get("resolved_to") == "tension"
    assert attrs.get("chain_depth") == 2


def test_unresolved_mood_emits_failed_span_with_reason(tmp_path: Path, otel_capture) -> None:
    """AC-3: music.mood_alias_failed with reason=broken_chain and
    fallback_mood for an undeclared unknown mood (the only kind that
    reaches runtime unresolved after AC-2 load validation)."""
    cfg = _audio_config(
        mood_tracks={"exploration": ["explore.ogg"]},
        mood_aliases={},
    )
    _backend(cfg, tmp_path).resolve(_music_cue("court"))

    spans = _alias_spans(otel_capture, "music.mood_alias_failed")
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("mood_name") == "court"
    assert attrs.get("reason") == "broken_chain"
    assert attrs.get("fallback_mood") == "exploration"


def test_loop_detected_classified_in_load_error() -> None:
    """AC-3 (reconciled): the 'loop_detected' classification is surfaced
    loudly in the LOAD-time pack error (AC-2 makes a declared cycle
    unloadable, so it never reaches a runtime span)."""
    with pytest.raises(Exception, match=r"(?i)loop|cycle") as exc:
        _audio_config(
            mood_tracks={"exploration": ["explore.ogg"]},
            mood_aliases={"m1": "m2", "m2": "m1"},
        )
    assert "loop_detected" in str(exc.value)


def test_depth_exceeded_classified_in_load_error() -> None:
    """AC-3 (reconciled): the 'depth_exceeded' classification is surfaced
    loudly in the LOAD-time pack error."""
    with pytest.raises(Exception, match=r"(?i)depth|hop") as exc:
        _audio_config(
            mood_tracks={"exploration": ["explore.ogg"], "tension": ["t.ogg"]},
            mood_aliases={
                "m0": "m1",
                "m1": "m2",
                "m2": "m3",
                "m3": "m4",
                "m4": "m5",
                "m5": "m6",
                "m6": "tension",
            },
        )
    assert "depth_exceeded" in str(exc.value)


# --------------------------------------------------------------------------
# AC-4 — Wiring: alias chain reachable through production paths
# --------------------------------------------------------------------------


def test_wiring_encounter_mood_override_alias_resolves_through_real_backend(
    tmp_path: Path, otel_capture
) -> None:
    """AC-4 wiring: the Step-4 encounter mood_override path feeds a mood
    string straight into LibraryBackend (the real production resolver).
    Drive an alias mood ('standoff') through resolve() and assert it lands
    on the resolved mood's track AND emits an alias-resolution span — proving
    the new resolver is wired into the production call site, not isolated."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"], "exploration": ["explore.ogg"]},
        mood_aliases={"standoff": "tension"},
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("standoff"))
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg")
    assert _alias_spans(otel_capture, "music.mood_alias_resolved"), (
        "alias resolution must be observable from the production resolve() path"
    )


def test_wiring_prose_classified_alias_mood_resolves_to_track(
    tmp_path: Path,
) -> None:
    """AC-4 wiring: narrator prose triggers a genre mood_keyword whose mood
    is alias-only ('standoff' not in mood_tracks). The interpreter must be
    able to classify it (today it skips moods not in mood_tracks) and the
    backend must resolve it via the alias chain to the tension track."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"], "exploration": ["explore.ogg"]},
        mood_aliases={"standoff": "tension"},
        mood_keywords={"standoff": ["standoff", "stare down", "face off"]},
    )
    cues = AudioInterpreter().interpret(
        "Both gunmen face off across the dusty street in a tense standoff.", cfg
    )
    music = [c for c in cues if c.lane == AudioLane.MUSIC]
    assert len(music) == 1, f"expected one music cue, got {music!r}"
    assert music[0].mood == "standoff", (
        "interpreter must classify an alias-only genre mood, not skip it"
    )
    resolved = _backend(cfg, tmp_path).resolve(music[0])
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg")


def test_wiring_encounter_override_alias_resolves_to_target(
    tmp_path: Path,
) -> None:
    """AC-4: a mood string arriving from the live Step-4 encounter
    mood_override path (confrontation.py:131-132 sets ``mood =
    encounter.mood_override``) that happens to be an alias must resolve to
    the target mood's track. Guards that wiring aliases does not break the
    already-live override flow."""
    cfg = _audio_config(
        mood_tracks={"tension": ["tension_a.ogg"], "exploration": ["explore.ogg"]},
        mood_aliases={"court": "tension"},
    )
    # "court" is what confrontation.py would assign to ``mood`` from
    # ``encounter.mood_override``; the resolver must follow the alias.
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("court"))
    assert resolved is not None
    assert resolved.endswith("tension_a.ogg")


# --------------------------------------------------------------------------
# AC-5 — No silent fallbacks (Python rule #1: silent fallback / swallowing)
# --------------------------------------------------------------------------


def test_unknown_mood_no_alias_no_silent_none(tmp_path: Path, otel_capture) -> None:
    """AC-5 / rule #1: a mood absent from mood_tracks with NO alias entry
    must not just `return None` silently — it emits a failed span and falls
    back to the default mood. This is the current bug: library_backend.py:91
    returns None with zero observability."""
    cfg = _audio_config(
        mood_tracks={"exploration": ["explore.ogg"]},
        mood_aliases={},
    )
    resolved = _backend(cfg, tmp_path).resolve(_music_cue("totally_unknown"))
    assert resolved is not None, "unknown mood must not silently resolve to None"
    assert resolved.endswith("explore.ogg")
    assert _alias_spans(otel_capture, "music.mood_alias_failed"), (
        "every fallback path must emit an observable span (no silent fallback)"
    )


@pytest.mark.parametrize(
    ("aliases", "cue_mood"),
    [
        ({}, "court"),
        ({}, "ghost"),
        ({"court": "tension"}, "uncharted"),  # valid alias map, unrelated cue
    ],
)
def test_every_runtime_failure_path_is_observable(
    tmp_path: Path, otel_capture, aliases, cue_mood
) -> None:
    """AC-5: every runtime fallback (an undeclared unknown mood — the only
    kind that survives AC-2 load validation) emits exactly one failed span
    with reason=broken_chain. No silent fallback path exists."""
    cfg = _audio_config(
        mood_tracks={"exploration": ["explore.ogg"], "tension": ["t.ogg"]},
        mood_aliases=aliases,
    )
    _backend(cfg, tmp_path).resolve(_music_cue(cue_mood))
    spans = _alias_spans(otel_capture, "music.mood_alias_failed")
    assert len(spans) == 1
    assert dict(spans[0].attributes or {}).get("reason") == "broken_chain"


def test_unresolved_mood_logs_at_warning_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule #4 (logging level correctness): an unresolved mood is a
    config/drift problem -> WARNING. A clean direct hit must NOT log."""
    import logging

    cfg = _audio_config(
        mood_tracks={"exploration": ["explore.ogg"], "tension": ["t.ogg"]},
        mood_aliases={},
    )
    with caplog.at_level(logging.WARNING):
        _backend(cfg, tmp_path).resolve(_music_cue("court"))
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records), (
        "unresolved mood must log at WARNING (drift, not silent)"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _backend(cfg, tmp_path).resolve(_music_cue("tension"))
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records), (
        "a clean direct hit must not emit a warning"
    )
