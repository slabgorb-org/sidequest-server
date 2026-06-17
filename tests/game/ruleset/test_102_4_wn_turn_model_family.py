"""Story 102-4 AC4 — the turn model is family-wide via the WN core.

The sealed round is ``WithoutNumberRulesetModule``-family behavior. After the
ADR-142 core extraction the four WN siblings (SWN/WWN/CWN/AWN) are clean
siblings — each subclasses the shared ``WithoutNumberRulesetModule`` directly,
with no Wwn(Swn) / Cwn(Swn) / Awn(Cwn) chains. Dispatch binds the turn model by
isinstance against ``WithoutNumberRulesetModule`` (epic invariant: "capability
binding is isinstance against module classes — no ``if genre ==`` branches"), so
that one check covers all four siblings. These locks are characterization guards
(green at RED time is correct for them — they pin the inheritance the dispatch
behavior relies on); the per-slug deferral behavior itself is proven in
tests/integration/test_102_4_wn_family_smoke.py.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule

WN_SLUGS = ["swn", "wwn", "cwn", "awn"]


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_every_wn_module_is_a_without_number_module(slug: str) -> None:
    """The isinstance capability-binding lock: if a sibling stops subclassing
    WithoutNumberRulesetModule, the sealed round silently stops applying to it."""
    assert isinstance(get_ruleset_module(slug), WithoutNumberRulesetModule), (
        f"{slug} must bind the WN turn model via WithoutNumberRulesetModule inheritance"
    )


def test_dial_is_not_a_wn_module() -> None:
    """Dial packs keep today's turn flow byte-for-byte — the regression
    half of AC4. Dial must never bind the sealed round."""
    assert not isinstance(get_ruleset_module("dial"), WithoutNumberRulesetModule)


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_every_wn_module_rolls_a_real_initiative_order(slug: str) -> None:
    """Family-wide 1d8+DEX: one entry per actor, descending, deterministic
    under a seeded rng — the order the sealed round walks."""
    module = get_ruleset_module(slug)
    actors = {"Vesska": 14, "Brakka": 10, "Hired Blade": 11}
    entries = module.roll_initiative(actor_dex_scores=dict(actors), rng=random.Random(7))
    assert entries is not None and len(entries) == len(actors), (
        f"{slug} must order every actor; got {entries!r}"
    )
    assert {e.token_id for e in entries} == set(actors)
    values = [e.value for e in entries]
    assert values == sorted(values, reverse=True), (
        f"{slug} initiative must sort descending; got {values}"
    )
    replay = module.roll_initiative(actor_dex_scores=dict(actors), rng=random.Random(7))
    assert [(e.token_id, e.value) for e in replay] == [(e.token_id, e.value) for e in entries], (
        f"{slug} initiative must be deterministic under a seeded rng (ADR-128)"
    )


def test_dial_rolls_no_initiative() -> None:
    """Dial returns None — no ordering is the truthful state, not a fallback."""
    assert (
        get_ruleset_module("dial").roll_initiative(
            actor_dex_scores={"A": 10, "B": 12}, rng=random.Random(7)
        )
        is None
    )
