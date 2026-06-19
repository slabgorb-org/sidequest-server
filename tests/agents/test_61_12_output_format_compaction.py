"""Story 61-12 — output_only.md prose compaction + npcs_met/npcs_present field-drift fix.

Pins the post-rewrite contract:

* **AC-1 (inverted vs original story description, user-confirmed 2026-05-24).**
  ``npcs_present`` is the canonical sidecar field name — every codebase site
  (``protocol/messages.py``, ``game/session.py``, ``game/persistence.py``,
  ``agents/orchestrator.py``, ``server/narration_apply.py``,
  ``server/emitters.py``, ``telemetry/spans/*``) names it that. The prose at
  ``output_only.md`` lines 208, 214, 275 (using ``npcs_met``) AND
  ``narrator_guardrails.py:31`` (NPC_INTRO_VISUAL_CONSTRAINT — "entry in
  ``npcs_met``") are the drift. Post-rewrite: zero ``npcs_met`` anywhere
  narrator-facing; the silent fallback at ``orchestrator.py:979,1003``
  (``patch.get("npcs_present", patch.get("npcs_met", []))``) is removed.

* **AC-2.** ``len(NARRATOR_OUTPUT_ONLY) <= 14900`` codepoints (~2,200 tok under
  Anthropic's chars/4 rule-of-thumb). 61-12 compacted to 13,156; three later
  load-bearing rules (anti-fabrication + ``is_creature`` + ``disengaged``) lifted
  it to 14,813, so the ceiling was raised in steps 13,800 → 14,600 (2026-06-05) →
  14,900 (2026-06-10) rather than re-compact.

* **AC-3.** The three CRITICAL MAGIC banners — ``CRITICAL MAGIC EFFECT RULE``
  (§1), ``CRITICAL MAGIC RULE`` (§3 plugin-aware), ``CRITICAL MAGIC NEGATIVE
  CASE`` — move OUT of ``NARRATOR_OUTPUT_ONLY`` and INTO a new constant
  ``NARRATOR_MAGIC_OUTPUT_RULES`` (loaded from
  ``narrator_prompts/magic_output_rules.md``). The new prose registers on
  the prompt registry ONLY when ``context.magic_state is not None`` — the
  existing chokepoint at ``orchestrator.py:1859`` for the
  ``magic_context`` block. On non-magic worlds (road_warrior, pulp_noir,
  tea_and_murder, spaghetti_western) zero bytes of magic prose leak into
  the assembled prompt.

* **AC-4.** The four ``items_*`` field paragraphs collapse into a
  consolidated block — total bytes between first and last ``items_``
  occurrence drops by ≥ 30 % from the current span. Each of the four field
  names still appears (rules still expressible).

* **AC-5.** ``count('CRITICAL') + count('MANDATORY')`` in
  ``NARRATOR_OUTPUT_ONLY`` ≤ 4. Today: 8 + 6 = 14. (The magic banners
  re-counted in ``NARRATOR_MAGIC_OUTPUT_RULES`` don't count toward this
  ceiling.)

* **AC-7 (synthetic proxy).** Building a narrator prompt for a non-magic
  turn (``context.magic_state is None``) produces an assembled system
  prompt at least 5,000 bytes smaller than the same build with
  ``magic_state`` set — proxy for the per-turn token saving on non-magic
  worlds. The 6,000-byte target in the context doc is an aggregate
  (prose compaction + magic suppression); this assertion isolates the
  magic-suppression delta which is the only piece this story uniquely
  owns.

NO new rules are introduced. Every rule asserted by existing tests
(``test_50_2_confrontation_trigger_prompt.py``, ``test_47_9_innate_proactive.py``,
``test_narrator_prompt.py``, ``test_57_4_recency_guardrails_migration.py``,
``test_narrator.py``) must remain expressible after rewrite — Dev updates
phrase-match strings to the new spelling where the rule survived
rewording. Never xfail, never skip.
"""

from __future__ import annotations

from typing import Any

import pytest

# Importing the tools package wires all 26 adapters onto default_registry
# (mirrors test_57_4_recency_guardrails_migration.py).
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import (
    Orchestrator,
    TurnContext,
    extract_structured_from_response,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.tooling_protocol import ToolingLlmClient


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 119-3: claude-agent-sdk over the Max subscription — both PAYG
    credentials must be UNSET (a SET key re-routes to PAYG and raises at call
    time). The prompt-build path these tests drive never fires the transport,
    but pin the absence so a polluted environment cannot leak in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _make_sdk_orchestrator() -> Orchestrator:
    """SDK-path orchestrator — the only viable narrator backend post-61-9.

    Story 119-3: ``AnthropicSdkClient()`` takes no args (the legacy ``sdk=``
    injection is gone; the transport is the late-bound module-level ``query``
    seam). These tests drive only ``build_narrator_prompt``, which never fires
    ``query``, so no fake stream is installed."""
    client = AnthropicSdkClient()
    assert isinstance(client, ToolingLlmClient), (
        "AnthropicSdkClient must satisfy ToolingLlmClient — the backend gate "
        "discriminator misroutes otherwise."
    )
    return Orchestrator(client=client)


def _section_by_name(registry: PromptRegistry, agent_name: str, name: str):
    for section in registry.registry(agent_name):
        if section.name == name:
            return section
    return None


def _minimal_magic_state() -> Any:
    """Minimal MagicState — a real one, since the existing magic_context
    block at orchestrator.py:1859 calls ``build_magic_context_block`` which
    duck-types into MagicState. Modeled on
    ``tests/magic/test_47_9_innate_proactive.py::_world_config_innate_active``
    but trimmed to what ``from_config`` actually needs.
    """
    from sidequest.magic.models import HardLimit, LedgerBarSpec, WorldKnowledge, WorldMagicConfig
    from sidequest.magic.state import MagicState

    config = WorldMagicConfig(
        world_slug="test_world",
        genre_slug="test_genre",
        allowed_sources=["innate"],
        active_plugins=["innate_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified"),
        visibility={"primary": "feared"},
        hard_limits=[HardLimit(id="test_limit", description="x")],
        cost_types=["sanity"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                starts_at_chargen=1.0,
            ),
        ],
        narrator_register="feared",
    )
    state = MagicState.from_config(config)
    state.add_character("Kael")
    return state


# ---------------------------------------------------------------------------
# AC-1 — field-drift correctness (inverted from original story)
# ---------------------------------------------------------------------------


def test_output_only_prose_has_zero_npcs_met_references() -> None:
    """``output_only.md`` ships with zero occurrences of ``npcs_met``.

    The codebase canonical name is ``npcs_present`` (25+ sites across
    ``protocol/messages.py``, ``game/session.py``, ``game/persistence.py``,
    ``agents/orchestrator.py``, ``server/narration_apply.py``,
    ``server/emitters.py``, ``telemetry/spans/*``). The original story
    description had AC-1 inverted; resolved 2026-05-24 in the session's
    Design Deviations log.

    Current state: 3 occurrences (lines 208, 214, 275).
    """
    assert "npcs_met" not in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must use ``npcs_present`` exclusively — "
        "found ``npcs_met`` reference(s). The codebase parser at "
        "``orchestrator.py:439,979,1003`` and 20+ other sites name the "
        "sidecar field ``npcs_present``; the prose must follow."
    )


def test_output_only_prose_documents_npcs_present_field() -> None:
    """The npcs_present sidecar-field rule must remain expressible after
    the drift fix. (Negative-only assertions are weaker — pin the positive
    so a future regression that deletes both fields fails loudly.)"""
    assert "npcs_present" in NARRATOR_OUTPUT_ONLY, (
        "NARRATOR_OUTPUT_ONLY must continue to document the ``npcs_present`` "
        "sidecar field. The drift fix renames ``npcs_met`` → ``npcs_present`` "
        "in-place; it does not delete the rule."
    )


def test_npc_intro_visual_guardrail_uses_npcs_present() -> None:
    """``NPC_INTRO_VISUAL_CONSTRAINT`` (narrator_guardrails.py) must use
    ``npcs_present`` — the same drift fix applies to the guardrail constant
    that is migrated into the output prose by story 57-4.

    Current state: line 31 says "entry in ``npcs_met`` —". Post-rewrite
    must say "entry in ``npcs_present`` —" so the migrated text in
    ``NARRATOR_OUTPUT_ONLY`` (per ADR-111 single-source-of-truth rule)
    stays consistent with the parser.
    """
    from sidequest.agents.narrator_guardrails import NPC_INTRO_VISUAL_CONSTRAINT

    assert "npcs_met" not in NPC_INTRO_VISUAL_CONSTRAINT, (
        "narrator_guardrails.NPC_INTRO_VISUAL_CONSTRAINT references "
        "``npcs_met`` — the canonical sidecar field is ``npcs_present``."
    )
    assert "npcs_present" in NPC_INTRO_VISUAL_CONSTRAINT, (
        "narrator_guardrails.NPC_INTRO_VISUAL_CONSTRAINT must continue "
        "to reference ``npcs_present`` after the drift fix — the visual-"
        "scene-on-intro rule is keyed off ``is_new: true`` on that field."
    )


def test_orchestrator_parser_has_no_npcs_met_silent_fallback() -> None:
    """``extract_structured_from_response`` must NOT silently rescue a
    narrator that emitted ``npcs_met`` instead of ``npcs_present``.

    Memory's hard ban on fallbacks (``feedback_no_fallbacks_hard``):
    silent backstop on a retired field name is the worst kind. The
    current implementation at ``orchestrator.py:979,1003`` carries
    ``patch.get("npcs_present", patch.get("npcs_met", []))`` — that
    silent rescue must go in this story.
    """
    raw = (
        "Prose here.\n\n"
        "```game_patch\n"
        '{"npcs_met": [{"name": "Boris", "role": "neutral", "side": "neutral"}]}\n'
        "```\n"
    )
    result = extract_structured_from_response(raw)
    assert result["npcs_present"] == [], (
        "extract_structured_from_response silently rescued the deprecated "
        "``npcs_met`` key into ``npcs_present``. The silent fallback at "
        "orchestrator.py:979,1003 must be removed — narrator that emits "
        "the wrong key drops to an empty list (the same behavior as any "
        "missing sidecar field) so the lie detector (OTEL spans / "
        "render_trigger) fires on the divergence instead of papering it over."
    )


# ---------------------------------------------------------------------------
# AC-2 — byte budget (chars/4 ≈ 2000 tok ceiling)
# ---------------------------------------------------------------------------


def test_output_only_prose_under_byte_budget() -> None:
    """``NARRATOR_OUTPUT_ONLY`` stays within the prompt token ceiling.

    Story 61-12 compacted the file to 13,156 (from a pre-compaction
    24,784). Four later commits each appended a load-bearing narrator
    rule — the ANTI-FABRICATION guard (playtest #431, +557), the
    ``is_creature`` routing field (npc #74, +625), the ``disengaged``
    opponent-departure field (sq-playtest 2026-06-10 long_foundry zombie
    negotiation, +~400 after compaction), and the ``grants_aspect``
    significant-item field (spec 2026-06-18 invokable-Fate-aspects, +~550)
    — which pushed it to 15,291. Each rule is intentional and kept as
    terse as the instruction allows, so the ceiling was lifted in
    deliberate steps (13,800 → 14,600 on 2026-06-05, 14,600 → 14,900 on
    2026-06-10, then 14,900 → 15,400 on 2026-06-18) rather than
    re-compacting prose at the risk of narrator-quality loss. The
    ``disengaged`` field is the narrator-facing half of the ADR-116 §4
    social end-on-no-Other fix — without it the narrator never emits the
    departure signal and the engine half is dead in production.

    CAVEAT (2026-06-18 narrator-prompt deep-dive): the earlier rationale
    that this prose is "primacy-cached (ADR-112), so the marginal per-turn
    cost is amortized" is STALE. ``narrator_output_only`` is NOT in
    STABLE_SECTION_NAMES (it routes to the uncached User bucket), and story
    119-3 collapsed the cacheable blocks to one plain string so the
    agent-SDK owns caching now — this section is paid ~in full each turn.
    The real cost/latency lever is story 126-9 (thinking-ON regression) +
    the 119-3 caching cleanup, NOT this ceiling. The budget still earns its
    keep as a GROWTH-DISCIPLINE gate (keep the prompt from ballooning,
    independent of caching). ``grants_aspect`` is Fate-only guidance that
    ideally belongs in a Fate-conditional section (story 126-11) rather
    than this always-on file. A future addition that crosses 15,400 must
    either compact or make a fresh ceiling decision.
    """
    actual = len(NARRATOR_OUTPUT_ONLY)
    assert actual <= 15_400, (
        f"NARRATOR_OUTPUT_ONLY is {actual} codepoints, exceeds the "
        f"15,400 budget (~ 2,300 tok ceiling). The narrator prompt grew "
        f"past its growth-discipline ceiling — compact the prose "
        f"(preservation-by-rewrite, keep every rule) or make a fresh "
        f"ceiling decision. Pre-61-12 baseline was 24,784."
    )


# ---------------------------------------------------------------------------
# AC-3 — magic prose extraction + conditional registration
# ---------------------------------------------------------------------------


MAGIC_BANNERS: tuple[str, ...] = (
    "CRITICAL MAGIC EFFECT RULE",
    "CRITICAL MAGIC RULE",
    "CRITICAL MAGIC NEGATIVE CASE",
)


@pytest.mark.parametrize("banner", MAGIC_BANNERS)
def test_magic_banner_removed_from_output_only(banner: str) -> None:
    """Each of the three CRITICAL MAGIC banners is gone from
    ``NARRATOR_OUTPUT_ONLY``. The prose moved into
    ``NARRATOR_MAGIC_OUTPUT_RULES`` (see next test) and is registered
    conditionally so non-magic worlds pay zero bytes for it."""
    assert banner not in NARRATOR_OUTPUT_ONLY, (
        f"NARRATOR_OUTPUT_ONLY still contains the {banner!r} banner — "
        f"AC-3 requires the magic-rule prose to move to "
        f"``NARRATOR_MAGIC_OUTPUT_RULES``. Non-magic worlds pay ~400 tok "
        f"per turn for prose they never use today; the conditional "
        f"registration at orchestrator.py:1859 is the gate."
    )


def test_narrator_magic_output_rules_constant_exists() -> None:
    """``narrator_prompts.NARRATOR_MAGIC_OUTPUT_RULES`` is a new constant
    loaded from ``narrator_prompts/magic_output_rules.md``. Mirrors the
    pattern of the existing ``NARRATOR_OUTPUT_ONLY = _load("output_only.md")``
    in ``narrator_prompts/__init__.py``.
    """
    from sidequest.agents import narrator_prompts as np

    assert hasattr(np, "NARRATOR_MAGIC_OUTPUT_RULES"), (
        "narrator_prompts module must expose NARRATOR_MAGIC_OUTPUT_RULES "
        "— the extracted magic-rule prose. Loaded from "
        "narrator_prompts/magic_output_rules.md (parallel to the existing "
        "output_only.md load at narrator_prompts/__init__.py)."
    )
    value = np.NARRATOR_MAGIC_OUTPUT_RULES
    assert isinstance(value, str) and len(value) > 200, (
        f"NARRATOR_MAGIC_OUTPUT_RULES must be a non-trivial prose string "
        f"(> 200 chars); got {type(value).__name__} "
        f"len={len(value) if isinstance(value, str) else 'n/a'}"
    )
    assert "NARRATOR_MAGIC_OUTPUT_RULES" in np.__all__, (
        "NARRATOR_MAGIC_OUTPUT_RULES must appear in narrator_prompts.__all__ "
        "so consumers see it through the canonical import surface."
    )


@pytest.mark.parametrize("banner", MAGIC_BANNERS)
def test_magic_banner_present_in_extracted_constant(banner: str) -> None:
    """Every CRITICAL MAGIC banner removed from NARRATOR_OUTPUT_ONLY must
    appear in NARRATOR_MAGIC_OUTPUT_RULES. Preservation-by-rewrite: the
    rule moves, it does not vanish."""
    from sidequest.agents.narrator_prompts import NARRATOR_MAGIC_OUTPUT_RULES

    assert banner in NARRATOR_MAGIC_OUTPUT_RULES, (
        f"NARRATOR_MAGIC_OUTPUT_RULES is missing the {banner!r} banner. "
        f"AC-3 is preservation-by-rewrite — the magic rule moves OUT of "
        f"output_only.md and INTO the new conditional section, but the "
        f"banner phrase itself must survive verbatim (existing tests in "
        f"tests/magic/test_47_9_innate_proactive.py phrase-match on it)."
    )


@pytest.mark.asyncio
async def test_magic_output_rules_section_registered_when_magic_state_present() -> None:
    """When ``context.magic_state is not None``, the orchestrator's
    section-build path registers a ``magic_output_rules`` section on the
    prompt registry. Mirrors the pattern at ``orchestrator.py:1859`` for
    the existing ``magic_context`` block — same gate, same chokepoint, no
    parallel mechanism."""
    context = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=3,
        magic_state=_minimal_magic_state(),
    )
    orch = _make_sdk_orchestrator()
    _, registry = await orch.build_narrator_prompt("act", context)
    section = _section_by_name(registry, orch._narrator.name(), "magic_output_rules")
    assert section is not None, (
        "When context.magic_state is not None, the orchestrator must "
        "register a ``magic_output_rules`` section (the extracted CRITICAL "
        "MAGIC prose). Current state: section not registered because the "
        "wiring at orchestrator.py:1859 only adds the magic_context block. "
        "AC-3: add a sibling registration for NARRATOR_MAGIC_OUTPUT_RULES "
        "behind the same ``context.magic_state is not None`` gate."
    )
    assert "CRITICAL MAGIC" in section.content, (
        f"magic_output_rules section is registered but does not contain "
        f"the CRITICAL MAGIC prose. The section must wrap "
        f"NARRATOR_MAGIC_OUTPUT_RULES. Got content head:\n"
        f"{section.content[:200]}"
    )


@pytest.mark.asyncio
async def test_magic_output_rules_section_absent_when_magic_state_none(
    simple_turn_context_turn_three,
) -> None:
    """When ``context.magic_state is None`` (non-magic worlds:
    road_warrior, pulp_noir, tea_and_murder, spaghetti_western), the
    ``magic_output_rules`` section MUST NOT be registered. Zero-byte-leak
    discipline, matching the pattern at ``orchestrator.py:1859`` for
    ``magic_context``."""
    assert simple_turn_context_turn_three.magic_state is None, (
        "Fixture invariant: simple_turn_context_turn_three has magic_state=None"
    )
    orch = _make_sdk_orchestrator()
    _, registry = await orch.build_narrator_prompt("act", simple_turn_context_turn_three)
    section = _section_by_name(registry, orch._narrator.name(), "magic_output_rules")
    assert section is None, (
        "Non-magic turn (context.magic_state is None) leaked a "
        "``magic_output_rules`` section into the prompt registry. AC-3 "
        "zero-byte-leak: the conditional gate must mirror the existing "
        "magic_context gate at orchestrator.py:1859 — single chokepoint, "
        "no parallel mechanism."
    )


# ---------------------------------------------------------------------------
# AC-4 — items_* fields RETIRED from the narrator output (superseded by 151-4)
#
# Story 61-12 AC-4 consolidated the four ``items_*`` paragraphs into one block.
# Story 151-4 / ADR-150 step 4 goes further and RETIRES the transactional
# fields from the narrator output contract entirely — items×4 (plus gold_change,
# companions×2) are extracted post-narration by the sidecar extractor now, not
# emitted by the narrator. The consolidation AC is therefore superseded by full
# removal; the block no longer exists to measure for compaction.
# ---------------------------------------------------------------------------


ITEMS_FIELDS: tuple[str, ...] = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
)


def test_items_fields_retired_from_narrator_output() -> None:
    """Story 151-4 / ADR-150 step 4: the four ``items_*`` transactional sidecar
    fields are RETIRED from the narrator output contract — the narrator no longer
    emits them; the post-narration sidecar extractor produces them. They must no
    longer be named in ``NARRATOR_OUTPUT_ONLY``. Supersedes the pre-151-4
    ``test_items_fields_all_still_named`` (61-12 AC-4 consolidation)."""
    for field_name in ITEMS_FIELDS:
        assert field_name not in NARRATOR_OUTPUT_ONLY, (
            f"NARRATOR_OUTPUT_ONLY still names {field_name!r}; ADR-150 step 4 "
            f"retires the transactional fields from the narrator output contract "
            f"(extracted post-narration now)."
        )


# ---------------------------------------------------------------------------
# AC-5 — banner demotion
# ---------------------------------------------------------------------------


def test_critical_and_mandatory_banner_count_under_ceiling() -> None:
    """At most 4 ``CRITICAL`` or ``MANDATORY`` banners survive in
    NARRATOR_OUTPUT_ONLY. Today: 8 CRITICAL + 6 MANDATORY = 14. When
    everything shouts, nothing reads as load-bearing — the surviving 4
    are the diamonds (Diamonds & Coal, ADR-014; ``feedback`` memory
    on no-over-emphasis).

    Banners moved to ``NARRATOR_MAGIC_OUTPUT_RULES`` (CRITICAL MAGIC
    EFFECT RULE, CRITICAL MAGIC RULE, CRITICAL MAGIC NEGATIVE CASE) do
    not count toward this ceiling because they live in a different
    constant.
    """
    critical = NARRATOR_OUTPUT_ONLY.count("CRITICAL")
    mandatory = NARRATOR_OUTPUT_ONLY.count("MANDATORY")
    total = critical + mandatory
    assert total <= 4, (
        f"NARRATOR_OUTPUT_ONLY contains {critical} CRITICAL + {mandatory} "
        f"MANDATORY banners ({total} total); AC-5 caps at 4. Banner "
        f"saturation devalues every banner. Demote all but the 4 most "
        f"load-bearing rules (suggested survivors: STRICT SPLIT / silent-"
        f"fallback gate, INVENTORY contract, ADVERSARY/ROSTER ``npcs_present`` "
        f"contract, RECIPIENT-per-PC contract — Reviewer judges fit)."
    )


# ---------------------------------------------------------------------------
# AC-6 — every existing rule still expressible
# ---------------------------------------------------------------------------


# Phrases / tokens load-bearing for rules that EXISTING tests assert on.
# Each entry is paired with the test file that fingerprints it — Dev
# updates phrase strings in those test files where the rewrite changes
# wording, but the rule's general concept must remain expressible here.
REQUIRED_TOKENS: tuple[str, ...] = (
    # NOTE (story 61-14, 2026-05-27): the 8 confrontation-type tokens
    # (ship_combat, dogfight, social_duel, trial, auction, scandal,
    # negotiation, chase) were REMOVED from this list. They were "load-bearing
    # for test_50_2_confrontation_trigger_prompt" — but that test no longer
    # exists, and the rule it pinned migrated off the narrator surface. Per
    # ADR-113 the narrator no longer chooses confrontation type; the Intent
    # Router does, reading the closed enum from game_state.confrontation_types
    # (sourced from pack.rules.confrontations at runtime — story 59-10). Live
    # measurement 2026-05-27: none of the 8 tokens appear in the assembled SDK
    # narrator prompt (the only viable backend post-61-9); the legacy guardrail
    # injection of CONFRONTATION_TRIGGER_CONSTRAINT is gated to the non-SDK
    # backend (_maybe_register_legacy_guardrail). Coverage of the new home is
    # preserved by tests/server/test_intent_router_confrontation_vocabulary.py
    # (mechanism + OTEL span, against a synthetic fixture pack — the genre type
    # names are CONTENT, correctly not hard-asserted in engine tests per
    # feedback_tests_not_point_at_content). Design Deviation logged in the
    # 61-14 session. Silent omission is forbidden — hence this banner.
    # test_narrator_prompt — sidecar fields + side enum + tiers
    "side",
    "player",
    "opponent",
    "neutral",
    "landscape",
    "portrait",
    "scene_illustration",
    "apply_status",
    "Boon",
    # Story 151-3 (ADR-150 step 3): "action_rewrite" REMOVED from this list —
    # retired from the narrator output contract, produced by the pre-narrator
    # IntentRouter now (same migration shape as the 61-14 confrontation-type
    # token removal above). New home covered by
    # tests/agents/test_action_rewrite_intent_router_prepass.py +
    # tests/agents/test_narrator.py::test_narrator_output_format_retires_action_rewrite.
    # Design Deviation logged in the 151-3 session — silent omission is forbidden,
    # hence this banner.
    # test_narrator — sidecar npc-adversary rule
    "CRITICAL ADVERSARY RULE",
    # test_57_4_recency_guardrails_migration — load-bearing fingerprint
    "Recurring NPCs",
    "Patients on a sickbed count",
)


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_required_rule_token_still_present(token: str) -> None:
    """Every load-bearing token / phrase that an existing test
    asserts on must remain in NARRATOR_OUTPUT_ONLY post-rewrite.

    Note: a few CURRENT phrase-tests reference banners that move to
    NARRATOR_MAGIC_OUTPUT_RULES (CRITICAL MAGIC EFFECT RULE, magic_working,
    consent_state). Those tests get updated in Dev's green phase to
    reference NARRATOR_MAGIC_OUTPUT_RULES instead — they are NOT in this
    test's REQUIRED_TOKENS because the rule moved out of this constant
    legitimately per AC-3.
    """
    assert token in NARRATOR_OUTPUT_ONLY, (
        f"NARRATOR_OUTPUT_ONLY lost the load-bearing token {token!r} that "
        f"existing tests phrase-match on. The compaction must preserve "
        f"every rule that is asserted today — the phrase may be reworded "
        f"(and Dev updates the assertion strings accordingly) but the "
        f"underlying token / rule survives. If a token genuinely no "
        f"longer belongs in this file (because the rule moved to "
        f"NARRATOR_MAGIC_OUTPUT_RULES), log a Design Deviation and remove "
        f"it from REQUIRED_TOKENS — silent omission is forbidden."
    )


# ---------------------------------------------------------------------------
# AC-7 — non-magic prompt is materially smaller than magic-on prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_magic_prompt_smaller_than_magic_prompt_by_at_least_5kb() -> None:
    """Synthetic AC-7 proxy: build a narrator prompt for a non-magic turn
    (context.magic_state is None) and a magic turn (context.magic_state is
    not None). The non-magic assembled prompt must be ≥ 5,000 bytes
    smaller — the byte savings the magic-prose extraction is supposed to
    unlock per turn on road_warrior / pulp_noir / tea_and_murder /
    spaghetti_western.

    The 5,000-byte threshold isolates the magic-suppression delta this
    story uniquely owns; the 6,000-byte aggregate target in the AC
    includes additional savings from the prose compaction passes
    (AC-2 / AC-4 / AC-5) which the non-magic prompt also pays.
    """
    non_magic = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=3,
    )
    magic = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=3,
        magic_state=_minimal_magic_state(),
    )
    orch = _make_sdk_orchestrator()
    non_magic_prompt, _ = await orch.build_narrator_prompt("act", non_magic)
    magic_prompt, _ = await orch.build_narrator_prompt("act", magic)
    delta = len(magic_prompt) - len(non_magic_prompt)
    assert delta >= 5_000, (
        f"Non-magic prompt is only {delta} bytes smaller than the magic "
        f"prompt; AC-7 requires the magic-suppression delta to be "
        f"≥ 5,000 bytes. Today the delta is ~0 because both paths load "
        f"the same NARRATOR_OUTPUT_ONLY (which carries the magic prose "
        f"inline). After AC-3 lands, the magic prose only registers on "
        f"the magic_state-set path, so the non-magic prompt drops the "
        f"~400 tok / ~1,600 bytes of CRITICAL MAGIC banners plus any "
        f"surrounding paragraphs that move with them."
    )


# Wiring is enforced by the two async tests above
# (test_magic_output_rules_section_registered_when_magic_state_present +
# test_magic_output_rules_section_absent_when_magic_state_none): they
# drive the live Orchestrator.build_narrator_prompt path and assert on
# what registered, rather than grepping source text for the constant
# name. Per sidequest-server CLAUDE.md "No Source-Text Wiring Tests" —
# a behavior-driving test is the load-bearing wiring check; a string
# grep on production source is the forbidden pattern.
