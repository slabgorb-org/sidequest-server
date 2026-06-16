"""Story 47-9 — Force first innate_v1 firing on Coyote Star with GM-panel observability.

The magic subsystem is wired end-to-end but never invoked in play: 1 working_log
entry across 7 saves / 111 turns. Architect audit (2026-05-07) identified the
break at the prompt level — the narrator's CRITICAL MAGIC RULE is reactive
("emit magic_working when prose depicts a working") with no proactive trigger
to depict one. innate_v1 has fired zero times because nothing in the prose flow
stresses a character into reflexive surfacing.

This test file enforces the proactive-prompt contract via three coordinated
assertions, each covering one segment of the path. None of these tests claims
to be "end-to-end" alone — together they triangulate the wiring:

* **AC1 (context_builder unit)**: ``build_magic_context_block`` injects an
  innate_v1 worked example when the plugin is active (sentinel-pinned), and
  omits the block entirely when not.
* **AC2 (prompt assembly + wiring)**: ``Orchestrator.build_narrator_prompt``
  surfaces both the rewritten CRITICAL MAGIC RULE phrases (NARRATOR_OUTPUT_ONLY
  → assembled prompt) and the worked-example sentinel from context_builder.
  Negative test confirms the worked example does NOT leak into a non-innate
  world's prompt.
* **AC3 (content schema + agency)**: ``coyote_star/openings.yaml`` includes
  at least one opening with ``magic_microbleed.cost_bar='sanity'`` and
  PC-anchored reflexive-surfacing detail, AND that detail does NOT narrate
  internal perception (NARRATOR_AGENCY enforcement).
* **AC4 (apply pipeline)**: ``apply_magic_working`` with a worked-example-shape
  dict produces the magic.working watcher span, appends to working_log, and
  debits the sanity bar.
* **AC4 supplement (dispatch seam)**: a raw narrator response string flows
  through ``extract_structured_from_response`` → ``apply_magic_working`` and
  produces the same span + debit. Pins the historic-failure-prone seam.
* **AC7 (persistence regression)**: after apply, the snapshot survives a
  SqliteStore roundtrip with working_log and ledger value preserved.

AC5 is exercised by AC4 / AC4-supplement (working_log length and sanity bar
value below chargen). AC6 (GM dashboard ``just otel`` verification) is a
manual step deferred to Story 47-2 — it cannot be automated without a
running orchestrator + browser.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.narrator import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.magic.context_builder import build_magic_context_block
from sidequest.magic.models import (
    HardLimit,
    LedgerBarSpec,
    StatusPromotion,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.state import BarKey, MagicState

# ---------------------------------------------------------------------------
# Module-level sentinels (single source of truth for all assertions)
# ---------------------------------------------------------------------------

# Sentinel phrase unique to the 47-9 worked-example block in
# context_builder.py. Used by both the AC1 context-level test and the
# orchestrator-level wiring test to keep the prompt → assembly contract in
# lockstep. If the label in context_builder.py changes, update here.
CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL = "Example innate_v1 working"

# 47-9-introduced phrases in narrator.py CRITICAL MAGIC RULE rewrite.
# Confirmed absent from origin/develop's narrator.py via:
#   git show origin/develop:sidequest/agents/narrator.py | grep -F '<phrase>'
# Each phrase is a multi-word string that did not appear in the pre-47-9
# prompt; together they pin the proactive-rule rewrite against future
# regressions (a generic single-word marker like "consider" would
# have been satisfied by the pre-existing prompt).
NARRATOR_PROACTIVE_RULE_PHRASES = [
    "every PC action under stress",
    "stress-triggered",
    "plugin-aware and proactive",
]

# Playtest 2026-05-09 turn 9 surfaced the inverse of 47-9: the narrator
# wrote "Willes' page has not warmed" (an explicit non-working signal)
# but still emitted ``magic_working`` with backlash 0.05. The proactive
# rule was unbalanced — MUST-emit had no equally-strong DON'T-emit
# counterweight. ``NARRATOR_NEGATIVE_CASE_PHRASES`` pins the negative
# rule so future prompt edits don't silently strip it.
NARRATOR_NEGATIVE_CASE_PHRASES = [
    "CRITICAL MAGIC NEGATIVE CASE",
    "MUST NOT emit magic_working",
    "passive carryover",
    "observation alone is not",
]


# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------


def _world_config_innate_active() -> WorldMagicConfig:
    """Coyote Star shape — innate_v1 + item_legacy_v1 active."""
    return WorldMagicConfig(
        world_slug="coyote_star",
        genre_slug="space_opera",
        allowed_sources=["innate", "item_based"],
        active_plugins=["innate_v1", "item_legacy_v1"],
        intensity=0.25,
        world_knowledge=WorldKnowledge(primary="classified", local_register="folkloric"),
        visibility={"primary": "feared", "local_register": "dismissed"},
        hard_limits=[HardLimit(id="psionics_never_decisive", description="x")],
        cost_types=["sanity", "notice"],
        ledger_bars=[
            LedgerBarSpec(
                id="sanity",
                scope="character",
                direction="down",
                range=(0.0, 1.0),
                threshold_low=0.40,
                consequence_on_low_cross="auto-fire The Bleeding-Through",
                starts_at_chargen=1.0,
                promote_to_status=StatusPromotion(text="Bleeding through", severity="Wound"),
            ),
            LedgerBarSpec(
                id="notice",
                scope="character",
                direction="up",
                range=(0.0, 1.0),
                threshold_high=0.75,
                starts_at_chargen=0.0,
            ),
        ],
        narrator_register="feared and folkloric",
    )


def _world_config_item_only() -> WorldMagicConfig:
    """Caverns-shape config — only item_legacy_v1 active, no innate_v1."""
    return WorldMagicConfig(
        world_slug="caverns_sunden",
        genre_slug="caverns_and_claudes",
        allowed_sources=["item_based"],
        active_plugins=["item_legacy_v1"],
        intensity=0.30,
        world_knowledge=WorldKnowledge(primary="folkloric"),
        visibility={"primary": "feared", "local_register": "dismissed"},
        hard_limits=[HardLimit(id="no_resurrection", description="death is permanent")],
        cost_types=["components", "backlash"],
        ledger_bars=[
            LedgerBarSpec(
                id="backlash",
                scope="character",
                direction="up",
                range=(0.0, 1.0),
                threshold_high=0.70,
                starts_at_chargen=0.0,
            ),
        ],
        narrator_register="folkloric",
    )


def _make_canned_client(canned_result: str) -> ClaudeClient:
    """Build a ClaudeClient whose subprocess returns the supplied canned string."""

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any):
        class FakeProcess:
            returncode = 0

            async def communicate(self):
                payload = {
                    "result": canned_result,
                    "session_id": "test-session-47-9",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                return json.dumps(payload).encode(), b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        return FakeProcess()

    return ClaudeClient(spawn_fn=spawn_fn)


@pytest.fixture
def captured_watcher_events(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, Any]]]:
    """Intercept ``narration_apply._watcher_publish`` calls.

    Each captured event has the shape::

        {
            "event_type": str,    # e.g. "state_transition"
            "fields": dict,       # span attributes; for magic.working spans
                                  #   includes plugin, actor, mechanism_engaged,
                                  #   domain, narrator_basis, costs_debited,
                                  #   flags, ledger_after, op="working", and
                                  #   innate_v1 extras (flavor, consent_state).
            "component": str,     # "magic" for magic.working spans
            "severity": str,      # e.g. "info"
        }

    For magic.working spans, filter on
    ``component == "magic" and event_type == "state_transition" and
    fields["op"] == "working"`` — mirrors the ``SPAN_ROUTES[SPAN_MAGIC_WORKING]``
    extractor in ``sidequest/telemetry/spans/magic.py``.
    """
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.server import narration_apply

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    yield captured


# ---------------------------------------------------------------------------
# AC1 — context_builder injects innate worked example
# ---------------------------------------------------------------------------


def test_context_block_includes_innate_v1_worked_example_when_active():
    """When innate_v1 ∈ active_plugins, the magic-context block includes a
    worked example showing stress → reflexive surfacing → sanity cost → the
    magic_working JSON shape (AC1)."""
    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")
    block = build_magic_context_block(magic_state=state, actor_id="sira_mendes")

    # 47-9 sentinel — unique to the worked-example block, not present in any
    # pre-47-9 narrator/context output.
    assert CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL in block, (
        f"Block should include the {CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL!r} "
        f"sentinel introducing the worked example. Got block:\n{block}"
    )
    assert "consent_state" in block, (
        "Worked example must show consent_state — the innate_v1-required field "
        "that signals reflexive vs willing surfacing"
    )
    assert "involuntary" in block.lower(), (
        "Worked example must demonstrate the involuntary (reflexive) consent_state — "
        "this is the shape we want narrator to emit on stress-triggered surfacing"
    )
    assert "flavor" in block, (
        "Worked example must show the flavor field (acquired|born_to_it|trained_register|covenant_lineage)"
    )
    # Cost shape must include sanity (the innate cost bar)
    assert "sanity" in block, "Worked example must show sanity as the cost being debited"
    # JSON-shape marker — give Claude the literal pattern to match
    assert (
        '"plugin": "innate_v1"' in block
        or '"plugin":"innate_v1"' in block
        or "plugin: innate_v1" in block
    ), "Worked example must show the literal magic_working JSON shape"

    # The flavor field MUST use a placeholder, not a hardcoded value. A
    # literal-following narrator parsing the example would otherwise emit
    # the hardcoded value (e.g., "acquired") for every PC regardless of
    # their actual chargen-bound flavor — flavor must vary per character
    # (acquired | born_to_it | trained_register | covenant_lineage). Other
    # placeholders in the same JSON ("<character_name>") demonstrate the
    # convention; flavor should follow it.
    forbidden_hardcoded_flavors = [
        '"flavor": "acquired"',
        '"flavor": "born_to_it"',
        '"flavor": "trained_register"',
        '"flavor": "covenant_lineage"',
    ]
    leaked_flavor = [f for f in forbidden_hardcoded_flavors if f in block]
    assert not leaked_flavor, (
        f"Worked example hardcodes a flavor value: {leaked_flavor}. The flavor "
        'must be a placeholder (e.g., "<character\'s chargen-bound flavor>") '
        "so the narrator picks the actual chargen flavor per character."
    )


# Sentinel phrases used across this file. Defined as module constants so the
# context_builder source-of-truth and orchestrator-level wiring assertions
# move in lockstep — if the sentinel changes in context_builder.py, only
# this constant needs updating.
# (Defined after the AC1 positive test so its forward-reference at line 200
# resolves at function-call time, not definition time.)


def test_context_block_omits_innate_example_when_only_item_legacy_active():
    """Negative: when innate_v1 NOT in active_plugins, no innate-specific
    worked example surfaces (AC1, schema purity)."""
    config = _world_config_item_only()
    state = MagicState.from_config(config)
    state.add_character("kael")
    block = build_magic_context_block(magic_state=state, actor_id="kael")

    # No innate-only fields should leak into a non-innate world's prompt
    assert "consent_state" not in block, (
        "consent_state is innate_v1-specific — must not appear when innate_v1 is not active"
    )
    assert "involuntary" not in block.lower(), (
        "Reflexive-surfacing language is innate-only; must not appear in non-innate worlds"
    )


# ---------------------------------------------------------------------------
# AC2 — narrator CRITICAL MAGIC RULE is proactive on innate-active worlds
# ---------------------------------------------------------------------------


def test_narrator_output_only_contains_proactive_rule_phrases():
    """NARRATOR_OUTPUT_ONLY contains the 47-9 CRITICAL MAGIC RULE rewrite (AC2).

    Asserts every multi-word phrase in NARRATOR_PROACTIVE_RULE_PHRASES is
    present (case-insensitive). Each phrase is unique to the 47-9 rewrite
    (verified absent from origin/develop's narrator.py); together they pin
    the rule against future regressions where someone reverts to the
    reactive-only formulation. A generic single-word marker like "consider"
    would have been satisfied by the pre-existing prompt — the multi-word
    phrases are the actual signal.
    """
    haystack = NARRATOR_OUTPUT_ONLY.lower()
    missing = [p for p in NARRATOR_PROACTIVE_RULE_PHRASES if p.lower() not in haystack]
    assert not missing, (
        f"NARRATOR_OUTPUT_ONLY is missing 47-9 proactive-rule phrases: {missing}. "
        f"The CRITICAL MAGIC RULE rewrite must include each of: "
        f"{NARRATOR_PROACTIVE_RULE_PHRASES}."
    )


def test_narrator_output_only_documents_magic_working_field():
    """Sanity/regression: NARRATOR_OUTPUT_ONLY still documents magic_working
    and still contains a CRITICAL MAGIC RULE block — these have been required
    since pre-47-9 and must continue to hold."""
    assert "magic_working" in NARRATOR_OUTPUT_ONLY
    assert "CRITICAL MAGIC RULE" in NARRATOR_OUTPUT_ONLY


def test_narrator_output_only_contains_negative_case_phrases():
    """Playtest 2026-05-09 fix: the narrator's MUST-emit rules need an
    equally-strong MUST-NOT-emit counterweight, or it over-emits
    magic_working on non-working turns ("page has not warmed", passive
    listen-thread carryover, sensory observation). Pin every phrase in
    NARRATOR_NEGATIVE_CASE_PHRASES so future prompt edits don't silently
    strip the negative rule.
    """
    haystack = NARRATOR_OUTPUT_ONLY.lower()
    missing = [p for p in NARRATOR_NEGATIVE_CASE_PHRASES if p.lower() not in haystack]
    assert not missing, (
        f"NARRATOR_OUTPUT_ONLY is missing negative-case phrases: {missing}. "
        f"The CRITICAL MAGIC NEGATIVE CASE rule must include each of: "
        f"{NARRATOR_NEGATIVE_CASE_PHRASES}."
    )


async def test_narrator_prompt_includes_negative_case_phrases_on_innate_world():
    """The negative-case rule must survive prompt assembly (Full tier)
    on innate-active worlds — same wiring contract as the proactive rule."""
    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")

    canned = "**Galley**\n\nNothing.\n\n```game_patch\n{}\n```"
    orch = Orchestrator(client=_make_canned_client(canned))
    context = TurnContext(character_name="sira_mendes", magic_state=state)
    prompt, _ = await orch.build_narrator_prompt(
        "the airlock hisses open and a stranger steps in",
        context,
    )

    haystack = prompt.lower()
    missing = [p for p in NARRATOR_NEGATIVE_CASE_PHRASES if p.lower() not in haystack]
    assert not missing, (
        f"Assembled narrator prompt is missing negative-case phrases: "
        f"{missing}. The CRITICAL MAGIC NEGATIVE CASE rule must reach the "
        f"final prompt without redaction."
    )


async def test_narrator_prompt_includes_proactive_rule_phrases_on_innate_world():
    """Orchestrator.build_narrator_prompt produces a prompt containing the
    47-9 proactive-rule phrases when assembling for an innate-active world (AC2).

    This is the integration assertion: it confirms NARRATOR_OUTPUT_ONLY
    actually flows through ``Orchestrator.build_narrator_prompt`` to the
    final assembled prompt string, and that the proactive rule survives
    any redaction or tier-based trimming that happens at assembly time.
    """
    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")

    canned = "**Galley**\n\nNothing.\n\n```game_patch\n{}\n```"
    orch = Orchestrator(client=_make_canned_client(canned))
    context = TurnContext(character_name="sira_mendes", magic_state=state)
    prompt, _ = await orch.build_narrator_prompt(
        "the airlock hisses open and a stranger steps in",
        context,
    )

    haystack = prompt.lower()
    missing = [p for p in NARRATOR_PROACTIVE_RULE_PHRASES if p.lower() not in haystack]
    assert not missing, (
        f"Assembled narrator prompt is missing 47-9 proactive-rule phrases: "
        f"{missing}. The CRITICAL MAGIC RULE rewrite in NARRATOR_OUTPUT_ONLY "
        f"must reach the final assembled prompt without redaction."
    )


async def test_orchestrator_assembles_innate_worked_example_into_prompt():
    """Wiring test: ``Orchestrator.build_narrator_prompt`` invokes
    ``build_magic_context_block`` and surfaces the innate worked-example
    sentinel in the assembled prompt when innate_v1 is active.

    This is the integration pin per CLAUDE.md "Every Test Suite Needs a
    Wiring Test" — without it, the new context_builder block could pass its
    own unit test (AC1) while never reaching the production prompt assembly.
    """
    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")

    canned = "**Galley**\n\nNothing.\n\n```game_patch\n{}\n```"
    orch = Orchestrator(client=_make_canned_client(canned))
    context = TurnContext(character_name="sira_mendes", magic_state=state)
    prompt, _ = await orch.build_narrator_prompt(
        "the airlock hisses open and a stranger steps in",
        context,
    )

    assert CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL in prompt, (
        f"Wiring broken: innate worked-example sentinel "
        f"{CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL!r} did not reach the assembled "
        f"narrator prompt. context_builder.build_magic_context_block produced the "
        f"block in isolation (AC1 test), but Orchestrator.build_narrator_prompt is "
        f"not surfacing it."
    )


async def test_orchestrator_omits_innate_worked_example_when_innate_not_active():
    """Wiring test (negative): when innate_v1 is NOT in active_plugins, the
    worked-example sentinel must NOT appear in the assembled prompt (AC1+AC2
    plugin-conditional schema purity).

    Pairs with ``test_orchestrator_assembles_innate_worked_example_into_prompt``
    to lock plugin-conditionality at the orchestrator level.
    """
    config = _world_config_item_only()
    state = MagicState.from_config(config)
    state.add_character("kael")

    canned = "**Cave**\n\nNothing.\n\n```game_patch\n{}\n```"
    orch = Orchestrator(client=_make_canned_client(canned))
    context = TurnContext(character_name="kael", magic_state=state)
    prompt, _ = await orch.build_narrator_prompt("you take a step into the darkness", context)

    assert CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL not in prompt, (
        f"Schema-purity violation: innate worked-example sentinel "
        f"{CONTEXT_BUILDER_INNATE_EXAMPLE_SENTINEL!r} leaked into a non-innate "
        f"world's prompt. context_builder must gate the worked example on "
        f"'innate_v1' in config.active_plugins."
    )


# ---------------------------------------------------------------------------
# AC3 — coyote_star openings.yaml has scripted innate-firing opening
# ---------------------------------------------------------------------------


def _coyote_star_openings_path() -> Path | None:
    """Resolve openings.yaml from SIDEQUEST_GENRE_PACKS or sibling content repo.

    Returns ``None`` (rather than raising) when neither path resolves, so the
    caller can ``pytest.skip(...)`` instead of erroring. The
    ``sidequest-server`` test suite is documented as runnable standalone
    (``just server-test``) — a CI runner without a content checkout should
    skip the AC3 content-validation test, not fail it.
    """
    base = os.environ.get("SIDEQUEST_GENRE_PACKS")
    if base:
        candidate = Path(base) / "space_opera/worlds/coyote_star/openings.yaml"
        if candidate.is_file():
            return candidate
    # Fallback: sibling sidequest-content checkout (oq-1/oq-2 layout)
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = (
            ancestor / "sidequest-content/genre_packs/space_opera/worlds/coyote_star/openings.yaml"
        )
        if candidate.is_file():
            return candidate
    return None


def test_coyote_star_has_scripted_innate_firing_opening():
    """At least one Coyote Star opening must script an innate working on turn 1
    by setting magic_microbleed.cost_bar='sanity' (the innate cost bar) AND
    addressing the PC directly in the detail prose, AND not narrating internal
    perception (NARRATOR_AGENCY / SOUL.md "The Test") (AC3).
    """
    path = _coyote_star_openings_path()
    if path is None:
        pytest.skip(
            "Could not locate coyote_star/openings.yaml — set SIDEQUEST_GENRE_PACKS "
            "or run from a checkout with a sibling sidequest-content directory."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    openings = data.get("openings", [])
    assert openings, f"openings.yaml has no openings entries: {path}"

    sanity_anchored: list[dict[str, Any]] = []
    for op in openings:
        mb = op.get("magic_microbleed")
        if not mb:
            continue
        if mb.get("cost_bar") != "sanity":
            continue
        detail = (mb.get("detail") or "").lower()
        # PC-anchored: detail addresses the player situationally (second-person
        # pronouns referring to the PC's stance / immediate surroundings).
        # Internal-perception markers ("your mind", "your senses", "behind your
        # eyes") are deliberately excluded — narrating the PC's perception
        # violates NARRATOR_AGENCY (internal cognition belongs to the player).
        # An opening that anchors via reflex stimulus + situation pronouns is
        # SOUL-compliant; one that anchors via "you feel..." is not.
        pc_anchors = [
            "you ",  # second-person you (situational)
            "your ",  # second-person possessive (most uses are situational)
            "yourself",
        ]
        reflexive_markers = [
            "reflexive",
            "surface",
            "surges",
            "surge",
            "involuntary",
            "flinch",
            "shudder",
            "uncanny",
        ]
        pc_anchored = any(a in detail for a in pc_anchors)
        reflexive = any(m in detail for m in reflexive_markers)
        if pc_anchored and reflexive:
            sanity_anchored.append(op)

    assert sanity_anchored, (
        "Expected at least one Coyote Star opening with magic_microbleed.cost_bar='sanity' "
        "AND PC-anchored reflexive-surfacing detail (second-person + reflexive/surface/"
        "involuntary language). None found in:\n  "
        + "\n  ".join(
            f"{op.get('id', '?')}: cost_bar={op.get('magic_microbleed', {}).get('cost_bar')}"
            for op in openings
        )
    )

    # NARRATOR_AGENCY / SOUL.md "The Test" enforcement: even when an opening
    # passes the sanity-cost + PC-anchored check above, it MUST NOT narrate
    # internal perception, thought, or felt experience. The PC's player owns
    # those; the narrator may only describe external stimulus and immediate
    # physical reflex follow-through. A future opening with "your senses
    # detect the pressure" + "uncanny" would otherwise satisfy the
    # pc_anchored + reflexive checks while violating SOUL.
    perception_violation_patterns = [
        "your mind",
        "your senses",
        "your perception",
        "behind your eyes",
        "you feel",
        "you sense",
        "you perceive",
        "you think",
        "yourself pull",  # "you feel yourself pull back" pattern
        "yourself recoil",
    ]
    for op in sanity_anchored:
        detail = (op.get("magic_microbleed", {}).get("detail") or "").lower()
        leaks = [p for p in perception_violation_patterns if p in detail]
        assert not leaks, (
            f"Opening {op.get('id', '?')!r} narrates the PC's internal "
            f"perception (NARRATOR_AGENCY violation) via patterns {leaks}. "
            f"The narrator may describe external stimulus and immediate "
            f"physical reflex but NOT what the PC perceives, senses, feels, "
            f"or thinks. Internal cognition belongs to the player's next turn."
        )


# ---------------------------------------------------------------------------
# AC4 + AC5 — apply pipeline + dispatch seam: a worked-example-shape
# magic_working dict produces the magic.working span, appends to working_log,
# and debits the sanity bar. Two tests below cover the apply call directly,
# and the extract → apply seam, respectively. Neither claims end-to-end on
# its own; together with the AC2 wiring tests above they triangulate the
# full path. See module docstring for the test-triangle rationale.
# ---------------------------------------------------------------------------


async def test_apply_pipeline_emits_span_and_debits_sanity_bar(
    captured_watcher_events: list[dict[str, Any]],
):
    """Apply-pipeline contract: when ``apply_magic_working`` is called with a
    magic_working dict matching the worked-example shape, it produces a
    magic.working span (AC4), appends a working_log entry (AC5), and debits
    the sanity bar below its chargen value of 1.0 (AC5).

    Scope: this test pins ``apply_magic_working`` only — it does NOT exercise
    the narrator → extraction → dispatch → apply chain. The dispatch seam is
    pinned separately by
    ``test_dispatch_seam_extracts_magic_working_and_fires_span``. The prompt
    side (worked example reaches the assembled prompt) is pinned by AC2 wiring
    tests above. This three-test triangle pins the full path without any one
    test overclaiming "end-to-end."
    """
    from sidequest.game.session import GameSnapshot
    from sidequest.server.narration_apply import apply_magic_working

    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")
    snapshot = GameSnapshot.model_construct(magic_state=state)

    # The shape the strengthened context-block worked example teaches Claude
    # to emit. If the worked example's plugin/mechanism/consent_state JSON
    # shape diverges from this dict, AC1 also fails. The flavor value is NOT
    # pinned by AC1 (the worked example uses a placeholder per the
    # forbidden_hardcoded_flavors check); a concrete value is fine here
    # because we're testing apply-pipeline mechanics, not prompt content.
    magic_working = {
        "plugin": "innate_v1",
        "mechanism": "condition",
        "actor": "sira_mendes",
        "costs": {"sanity": 0.15},
        "domain": "psychic",
        "narrator_basis": "reflexive recoil from uncanny presence",
        "flavor": "acquired",
        "consent_state": "involuntary",
    }
    result = apply_magic_working(snapshot=snapshot, patch_field=magic_working)

    # AC4: magic.working span emitted via watcher route
    matching_events = [
        e
        for e in captured_watcher_events
        if e["component"] == "magic"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == "working"
    ]
    assert len(matching_events) == 1, (
        f"Expected exactly one magic.working span; got {len(matching_events)}. "
        f"All captured: {captured_watcher_events}"
    )
    fields = matching_events[0]["fields"]
    assert fields["plugin"] == "innate_v1"
    assert fields["actor"] == "sira_mendes"
    # ledger_after must include the post-debit sanity value (< 1.0)
    assert "sanity" in fields["ledger_after"]
    assert fields["ledger_after"]["sanity"] < 1.0

    # AC5: working_log appended
    assert len(snapshot.magic_state.working_log) == 1
    log_entry = snapshot.magic_state.working_log[0]
    assert log_entry.plugin == "innate_v1"
    assert log_entry.actor == "sira_mendes"

    # AC5: sanity bar debited below 1.0 (chargen default)
    sanity = snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    )
    assert sanity.value < 1.0, (
        f"sanity bar must debit below chargen value of 1.0; got {sanity.value}"
    )
    assert sanity.value == pytest.approx(0.85), (
        f"sanity should be 1.0 - 0.15 = 0.85; got {sanity.value}"
    )

    # No DEEP_RED flags on a clean firing
    deep_red = [f for f in result.flags if f.severity.value == "deep_red"]
    assert deep_red == [], f"Clean firing must not raise DEEP_RED flags; got {deep_red}"


def test_dispatch_seam_extracts_magic_working_and_fires_span(
    captured_watcher_events: list[dict[str, Any]],
):
    """Dispatch-seam wiring (AC4 supplement): a raw narrator response string
    containing a ``magic_working`` JSON block flows through
    ``extract_structured_from_response`` → ``apply_magic_working`` and
    produces the watcher span + ledger debit.

    This pins the seam where the historic zero-firing failure could live: the
    apply pipeline is intact (covered by ``test_apply_pipeline_emits_span_*``)
    and the prompt assembly is intact (covered by AC2 wiring tests), but
    nothing else in the test suite verifies the extraction step actually
    forwards a ``magic_working`` field from the narrator's raw response into
    the apply call. Without this test, a regression in
    ``extract_structured_from_response`` (e.g., dropping the
    ``magic_working`` key from its return dict) would silently revert the
    fix while AC1/AC2/AC4 isolated tests stay green.
    """
    from sidequest.agents.orchestrator import extract_structured_from_response
    from sidequest.game.session import GameSnapshot
    from sidequest.server.narration_apply import apply_magic_working

    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")
    snapshot = GameSnapshot.model_construct(magic_state=state)

    # Synthesised raw narrator response — the shape the strengthened prompt
    # is meant to elicit. Note: prose content is illustrative; only the
    # ``game_patch`` block's ``magic_working`` field affects the dispatch.
    raw_response = (
        "**Galley**\n\n"
        "The air shifts. Her hand tightens on the mug, involuntary.\n\n"
        "```game_patch\n"
        '{"magic_working": {'
        '"plugin": "innate_v1", '
        '"mechanism": "condition", '
        '"actor": "sira_mendes", '
        '"costs": {"sanity": 0.12}, '
        '"domain": "psychic", '
        '"narrator_basis": "reflexive recoil from uncanny presence", '
        '"flavor": "acquired", '
        '"consent_state": "involuntary"'
        "}}\n"
        "```"
    )

    # Step 1: extraction must surface magic_working as a dict (not None,
    # not stripped, not silently swallowed).
    extraction = extract_structured_from_response(raw_response)
    assert isinstance(extraction.get("magic_working"), dict), (
        f"extract_structured_from_response must return magic_working as a dict "
        f"when the game_patch block contains one. Got: "
        f"{extraction.get('magic_working')!r}. Full extraction keys: "
        f"{sorted(extraction.keys())}."
    )

    # Step 2: apply must consume the extracted dict and emit the span +
    # debit the bar. This is the same path orchestrator.run_narration_turn
    # uses: extraction["magic_working"] → apply_magic_working(patch_field=...).
    apply_magic_working(snapshot=snapshot, patch_field=extraction["magic_working"])

    matching = [
        e
        for e in captured_watcher_events
        if e["component"] == "magic"
        and e["event_type"] == "state_transition"
        and e["fields"].get("op") == "working"
    ]
    assert len(matching) == 1, (
        f"Dispatch seam broken: expected exactly one magic.working span after "
        f"extract→apply; got {len(matching)}. Captured: {captured_watcher_events}"
    )
    fields = matching[0]["fields"]
    assert fields["plugin"] == "innate_v1"
    assert fields["actor"] == "sira_mendes"

    sanity = snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    )
    assert sanity.value == pytest.approx(0.88), (
        f"Dispatch seam broken: sanity should be 1.0 - 0.12 = 0.88 after "
        f"extract→apply; got {sanity.value}"
    )


# ---------------------------------------------------------------------------
# AC7 — save/load roundtrip preserves working_log + ledger after firing
# ---------------------------------------------------------------------------


def test_save_load_roundtrip_preserves_working_log_and_sanity():
    """After apply_magic_working, the snapshot serializes and deserializes via
    SqliteStore with working_log entries and sanity bar value preserved (AC7,
    regression-protection — this contract must continue to hold post-47-9)."""
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.server.narration_apply import apply_magic_working

    config = _world_config_innate_active()
    state = MagicState.from_config(config)
    state.add_character("sira_mendes")
    snapshot = GameSnapshot(
        genre_slug="space_opera",
        world_slug="coyote_star",
        magic_state=state,
    )

    apply_magic_working(
        snapshot=snapshot,
        patch_field={
            "plugin": "innate_v1",
            "mechanism": "condition",
            "actor": "sira_mendes",
            "costs": {"sanity": 0.18},
            "domain": "psychic",
            "narrator_basis": "uncanny resonance washes through her",
            "flavor": "acquired",
            "consent_state": "involuntary",
        },
    )

    pre_log_len = len(snapshot.magic_state.working_log)
    pre_sanity = snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    ).value
    assert pre_log_len == 1
    assert pre_sanity == pytest.approx(0.82)

    # Roundtrip via the same code path production uses (in-memory store mirrors
    # the SQLite file path; only the sqlite3 connection differs).
    store = SqliteStore.open_in_memory()
    store.init_session("space_opera", "coyote_star")
    store.save(snapshot)
    saved = store.load()

    assert saved is not None, "SqliteStore.load() must rehydrate the saved session"
    assert saved.snapshot.magic_state is not None
    assert len(saved.snapshot.magic_state.working_log) == pre_log_len
    loaded_log_entry = saved.snapshot.magic_state.working_log[0]
    assert loaded_log_entry.plugin == "innate_v1"
    assert loaded_log_entry.actor == "sira_mendes"

    loaded_sanity = saved.snapshot.magic_state.get_bar(
        BarKey(scope="character", owner_id="sira_mendes", bar_id="sanity")
    ).value
    assert loaded_sanity == pytest.approx(pre_sanity), (
        f"sanity bar must roundtrip; pre={pre_sanity}, post={loaded_sanity}"
    )
