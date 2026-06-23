"""Story 151-1 — Cache-promote the narrator output contract.

ADR-150 §Companion quick-win / Alternative A. Promote ``narrator_output_only``
(the ~15k-codepoint, byte-static game_patch output-format contract loaded from
``narrator_prompts/output_only.md``) from the default User bucket into
``STABLE_SECTION_NAMES`` so it routes to the System bucket and rides the stable,
CLI-cached ``system_prompt`` prefix instead of being re-sent in full on every
per-turn user message.

CURRENT STATE (verified 2026-06-18, this branch)
  - ``narrator_output_only`` is registered in ``AttentionZone.Primacy`` by
    ``NarratorAgent.build_output_format`` (narrator.py:290) and is NOT in
    ``STABLE_SECTION_NAMES`` -> default User bucket -> it lands in the per-turn
    user ``Message`` (uncached, paid ~in full every turn).
  - The section content is the static module constant ``NARRATOR_OUTPUT_ONLY``
    (= ``_load("output_only.md")``) wrapped in ``<critical>`` tags — there is
    NO runtime interpolation, so it is byte-identical for the life of a
    session. That byte-stability is the precondition that makes promotion safe.

POST-119-3 CACHE MECHANISM (the spec's named landmine is stale — see the
session file's Delivery Findings):
  - Story 119-3 (f970091e) ported the narrator onto claude-agent-sdk. It
    DELETED the spec's named landmine ``test_60_6_stable_prefix_live_drift.py``
    (there is nothing to "update") and ``test_cache_ttl_prefix_and_otel.py``,
    and it collapsed the per-block ``cache=True`` markers: the SDK client now
    flattens every ``system_blocks`` entry into ONE plain ``system_prompt``
    string (anthropic_sdk_client.py:434) and the ``claude`` CLI owns caching.
  - The promotion still delivers the win on a different axis. System-bucket
    content -> the stable ``system_prompt`` (CLI-cached across turns);
    User-bucket content -> the volatile per-turn user message (never
    cross-turn cached). Moving ``narrator_output_only`` System-ward relocates
    ~3,800 tokens off the volatile message onto the cached prefix.
  - The wire-payload placement asserted below is the unit-level proxy for
    "promoted into the cached prefix". Real cache read/write DELTA validation
    (the ``narration.turn.cached_input_*`` spans) needs a LIVE call across two
    turns -> that is a playtest check, not a fixture assertion.

Wiring discipline (server CLAUDE.md "No Source-Text Wiring Tests"): the
behavioral tests drive a real turn through the production
``Orchestrator.run_narration_turn`` SDK path and inspect the captured wire
payload's ``system_blocks`` / ``messages``. No test greps source.
"""

from __future__ import annotations

import pytest

# Wires the tool adapters onto default_registry, matching the SDK path.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.prompt_framework.bucket import (
    STABLE_SECTION_NAMES,
    SectionBucket,
    default_bucket_for_section,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

SECTION = "narrator_output_only"

# Unique, stable opening sentinel of output_only.md (verified single
# occurrence in the file). Used to LOCATE the section's content inside the
# assembled wire payload — a behavior probe on the outbound request, not a
# source grep.
OUTPUT_ONLY_MARKER = "You are running with NATIVE TOOLS."


def _end_turn() -> ScriptedResponse:
    return ScriptedResponse(
        text='{"narration":"ok"}',
        stop_reason="end_turn",
        input_tokens=120,
        output_tokens=18,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
    )


def _output_only_section_content() -> str:
    """Build the ``narrator_output_only`` section the way production does and
    return its assembled content. ``build_output_format`` takes only the
    registry — it has no turn-context parameter, so by construction it cannot
    interpolate per-turn state."""
    agent = NarratorAgent()
    registry = PromptRegistry()
    agent.build_output_format(registry)
    return next(s for s in registry.registry(agent.name()) if s.name == SECTION).content


# ---------------------------------------------------------------------------
# AC1 — allowlist membership + bucket resolution (deterministic, no I/O)
# ---------------------------------------------------------------------------


def test_narrator_output_only_in_stable_section_names() -> None:
    """AC1. ``narrator_output_only`` is on the STABLE_SECTION_NAMES allowlist.

    Pre-fix it is absent (default User bucket); the promotion adds it.
    """
    assert SECTION in STABLE_SECTION_NAMES, (
        f"{SECTION!r} must be in STABLE_SECTION_NAMES so it routes to the "
        f"System bucket (the cached system_prompt prefix). Add it in "
        f"prompt_framework/bucket.py. Current allowlist: "
        f"{sorted(STABLE_SECTION_NAMES)}"
    )


def test_narrator_output_only_resolves_to_system_bucket() -> None:
    """AC1. With the name on the allowlist, the bucket resolver routes it
    System-ward — the half that lands in the stable, CLI-cached prefix."""
    assert default_bucket_for_section(SECTION) == SectionBucket.System, (
        f"{SECTION!r} must resolve to SectionBucket.System; got "
        f"{default_bucket_for_section(SECTION)!r}. This is the User->System "
        f"flip that moves the ~15k-codepoint output contract off the per-turn "
        f"user message and onto the cached system_prompt prefix."
    )


# ---------------------------------------------------------------------------
# AC2 — byte-stability precondition (the safety guard that justifies promotion)
# ---------------------------------------------------------------------------


def test_narrator_output_only_section_carries_no_per_turn_interpolation() -> None:
    """AC2 (precondition guard). The section content is the static
    ``NARRATOR_OUTPUT_ONLY`` constant plus only a tiny wrapper — no per-turn
    data is interpolated. This is WHAT MAKES the promotion safe: a section that
    drifted turn-to-turn would thrash the cache root.

    Green today (the section is a static .md load wrapped in ``<critical>``
    tags) and MUST stay green. If a later change threads per-turn state into
    this section, REVERT the promotion — do not relax this guard.
    """
    content = _output_only_section_content()

    # Two independent builds are identical (no nondeterminism in assembly).
    assert content == _output_only_section_content(), (
        "narrator_output_only section content differs across two independent "
        "builds — it carries nondeterministic/per-turn drift and is NOT safe "
        "to cache-promote."
    )
    # The whole static constant rides verbatim, and the section is not
    # materially larger than the constant: only a small static wrapper is
    # added, never a large per-turn payload.
    assert NARRATOR_OUTPUT_ONLY in content
    assert len(content) - len(NARRATOR_OUTPUT_ONLY) < 64, (
        f"narrator_output_only section is {len(content) - len(NARRATOR_OUTPUT_ONLY)} "
        f"codepoints larger than the static NARRATOR_OUTPUT_ONLY constant — "
        f"that extra content would have to be re-derived per turn, breaking "
        f"the byte-stability precondition for cache promotion."
    )


# ---------------------------------------------------------------------------
# AC3 — wire-payload placement (the behavioral RED; Story 61-20 pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_contract_rides_cached_system_prefix(simple_turn_context) -> None:
    """AC3 (promotion). Driven through the production SDK path, the output
    contract MUST land in the cache-marked ``system_blocks[0]`` prefix.

    Pre-fix (User bucket) it rides the per-turn user Message; post-fix
    (System bucket + the already-present Primacy zone) it rides ``stable_text``
    -> ``system_blocks[0]``.
    """
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", simple_turn_context)

    assert len(fake.recorded_requests) == 1
    request = fake.recorded_requests[0]
    cached = request.system_blocks[0].text
    volatile = "\n".join(b.text for b in request.system_blocks[1:])
    user_msg = "\n".join(
        m.content for m in request.messages if m.role == "user" and isinstance(m.content, str)
    )

    assert OUTPUT_ONLY_MARKER in cached, (
        f"The narrator output contract must ride the cache-marked "
        f"system_blocks[0] prefix so it is sent once and read every turn. "
        f"Found in volatile system_blocks[1:]: {OUTPUT_ONLY_MARKER in volatile}; "
        f"found in per-turn user message: {OUTPUT_ONLY_MARKER in user_msg}. "
        f"Promote 'narrator_output_only' to STABLE_SECTION_NAMES (it is already "
        f"Primacy-zoned, so System-bucket content rides stable_text -> block 0)."
    )


@pytest.mark.asyncio
async def test_output_contract_absent_from_per_turn_user_message(
    simple_turn_context,
) -> None:
    """AC3 (promotion). The output contract must NOT ride the per-turn user
    Message — that is the uncached path the promotion removes it from.

    Pre-fix it IS on the user message (default User bucket); this asserts the
    inverse and is RED until the promotion lands.
    """
    fake = FakeAnthropicSdkClient(responses=[_end_turn()])
    from sidequest.agents.orchestrator import Orchestrator

    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", simple_turn_context)

    request = fake.recorded_requests[0]
    user_msg = "\n".join(
        m.content for m in request.messages if m.role == "user" and isinstance(m.content, str)
    )
    assert OUTPUT_ONLY_MARKER not in user_msg, (
        "The narrator output contract is still on the per-turn user message "
        "(uncached, paid ~in full every turn). Promoting 'narrator_output_only' "
        "to the System bucket moves it onto the stable cached system_prompt "
        "prefix."
    )
