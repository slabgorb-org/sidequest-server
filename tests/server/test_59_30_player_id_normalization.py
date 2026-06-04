"""RED tests — Story 59-30: live-path ``player_id`` normalization.

Keith's ruling pairs the movement engagement witness with the live-path
``player_id`` normalization in 59-30 (pairing removes the cry-wolf: the witness
resolves the moving PC via ``snapshot.player_seats.get(player_id)``, so the
``player_id`` riding on ``DispatchPackage.per_player[]`` MUST be the real
submitting seat id — a ``player_seats`` key — not the LLM-emitted value the
router's Haiku pass hallucinates).

Construction site: ``execute_intent_router_pre_narrator_pass``
(``sidequest/server/intent_router_pass.py``), AFTER ``intent_router.decompose()``
produces the package and BEFORE ``run_dispatch_bank`` engages the engines. The
pass is single-submitter-per-turn, so the package carries exactly one
``per_player`` entry to normalize.

Invariant pinned:
  - the returned package's ``per_player[0].player_id`` is OVERWRITTEN with the
    submitting seat id (the ``player_seats`` key whose value is the submitting
    ``player_name``), NOT left as the verbatim LLM value;
  - therefore ``snapshot.player_seats.get(per_player[0].player_id) ==
    player_name`` — i.e. the movement witness's ``player_seats.get(player_id)``
    bridge resolves to a real character on the live path.

These FAIL TODAY: no normalization exists — the LLM-emitted ``player_id`` passes
through verbatim, so it is not a ``player_seats`` key and the bridge would not
resolve (the exact cry-wolf the pairing closes).

Architect verified low ripple: ``pd.player_id`` is unconsumed on the live bank
path and redaction treats it as passthrough, so the overwrite should not break
existing tests (affected suites re-run during RED verification).

Note: the loud-evidence-on-unresolvable witness test in
``tests/agents/test_59_30_witnesses.py`` stays AS-IS — post-normalization the
live path never trips it; it correctly guards only a true future plumbing break.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# LLM-hallucinated owner id the router emits — deliberately NOT a player_seats
# key, so an un-normalized passthrough is detectable.
_LLM_EMITTED_PLAYER_ID = "llm:hallucinated-owner"
# The real submitting seat id (the key player_seats is keyed by) and the
# character name it maps to (the submitting player_name).
_SUBMITTING_SEAT_ID = "seat-rux-7f3a"
_SUBMITTING_CHARACTER = "Rux"


def _open_viz() -> VisibilityTag:
    return VisibilityTag(visible_to="all")


def _confrontation_package_with_llm_player_id() -> DispatchPackage:
    """A single-submitter package whose per_player owner id is the LLM-emitted
    value (not a real seat id). Confrontation dispatch chosen because it engages
    cleanly against the synthetic negotiation pack (no dungeon_store needed)."""
    return DispatchPackage(
        turn_id="t-1",
        per_player=[
            PlayerDispatch(
                player_id=_LLM_EMITTED_PLAYER_ID,
                raw_action="I block his way and call the bluff.",
                dispatch=[
                    SubsystemDispatch(
                        subsystem="confrontation",
                        params={"type": "negotiation"},
                        idempotency_key="k-conf-1",
                        confidence=1.0,
                        visibility=_open_viz(),
                    )
                ],
            )
        ],
        confidence_global=1.0,
    )


def _synthetic_pack_with_negotiation() -> Any:
    """Synthetic pack with a negotiation ConfrontationDef (mirrors
    tests/server/test_59_4_router_wiring.py)."""
    from sidequest.genre.models.pack import GenrePack
    from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef, RulesConfig

    cdef = ConfrontationDef(
        type="negotiation",
        label="Negotiation",
        category="social",
        player_metric=MetricDef(name="leverage", starting=0, threshold=10),
        opponent_metric=MetricDef(name="leverage", starting=0, threshold=10),
        beats=[
            BeatDef.model_validate(
                {
                    "id": "press",
                    "label": "Press the Point",
                    "kind": "strike",
                    "base": 1,
                    "stat_check": "CHA",
                }
            )
        ],
    )
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(confrontations=[cdef])
    pack.witnessed_acts = None
    return pack


def _snapshot_with_seat() -> Any:
    """Snapshot whose player_seats maps the real submitting seat id → the
    submitting character name. The LLM-emitted id is intentionally absent from
    this mapping."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    return GameSnapshot(
        genre_slug="test_pack",
        world_slug="test_world",
        encounter=None,
        player_seats={_SUBMITTING_SEAT_ID: _SUBMITTING_CHARACTER},
        turn_manager=TurnManager(interaction=7),
    )


@pytest.mark.asyncio
async def test_live_path_overwrites_llm_player_id_with_submitting_seat() -> None:
    """After the pre-narrator pass, the package's ``per_player[0].player_id`` is
    the real submitting seat id (a ``player_seats`` key for the submitting
    ``player_name``), NOT the verbatim LLM-emitted value.

    FAILS TODAY: no normalization → the LLM value passes through verbatim."""
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _snapshot_with_seat()
    pack = _synthetic_pack_with_negotiation()
    package = _confrontation_package_with_llm_player_id()

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank_result = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I block his way and call the bluff.",
        player_name=_SUBMITTING_CHARACTER,
    )

    assert len(returned.per_player) == 1, "single-submitter-per-pass invariant"
    stamped = returned.per_player[0].player_id

    assert stamped != _LLM_EMITTED_PLAYER_ID, (
        "the LLM-emitted player_id must NOT pass through verbatim — it must be "
        f"overwritten with the real submitting seat id; got {stamped!r}"
    )
    assert stamped == _SUBMITTING_SEAT_ID, (
        "player_id must be normalized to the player_seats KEY for the submitting "
        f"player_name; expected {_SUBMITTING_SEAT_ID!r}, got {stamped!r}"
    )
    assert stamped in snap.player_seats, (
        "the stamped player_id must be a real player_seats key"
    )


@pytest.mark.asyncio
async def test_live_path_normalized_player_id_resolves_movement_witness_bridge() -> None:
    """Payoff: the normalized ``player_id`` makes the movement witness's
    ``player_seats.get(player_id)`` bridge resolve to the submitting character on
    the live path — so a real relocation reads ENGAGED (no cry-wolf), and the
    un-normalized LLM value (which would NOT resolve) is proven gone.

    FAILS TODAY: without normalization the stamped id is the LLM value, which is
    not a player_seats key, so the witness bridge can't resolve it."""
    from sidequest.agents.dispatch_engagement_watcher import _check_movement_engaged
    from sidequest.server.intent_router_pass import execute_intent_router_pre_narrator_pass

    snap = _snapshot_with_seat()
    pack = _synthetic_pack_with_negotiation()
    package = _confrontation_package_with_llm_player_id()

    router = MagicMock()
    router.decompose = AsyncMock(return_value=package)

    returned, _bank_result = await execute_intent_router_pre_narrator_pass(
        intent_router=router,
        snapshot=snap,
        pack=pack,
        action="I block his way and call the bluff.",
        player_name=_SUBMITTING_CHARACTER,
    )
    stamped = returned.per_player[0].player_id

    # The bridge resolves the normalized id → the submitting character.
    assert snap.player_seats.get(stamped) == _SUBMITTING_CHARACTER, (
        "movement witness bridge (player_seats.get(player_id)) must resolve the "
        "normalized live-path player_id to the submitting character"
    )

    # End-to-end: feed the normalized id into the witness with a matching
    # this-turn relocation → ENGAGED (None). The un-normalized LLM id would have
    # returned 'no seat→character mapping' evidence instead.
    rt_cls = _region_transition_cls()
    snap.region_transitions = [
        rt_cls(
            turn=7,
            pc_name=_SUBMITTING_CHARACTER,
            from_region="a",
            to_region="b",
            via="world_patch",
        )
    ]
    movement_dispatch = SubsystemDispatch(
        subsystem="movement",
        params={"direction": "deeper"},
        idempotency_key="k-move-1",
        confidence=1.0,
        visibility=_open_viz(),
    )

    assert _check_movement_engaged(movement_dispatch, snap, stamped) is None, (
        "with the normalized player_id, the movement witness must resolve the PC "
        "and read the this-turn relocation as engaged"
    )


# ---------------------------------------------------------------------------
# RegionTransition import shim (new model in 59-30; home left open by the
# Architect note). Fails RED until Dev creates it.
# ---------------------------------------------------------------------------


def _region_transition_cls() -> Any:
    try:
        from sidequest.game.session import RegionTransition

        return RegionTransition
    except ImportError:
        from sidequest.game.region_transition import RegionTransition

        return RegionTransition
