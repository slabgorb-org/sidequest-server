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
        # Story 151-1 (ADR-150 §Companion quick-win / Alternative A) — the
        # game_patch output contract ``narrator_output_only`` (built by
        # ``NarratorAgent.build_output_format`` at narrator.py:290 from the
        # static ``NARRATOR_OUTPUT_ONLY = _load("output_only.md")`` constant,
        # no runtime interpolation -> byte-identical per session). Already
        # registered in ``AttentionZone.Primacy``; the missing half was the
        # System bucket. Promoting it moves ~15k codepoints (~3.8k tok) off the
        # per-turn user message onto the stable, CLI-cached ``system_prompt``
        # prefix. NOTE (post-119-3): the per-block ``cache=True`` markers are
        # dead (the agent-SDK flattens system_blocks into one system_prompt and
        # the CLI owns caching) — the win is System->system_prompt (stable,
        # cached across turns) vs User->per-turn message (uncached), NOT the
        # legacy 1h-breakpoint mechanism. The spec's named landmine
        # ``test_60_6_stable_prefix_live_drift`` was deleted in 119-3; nothing
        # breaks on this promotion.
        "narrator_output_only",
        # Story 61-20 (ADR-112 zone-promotion) — session-static content lifted
        # out of the volatile Valley tail into the cache-marked system prefix so
        # it is written once and read every subsequent turn (closes 61-19
        # AC1/AC2/AC3 on volume, not just tier).
        #
        # ``world_context`` carries the per-world AVAILABLE CULTURES roster
        # (orchestrator.py ~2076) — fixed for the life of a session.
        #
        # ``magic_hard_limits`` is the session-static HEAD of the old
        # ``magic_context`` block (world_slug / allowed_sources / active_plugins
        # / valid_cost_types / hard_limits / world_knowledge), split off in
        # 61-20. The per-actor ledger (``active_ledger_for_<actor>`` + bar
        # values), the magic_working instruction, learned-magic, and reliquary
        # blocks STAY in ``magic_context`` (Valley, User bucket) because they
        # change as magic is cast — promoting them would re-create the 61-19
        # cross-turn churn. The split is the load-bearing half of this story.
        "world_context",
        "magic_hard_limits",
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
