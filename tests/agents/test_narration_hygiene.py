"""Narration hygiene — strip leaked meta-cognitive preambles from player prose.

Playtest 2026-06-20 (sq-playtest-pingpong) [BUG / WWN-COMBAT-NARRATOR-LEAK]:
on barsoom/WWN turn 2, the narrator's mechanics->prose transition leaked
verbatim into the rendered NarrationCard:

    "The blade finds its mark - 6 damage, a clean hit. Now I narrate.

     Kantos drives the iron home, ..."

The leading scratchpad ("6 damage, a clean hit") and the meta-cognitive
transition ("Now I narrate.") are out-of-fiction machinery the player must
never see. ``scrub_meta_preamble`` strips that leading preamble — anchored on
the narrator's self-referential reference to the act of narrating — and emits a
``narrator.meta_preamble_stripped`` OTEL span so the GM panel can see the
hygiene pass fire (the lie-detector for narration leaks).

Sibling of ``agents/narrator_directives.py`` (prompt-side PREVENTION of the
2026-06-19 ``must_not_narrate`` leak) — this module is the post-narration STRIP
safety net the board asked for ("the meta-text leak itself is a distinct
narration-hygiene bug and should be stripped regardless").
"""

from __future__ import annotations

import json

import pytest

from sidequest.agents.narration_hygiene import (
    META_PREAMBLE_STRIPPED_SPAN,
    MetaPreambleScrub,
    scrub_meta_preamble,
)

# The exact barsoom/WWN repro from the ping-pong board.
_BARSOOM_LEAK = (
    "The blade finds its mark - 6 damage, a clean hit. Now I narrate.\n\n"
    "Kantos drives the iron home and the prince staggers back against the "
    "mooring-rail, breath gone ragged."
)
_BARSOOM_REAL = (
    "Kantos drives the iron home and the prince staggers back against the "
    "mooring-rail, breath gone ragged."
)


def test_strips_barsoom_meta_narration_preamble():
    scrub = scrub_meta_preamble(_BARSOOM_LEAK)
    assert isinstance(scrub, MetaPreambleScrub)
    assert scrub.stripped is True
    # The real narration survives intact.
    assert scrub.cleaned == _BARSOOM_REAL
    # The meta-transition and the raw damage scratchpad are gone from prose.
    assert "Now I narrate" not in scrub.cleaned
    assert "6 damage" not in scrub.cleaned
    # ...and preserved on the fragment for GM-panel forensics.
    assert "Now I narrate" in scrub.fragment
    assert "6 damage" in scrub.fragment


def test_clean_combat_prose_is_untouched():
    prose = (
        "The blade catches the prince's shoulder and he reels, snarling, blood "
        "bright on the deck-iron. He brings his own sword up between you."
    )
    scrub = scrub_meta_preamble(prose)
    assert scrub.stripped is False
    assert scrub.cleaned == prose
    assert scrub.fragment == ""


def test_strips_i_cannot_narrate_breakframe():
    # The 2026-06-19 break-frame leak class: the narrator explains its own
    # constraint to the player instead of obeying it in-fiction.
    leak = (
        "I cannot narrate an envelope that does not exist in this scene.\n\n"
        "Your hand closes on empty desk; there is no letter here."
    )
    scrub = scrub_meta_preamble(leak)
    assert scrub.stripped is True
    assert scrub.cleaned == "Your hand closes on empty desk; there is no letter here."


def test_does_not_strip_marker_deep_in_prose():
    # A long, legitimate narration whose meta-marker sits far past the
    # leading-preamble window must NOT be truncated — the leak is always a
    # LEADING preamble, never buried mid-scene. Stripping from index 0 to a
    # deep marker would nuke real prose, so the window guard suppresses it.
    body = "The market roars around you, a hundred haggling voices. " * 10
    assert len(body) > 400  # past the leading window
    prose = body + "Now I narrate the final bargain."
    scrub = scrub_meta_preamble(prose)
    assert scrub.stripped is False
    assert scrub.cleaned == prose


def test_failsafe_does_not_blank_narration_when_only_preamble():
    # Degenerate: the entire content is the meta-preamble with no real prose
    # after it. Never emit a blank card — keep the original rather than blank.
    only_preamble = "6 damage, a clean hit. Now I narrate."
    scrub = scrub_meta_preamble(only_preamble)
    assert scrub.stripped is False
    assert scrub.cleaned == only_preamble


def test_empty_prose_is_noop():
    scrub = scrub_meta_preamble("")
    assert scrub.stripped is False
    assert scrub.cleaned == ""


def test_emits_otel_span_on_strip(otel_capture):
    scrub_meta_preamble(_BARSOOM_LEAK)
    spans = [s for s in otel_capture.get_finished_spans() if s.name == META_PREAMBLE_STRIPPED_SPAN]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["stripped"] is True
    assert attrs["marker_found"] is True
    assert "Now I narrate" in attrs["fragment"]


def test_emits_otel_span_steady_state_clean(otel_capture):
    # The span fires every turn (like narrator.canonical_leak_audit) so the GM
    # panel sees the pass ran; stripped=False is the steady state.
    scrub_meta_preamble("A clean line of genre-true prose, nothing leaked.")
    spans = [s for s in otel_capture.get_finished_spans() if s.name == META_PREAMBLE_STRIPPED_SPAN]
    assert len(spans) == 1
    assert spans[0].attributes["stripped"] is False
    assert spans[0].attributes["marker_found"] is False


# ---------------------------------------------------------------------------
# Wiring: the strip must run inside the REAL narration-result assembler so the
# leaked preamble never reaches the player. Drive run_narration_turn through the
# SDK path (the shared _presentation_and_untooled_fields seam) and assert the
# emitted ``narration`` is scrubbed + the span fired. Mirrors the SDK driver in
# test_narrator_sdk_hybrid_split.py.
# ---------------------------------------------------------------------------


async def _run_sdk_turn_with_prose(monkeypatch: pytest.MonkeyPatch, prose: str):
    import sidequest.agents.tools  # noqa: F401  -- wires the 26 tool adapters
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from tests.agents.fakes.fake_anthropic_sdk_client import (
        FakeAnthropicSdkClient,
        ScriptedResponse,
    )

    sidecar_text = f"{prose}\n\n```game_patch\n{json.dumps({})}\n```\n"
    client = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text=sidecar_text,
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=48,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    orch = Orchestrator(client=client)

    class _FakeRegistry:
        def compose_split(self, agent_name: str) -> tuple[str, str]:
            return ("system text", "user text")

        def compose_split_by_zone(self, agent_name: str):
            from sidequest.agents.prompt_framework.types import AttentionZone

            return ({AttentionZone.Primacy: "system text"}, "user text")

        def registry(self, agent_name: str) -> list:
            return []

    async def _fake_build_prompt(self: Orchestrator, action: str, context: TurnContext):
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kantos", genre="heavy_metal", turn_number=2)
    return await orch.run_narration_turn("strike the prince", ctx)


async def test_assembler_scrubs_leaked_preamble_from_narration(
    monkeypatch: pytest.MonkeyPatch, otel_capture
):
    result = await _run_sdk_turn_with_prose(monkeypatch, _BARSOOM_LEAK)
    assert "Now I narrate" not in result.narration
    assert "6 damage" not in result.narration
    assert result.narration == _BARSOOM_REAL
    spans = [s for s in otel_capture.get_finished_spans() if s.name == META_PREAMBLE_STRIPPED_SPAN]
    assert any(s.attributes["stripped"] is True for s in spans)
