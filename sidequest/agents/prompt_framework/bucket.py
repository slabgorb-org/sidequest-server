"""System/user prompt bucketing for stateless narrator turns.

ADR-098 splits the per-turn prompt into a stable scaffold (system_prompt)
and turn-dynamic content (user_message). This module owns the allowlist
that drives the partition; section names not on the allowlist default to
the user bucket.
"""

from __future__ import annotations

from enum import StrEnum


class SectionBucket(StrEnum):
    """Outbound destination for a registered prompt section."""

    System = "system"
    User = "user"


# Section names whose content is byte-identical across every turn of the
# same game given fixed operator settings (genre + verbosity + vocabulary).
# Spec: docs/superpowers/specs/2026-05-10-stateless-narrator-design.md §Composition.
#
# Adding a section here is a load-bearing decision: it must remain stable
# turn-to-turn. If it can change per turn (state, encounter, magic, action,
# recency guardrails), leave it OFF this list — the default is User.
STABLE_SECTION_NAMES: frozenset[str] = frozenset(
    {
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
        # ADR-112 / Story 57-3 — unconditional, session-static genre prose
        # sourced from per-pack ``prompts.yaml`` (gp.extraction / gp.keeper_
        # monologue / gp.town). These three are registered on every
        # narrator turn at ``orchestrator.py`` (extraction ~1558, keeper
        # ~1571, town ~1583) with ``AttentionZone.Early`` (re-zoned from
        # Valley as part of the same change so the content actually lands
        # in ``system_blocks[0]``, the cache-marked block — see Story 57-3
        # Dev deviation log).
        # Promotion DOES NOT apply to ``genre_combat_voice`` /
        # ``genre_chase_voice`` (ADR-112 §Defer: conditional registration
        # would thrash the cache root at every encounter boundary).
        #
        # Story 61-11 (ADR-112 amendment): ``genre_chargen`` is REMOVED
        # from this set — the predicate audit found that its scene scope
        # (post-chargen opening turn only) is cleanly expressible via the
        # existing ``TurnContext.opening_directive`` field, so the
        # per-turn carry is no longer justified. Registration at
        # ``orchestrator.py`` (chargen block ~1595) now gates on
        # ``context.opening_directive is not None``; the section routes
        # to the User bucket. ``genre_extraction`` and
        # ``genre_keeper_monologue`` retain Stable classification — no
        # existing runtime signal expresses their scene scope today; a
        # future story will either build one or migrate to ADR-113
        # tool-attached scope. ``genre_town`` stays per the original
        # §Defer (high firing rate; needs profiling before demotion).
        "genre_extraction",
        "genre_keeper_monologue",
        "genre_town",
        # Story 61-10 — byte-static narrator prose loaded from .md files
        # (narrator_prompts/__init__.py) with no runtime interpolation.
        # Omitted at ADR-098/111 cutover, not deliberately excluded.
        "narrator_constraints",
        "narrator_agency",
        "narrator_consequences",
        "narrator_pov_rules",
        "narrator_referral_rule",
        "narrator_output_style",
    }
)


def default_bucket_for_section(name: str) -> SectionBucket:
    """Return the bucket a section name resolves to.

    Names in :data:`STABLE_SECTION_NAMES` go to :attr:`SectionBucket.System`;
    everything else (encounter context, state, recency guardrails, player
    action, etc.) goes to :attr:`SectionBucket.User`.
    """
    if name in STABLE_SECTION_NAMES:
        return SectionBucket.System
    return SectionBucket.User


__all__ = [
    "STABLE_SECTION_NAMES",
    "SectionBucket",
    "default_bucket_for_section",
]
