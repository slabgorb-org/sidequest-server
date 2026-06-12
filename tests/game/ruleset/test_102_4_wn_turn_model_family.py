"""Story 102-4 AC4 — the turn model is family-wide via module inheritance.

The sealed round is `SwnRulesetModule`-family behavior: WWN/CWN subclass SWN,
AWN subclasses CWN. Dispatch binds the turn model by isinstance against the
module class (epic invariant: "capability binding is isinstance against
module classes — no ``if genre ==`` branches"), so these locks are the
mechanism that makes one implementation cover all four sisters. They are
characterization guards (green at RED time is correct for them — they pin
the inheritance the new dispatch behavior relies on); the per-slug deferral
behavior itself is proven in
tests/integration/test_102_4_wn_family_smoke.py.
"""

from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.swn import SwnRulesetModule

WN_SLUGS = ["swn", "wwn", "cwn", "awn"]


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_every_wn_module_is_an_swn_module(slug: str) -> None:
    """The isinstance capability-binding lock: if a sister stops subclassing
    SwnRulesetModule, the sealed round silently stops applying to it."""
    assert isinstance(get_ruleset_module(slug), SwnRulesetModule), (
        f"{slug} must bind the WN turn model via SwnRulesetModule inheritance"
    )


def test_native_is_not_a_wn_module() -> None:
    """Native packs keep today's turn flow byte-for-byte — the regression
    half of AC4. Native must never bind the sealed round."""
    assert not isinstance(get_ruleset_module("native"), SwnRulesetModule)


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


def test_native_rolls_no_initiative() -> None:
    """Native returns None — no ordering is the truthful state, not a fallback."""
    assert (
        get_ruleset_module("native").roll_initiative(
            actor_dex_scores={"A": 10, "B": 12}, rng=random.Random(7)
        )
        is None
    )
