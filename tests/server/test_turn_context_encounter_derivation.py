from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader


@pytest.fixture
def sd_factory():
    """Build a minimal _SessionData with a loaded caverns_and_claudes pack."""
    from sidequest.server.session_handler import _SessionData

    pack = GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS).load("caverns_and_claudes")

    def _make(encounter: StructuredEncounter | None) -> _SessionData:
        snap = GameSnapshot(genre="caverns_and_claudes")
        snap.encounter = encounter
        return _SessionData(
            genre_slug="caverns_and_claudes",
            world_slug="crypt_of_the_seven",
            player_name="Rux",
            player_id="p1",
            snapshot=snap,
            store=MagicMock(),  # not exercised in this test
            genre_pack=pack,
            orchestrator=MagicMock(),
        )

    return _make


def test_no_encounter_defaults_to_all_false(sd_factory) -> None:
    from sidequest.server.session_handler import _build_turn_context

    sd = sd_factory(None)
    ctx = _build_turn_context(sd)
    assert ctx.in_combat is False
    assert ctx.in_chase is False
    assert ctx.in_encounter is False
    assert ctx.encounter is None
    assert ctx.confrontation_def is None
    assert ctx.encounter_summary is None


def test_combat_encounter_sets_in_combat_true(sd_factory) -> None:
    from sidequest.server.session_handler import _build_turn_context

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Rux", role="combatant", side="player")],
    )
    sd = sd_factory(enc)
    ctx = _build_turn_context(sd)
    assert ctx.in_combat is True
    assert ctx.in_chase is False
    assert ctx.in_encounter is True
    assert ctx.encounter is enc
    assert ctx.confrontation_def is not None
    assert ctx.confrontation_def.confrontation_type == "combat"
    assert ctx.encounter_summary is not None
    assert "combat" in ctx.encounter_summary


def test_resolved_encounter_flags_all_false(sd_factory) -> None:
    from sidequest.server.session_handler import _build_turn_context

    enc = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[EncounterActor(name="Rux", role="combatant", side="player")],
    )
    enc.resolved = True
    sd = sd_factory(enc)
    ctx = _build_turn_context(sd)
    assert ctx.in_combat is False
    assert ctx.in_encounter is False
    # Encounter + cdef + summary should also be None for a resolved encounter,
    # since the narrator no longer needs them.
    assert ctx.encounter is None
    assert ctx.confrontation_def is None
    assert ctx.encounter_summary is None


def test_chase_encounter_sets_in_chase_true(sd_factory) -> None:
    from sidequest.server.session_handler import _build_turn_context

    enc = StructuredEncounter(
        encounter_type="chase",
        player_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=20),
        opponent_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=20),
        actors=[EncounterActor(name="Rux", role="participant", side="player")],
    )
    sd = sd_factory(enc)
    ctx = _build_turn_context(sd)
    assert ctx.in_chase is True
    assert ctx.in_combat is False
    assert ctx.in_encounter is True
