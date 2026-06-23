"""Wiring test: _maybe_build_runtime_cavern_payload populates tokens + features.

Story 158-18. The test that would have caught the hollow-payload bug:
a materialised region with party + a revealed creature emits non-empty
tokens + features. Drives the builder directly (fixture-driven behaviour
test per CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from pathlib import Path


def test_runtime_payload_has_tokens_and_features(tmp_path: Path, monkeypatch) -> None:
    """Payload from a region with a tactical block carries non-empty features + tokens.

    Party PC "Rux" is in room → entrance-anchor token present.
    One live opponent EncounterActor ("rope-spider") → creature token present.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(
        sd=sd, room_id=room_id, snapshot=snapshot
    )
    assert payload is not None, "Builder returned None — runtime branch not entered"
    assert payload.features, "features must be populated from the tactical block"
    assert payload.tokens, "tokens must be placed for the party PC present in the room"
    pc_tokens = [t for t in payload.tokens if t.token_id.startswith("pc:")]
    assert pc_tokens, "party PC 'Rux' must appear as a pc: token"
    creature_tokens = [t for t in payload.tokens if t.token_id.startswith("creature:")]
    assert creature_tokens, "revealed opponent actor must appear as a creature: token"
    assert payload.derived is not None, "derived must be set"
    assert payload.derived.pois is not None, "derived.pois must be set (may be empty list)"


def test_unrevealed_creature_not_placed(tmp_path: Path, monkeypatch) -> None:
    """Pre-ambush creatures (not yet encounter actors) must not leak onto the map.

    creature_revealed=False → encounter is None → concealment gate fires →
    no creature: tokens. The party PC token may still be present.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=False)

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(
        sd=sd, room_id=room_id, snapshot=snapshot
    )
    assert payload is not None, "Builder returned None — runtime branch not entered"
    hostile = [t for t in payload.tokens if t.token_id.startswith("creature:")]
    assert hostile == [], "pre-ambush creatures must not leak onto the map"


def test_withdrawn_actor_not_placed(tmp_path: Path, monkeypatch) -> None:
    """Actors that withdrew mid-combat must not linger on the tactical map.

    creature_revealed=True, creature_withdrawn=True → encounter is NOT None;
    the EncounterActor exists with side="opponent" but withdrawn=True.
    The concealment gate's ``not withdrawn`` filter must suppress it, so
    no creature: tokens appear even though the encounter is live.
    The party PC token (entrance anchor) may still be present.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(
        creature_revealed=True, creature_withdrawn=True
    )

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(
        sd=sd, room_id=room_id, snapshot=snapshot
    )
    assert payload is not None, "Builder returned None — runtime branch not entered"
    creature_tokens = [t for t in payload.tokens if t.token_id.startswith("creature:")]
    assert creature_tokens == [], (
        "withdrawn opponent actor must NOT be placed on the map"
    )
    # The PC (Rux) is still in the room — entrance-anchor token must be present.
    pc_tokens = [t for t in payload.tokens if t.token_id.startswith("pc:")]
    assert pc_tokens, "party PC 'Rux' must still appear even when opponent withdrew"
