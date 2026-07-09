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

    # 158-18 token-contract assertions: faction/hp/ac enrichment from live game state.
    pc_tok = pc_tokens[0]
    assert pc_tok.faction == "player", f"PC token faction must be 'player', got {pc_tok.faction!r}"
    assert pc_tok.hp is not None, "PC token hp must be populated from snapshot character"
    assert pc_tok.hp.current == 18 and pc_tok.hp.max == 22, (
        f"PC token hp must match fixture values (18/22), got {pc_tok.hp}"
    )
    assert pc_tok.ac == 14, f"PC token ac must match fixture armor_class=14, got {pc_tok.ac}"

    creature_tok = creature_tokens[0]
    assert creature_tok.faction == "hostile", (
        f"opponent creature token faction must be 'hostile', got {creature_tok.faction!r}"
    )
    assert creature_tok.hp is not None, "creature token hp must be populated from snapshot NPC"
    assert creature_tok.hp.current == 8 and creature_tok.hp.max == 12, (
        f"creature token hp must match fixture values (8/12), got {creature_tok.hp}"
    )
    assert creature_tok.ac == 13, (
        f"creature token ac must match fixture armor_class=13, got {creature_tok.ac}"
    )


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


def test_runtime_payload_populates_move_adjudications(tmp_path: Path, monkeypatch) -> None:
    """RED (Story 165-4, plan Task 9): the emit path echoes the round move summary.

    A PC standing in a tactical region during an active combat encounter gets a
    ``kind="move"`` TacticalAdjudication whose ``cells_budget`` equals the bound
    ruleset's per-turn Move (``combat_move_cells``). This is the PRODUCTION-PATH
    wiring test for the echo — the tests/protocol/ unit tests prove the payload
    *shape*; this proves ``_maybe_build_runtime_cavern_payload`` actually fills it,
    so a Dev who adds the field but never populates it still fails here.
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

    move_adjs = [a for a in payload.adjudications if a.kind == "move"]
    assert move_adjs, "emit path must echo a round move summary for present PCs"

    rux = next((a for a in move_adjs if a.actor == "Rux"), None)
    assert rux is not None, "PC 'Rux' present in the region must get a move-budget echo"
    assert rux.valid is True, "an unmoved PC's move budget echo is a valid move summary"

    # cells_budget must be the REAL bound-ruleset Move, not a hardcoded constant.
    # caverns_and_claudes binds WWN; combat_move_cells(core) == max(1, move_m // 1.5).
    from sidequest.game.ruleset.registry import get_ruleset_module

    ruleset = get_ruleset_module(sd.genre_pack.rules.ruleset)
    rux_char = next(c for c in snapshot.characters if c.core.name == "Rux")
    expected_budget = ruleset.combat_move_cells(rux_char.core)
    assert expected_budget >= 1, "sanity: a live PC always has at least 1 cell of Move"
    assert rux.cells_budget == expected_budget, (
        f"move echo must carry the bound ruleset's Move budget "
        f"({expected_budget} cells), got {rux.cells_budget}"
    )
