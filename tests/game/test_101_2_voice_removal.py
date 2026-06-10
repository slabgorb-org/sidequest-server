"""Failing tests for Story 101-2 — remove all dead voice-generation references.

Operator decision 2026-06-09: the TTS/voice-generation surface in the engine
is fully dead (zero runtime readers, ``voice_presets.yaml`` in zero packs,
zero kokoro refs, UI never reads voice fields). This story rips it out.
``/sq-voice`` remains orchestrator-level authoring tooling and is out of scope.

These tests assert the POST-removal target state, so they fail on the current
tree (RED) and pass once Dev deletes the five surfaces and adds the save-shim.

Surfaces (from context-story-101-2.md):
  1. genre/models/audio.py — VoiceConfig, VoicePresets, CreatureVoicePreset,
     AudioConfig.creature_voice_presets
  2. genre/loader.py — voice_presets.yaml optional-load path
  3. genre/models/pack.py — GenrePack.voice_presets + models/__init__ exports
  4. protocol/enums.py — MessageType.VOICE_SIGNAL / VOICE_TEXT (+ the
     VOICE_TEXT entry in game/projection/invariants.py TARGETED_KINDS)
  5. game/session.py — Npc.voice_id

AC-2 (load-bearing): ``Npc`` has ``extra=forbid`` and persisted Postgres saves
may carry ``voice_id``. Removal must tolerate those saves. The canonical home
for a backward-compat shim is ``sidequest/game/migrations.py`` (see its module
docstring: "this module is the ONLY place backward-compat shims live"). So the
fix is a new ``_migrate_s6_strip_npc_voice_id`` sub-function — mirroring the S3
pattern in ``tests/game/test_party_location_migration.py``.

Scope guard: ``ChassisVoiceSpec`` / ``register_chassis_voice_section`` /
``MixerConfig.voice_volume`` are ADR-125 chassis-voice and audio-mixer dials —
NOT TTS voice-generation — and stay. Tests below assert ONLY the five surfaces.

Wiring note (server CLAUDE.md "No Source-Text Wiring Tests"): the AC-3
"grep-guard" is implemented as reflection/import assertions on runtime types
(``hasattr``, ``model_fields``, enum membership, dict membership) — never as a
``read_text()`` grep of production source.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Surface 1 + 3 — genre model voice types are gone (reflection, not grep)
# ---------------------------------------------------------------------------


def test_voice_presets_not_exported_from_genre_models() -> None:
    """``VoicePresets`` must no longer be importable from the genre models
    package — neither in its namespace nor its ``__all__``."""
    import sidequest.genre.models as gm

    assert not hasattr(gm, "VoicePresets"), "VoicePresets still exported from genre.models"
    assert "VoicePresets" not in getattr(gm, "__all__", [])


def test_voice_config_not_exported_from_genre_models() -> None:
    """``VoiceConfig`` (single TTS voice config) must be gone."""
    import sidequest.genre.models as gm

    assert not hasattr(gm, "VoiceConfig"), "VoiceConfig still exported from genre.models"
    assert "VoiceConfig" not in getattr(gm, "__all__", [])


def test_creature_voice_preset_not_exported_from_genre_models() -> None:
    """``CreatureVoicePreset`` must be gone."""
    import sidequest.genre.models as gm

    assert not hasattr(gm, "CreatureVoicePreset"), (
        "CreatureVoicePreset still exported from genre.models"
    )
    assert "CreatureVoicePreset" not in getattr(gm, "__all__", [])


def test_audio_module_has_no_voice_types() -> None:
    """The names must be gone from the defining module too, not just the
    package re-export."""
    import sidequest.genre.models.audio as audio

    for name in ("VoiceConfig", "VoicePresets", "CreatureVoicePreset"):
        assert not hasattr(audio, name), f"{name} still defined in genre.models.audio"


def test_audio_config_has_no_creature_voice_presets_field() -> None:
    """``AudioConfig.creature_voice_presets`` field is removed."""
    from sidequest.genre.models import AudioConfig

    assert "creature_voice_presets" not in AudioConfig.model_fields, (
        "AudioConfig still declares creature_voice_presets"
    )


def test_audio_config_rejects_creature_voice_presets_payload() -> None:
    """With the field gone and ``extra=forbid``, a payload carrying
    ``creature_voice_presets`` must be rejected (proves true removal, not a
    hidden alias)."""
    from sidequest.genre.models import AudioConfig

    with pytest.raises(ValidationError):
        AudioConfig.model_validate(
            {
                "mood_tracks": {},
                "sfx_library": {},
                "mixer": {
                    "music_volume": 0.8,
                    "sfx_volume": 0.9,
                    "crossfade_default_ms": 500,
                },
                "creature_voice_presets": {},
            }
        )


def test_genre_pack_has_no_voice_presets_field() -> None:
    """``GenrePack.voice_presets`` field is removed."""
    from sidequest.genre.models.pack import GenrePack

    assert "voice_presets" not in GenrePack.model_fields, (
        "GenrePack still declares voice_presets"
    )


# ---------------------------------------------------------------------------
# Surface 2 — chassis-voice / mixer dials are NOT in scope (negative guard)
# ---------------------------------------------------------------------------


def test_mixer_voice_volume_field_is_retained() -> None:
    """Scope guard: ``MixerConfig.voice_volume`` is an audio mixer dial, NOT a
    TTS voice-generation surface. It must survive this removal — its presence
    proves the deletion stayed inside the five listed surfaces."""
    from sidequest.genre.models import MixerConfig

    assert "voice_volume" in MixerConfig.model_fields, (
        "MixerConfig.voice_volume was removed — out of scope for 101-2"
    )


def test_chassis_voice_spec_is_retained() -> None:
    """Scope guard: ``ChassisVoiceSpec`` is ADR-125 chassis-voice, not TTS.
    It must remain exported."""
    import sidequest.genre.models as gm

    assert hasattr(gm, "ChassisVoiceSpec"), (
        "ChassisVoiceSpec was removed — that is ADR-125 chassis voice, out of scope"
    )


# ---------------------------------------------------------------------------
# Surface 4 — protocol message types + routing entry are gone
# ---------------------------------------------------------------------------


def test_message_type_has_no_voice_signal_member() -> None:
    from sidequest.protocol.enums import MessageType

    assert not hasattr(MessageType, "VOICE_SIGNAL"), "MessageType.VOICE_SIGNAL still defined"


def test_message_type_has_no_voice_text_member() -> None:
    from sidequest.protocol.enums import MessageType

    assert not hasattr(MessageType, "VOICE_TEXT"), "MessageType.VOICE_TEXT still defined"


def test_message_type_rejects_voice_signal_wire_value() -> None:
    """The wire string must no longer resolve to a member."""
    from sidequest.protocol.enums import MessageType

    with pytest.raises(ValueError):
        MessageType("VOICE_SIGNAL")


def test_message_type_rejects_voice_text_wire_value() -> None:
    from sidequest.protocol.enums import MessageType

    with pytest.raises(ValueError):
        MessageType("VOICE_TEXT")


def test_targeted_kinds_has_no_voice_text_route() -> None:
    """The ``VOICE_TEXT`` recipient-routing entry in the projection invariants
    must be removed — there is no VOICE_TEXT message to route."""
    from sidequest.game.projection.invariants import TARGETED_KINDS

    assert "VOICE_TEXT" not in TARGETED_KINDS, (
        "TARGETED_KINDS still routes VOICE_TEXT"
    )


# ---------------------------------------------------------------------------
# Surface 5 — Npc.voice_id field is gone
# ---------------------------------------------------------------------------


def test_npc_has_no_voice_id_field() -> None:
    from sidequest.game.session import Npc

    assert "voice_id" not in Npc.model_fields, "Npc still declares voice_id"


# ---------------------------------------------------------------------------
# AC-2 (load-bearing) — legacy saves carrying voice_id still load
#
# Pattern mirrors S3 in tests/game/test_party_location_migration.py. The shim
# lives in sidequest/game/migrations.py as a new sub-function registered in the
# migrate_legacy_snapshot orchestrator.
# ---------------------------------------------------------------------------


def _legacy_snapshot_with_voice_id() -> dict:
    """A minimal pre-removal snapshot whose NPCs carry the dead ``voice_id``.

    A real Postgres save predating this story has ``voice_id`` on every Npc
    (assigned ``None`` at materialization, but persisted as a key). Under the
    post-removal ``Npc`` model (``extra=forbid``, no ``voice_id`` field) the
    key would raise ``ValidationError`` unless the migration strips it first.
    """
    return {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "rookhollow",
        "characters": [],
        "npcs": [
            {
                "core": {"name": "Mara", "description": "barkeep", "personality": "wry"},
                "voice_id": 7,
                "disposition": 0,
            },
            {
                "core": {"name": "Joss", "description": "smith", "personality": "gruff"},
                "voice_id": None,
                "disposition": 0,
            },
        ],
        "narrative_log": [],
    }


def test_s6_strip_voice_id_sub_function_is_registered() -> None:
    """Wire test: ``migrations`` must expose the voice_id strip sub-function,
    or the shim is dead code and legacy saves blow up on load."""
    from sidequest.game import migrations

    assert hasattr(migrations, "_migrate_s6_strip_npc_voice_id"), (
        "S6 voice_id strip sub-function missing — 101-2 AC-2 save shim not wired"
    )


def test_migrate_strips_voice_id_from_every_npc() -> None:
    """After migration, no NPC dict carries a ``voice_id`` key — so the
    post-removal ``Npc`` (extra=forbid) accepts it."""
    migrated = _run_migration(_legacy_snapshot_with_voice_id())

    for npc in migrated["npcs"]:
        assert "voice_id" not in npc, f"voice_id survived migration on {npc.get('core')}"


def test_migrated_npcs_validate_under_extra_forbid() -> None:
    """The migrated NPC dicts must hydrate into the live ``Npc`` model without
    error — this is the actual pre-removal-save load path AC-2 protects."""
    from sidequest.game.session import Npc

    migrated = _run_migration(_legacy_snapshot_with_voice_id())

    for npc in migrated["npcs"]:
        Npc.model_validate(npc)  # must not raise


def test_migration_no_op_on_snapshot_without_voice_id() -> None:
    """A canonical (post-removal) snapshot with no ``voice_id`` keys is passed
    through unchanged — the shim must not fabricate work or mutate input."""
    clean = {
        "genre_slug": "g",
        "world_slug": "w",
        "characters": [],
        "npcs": [
            {"core": {"name": "Mara", "description": "d", "personality": "p"}, "disposition": 0}
        ],
        "narrative_log": [],
    }
    migrated = _run_migration(clean)
    assert migrated["npcs"][0] == clean["npcs"][0]


# ---------------------------------------------------------------------------
# AC-2 OTEL — the strip must be observable on snapshot.canonicalize
# (OTEL Observability Principle: every subsystem decision emits a span.)
# ---------------------------------------------------------------------------


def test_voice_id_strip_routed_through_canonicalize_extractor() -> None:
    """The ``snapshot.canonicalize`` payload routed to the GM panel must
    surface the voice-strip counter when the migration ran — so the new key
    has to be added to the extractor allow-list (no defaulted keys)."""
    from types import SimpleNamespace

    from sidequest.telemetry.spans._core import SPAN_ROUTES

    route = SPAN_ROUTES["snapshot.canonicalize"]
    span = SimpleNamespace(
        name="snapshot.canonicalize",
        attributes={"s6_voice_id_stripped": 2},
    )
    payload = route.extract(span)

    assert payload.get("s6_voice_id_stripped") == 2


def test_canonicalize_span_fires_when_voice_id_stripped() -> None:
    """Wire test: a legacy save with ``voice_id`` triggers the
    ``snapshot.canonicalize`` span with the S6 counter set. In-memory OTEL
    exporter, same pattern as test_party_location_migration.py."""
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.game.migrations import migrate_legacy_snapshot
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        migrate_legacy_snapshot(_legacy_snapshot_with_voice_id())

        canonicalize = [
            s for s in exporter.get_finished_spans() if s.name == "snapshot.canonicalize"
        ]
        assert canonicalize, "canonicalize span did not fire for S6 voice_id strip"
        attrs = dict(canonicalize[-1].attributes or {})
        assert attrs.get("s6_voice_id_stripped", 0) >= 1
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_migration(snapshot: dict) -> dict:
    """Run the snapshot through the canonicalizing migration orchestrator."""
    import copy

    from sidequest.game.migrations import migrate_legacy_snapshot

    before = copy.deepcopy(snapshot)
    out = migrate_legacy_snapshot(snapshot)
    assert snapshot == before, "migrate_legacy_snapshot mutated its input"
    return out
