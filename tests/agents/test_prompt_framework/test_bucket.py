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
    ``genre_town``, ``genre_chargen``) were promoted into the Stable
    allowlist. They were session-static (sourced from per-pack
    ``prompts.yaml`` blocks that don't mutate at runtime) and registered
    unconditionally on every narrator turn.

    Story 61-11 (ADR-112 amendment): ``genre_chargen`` is demoted from
    Stable — it was the only one of the four for which an existing
    runtime predicate (``TurnContext.opening_directive is not None``)
    cleanly expressed its scene scope. The chargen prose is needed only
    on the post-chargen opening turn; carrying it on every subsequent
    "you walk into the tavern" turn was misallocated cache budget.
    ``genre_extraction`` and ``genre_keeper_monologue`` retain Stable
    classification pending a future story that builds the missing
    runtime signals (no ``extraction_active`` or ``keeper_speaking``
    field exists today). ``genre_town`` retains Stable classification
    per the original ADR-112 §Defer — high firing rate, needs profiling
    before demotion.
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
        # ADR-112 promotions that remain Stable (Story 57-3, unchanged) ----
        "genre_extraction",
        "genre_keeper_monologue",
        "genre_town",
        # NOTE: ``genre_chargen`` removed from this set per Story 61-11.
        # See ``test_genre_chargen_resolves_to_user`` for the inverse pin.
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


def test_genre_chargen_resolves_to_user():
    """Story 61-11 (ADR-112 amendment) — ``genre_chargen`` is demoted.

    Source: ``prompts.yaml gp.chargen``; registered ONLY when
    ``TurnContext.opening_directive is not None`` at the post-chargen
    opening turn (see ``orchestrator.py`` chargen registration block —
    Story 61-11 added the predicate gate around the existing ``if
    gp.chargen:`` outer guard).

    The original ADR-112 promotion (Story 57-3) carried the prose on
    every turn for cache stability, but the predicate audit performed
    during 61-11 found that ``opening_directive`` (set by
    ``_populate_opening_directive_on_chargen_complete`` at
    ``websocket_session_handler.py:181-346``, cleared after the opening
    turn fires) IS a clean existing signal for the chargen scene. The
    section is needed only on the opening turn — at most once per
    session. Carrying it on every subsequent neutral turn was a
    ~150-token misallocation that this story corrects.

    Regression guard: if a future author promotes ``genre_chargen``
    back into ``STABLE_SECTION_NAMES``, this test fails loudly. The
    chargen prose ("describe what the character is carrying — their
    weapon, their pack contents, how much coin they have. The player
    should know their loadout before they step into the dark") is
    scene-typed and does not belong in the cache root.
    """
    assert default_bucket_for_section("genre_chargen") == SectionBucket.User
    assert "genre_chargen" not in STABLE_SECTION_NAMES, (
        "genre_chargen was added back to STABLE_SECTION_NAMES — Story 61-11 "
        "(ADR-112 amendment) demoted it. Promotion would re-introduce the "
        "~150-tok per-turn carry on every neutral turn. The chargen prose is "
        "scene-typed (post-chargen opening turn only); the predicate gate at "
        "the orchestrator registration block keeps it relevant where it fires."
    )


# --- Story 61-10: byte-static narrator prose promotion ----------------------
#
# Six narrator prose sections are loaded byte-exactly from static .md files
# (narrator_prompts/__init__.py) with no runtime interpolation. They meet
# STABLE_SECTION_NAMES' criterion ("byte-identical across every turn of the
# same game") and belong in the System bucket. They were omitted at the
# ADR-098/111 cutover, not deliberately excluded.

_STORY_61_10_SECTIONS: frozenset[str] = frozenset(
    {
        "narrator_constraints",
        "narrator_agency",
        "narrator_consequences",
        "narrator_pov_rules",
        "narrator_referral_rule",
        "narrator_output_style",
    }
)


class TestStory6110ByteStaticProsePromotion:
    """Story 61-10 — promote six byte-static narrator prose sections."""

    def test_all_six_in_stable_section_names(self):
        """AC-1: All six sections appear in STABLE_SECTION_NAMES."""
        missing = _STORY_61_10_SECTIONS - STABLE_SECTION_NAMES
        assert not missing, (
            f"Story 61-10: these byte-static narrator prose sections are "
            f"missing from STABLE_SECTION_NAMES: {sorted(missing)}"
        )

    def test_each_resolves_to_system_bucket(self):
        """AC-2: default_bucket_for_section returns System for each."""
        for name in sorted(_STORY_61_10_SECTIONS):
            assert default_bucket_for_section(name) == SectionBucket.System, (
                f"{name!r} should resolve to SectionBucket.System after "
                f"promotion — currently resolves to User because it is "
                f"not in STABLE_SECTION_NAMES"
            )

    def test_minimum_contents_includes_promoted_sections(self):
        """AC-3: snapshot of STABLE_SECTION_NAMES includes all six."""
        expected = {
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
            "genre_extraction",
            "genre_keeper_monologue",
            "genre_town",
            # Story 61-10 promotions:
            "narrator_constraints",
            "narrator_agency",
            "narrator_consequences",
            "narrator_pov_rules",
            "narrator_referral_rule",
            "narrator_output_style",
        }
        missing = expected - set(STABLE_SECTION_NAMES)
        assert not missing, f"STABLE_SECTION_NAMES snapshot is missing sections: {sorted(missing)}"

    def test_narrator_constraints_resolves_to_system(self):
        """Per-section pin — constraints.md (~112 tok)."""
        assert default_bucket_for_section("narrator_constraints") == SectionBucket.System
        assert "narrator_constraints" in STABLE_SECTION_NAMES

    def test_narrator_agency_resolves_to_system(self):
        """Per-section pin — agency.md (~201 tok)."""
        assert default_bucket_for_section("narrator_agency") == SectionBucket.System
        assert "narrator_agency" in STABLE_SECTION_NAMES

    def test_narrator_consequences_resolves_to_system(self):
        """Per-section pin — consequences.md (~71 tok)."""
        assert default_bucket_for_section("narrator_consequences") == SectionBucket.System
        assert "narrator_consequences" in STABLE_SECTION_NAMES

    def test_narrator_pov_rules_resolves_to_system(self):
        """Per-section pin — pov_rules.md (~200 tok)."""
        assert default_bucket_for_section("narrator_pov_rules") == SectionBucket.System
        assert "narrator_pov_rules" in STABLE_SECTION_NAMES

    def test_narrator_referral_rule_resolves_to_system(self):
        """Per-section pin — referral_rule.md (~65 tok)."""
        assert default_bucket_for_section("narrator_referral_rule") == SectionBucket.System
        assert "narrator_referral_rule" in STABLE_SECTION_NAMES

    def test_narrator_output_style_resolves_to_system(self):
        """Per-section pin — output_style.md (~115 tok)."""
        assert default_bucket_for_section("narrator_output_style") == SectionBucket.System
        assert "narrator_output_style" in STABLE_SECTION_NAMES


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
