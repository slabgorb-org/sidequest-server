"""Tests for the stable-section allowlist that drives system/user split."""

from __future__ import annotations

from sidequest.agents.prompt_framework.bucket import (
    STABLE_SECTION_NAMES,
    SectionBucket,
    default_bucket_for_section,
)


def test_known_stable_sections_resolve_to_system():
    """Every name in the allowlist is bucketed as ``system``."""
    for name in STABLE_SECTION_NAMES:
        assert default_bucket_for_section(name) == SectionBucket.System, (
            f"{name!r} is in allowlist but did not resolve to System"
        )


def test_unknown_section_defaults_to_user():
    """A section name not in the allowlist defaults to ``user`` bucket.

    Safer default: dynamic content goes to user message. Stable scaffold
    requires explicit opt-in via the allowlist.
    """
    assert default_bucket_for_section("__never_registered_in_real_code") == SectionBucket.User


def test_allowlist_minimum_contents():
    """Pin the load-bearing stable-scaffold sections (spec §Composition).

    If a section moves out of system bucket, this test breaks loudly so
    the human reviewer sees the regression.

    Story 57-3 / ADR-112: the four unconditional Valley-zone genre prose
    sections (``genre_extraction``, ``genre_keeper_monologue``,
    ``genre_town``, ``genre_chargen``) are promoted into the Stable
    allowlist. They are session-static (sourced from per-pack
    ``prompts.yaml`` blocks that don't mutate at runtime) and registered
    unconditionally on every narrator turn, so they satisfy ADR-112's
    mutability rubric for Stable membership.
    """
    required = {
        "narrator_identity",
        "narrator_dialogue",
        "soul_principles",
        "output_format",
        "genre_identity",
        "genre_narrator_voice",
        "genre_npc_voice",
        "genre_world_state",
        "narrator_vocabulary",
        "genre_transition_hints",
        # ADR-112 promotions (Story 57-3) ----------------------------------
        "genre_extraction",
        "genre_keeper_monologue",
        "genre_town",
        "genre_chargen",
    }
    missing = required - set(STABLE_SECTION_NAMES)
    assert not missing, f"Required stable sections missing from allowlist: {missing}"


# --- ADR-112 / Story 57-3: per-section promotion + deferral pins ----------
#
# Each of the four "promote" tests fails loudly with a specific section
# name on regression, instead of being lost in the set-aggregate above.
# Each "defer" test is a regression guard that fails loudly if a future
# author over-promotes a conditional section into the cached zone — the
# resulting cache-thrash at every encounter boundary would be silent and
# expensive. ADR-112 §Defer is explicit: conditional registration is
# incompatible with Stable classification.


def test_genre_extraction_resolves_to_system():
    """ADR-112 §Promote — ``genre_extraction`` rides the cached System block.

    Source: ``prompts.yaml gp.extraction``; unconditionally registered at
    ``orchestrator.py:1369``. Session-static.
    """
    assert default_bucket_for_section("genre_extraction") == SectionBucket.System
    assert "genre_extraction" in STABLE_SECTION_NAMES


def test_genre_keeper_monologue_resolves_to_system():
    """ADR-112 §Promote — ``genre_keeper_monologue`` rides the cached System block.

    Source: ``prompts.yaml gp.keeper_monologue``; unconditionally registered
    at ``orchestrator.py:1381``. Session-static (ADR-098: every tier).
    """
    assert default_bucket_for_section("genre_keeper_monologue") == SectionBucket.System
    assert "genre_keeper_monologue" in STABLE_SECTION_NAMES


def test_genre_town_resolves_to_system():
    """ADR-112 §Promote — ``genre_town`` rides the cached System block.

    Source: ``prompts.yaml gp.town``; unconditionally registered at
    ``orchestrator.py:1392``. Session-static.
    """
    assert default_bucket_for_section("genre_town") == SectionBucket.System
    assert "genre_town" in STABLE_SECTION_NAMES


def test_genre_chargen_resolves_to_system():
    """ADR-112 §Promote — ``genre_chargen`` rides the cached System block.

    Source: ``prompts.yaml gp.chargen``; unconditionally registered at
    ``orchestrator.py:1403``. Session-static.
    """
    assert default_bucket_for_section("genre_chargen") == SectionBucket.System
    assert "genre_chargen" in STABLE_SECTION_NAMES


def test_genre_combat_voice_remains_in_user_bucket():
    """ADR-112 §Defer regression guard — ``genre_combat_voice`` stays uncached.

    Registered conditionally on ``context.in_combat`` at
    ``orchestrator.py:1345``. Promoting a conditional section to Stable
    would thrash the cache root at every encounter boundary (cache MISS
    on enter-combat AND on exit-combat, every fight), costing more than
    no cache at all. If this test fails because the name was added to
    ``STABLE_SECTION_NAMES``, REVERT — do not amend the allowlist without
    a new ADR superseding 112's defer rationale.
    """
    assert default_bucket_for_section("genre_combat_voice") == SectionBucket.User
    assert "genre_combat_voice" not in STABLE_SECTION_NAMES, (
        "genre_combat_voice was added to STABLE_SECTION_NAMES — ADR-112 "
        "§Defer forbids this without a superseding ADR. The conditional "
        "registration at orchestrator.py:1345 makes Stable classification "
        "cache-thrashy across combat boundaries."
    )


def test_genre_chase_voice_remains_in_user_bucket():
    """ADR-112 §Defer regression guard — ``genre_chase_voice`` stays uncached.

    Same rationale as ``genre_combat_voice``: registered conditionally on
    ``context.in_chase`` at ``orchestrator.py:1357``. Cache thrash at every
    chase boundary if promoted.
    """
    assert default_bucket_for_section("genre_chase_voice") == SectionBucket.User
    assert "genre_chase_voice" not in STABLE_SECTION_NAMES, (
        "genre_chase_voice was added to STABLE_SECTION_NAMES — ADR-112 "
        "§Defer forbids this without a superseding ADR. The conditional "
        "registration at orchestrator.py:1357 makes Stable classification "
        "cache-thrashy across chase boundaries."
    )
