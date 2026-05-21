"""RED-phase tests for Story 22-1 — seed trope schema models.

Covers AC1 (SeedTrope), AC3 (SeedGhost), AC4 (SeedState). Pure model-level
contract tests: construction, defaults, round-trip, and the extra-field policy
that mirrors the existing TropeDefinition (forbid) / TropeState (ignore) split.

Test-data discipline (feedback_no_content_coupled_tests): every seed here is a
fixture constructed in-test. No live ``genre_packs/*`` is loaded.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.tropes import SeedTrope
from sidequest.game.session import SeedGhost, SeedState


# ---------------------------------------------------------------------------
# AC1 — SeedTrope schema
# ---------------------------------------------------------------------------


def _full_seed() -> SeedTrope:
    return SeedTrope(
        id="sealed-letter",
        name="A Sealed Letter",
        description="A wax-sealed letter addressed to no one.",
        flavor_tags=["mystery", "correspondence"],
        lifespan_turns=8,
        delivery_hints=["an innkeeper hands it over", "found under a door"],
        narrative_hint="Connect to whoever the players suspect.",
    )


def test_seed_trope_has_all_seven_fields():
    seed = _full_seed()
    assert seed.id == "sealed-letter"
    assert seed.name == "A Sealed Letter"
    assert seed.description == "A wax-sealed letter addressed to no one."
    assert seed.flavor_tags == ["mystery", "correspondence"]
    assert seed.lifespan_turns == 8
    assert seed.delivery_hints == ["an innkeeper hands it over", "found under a door"]
    assert seed.narrative_hint == "Connect to whoever the players suspect."


def test_seed_trope_round_trips_through_pydantic():
    seed = _full_seed()
    reloaded = SeedTrope.model_validate(seed.model_dump())
    assert reloaded == seed


def test_seed_trope_list_fields_default_empty_not_shared():
    a = SeedTrope(id="a", name="A", lifespan_turns=3)
    b = SeedTrope(id="b", name="B", lifespan_turns=3)
    assert a.flavor_tags == []
    assert a.delivery_hints == []
    # Rule #2 (mutable defaults): each instance must own its own list.
    a.flavor_tags.append("leaked")
    assert b.flavor_tags == []


def test_seed_trope_lifespan_turns_is_int():
    seed = SeedTrope(id="x", name="X", lifespan_turns=5)
    assert isinstance(seed.lifespan_turns, int)


def test_seed_trope_rejects_unknown_field():
    # Mirrors TropeDefinition extra="forbid": authoring typos must fail loudly,
    # not silently drop (No Silent Fallbacks).
    with pytest.raises(ValidationError):
        SeedTrope(id="x", name="X", lifespan_turns=5, lifespam_turns=5)


# ---------------------------------------------------------------------------
# AC4 — SeedState (active seed tracking)
# ---------------------------------------------------------------------------


def _full_state() -> SeedState:
    return SeedState(
        id="sealed-letter",
        name="A Sealed Letter",
        activated_at_turn=4,
        flavor_tags=["mystery"],
        lifespan_turns=8,
        delivery_hints=["an innkeeper hands it over"],
    )


def test_seed_state_has_all_six_fields():
    state = _full_state()
    assert state.id == "sealed-letter"
    assert state.name == "A Sealed Letter"
    assert state.activated_at_turn == 4
    assert state.flavor_tags == ["mystery"]
    assert state.lifespan_turns == 8
    assert state.delivery_hints == ["an innkeeper hands it over"]


def test_seed_state_round_trips_as_json():
    state = _full_state()
    reloaded = SeedState.model_validate_json(state.model_dump_json())
    assert reloaded == state


def test_seed_state_ignores_unknown_fields_for_forward_compat():
    # Mirrors TropeState extra="ignore": old saves with extra keys stay loadable.
    state = SeedState.model_validate(
        {"id": "x", "name": "X", "activated_at_turn": 1, "future_field": 99}
    )
    assert state.id == "x"
    assert not hasattr(state, "future_field")


def test_seed_state_expiry_predicate_at_boundary():
    # activated_at_turn + lifespan_turns is the expiry turn (inclusive boundary).
    state = SeedState(id="x", name="X", activated_at_turn=4, lifespan_turns=3)
    assert state.is_expired(current_turn=6) is False  # turn 6 < 7, still active
    assert state.is_expired(current_turn=7) is True  # turn 7 == 4+3, expired
    assert state.is_expired(current_turn=8) is True


# ---------------------------------------------------------------------------
# AC3 — SeedGhost (retention)
# ---------------------------------------------------------------------------


def _full_ghost() -> SeedGhost:
    return SeedGhost(
        id="sealed-letter",
        name="A Sealed Letter",
        expired_at_turn=12,
        delivery_hints=["an innkeeper hands it over"],
    )


def test_seed_ghost_has_all_four_fields():
    ghost = _full_ghost()
    assert ghost.id == "sealed-letter"
    assert ghost.name == "A Sealed Letter"
    assert ghost.expired_at_turn == 12
    assert ghost.delivery_hints == ["an innkeeper hands it over"]


def test_seed_ghost_round_trips_as_json():
    ghost = _full_ghost()
    reloaded = SeedGhost.model_validate_json(ghost.model_dump_json())
    assert reloaded == ghost


def test_seed_ghost_is_immutable():
    # AC3: SeedGhost is record-only. Frozen model — mutation must raise.
    ghost = _full_ghost()
    with pytest.raises(ValidationError):
        ghost.name = "tampered"


def test_expired_state_maps_to_ghost_preserving_identity():
    # AC3: expired seeds migrate to ghost retention, preserving id/name/hints
    # and recording the turn at which they expired.
    state = SeedState(
        id="sealed-letter",
        name="A Sealed Letter",
        activated_at_turn=4,
        lifespan_turns=8,
        delivery_hints=["an innkeeper hands it over"],
    )
    ghost = state.to_ghost(current_turn=12)
    assert isinstance(ghost, SeedGhost)
    assert ghost.id == state.id
    assert ghost.name == state.name
    assert ghost.delivery_hints == state.delivery_hints
    assert ghost.expired_at_turn == 12
