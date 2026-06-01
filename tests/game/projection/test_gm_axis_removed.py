"""Story 71-35 contract: the dead is_gm / GM-seat axis is gone.

SideQuest's thesis is *the narrator is the GM; every human is a player* —
no human occupies a GM seat. The ``is_gm()`` predicate, the
``gm_player_id`` field, and the ``CoreInvariantStage`` ``gm_sees_all``
short-circuit were ported from tabletop-VTT convention and were already
**always-false dead code** (``gm_player_id`` is initialized ``None`` and
never reassigned). This module is the executable contract that the axis
has been *deleted* (behaviour-preserving), not merely wired false.

These assertions FAIL while the axis still exists (RED) and PASS once Dev
removes it (GREEN). The firewall must then stand on player-identity
predicates alone (is_self / is_owner_of / in_same_zone / visible_to /
in_same_party). The narrator still gets canonical state because it is
server-side and NOT a projection recipient at all — not because it is
"the GM" in the filter.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

import sidequest.game.projection.predicates as predicates_mod
from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.invariants import CoreInvariantStage
from sidequest.game.projection.predicates import PREDICATES
from sidequest.game.projection.view import GameStateView, SessionGameStateView


def test_is_gm_not_in_predicate_registry() -> None:
    """The closed predicate vocabulary no longer offers ``is_gm``."""
    assert "is_gm" not in PREDICATES


def test_surviving_predicates_are_player_identity_only() -> None:
    """The firewall stands on player-identity predicates alone."""
    assert set(PREDICATES) == {
        "is_self",
        "is_owner_of",
        "in_same_zone",
        "visible_to",
        "in_same_party",
    }


def test_is_gm_predicate_function_is_deleted() -> None:
    """The ``_is_gm`` implementation is removed from the module, not just
    unregistered — dead code is worse than no code (CLAUDE.md)."""
    assert not hasattr(predicates_mod, "_is_gm")


def test_game_state_view_protocol_has_no_is_gm() -> None:
    """The read-only projection Protocol no longer declares ``is_gm``."""
    assert not hasattr(GameStateView, "is_gm")


def test_session_game_state_view_has_no_gm_player_id_field() -> None:
    """The GM-seat field is gone from the dataclass entirely."""
    field_names = {f.name for f in fields(SessionGameStateView)}
    assert "gm_player_id" not in field_names


def test_session_game_state_view_has_no_is_gm_method() -> None:
    assert not hasattr(SessionGameStateView, "is_gm")


def test_session_game_state_view_constructs_without_gm_player_id() -> None:
    """The post-deletion construction API takes no ``gm_player_id``.

    While the field still exists (and is required) this raises TypeError;
    the deletion makes the keyword-free construction valid.
    """
    try:
        view = SessionGameStateView(player_id_to_character={"alice": "alice_char"})
    except TypeError as exc:
        pytest.fail(f"gm_player_id is still a constructor parameter: {exc}")
    assert view.character_of("alice") == "alice_char"


def test_core_invariant_stage_never_emits_gm_sees_all() -> None:
    """No envelope/viewer combination yields the deleted gm_sees_all
    short-circuit. A STATE_UPDATE (which the old GM branch would have
    short-circuited to canonical for a 'gm' viewer) is now non-terminal
    for every viewer — the canonical truth lives in the events table
    (server-side), never in a per-player projection.
    """
    try:
        view = SessionGameStateView(
            player_id_to_character={"alice": "alice_char", "gm": "gm_char"}
        )
    except TypeError as exc:
        pytest.fail(f"gm_player_id is still a constructor parameter: {exc}")

    stage = CoreInvariantStage()
    env = MessageEnvelope(kind="STATE_UPDATE", payload_json='{"hp":10}', origin_seq=1)
    for viewer in ("alice", "gm"):
        outcome = stage.evaluate(envelope=env, view=view, player_id=viewer)
        assert outcome.source != "invariant:gm_sees_all"
        assert outcome.terminal is False
