"""Task 12 Part B — dogfight actor-seating must seed frame HP at instantiation.

These tests drive the real production seam (``instantiate_encounter_from_trigger``)
and assert that BOTH seated actors carry ``per_actor_state["frame_hp"] == 8`` after
the dogfight is instantiated. The value 8 comes from the space_opera dogfight
ConfrontationDef's player_default_stats.hp / opponent_default_stats.hp.

Also covers the fail-loud contract: a dogfight ConfrontationDef missing
player_default_stats.hp raises ValueError at instantiation time — no silent
default, per CLAUDE.md no-silent-fallbacks.

Skips when sidequest-content is not checked out alongside sidequest-server
(matches the pattern in test_sealed_letter_dispatch_integration.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.dogfight_shot import FRAME_HP_KEY, FRAME_HP_MAX_KEY
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    InteractionTable,
    ResolutionMode,
    WinCondition,
)
from sidequest.server.dispatch.encounter_lifecycle import instantiate_encounter_from_trigger

CONTENT_ROOT = Path(__file__).resolve().parents[3].parent / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)

PLAYER = "Maverick"
OPPONENT = "Vulture"
DOGFIGHT = "dogfight"


@pytest.fixture(scope="module")
def space_opera_pack():
    from sidequest.genre.loader import load_genre_pack

    return load_genre_pack(CONTENT_ROOT / "space_opera")


@pytest.fixture
def snap():
    from sidequest.game.session import GameSnapshot

    s = GameSnapshot(genre="space_opera")
    s.genre_slug = "space_opera"
    return s


def test_both_pilots_have_frame_hp_seeded(snap, space_opera_pack):
    """Production path: both seated actors carry frame_hp == their authored cdef HP."""
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=space_opera_pack,
        encounter_type=DOGFIGHT,
        player_name=PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
        genre_slug="space_opera",
    )

    enc = snap.encounter
    assert enc is not None, "dogfight must instantiate"
    assert len(enc.actors) == 2

    for actor in enc.actors:
        assert FRAME_HP_KEY in actor.per_actor_state, (
            f"actor {actor.name!r} (role={actor.role!r}) has no {FRAME_HP_KEY!r} "
            f"in per_actor_state — frame HP seeding failed"
        )
        assert FRAME_HP_MAX_KEY in actor.per_actor_state, (
            f"actor {actor.name!r} (role={actor.role!r}) has no {FRAME_HP_MAX_KEY!r} "
            f"in per_actor_state — frame HP seeding failed"
        )
        hp = actor.per_actor_state[FRAME_HP_KEY]
        hp_max = actor.per_actor_state[FRAME_HP_MAX_KEY]
        assert hp == 8, f"actor {actor.name!r} frame_hp should be 8 (cdef hp), got {hp!r}"
        assert hp_max == 8, f"actor {actor.name!r} frame_hp_max should be 8, got {hp_max!r}"


def test_red_actor_has_frame_hp_seeded(snap, space_opera_pack):
    """Confirm the red (player) actor specifically has frame HP."""
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=space_opera_pack,
        encounter_type=DOGFIGHT,
        player_name=PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
        genre_slug="space_opera",
    )
    enc = snap.encounter
    assert enc is not None
    red = next(a for a in enc.actors if a.role == "red")
    assert red.per_actor_state.get(FRAME_HP_KEY) == 8


def test_blue_actor_has_frame_hp_seeded(snap, space_opera_pack):
    """Confirm the blue (opponent) actor specifically has frame HP."""
    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=space_opera_pack,
        encounter_type=DOGFIGHT,
        player_name=PLAYER,
        npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
        genre_slug="space_opera",
    )
    enc = snap.encounter
    assert enc is not None
    blue = next(a for a in enc.actors if a.role == "blue")
    assert blue.per_actor_state.get(FRAME_HP_KEY) == 8


# ---------------------------------------------------------------------------
# Fail-loud: cdef missing player hp → ValueError at instantiation
# ---------------------------------------------------------------------------


def _minimal_interaction_table() -> InteractionTable:
    from sidequest.genre.models.rules import InteractionCell

    return InteractionTable(
        version="0.1.0",
        starting_state="merge",
        maneuvers_consumed=["straight", "bank"],
        cells=[
            InteractionCell(
                pair=["straight", "straight"],
                name="neutral",
                narration_hint="Straight on.",
            )
        ],
    )


def _dogfight_cdef_no_player_hp() -> ConfrontationDef:
    """A dogfight ConfrontationDef with NO player_default_stats.hp — triggers the
    fail-loud guard at seating time."""
    return ConfrontationDef(
        type=DOGFIGHT,
        label="Dogfight",
        category="combat",
        resolution_mode=ResolutionMode.sealed_letter_lookup,
        win_condition=WinCondition.hp_depletion,
        # opponent_default_stats includes hp (required by category=combat
        # hp_depletion validation); player_default_stats omits hp.
        opponent_default_stats={"hp": 8, "armor_class": 14, "dexterity": 10},
        player_default_stats={"armor_class": 14},  # <-- missing hp!
        beats=[BeatDef(id="straight", label="Straight", kind="strike", stat_check="STR")],
        interaction_table=_minimal_interaction_table(),
    )


def test_fail_loud_when_player_hp_missing_in_cdef(space_opera_pack):
    """Fail-loud contract: dogfight instantiation raises ValueError when cdef lacks
    player_default_stats.hp — no silent default, per CLAUDE.md no-silent-fallbacks."""
    from unittest.mock import patch

    from sidequest.game.session import GameSnapshot

    snap = GameSnapshot(genre="space_opera")
    snap.genre_slug = "space_opera"

    bad_cdef = _dogfight_cdef_no_player_hp()

    # Patch find_confrontation_def in the lifecycle module so instantiation
    # uses our bad cdef instead of the real content one.
    with (
        patch(
            "sidequest.server.dispatch.encounter_lifecycle.find_confrontation_def",
            return_value=bad_cdef,
        ),
        pytest.raises(ValueError, match="missing fighter-frame HP"),
    ):
        instantiate_encounter_from_trigger(
            snapshot=snap,
            pack=space_opera_pack,
            encounter_type=DOGFIGHT,
            player_name=PLAYER,
            npcs_present=[NpcMention(name=OPPONENT, role="hostile", side="opponent")],
            genre_slug="space_opera",
        )
