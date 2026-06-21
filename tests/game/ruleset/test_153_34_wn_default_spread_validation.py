"""Story 153-34 (scope item 2) — the WN default standard-array spread must be
size-validated against the pack's ability-score count AT LOAD, failing loud,
rather than IndexError-ing deep in chargen.

FINDING (153-4 review, Reviewer non-blocking gap): ``_WN_STANDARD_ARRAY`` in
``without_number.py`` is a hardcoded 6-element default. ``RulesConfig`` already
validates a pack-*authored* ``standard_array`` against ability-score count
(``_validate_standard_array``), but nothing validates this *default*. A future WN
pack declaring ≠6 abilities (e.g. a psionics-heavy SWN setting that adds a
"Psyche" stat) and omitting an authored ``standard_array`` falls back to the
6-element default and then crashes with ``IndexError: pop from empty list`` in
``assign_attributes`` at chargen — a player-facing crash with an opaque
traceback, not a loud config error at load.

Reproduced before writing these tests (wwn, 7 abilities, point_buy, no authored
standard_array):
  construction → OK today (no load-time validation)        <-- the gap
  generate_attributes(point_buy)      → IndexError: pop from empty list
  generate_attributes(standard_array) → IndexError: pop from empty list

The fix (mirroring ``RulesConfig._validate_standard_array``): fail loud at LOAD
when the *effective* default spread cannot cover every declared ability score.

RED until that load-time validation exists:
  - ``test_undersized_wn_default_spread_raises_at_load`` — construction must raise
    ValueError; today it silently succeeds (DID NOT RAISE).
The negative-guard tests (6-ability default, 7-ability-with-authored-array) must
stay GREEN — they pin the validation to fire ONLY on the missing-default case.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.builder import AccumulatedChoices
from sidequest.game.ruleset import get_ruleset_module
from sidequest.genre.models.rules import RulesConfig

# ---------------------------------------------------------------------------
# Synthetic WWN fixtures — the lightest valid WN-family config (mirrors
# tests/game/ruleset/test_loader_binding.py::test_wwn_ruleset_binds). The six
# canonical WWN attributes map to flavor names; a 7th flavor ability ("Psyche")
# is DECLARED but unmapped, which still passes _validate_wwn (it only requires
# the six keys present and every map value in ability_score_names).
# ---------------------------------------------------------------------------

_FLAVOR6 = ["Strength", "Agility", "Endurance", "Insight", "Spirit", "Harmony"]
_FLAVOR7 = [*_FLAVOR6, "Psyche"]  # the reviewer's "adds a seventh attribute" scenario
_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}

# The canonical WWN/SWN SRD standard array, length 6 — the same length as
# _WN_STANDARD_ARRAY. A 7-ability pack needs SEVEN entries.
_WN_SHAPED_SPREAD_6 = [14, 12, 11, 10, 9, 7]
_AUTHORED_SPREAD_7 = [14, 12, 11, 10, 9, 8, 7]


def _wwn_rules(
    *,
    ability_names: list[str],
    stat_generation: str,
    standard_array: list[int] | None,
) -> RulesConfig:
    payload: dict[str, object] = {
        "ruleset": "wwn",
        "stat_generation": stat_generation,
        "point_buy_budget": 27,
        "ability_score_names": ability_names,
        "wwn": {"attribute_map": _AMAP},
    }
    if standard_array is not None:
        payload["standard_array"] = standard_array
    return RulesConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# AC (primary): a WN pack whose ability count exceeds the default-spread length
# and that omits an authored standard_array must FAIL LOUD AT LOAD.
# ---------------------------------------------------------------------------


def test_undersized_wn_default_spread_raises_at_load_point_buy() -> None:
    """7 abilities, ``point_buy`` (the WN-dead path that falls to _WN_STANDARD_ARRAY),
    no authored ``standard_array`` → constructing the RulesConfig must raise a loud,
    descriptive ValueError.

    RED today: construction succeeds silently; the failure is deferred to an
    opaque ``IndexError: pop from empty list`` at chargen. The whole point of the
    fix is to move the failure to load and make it descriptive."""
    with pytest.raises(ValueError) as excinfo:
        _wwn_rules(
            ability_names=_FLAVOR7,
            stat_generation="point_buy",
            standard_array=None,
        )
    message = str(excinfo.value)
    # Descriptive — must name the domain so an author can fix it (not a bare
    # IndexError / opaque pydantic noise). Loose match: the message references
    # the ability scores and the (standard) array/spread it cannot cover.
    assert "7" in message, f"error should cite the 7 declared abilities; got: {message!r}"
    assert any(token in message.lower() for token in ("ability", "ability_score", "abilities")), (
        f"error must reference ability scores; got: {message!r}"
    )
    assert any(
        token in message.lower() for token in ("standard_array", "standard array", "spread")
    ), f"error must reference the standard array / spread; got: {message!r}"


def test_undersized_wn_default_spread_raises_at_load_standard_array_method() -> None:
    """Same gap via the other default path: ``stat_generation: standard_array`` with
    NO authored array also falls back to a 6-element default (the base
    ``_DEFAULT_STANDARD_ARRAY``) and IndexErrors at chargen for 7 abilities. Load
    must fail loud here too.

    RED today: construction succeeds silently."""
    with pytest.raises(ValueError):
        _wwn_rules(
            ability_names=_FLAVOR7,
            stat_generation="standard_array",
            standard_array=None,
        )


def test_undersized_failure_is_not_a_chargen_indexerror() -> None:
    """Contract: the undersized-default config never reaches a chargen-time
    ``IndexError``. After the fix, load raises ValueError first; this test fails
    RED if the config is constructible (the IndexError still lurks downstream).

    We assert the *load* seam guards it: constructing must raise ValueError, NOT
    leave a config that later pops from an empty list. Documents the pre-fix
    failure mode so a regression that re-defers the error to chargen is caught."""
    with pytest.raises(ValueError):
        rules = _wwn_rules(
            ability_names=_FLAVOR7,
            stat_generation="point_buy",
            standard_array=None,
        )
        # Unreachable once load fails loud. If load DID NOT raise (RED), this
        # demonstrates the lurking IndexError that the fix must pre-empt.
        module = get_ruleset_module(rules.ruleset)
        module.generate_attributes(
            method=rules.stat_generation,
            ability_names=rules.ability_score_names,
            standard_array=rules.standard_array,
            point_buy_budget=rules.point_buy_budget,
            rolled_stats=None,
            acc=AccumulatedChoices(),
            rng=random.Random(1),
            class_def=None,
        )


# ---------------------------------------------------------------------------
# Negative guards: the validation must NOT fire on the legitimate cases. These
# stay GREEN before and after the fix — they pin the blast radius.
# ---------------------------------------------------------------------------


def test_six_ability_wn_default_spread_loads_and_generates() -> None:
    """The CURRENT production reality for the four migrating packs (pre-migration)
    and for any 6-ability WN pack: ``point_buy``, six abilities, NO authored
    ``standard_array``. The 6-element default covers all six — this must keep
    loading and generate a full 6-value pool (no false-positive rejection)."""
    rules = _wwn_rules(
        ability_names=_FLAVOR6,
        stat_generation="point_buy",
        standard_array=None,
    )
    assert rules.standard_array is None
    pool = get_ruleset_module(rules.ruleset).generate_attributes(
        method="point_buy",
        ability_names=_FLAVOR6,
        standard_array=None,
        point_buy_budget=27,
        rolled_stats=None,
        acc=AccumulatedChoices(),
        rng=random.Random(1),
        class_def=None,
    )
    assert len(pool) == 6, f"all six abilities must receive a value; got {pool}"
    assert sorted(pool.values(), reverse=True) == _WN_SHAPED_SPREAD_6, (
        f"6-ability WN point_buy must still yield the shaped WN spread; got {pool}"
    )


def test_seven_abilities_with_authored_spread_loads_fine() -> None:
    """The escape hatch: a 7-ability WN pack that AUTHORS a 7-element
    ``standard_array`` is fully covered — the default is never used — so it must
    load cleanly. The validation fires only when the *default* would be relied on,
    never when the author supplied a sufficient array."""
    rules = _wwn_rules(
        ability_names=_FLAVOR7,
        stat_generation="standard_array",
        standard_array=_AUTHORED_SPREAD_7,
    )
    assert rules.standard_array == _AUTHORED_SPREAD_7
    pool = get_ruleset_module(rules.ruleset).generate_attributes(
        method="standard_array",
        ability_names=_FLAVOR7,
        standard_array=_AUTHORED_SPREAD_7,
        point_buy_budget=27,
        rolled_stats=None,
        acc=AccumulatedChoices(),
        rng=random.Random(1),
        class_def=None,
    )
    assert len(pool) == 7, f"all seven abilities must receive a value; got {pool}"
    assert sorted(pool.values(), reverse=True) == _AUTHORED_SPREAD_7
