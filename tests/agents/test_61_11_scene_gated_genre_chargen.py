"""Story 61-11 — ``genre_chargen`` is scene-gated, not unconditional.

Story 57-3 / ADR-112 promoted four genre-prose sections into
``STABLE_SECTION_NAMES`` so the cache root would carry them and survive
prefix re-computation. ``genre_chargen`` was one of them, registered
unconditionally on every turn at the chargen block in
``Orchestrator.build_narrator_prompt`` (today: any turn with
``context.genre_prompts`` and a non-empty ``gp.chargen`` pulls the prose
in).

Story 61-11 (ADR-112 amendment): the chargen prose is only relevant on
the post-chargen opening turn — the turn where the narrator describes
the character's loadout and the world they're stepping into. The
existing ``TurnContext.opening_directive`` field (set by
``_populate_opening_directive_on_chargen_complete`` at the
chargen-confirmation site and cleared after the opening turn) is the
runtime predicate. Carrying chargen prose on every neutral turn (e.g.
"you walk into the tavern" three rounds later) wasted ~150 tokens of
cache budget on out-of-scope flavor.

This test file exercises the wiring end-to-end through
``Orchestrator.build_narrator_prompt`` + ``registry.compose_split`` so
that the bucket assignment AND the predicate gating fire together.
A bucket-only unit test (``test_bucket.py``) catches the demotion but
not the gating; a gating-only orchestrator test would miss the bucket
half. Both halves are load-bearing — the per-turn carry only
disappears if BOTH the section is in the User bucket (so it's not
glued onto the cached System prefix) AND the registration block skips
when the predicate is false.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.genre.models.narrative import Prompts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A distinctive chargen prose string. Must be unique enough that grep'ing
# the composed prompt for it cannot collide with any other section's
# content (the unique marker tokens defeat false-positive matches on
# substrings like "loadout" that might appear elsewhere).
_CHARGEN_PROSE = (
    "STORY-61-11-CHARGEN-FIXTURE: Brecca Half-Hand stamps your seven-delve token. "
    "She does not remember your name. The dungeon is waiting."
)

# Minimal Prompts fixture — required fields plus the chargen prose under test.
# The four required fields (narrator, combat, npc, world_state) get distinct
# sentinel strings so a misrouted assertion (e.g. checking for chargen content
# but finding combat content) fails loudly with the wrong-section name in the
# diff.
_FIXTURE_PROMPTS = Prompts(
    narrator="STORY-61-11-NARRATOR-FIXTURE: standard narrator voice for the test.",
    combat="STORY-61-11-COMBAT-FIXTURE: combat voice for the test.",
    npc="STORY-61-11-NPC-FIXTURE: NPC behavior for the test.",
    world_state="STORY-61-11-WORLD-STATE-FIXTURE: world tracking for the test.",
    chargen=_CHARGEN_PROSE,
)


def _make_canned_client() -> ClaudeClient:
    """ClaudeClient whose subprocess always returns a minimal canned response.

    The narrator LLM is not exercised by these tests — they only assemble
    the prompt and inspect the registry. A canned client keeps Orchestrator
    construction working without hitting a real backend.
    """

    async def spawn_fn(command: str, *args: str, env: Any = None, **kwargs: Any):
        class FakeProcess:
            returncode = 0

            async def communicate(self):
                payload = {
                    "result": "**Stub**\n\nStub narration.\n\n```game_patch\n{}\n```",
                    "session_id": "test-session-61-11",
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                return json.dumps(payload).encode(), b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        return FakeProcess()

    return ClaudeClient(spawn_fn=spawn_fn)


def _make_opening_turn_context() -> TurnContext:
    """Predicate-TRUE fixture: post-chargen opening turn.

    ``opening_directive`` is non-None (the chargen-confirmation handler
    populated it; the opening-turn dispatch site has not yet cleared
    it). ``turn_number == 0`` matches the opening-turn invariant. The
    chargen prose SHOULD land in the prompt on this turn.
    """
    return TurnContext(
        character_name="Rux",
        genre="caverns_and_claudes",
        genre_prompts=_FIXTURE_PROMPTS,
        opening_directive="ESTABLISHING: torchlight, dripping water, the descent begins.",
        turn_number=0,
    )


def _make_neutral_turn_context() -> TurnContext:
    """Predicate-FALSE fixture: any post-opening turn.

    ``opening_directive`` is None (cleared after the opening turn
    fired). ``turn_number > 0``. The chargen prose SHOULD be absent
    from the prompt on this turn — that's the per-turn carry the
    story is eliminating.
    """
    return TurnContext(
        character_name="Rux",
        genre="caverns_and_claudes",
        genre_prompts=_FIXTURE_PROMPTS,
        opening_directive=None,
        turn_number=5,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_genre_chargen_lands_in_user_message_on_opening_turn():
    """Predicate true: post-chargen opening turn → chargen prose lands in user_message.

    Verifies the demotion + gating together: the section is registered
    (gate passes because ``opening_directive`` is non-None) AND lands in
    the User bucket (because ``genre_chargen`` was demoted from
    ``STABLE_SECTION_NAMES``). Inspecting ``user_text`` rather than
    ``system_text`` is the load-bearing assertion — pre-demotion the
    prose landed in ``system_text``, which would have made the cached
    prefix carry it on every turn.
    """
    orch = Orchestrator(client=_make_canned_client())
    context = _make_opening_turn_context()

    _prompt, registry = await orch.build_narrator_prompt("look around", context)
    system_text, user_text = registry.compose_split("narrator")

    assert _CHARGEN_PROSE in user_text, (
        "genre_chargen prose missing from user_message on the opening turn. "
        "Story 61-11 expects the registration block at orchestrator.py "
        "(chargen section, ~line 1595) to fire when "
        "context.opening_directive is not None, and the demoted section "
        "to land in the User bucket via compose_split. "
        f"user_text preview: {user_text[:200]!r}"
    )
    assert _CHARGEN_PROSE not in system_text, (
        "genre_chargen prose appeared in system_prompt — Story 61-11 "
        "demoted the section out of STABLE_SECTION_NAMES, so it must "
        "route to the User bucket (uncached) not the System bucket "
        "(cached prefix). Re-check STABLE_SECTION_NAMES membership. "
        f"system_text preview: {system_text[:200]!r}"
    )


async def test_genre_chargen_absent_from_prompt_on_neutral_turn():
    """Predicate false: post-opening turn → chargen prose absent from BOTH buckets.

    This is the per-turn savings the story delivers. Pre-fix, the
    section was unconditionally registered (STABLE, always in
    ``system_text``) and the cache prefix carried ~150 tokens of
    out-of-scope chargen flavor on every "you walk into the tavern"
    turn. Post-fix, the gate skips registration entirely when
    ``opening_directive is None``, so neither bucket receives the
    content.

    The negative-on-both-buckets assertion is the wiring lie-detector:
    if the section is registered but routed to System (gate fired,
    bucket misclassified), it would appear in ``system_text``; if
    routed to User but registered when it shouldn't be (bucket right,
    gate broken), it would appear in ``user_text``. Asserting
    absence on BOTH catches both failure modes.
    """
    orch = Orchestrator(client=_make_canned_client())
    context = _make_neutral_turn_context()

    _prompt, registry = await orch.build_narrator_prompt(
        "you walk into the tavern", context
    )
    system_text, user_text = registry.compose_split("narrator")

    assert _CHARGEN_PROSE not in system_text, (
        "genre_chargen prose appeared in system_prompt on a neutral "
        "(non-opening) turn. Story 61-11 expects the registration "
        "block at orchestrator.py (chargen section, ~line 1595) to "
        "skip when context.opening_directive is None. If the section "
        "is still in STABLE_SECTION_NAMES, the bucket test catches "
        "that; this test catches the gating half independently. "
        f"system_text preview: {system_text[:200]!r}"
    )
    assert _CHARGEN_PROSE not in user_text, (
        "genre_chargen prose appeared in user_message on a neutral "
        "(non-opening) turn. The predicate gate must skip registration "
        "entirely when context.opening_directive is None — not register "
        "into the User bucket. "
        f"user_text preview: {user_text[:200]!r}"
    )


@pytest.mark.parametrize(
    "section_marker, prose, fixture",
    [
        ("genre_extraction", "STORY-57-3-EXTRACTION-FIXTURE: party hauls treasure.", "extraction"),
        ("genre_keeper_monologue", "STORY-57-3-KEEPER-FIXTURE: the walls speak.", "keeper_monologue"),
        ("genre_town", "STORY-57-3-TOWN-FIXTURE: surface waystation.", "town"),
    ],
)
async def test_unaffected_stable_sections_still_carry_unconditionally(
    section_marker: str, prose: str, fixture: str
) -> None:
    """Regression guard: Story 61-11 must NOT touch the other three sections.

    ``genre_extraction``, ``genre_keeper_monologue``, and ``genre_town``
    remain on STABLE_SECTION_NAMES per the predicate audit (no runtime
    signal exists for extraction/keeper; town deferred for profiling).
    They must still register unconditionally on every turn and land in
    the system_prompt (cached prefix).

    This test runs the neutral-turn fixture (``opening_directive=None``)
    and asserts each section still lands in ``system_text``. If
    61-11's diff accidentally gated one of these on opening_directive
    too (e.g. by a copy-paste error in the registration block), this
    test fails immediately.
    """
    # Build a Prompts fixture with the one section we're checking populated.
    prompts_kwargs: dict[str, str] = {
        "narrator": "STORY-61-11-NARRATOR-FIXTURE: standard narrator voice.",
        "combat": "STORY-61-11-COMBAT-FIXTURE: combat voice.",
        "npc": "STORY-61-11-NPC-FIXTURE: NPC behavior.",
        "world_state": "STORY-61-11-WORLD-STATE-FIXTURE: world tracking.",
        fixture: prose,
    }
    prompts = Prompts(**prompts_kwargs)
    context = TurnContext(
        character_name="Rux",
        genre="caverns_and_claudes",
        genre_prompts=prompts,
        opening_directive=None,  # neutral turn — chargen would skip here
        turn_number=5,
    )

    orch = Orchestrator(client=_make_canned_client())
    _prompt, registry = await orch.build_narrator_prompt("look around", context)
    system_text, user_text = registry.compose_split("narrator")

    assert prose in system_text, (
        f"{section_marker} prose missing from system_prompt on a "
        "neutral turn. Story 61-11 must NOT touch the registration "
        f"block for {section_marker} — only the chargen block was "
        "in scope. Check that the predicate-gate edit at the chargen "
        "registration block did not bleed into adjacent blocks via "
        "copy-paste. "
        f"system_text preview: {system_text[:200]!r}"
    )
    assert prose not in user_text, (
        f"{section_marker} prose appeared in user_message — this "
        "section is in STABLE_SECTION_NAMES (per the test_bucket.py "
        "pins) so compose_split must route it to the System bucket. "
        f"user_text preview: {user_text[:200]!r}"
    )
