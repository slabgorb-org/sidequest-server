"""Wiring test: _maybe_build_runtime_cavern_payload populates tokens + features.

Story 158-18. The test that would have caught the hollow-payload bug:
a materialised region with party + a revealed creature emits non-empty
tokens + features. Drives the builder directly (fixture-driven behaviour
test per CLAUDE.md "No Source-Text Wiring Tests").
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


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

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
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

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
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

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
    assert payload is not None, "Builder returned None — runtime branch not entered"
    creature_tokens = [t for t in payload.tokens if t.token_id.startswith("creature:")]
    assert creature_tokens == [], "withdrawn opponent actor must NOT be placed on the map"
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

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
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


# ---------------------------------------------------------------------------
# 165-4 REWORK RED — Reviewer (rejected 2026-07-09) blocking + coverage findings.
#
# The move-summary echo block in _maybe_build_runtime_cavern_payload
# (map_emit.py:281-312) shipped with three defects the reviewer flagged:
#   [HIGH] no OTEL span on the ruleset-resolve + adjudication-build decision
#          (its two siblings in the SAME function both emit tactical_grid.*
#          watcher events; this one emits nothing → GM panel can't tell whether
#          the move echo engaged, skipped, or silently no-op'd);
#   [HIGH] getattr(sd, "genre_pack", None) / getattr(pack, "rules", None) on
#          REQUIRED non-Optional fields — masks a broken invariant as "zero
#          adjudications" instead of failing loud (No Silent Fallbacks);
#   [MED/TEST] coverage gaps — no Fate/no-combat_move_cells branch, no multi-PC.
#
# CONTRACT this RED phase pins (TEA design decision — logged as a deviation):
#   the decision emits via _watcher_publish (the tactical_grid.* ephemeral-event
#   pattern its two siblings use), event name MOVE_SUMMARY_EVENT, carrying
#   `capable` (does the bound ruleset expose a cell-based Move?), `ruleset`, and
#   `adjudication_count` (N move echoes built). Both the engaged and the
#   no-capability-skip paths MUST fire it so the panel can distinguish them.
# ---------------------------------------------------------------------------

MOVE_SUMMARY_EVENT = "tactical_grid.move_summary"


def _capture_watcher_events(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    """Intercept map_emit._watcher_publish (the sibling events' emit point).

    Mirrors the capture shape in tests/server/test_tactical_grid_otel.py so the
    move-summary span is asserted the same way tactical_grid.emitted is.
    """
    import sidequest.server.websocket_handlers.map_emit as map_emit

    events: list[tuple[str, dict[str, Any]]] = []

    def _capture(
        event: str,
        attrs: dict[str, Any],
        *,
        component: str | None = None,
        severity: str | None = None,
    ) -> Any:
        events.append((event, dict(attrs)))

    monkeypatch.setattr(map_emit, "_watcher_publish", _capture)
    return events


def _add_second_pc(snap: Any, room_id: str, *, name: str) -> None:
    """Seat a second live PC in the region so the move-summary loop echoes N>1."""
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool

    core = CreatureCore(
        name=name,
        description="A second explorer.",
        personality="Wary.",
        hp=HpPool(current=20, max=20, base_max=20),
        armor_class=15,
    )
    snap.characters.append(
        Character(
            core=core,
            backstory="A second delver who came up through the lower galleries.",
            char_class="Fighter",
            race="Human",
        )
    )
    snap.character_locations[name] = room_id


def test_move_summary_emits_watcher_span_when_move_capable(
    tmp_path: Path, monkeypatch
) -> None:
    """RED [HIGH OTEL]: the engaged move-summary decision must emit a watcher event.

    A WWN pack (combat_move_cells present) + a PC in the region builds one move
    adjudication; the decision MUST surface a tactical_grid.move_summary watcher
    event marking it engaged (capable=True) and carrying the count built, exactly
    like its siblings tactical_grid.tactical_missing / .runtime_render_skipped.

    Fails on develop: the block emits no watcher event at all — the GM panel
    cannot tell an engaged move echo from a silent no-op.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)
    events = _capture_watcher_events(monkeypatch)

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
    assert payload is not None, "Builder returned None — runtime branch not entered"

    move_count = len([a for a in payload.adjudications if a.kind == "move"])
    assert move_count == 1, "fixture seats exactly one PC — sanity for the count assertion"

    summaries = [attrs for ev, attrs in events if ev == MOVE_SUMMARY_EVENT]
    assert summaries, (
        f"{MOVE_SUMMARY_EVENT!r} watcher event did not fire — the move-summary "
        f"subsystem decision emits no span (OTEL Observability Principle). Saw: "
        f"{[ev for ev, _ in events]}"
    )
    fields = summaries[0]
    assert fields.get("capable") is True, (
        f"engaged span must mark the ruleset move-capable, got capable={fields.get('capable')!r}"
    )
    assert fields.get("adjudication_count") == move_count, (
        f"span must carry the count of move echoes built ({move_count}), got "
        f"adjudication_count={fields.get('adjudication_count')!r}"
    )


def test_move_summary_reports_skip_for_ruleset_without_move_cells(
    tmp_path: Path, monkeypatch
) -> None:
    """RED [HIGH OTEL] + [TEST] Fate coverage: a non-move ruleset is a logged skip.

    A Fate-bound pack has no combat_move_cells (it is not a WN-family ruleset), so
    the block builds ZERO move echoes — a deliberate scope boundary. Two contracts:
      * scope boundary (passes on develop): no move adjudications, no crash;
      * observability (RED on develop): the decision still emits
        tactical_grid.move_summary, marked capable=False so the GM panel sees the
        deliberate no-capability skip rather than an invisible no-op.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)
    # Swap the bound ruleset to Fate via a lightweight pack stub — the block reads
    # only pack.rules.ruleset, then the REAL registry resolves the real FateModule
    # (which genuinely lacks combat_move_cells). Survives the fail-loud fix, which
    # accesses sd.genre_pack.rules.ruleset directly.
    sd.genre_pack = SimpleNamespace(rules=SimpleNamespace(ruleset="fate"))

    events = _capture_watcher_events(monkeypatch)

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
    assert payload is not None, "Builder returned None — runtime branch not entered"

    move_adjs = [a for a in payload.adjudications if a.kind == "move"]
    assert move_adjs == [], (
        "a ruleset without a cell-based Move must contribute NO move echo "
        f"(scope boundary), got {move_adjs!r}"
    )

    summaries = [attrs for ev, attrs in events if ev == MOVE_SUMMARY_EVENT]
    assert summaries, (
        f"{MOVE_SUMMARY_EVENT!r} must fire even on the no-capability skip so the GM "
        f"panel sees the deliberate boundary — got none. Saw: {[ev for ev, _ in events]}"
    )
    fields = summaries[0]
    assert fields.get("capable") is False, (
        f"skip span must mark the ruleset non-move-capable, got capable={fields.get('capable')!r}"
    )
    assert fields.get("adjudication_count") == 0, (
        f"skip span must report zero echoes built, got {fields.get('adjudication_count')!r}"
    )


def test_move_summary_echoes_one_adjudication_per_present_pc(
    tmp_path: Path, monkeypatch
) -> None:
    """[TEST] multi-PC coverage: the echo is per-PC, not once-per-room.

    Two PCs standing in the region must each get their OWN move adjudication with
    their own actor name and budget — the reviewer flagged that every prior test
    exercised a single PC, so a loop bug (echo only the first, or collapse to one)
    would slip through. Each PC's budget is the bound ruleset's real Move.
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)
    _add_second_pc(snapshot, room_id, name="Bexley")

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    payload = _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
    assert payload is not None, "Builder returned None — runtime branch not entered"

    move_by_actor = {a.actor: a for a in payload.adjudications if a.kind == "move"}
    assert set(move_by_actor) == {"Rux", "Bexley"}, (
        f"each PC in the region must get its own move echo — got actors "
        f"{sorted(move_by_actor)}"
    )

    from sidequest.game.ruleset.registry import get_ruleset_module

    ruleset = get_ruleset_module(sd.genre_pack.rules.ruleset)
    for name, adj in move_by_actor.items():
        core = next(c.core for c in snapshot.characters if c.core.name == name)
        assert adj.cells_budget == ruleset.combat_move_cells(core), (
            f"{name}'s move echo must carry its OWN ruleset Move budget"
        )


def test_move_summary_fails_loud_when_genre_pack_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """RED [HIGH No-Silent-Fallbacks]: a missing genre_pack must crash, not skip.

    genre_pack is a REQUIRED, non-Optional _SessionData field. The move-summary
    block guarded it with getattr(sd, "genre_pack", None), so a broken invariant
    (genre_pack absent) silently degraded to "zero adjudications." Accessing
    sd.genre_pack directly makes the broken invariant fail LOUD.

    Fails on develop: the getattr guard swallows the None and the builder returns
    a payload with empty adjudications (no raise).
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)
    sd.genre_pack = None  # simulate the broken required invariant

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    with pytest.raises((AttributeError, TypeError, ValueError)):
        _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)


def test_move_summary_fails_loud_when_pack_rules_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """RED [HIGH No-Silent-Fallbacks]: a pack with no rules must crash, not skip.

    Companion to the genre_pack guard — getattr(pack, "rules", None) masks a pack
    whose required `rules` is missing. Accessing pack.rules directly must fail loud
    rather than degrade to "zero adjudications."

    Fails on develop: the getattr guard's `is not None` short-circuits the block
    and the builder returns a payload with empty adjudications (no raise).
    """
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region

    sd, snapshot, room_id = build_sd_with_tactical_region(creature_revealed=True)
    sd.genre_pack = SimpleNamespace(rules=None)  # pack present, rules invariant broken

    from sidequest.server.websocket_handlers.map_emit import (
        _maybe_build_runtime_cavern_payload,
    )

    with pytest.raises((AttributeError, TypeError, ValueError)):
        _maybe_build_runtime_cavern_payload(sd=sd, room_id=room_id, snapshot=snapshot)
