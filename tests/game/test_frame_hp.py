"""Task 12 — frame-HP adapter + resolver for dogfight transient fighter HP.

Tests cover Part A:
- seed_frame_hp seeds per_actor_state with correct keys/values
- frame_hp_resolver exposes .hp.current from per_actor_state
- apply_hp_delta persists to per_actor_state (read-back after mutation)
- apply_hp_delta floors at 0 and caps at max
- resolver returns None for an unseeded actor name
- INTEGRATION: frame_hp_resolver plugs into check_hp_depletion; ablating the
  opponent's frame to 0 resolves player_victory
"""

from __future__ import annotations

from sidequest.game.dogfight_shot import (
    FRAME_HP_KEY,
    FRAME_HP_MAX_KEY,
    frame_hp_resolver,
    seed_frame_hp,
)
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.hp_depletion import check_hp_depletion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(name: str, role: str, side: str) -> EncounterActor:
    return EncounterActor(name=name, role=role, side=side)


def _dogfight_enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="dogfight",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            _actor("PC", "red", "player"),
            _actor("Ace", "blue", "opponent"),
        ],
    )


# ---------------------------------------------------------------------------
# seed_frame_hp
# ---------------------------------------------------------------------------


def test_seed_frame_hp_sets_both_keys():
    actor = _actor("PC", "red", "player")
    seed_frame_hp(actor, 8)
    assert actor.per_actor_state[FRAME_HP_KEY] == 8
    assert actor.per_actor_state[FRAME_HP_MAX_KEY] == 8


def test_seed_frame_hp_coerces_to_int():
    actor = _actor("PC", "red", "player")
    seed_frame_hp(actor, 8.0)  # type: ignore[arg-type]
    assert isinstance(actor.per_actor_state[FRAME_HP_KEY], int)
    assert isinstance(actor.per_actor_state[FRAME_HP_MAX_KEY], int)


# ---------------------------------------------------------------------------
# frame_hp_resolver — basic reads
# ---------------------------------------------------------------------------


def test_frame_hp_resolver_reads_current():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("PC")
    assert core is not None
    assert core.hp.current == 8


def test_frame_hp_resolver_reads_max():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("Ace")
    assert core is not None
    assert core.hp.max == 8


# ---------------------------------------------------------------------------
# apply_hp_delta — mutation persisted to per_actor_state
# ---------------------------------------------------------------------------


def test_apply_hp_delta_persists_to_per_actor_state():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("PC")
    assert core is not None
    core.apply_hp_delta(-3)
    # Re-read via resolver to prove the dict was mutated
    core2 = resolver("PC")
    assert core2 is not None
    assert core2.hp.current == 5


def test_apply_hp_delta_floors_at_zero():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("Ace")
    assert core is not None
    core.apply_hp_delta(-100)
    assert resolver("Ace").hp.current == 0  # type: ignore[union-attr]


def test_apply_hp_delta_caps_at_max():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("PC")
    assert core is not None
    core.apply_hp_delta(50)
    assert resolver("PC").hp.current == 8  # type: ignore[union-attr]


def test_apply_hp_delta_returns_new_current():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    core = resolver("PC")
    assert core is not None
    result = core.apply_hp_delta(-3)
    assert result == 5


# ---------------------------------------------------------------------------
# resolver returns None for unseeded / unknown actor
# ---------------------------------------------------------------------------


def test_resolver_returns_none_for_unknown_name():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)
    resolver = frame_hp_resolver(enc)
    assert resolver("NoSuchPilot") is None


def test_resolver_returns_none_for_unseeded_actor():
    enc = _dogfight_enc()
    # Only seed one actor; the other has no frame_hp key
    seed_frame_hp(enc.actors[0], 8)
    resolver = frame_hp_resolver(enc)
    # The un-seeded actor (enc.actors[1]) should return None
    assert resolver(enc.actors[1].name) is None


# ---------------------------------------------------------------------------
# INTEGRATION: frame_hp_resolver + check_hp_depletion
# ---------------------------------------------------------------------------


def test_depletion_check_via_frame_hp_resolver_player_victory():
    """Ablate opponent frame to 0, verify check_hp_depletion resolves player_victory.

    This is the wiring test: proves _FrameCore plugs into the shared depletion
    check correctly — the resolver shape matches what check_hp_depletion expects.
    """
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)

    resolver = frame_hp_resolver(enc)
    # Ablate the opponent's frame fully
    opp_core = resolver("Ace")
    assert opp_core is not None
    opp_core.apply_hp_delta(-8)

    result = check_hp_depletion(enc, resolver)
    assert result is not None
    assert enc.outcome == "player_victory"
    assert enc.resolved is True


def test_depletion_check_via_frame_hp_resolver_nobody_down_returns_none():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)

    resolver = frame_hp_resolver(enc)
    result = check_hp_depletion(enc, resolver)
    assert result is None
    assert enc.resolved is False


def test_depletion_check_via_frame_hp_resolver_mutual_destruction():
    enc = _dogfight_enc()
    for a in enc.actors:
        seed_frame_hp(a, 8)

    resolver = frame_hp_resolver(enc)
    resolver("PC").apply_hp_delta(-8)  # type: ignore[union-attr]
    resolver("Ace").apply_hp_delta(-8)  # type: ignore[union-attr]

    result = check_hp_depletion(enc, resolver)
    assert result is not None
    assert enc.outcome == "mutual_destruction"
