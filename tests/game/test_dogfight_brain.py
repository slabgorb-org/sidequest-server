"""RED tests — Story 158-39 — the dogfight opponent brain's pure policy (ADR-153 §4).

The NPC ace picks its maneuver each turn from the legal menu, MOTIVATED by its
disposition/goal, GATED by energy affordability, and it NEVER wedges the duel: a
deterministic disposition-weighted pick is always a legal maneuver (the floor
under a skipped/illegal narrator commit, ADR-006). This module pins the *pure*
policy — no I/O, no snapshot, fully unit-testable — so the motivation, the
affordability gate, and the always-legal firewall floor are all provable in
isolation. The seam wiring (fallback fires at the sealed-letter seam, OTEL
``source``) is proven separately in
``tests/server/dispatch/test_opponent_brain_wiring.py``.

FIREWALL (ADR-153 §2): the brain returns a maneuver **id** and nothing else. It
never composes geometry (the cross-product interaction table does) and never
resolves damage (SWN does). Its entire output is a legal maneuver id string —
that containment IS the firewall at this layer.

These import ``select_opponent_maneuver`` + ``ManeuverDef`` which do not exist
yet — collection fails until Dev (158-39 GREEN) adds
``sidequest/game/dogfight_brain.py`` (Task 2) and ``ManeuverDef`` on
``sidequest/genre/models/rules.py`` (Task 1). That is the intended RED.
"""

from __future__ import annotations

import pytest

from sidequest.game.dogfight_brain import select_opponent_maneuver
from sidequest.genre.models.rules import ManeuverDef

# Mirrors the live space_opera dogfight menu (dogfight/maneuvers_mvp.yaml):
# straight/passive/-5 (recovery), bank/evasive/5, loop/offensive/30,
# kill_rotation/offensive_space_only/5. Constructed here (not loaded) so the
# policy is tested purely — content changes can never turn this red.
MANEUVERS = [
    ManeuverDef(id="straight", **{"class": "passive"}, energy_cost=-5),
    ManeuverDef(id="bank", **{"class": "evasive"}, energy_cost=5),
    ManeuverDef(id="loop", **{"class": "offensive"}, energy_cost=30),
    ManeuverDef(id="kill_rotation", **{"class": "offensive_space_only"}, energy_cost=5),
]
_IDS = {m.id for m in MANEUVERS}
_OFFENSIVE = {"loop", "kill_rotation"}  # the two attack-class ids


# ---------------------------------------------------------------------------
# Motivation (AC-1) — the pick follows the ace's disposition
# ---------------------------------------------------------------------------


def test_hostile_presses_offensive_when_affordable() -> None:
    """A hostile ace out for blood presses the attack: at full energy it commits
    an offensive-class maneuver (loop or kill_rotation), not a passive hold."""
    pick = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=1)
    assert pick in _OFFENSIVE, f"hostile ace at full energy should attack, picked {pick!r}"


def test_friendly_disengages_never_presses_offense() -> None:
    """A friendly/disengaging pilot favors an evasive break or energy recovery —
    it must NOT open fire with an offensive reversal (the motivation contract:
    disposition drives the maneuver, ADR-153 §4)."""
    pick = select_opponent_maneuver(
        attitude="friendly", maneuvers=MANEUVERS, energy=60, turn_seed=1
    )
    assert pick in {"bank", "straight"}, f"friendly pilot should not attack, picked {pick!r}"
    assert pick not in _OFFENSIVE


def test_attitude_changes_the_pick_all_else_equal() -> None:
    """Motivation is real, not cosmetic: the same energy/seed yields a different
    stance for a hostile vs a friendly ace (offense vs not-offense)."""
    hostile = select_opponent_maneuver(
        attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=2
    )
    friendly = select_opponent_maneuver(
        attitude="friendly", maneuvers=MANEUVERS, energy=60, turn_seed=2
    )
    assert hostile in _OFFENSIVE
    assert friendly not in _OFFENSIVE
    assert hostile != friendly


# ---------------------------------------------------------------------------
# Affordability gate (energy) — never spend energy you don't have
# ---------------------------------------------------------------------------


def test_hostile_takes_cheaper_offensive_when_loop_unaffordable() -> None:
    """Energy 10 can't afford loop (30) but can afford kill_rotation (5): the
    hostile ace still attacks, with the affordable offensive move."""
    pick = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=10, turn_seed=1)
    assert pick == "kill_rotation"


def test_never_returns_an_unaffordable_maneuver_when_an_affordable_one_exists() -> None:
    """The gate holds for every attitude: at energy 10, loop (cost 30) is
    unaffordable while straight/bank/kill_rotation are affordable, so loop is
    never committed."""
    for attitude in ("hostile", "neutral", "friendly"):
        pick = select_opponent_maneuver(
            attitude=attitude, maneuvers=MANEUVERS, energy=10, turn_seed=1
        )
        assert pick != "loop", f"{attitude} committed unaffordable loop at energy 10"
        assert pick in {"straight", "bank", "kill_rotation"}


def test_no_affordable_spend_falls_back_to_recovery() -> None:
    """At energy 0 only the recovery move (straight, cost -5) is affordable —
    bank/loop/kill_rotation all cost energy the ace doesn't have — so even a
    hostile ace holds and recovers rather than committing an illegal spend."""
    pick = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=0, turn_seed=1)
    assert pick == "straight"


def test_nothing_affordable_at_all_returns_cheapest_legal() -> None:
    """When NO maneuver is affordable (all positive cost, zero energy, no recovery
    move) the brain still commits — the cheapest legal maneuver — rather than
    wedging the duel. The floor never returns nothing."""
    all_costly = [
        ManeuverDef(id="a", **{"class": "offensive"}, energy_cost=30),
        ManeuverDef(id="b", **{"class": "evasive"}, energy_cost=15),
        ManeuverDef(id="c", **{"class": "offensive_space_only"}, energy_cost=25),
    ]
    pick = select_opponent_maneuver(attitude="hostile", maneuvers=all_costly, energy=0, turn_seed=1)
    assert pick == "b", "with nothing affordable, the cheapest legal maneuver wins"


# ---------------------------------------------------------------------------
# Determinism (AC-3) — resume-safe, seeded tie-break (ADR-128)
# ---------------------------------------------------------------------------


def test_deterministic_for_same_inputs() -> None:
    """Same (attitude, maneuvers, energy, turn_seed) → same pick, every time.
    Resume-safe: the tie-break is seeded, never Math.random/wall-clock."""
    kw = dict(attitude="neutral", maneuvers=MANEUVERS, energy=60, turn_seed=7)
    first = select_opponent_maneuver(**kw)
    for _ in range(5):
        assert select_opponent_maneuver(**kw) == first


def test_seed_is_the_only_nondeterminism_source() -> None:
    """The pick is a pure function of its inputs — no hidden global state leaks
    between calls with different seeds (each remains individually deterministic
    and legal)."""
    a = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=0)
    b = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=1)
    assert a in _IDS and b in _IDS
    # Re-running with seed 0 still yields `a` (seed 1's call did not perturb it).
    assert (
        select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=0)
        == a
    )


# ---------------------------------------------------------------------------
# Firewall floor (AC-2) — the brain outputs ONLY a legal maneuver id
# ---------------------------------------------------------------------------


def test_always_returns_a_legal_maneuver() -> None:
    """The floor: across every attitude and a wide energy sweep, the pick is
    always an id present in the legal menu — the brain can never wedge the duel
    by returning an unknown maneuver."""
    for attitude in ("hostile", "neutral", "friendly"):
        for energy in (-20, 0, 5, 30, 60, 999):
            pick = select_opponent_maneuver(
                attitude=attitude, maneuvers=MANEUVERS, energy=energy, turn_seed=3
            )
            assert pick in _IDS, f"illegal pick {pick!r} for {attitude} @ energy {energy}"


def test_firewall_output_is_only_a_maneuver_id() -> None:
    """ADR-153 §2 firewall at the brain layer: the entire return is a maneuver id
    string — the brain composes no geometry and resolves no damage. Its output is
    a plain id, nothing more."""
    pick = select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=1)
    assert isinstance(pick, str)
    assert pick in _IDS


def test_unknown_attitude_still_returns_a_legal_maneuver() -> None:
    """A disposition string the policy doesn't recognize must degrade to a legal
    maneuver (treated as neutral), never raise — the brain is the floor."""
    pick = select_opponent_maneuver(
        attitude="berserk_unknown", maneuvers=MANEUVERS, energy=60, turn_seed=1
    )
    assert pick in _IDS


# ---------------------------------------------------------------------------
# Fail loud + purity (CLAUDE.md No Silent Fallbacks)
# ---------------------------------------------------------------------------


def test_empty_menu_raises_not_silent() -> None:
    """No legal maneuvers is a genuine engine/content defect — fail loud, never
    silently return a fabricated id (CLAUDE.md No Silent Fallbacks)."""
    with pytest.raises(ValueError):
        select_opponent_maneuver(attitude="hostile", maneuvers=[], energy=60, turn_seed=1)


def test_does_not_mutate_the_input_menu() -> None:
    """The policy is pure: selecting a maneuver must not reorder or mutate the
    caller's maneuver list (it is the shared ConfrontationDef.maneuvers)."""
    snapshot = list(MANEUVERS)
    select_opponent_maneuver(attitude="hostile", maneuvers=MANEUVERS, energy=60, turn_seed=1)
    assert snapshot == MANEUVERS
