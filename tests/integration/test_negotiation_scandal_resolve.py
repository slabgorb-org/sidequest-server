"""Story 73-1 (RED): integration counterpart to
``tests/server/test_negotiation_scandal_opposed_check.py``.

Where the unit file pins content shape, seating, and the opposed dice
mechanics, this file proves the *resolution* path end-to-end through the trigger
instantiation: each converted confrontation is actually RESOLVABLE, so the
frozen-dial soft-lock the 59-8 fix closed for ``social_duel`` is closed for its
``negotiation`` and ``scandal`` siblings too.

Mirrors ``tests/integration/test_glenross_social_duel_concede.py``.

RED: ``scandal.weather_it`` lacks ``resolution: true`` today, so a failed
``weather_it`` roll does NOT end the scandal — the soft-lock is live. The
negotiation case is a regression guard (``walk_away`` already resolves).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import BeatSelection, NarrationTurnResult
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.dice import RollOutcome
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
)

pytestmark = pytest.mark.skipif(
    not (CONTENT_GENRE_PACKS / "tea_and_murder").exists(),
    reason="tea_and_murder content pack not available",
)


def _pack():
    return load_genre_pack(CONTENT_GENRE_PACKS / "tea_and_murder")


def _seat_other(snap: GameSnapshot, name: str, location: str) -> None:
    snap.character_locations["Inspector Pryce"] = location
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.session import Npc

    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name=name,
                description="A member of the village with something to lose.",
                personality="Guarded.",
                level=1,
                inventory=Inventory(),
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            last_seen_location=location,
            last_seen_turn=1,
        )
    )


def _resolved(snap: GameSnapshot) -> bool:
    return snap.encounter is None or snap.encounter.structured_phase.name == "Resolved"


def test_scandal_resolves_on_weather_it(monkeypatch):
    """AC-4: a failed ``weather_it`` push must still end the scandal because the
    beat carries ``resolution: true`` (the always-resolves voluntary exit).

    RED today: ``weather_it`` lacks the flag, so on a Fail tier the push does
    not resolve — the player is soft-locked in a scandal the fiction has closed.
    """
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    _seat_other(snap, "Miss Crane", "The Parish Tea Room")

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="scandal",
        player_name="Inspector Pryce",
        npcs_present=[],
        genre_slug="tea_and_murder",
    )
    snap.encounter = enc

    # weather_it on a Fail tier — only resolution: true makes this resolve.
    result = NarrationTurnResult(
        narration="Inspector Pryce lets the talk wash over him and says nothing.",
        beat_selections=[
            BeatSelection(actor="Inspector Pryce", beat_id="weather_it", outcome=RollOutcome.Fail),
        ],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=_pack(),
        room=room_for(snap),
    )

    assert _resolved(snap), (
        "weather_it must resolve the scandal on ANY tier (resolution: true) — "
        "a failed weather_it currently leaves the scandal unresolved (soft-lock)"
    )


def test_negotiation_resolves_on_walk_away(monkeypatch):
    """AC-4 (regression guard): a failed ``walk_away`` push still ends the
    negotiation — ``walk_away`` already carries ``resolution: true`` and the
    conversion must not drop it."""
    snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
    _seat_other(snap, "Mrs. Galbraith", "Castle Ross — The Drawing Room")

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=_pack(),
        encounter_type="negotiation",
        player_name="Inspector Pryce",
        npcs_present=[],
        genre_slug="tea_and_murder",
    )
    snap.encounter = enc

    result = NarrationTurnResult(
        narration="Inspector Pryce rises, thanks her for her time, and takes his leave.",
        beat_selections=[
            BeatSelection(actor="Inspector Pryce", beat_id="walk_away", outcome=RollOutcome.Fail),
        ],
    )

    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name="Inspector Pryce",
        pack=_pack(),
        room=room_for(snap),
    )

    assert _resolved(snap), "walk_away (resolution: true) must end the negotiation on any tier"
