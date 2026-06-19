"""Story 151-6 — Shrink output_only.md to a prose + private_segments brief, and
add the permanent perception-firewall guard (ADR-150 step 5).

ADR-150 §Decision step 2 ratifies that, once the bucket-B fields and
``action_rewrite`` are gone (151-3/4/5 — verified absent from ``output_only.md``
on this branch), the narrator output contract "collapses from ~255 lines of
recording contract to a short brief: *write the prose; obey the perception
firewall; withhold single-PC perception into* ``private_segments``." ADR-150
§Implementation-Notes step 5 names the file to shrink; §"Perception-firewall
guard (non-negotiable)" demands a test proving ``private_segments`` "is *not*
derivable post-hoc … so no future 'optimization' sweeps field C into the
extractor and silently reopens the ADR-105 leak."

This module is the RED contract for 151-6:

  AC1 (shrink)        — the one genuinely RED driver: ``output_only.md`` is still
                        a ~7.9k-char recording manual; the shrink makes it a
                        brief. ``test_output_only_is_a_short_brief`` fails until
                        the recording-manual bulk is removed.
  AC2 (cache-stable)  — regression tripwire: the shrunk section stays byte-stable
                        (no per-turn interpolation) and rides the cached System
                        prefix, consistent with 151-1.
  AC3 (firewall guard)— THE load-bearing permanent guard (per ADR-150,
                        non-negotiable): ``private_segments`` is not a
                        post-narration-extractor field, and a single-PC
                        perception correctly partitioned by the narrator appears
                        ONLY in ``private_segments`` — never in PART 1.
  AC4 (routing intact)— ``private_segments`` survives extraction → turn-result
                        assembly with ``anchor_pc`` intact (the precondition the
                        broadcast-layer per-PC routing reads), driven end-to-end
                        through the production SDK assembler.

Testing discipline (CLAUDE.md *No Source-Text Wiring Tests*; ADR-150 §Testing
strategy): behavioral / fixture-driven only. The firewall guards drive the real
``extract_structured_from_response`` and interrogate the real extractor pydantic
model; the routing guard drives a real turn through ``run_narration_turn``. No
test greps production source. The brevity/retention checks assert on the loaded
``NARRATOR_OUTPUT_ONLY`` prompt *content* — the artifact the story rewrites —
which is the same content-as-deliverable surface 151-1 asserts on, not a source
grep of code.
"""

from __future__ import annotations

import json
import re

import pytest

# Wires the tool adapters onto default_registry, matching the SDK path (151-1).
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.narrator_prompts import NARRATOR_OUTPUT_ONLY
from sidequest.agents.orchestrator import (
    Orchestrator,
    extract_structured_from_response,
)
from sidequest.agents.sidecar_extractor import BUCKET_B_FIELDS, SidecarExtraction
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# ---------------------------------------------------------------------------
# AC1 — the shrink (the one genuinely RED test)
# ---------------------------------------------------------------------------

# The residual brief is *write the prose; obey the perception firewall; withhold
# single-PC perception into private_segments*. A faithful brief of exactly that
# content is ≈2.2–2.8k chars (the dense, load-bearing firewall + private_segments
# instruction plus a short PART-1 framing). The file on this branch is ~7.9k
# chars — still dominated by the ~92-line "TOOL-OWNED MECHANICS" recording-manual
# recap. The ceiling sits well above a faithful brief and well below the current
# manual: it exists to fail the manual, not to micro-manage the rewrite's wording.
_BRIEF_CHAR_CEILING = 5000


def test_output_only_is_a_short_brief() -> None:
    """AC1. ``output_only.md`` must shrink from a recording manual to a brief.

    RED on this branch (the file is ~7.9k chars of recording contract); GREEN
    once the recording-manual bulk is removed, leaving the prose + perception-
    firewall + ``private_segments`` brief. This is the story's single
    behavior-changing deliverable — the firewall/routing tests below are
    permanent regression tripwires that codify already-correct behavior.
    """
    size = len(NARRATOR_OUTPUT_ONLY)
    assert size < _BRIEF_CHAR_CEILING, (
        f"output_only.md is {size} chars — still a recording manual, not a brief. "
        f"ADR-150 §Decision: collapse it to 'write the prose; obey the perception "
        f"firewall; withhold single-PC perception into private_segments' "
        f"(target < {_BRIEF_CHAR_CEILING} chars). The bucket-B/action_rewrite field "
        f"instructions are already gone; the remaining bulk to cut is the "
        f"TOOL-OWNED MECHANICS recording-manual recap."
    )


def test_output_only_brief_retains_firewall_and_private_segments() -> None:
    """AC1 (retention guard). The shrink must NOT throw out the irreducible
    content. ``private_segments`` (the one field that stays — ADR-150 bucket C),
    its ``anchor_pc`` routing key, the perception-firewall rule, and the PART-1
    prose instruction must all survive the rewrite.

    Green today and MUST stay green: a shrink that drops any of these has
    deleted the brief's reason to exist.
    """
    text = NARRATOR_OUTPUT_ONLY
    lower = text.lower()

    # The one sidecar field that stays narrator-inline — the API contract name
    # the extractor (orchestrator.py) parses out of game_patch.
    assert "private_segments" in text, (
        "the shrunk brief dropped the 'private_segments' field instruction — "
        "that is the one irreducible sidecar field (ADR-150 bucket C)"
    )
    # The per-PC routing key the broadcast layer reads to deliver each segment.
    assert "anchor_pc" in text, (
        "the shrunk brief dropped 'anchor_pc' — the broadcast layer routes each "
        "private segment to its owning PC by this key (ADR-105 path)"
    )
    # The perception firewall must be stated as a rule.
    assert "firewall" in lower, (
        "the shrunk brief dropped the perception-firewall rule — the MOVE-not-COPY "
        "guarantee (ADR-105) is the brief's load-bearing instruction"
    )
    # PART 1 / prose remains the narrator's primary instruction.
    assert "part 1" in lower or "prose" in lower, (
        "the shrunk brief dropped the PART-1 prose instruction — 'write the prose' "
        "is the brief's first job"
    )


# ---------------------------------------------------------------------------
# AC2 — cache / byte-stability (regression tripwire, consistent with 151-1)
# ---------------------------------------------------------------------------


def test_shrunk_output_only_carries_no_per_turn_interpolation() -> None:
    """AC2. The (shrunk) contract must remain byte-static — no per-turn data
    interpolated into it — or it would thrash the cache root it rides.

    The loaded constant is a plain ``.md`` read with no ``.format``/f-string
    substitution by construction; this pins that invariant so a careless rewrite
    cannot smuggle per-turn state (e.g. a PC name, turn number) into the brief.
    A ``str.format``-style named placeholder (``{character_name}``) would be the
    smuggling vector; a JSON example like ``{}`` or ``{"text": ...}`` (which the
    brief legitimately keeps to teach the game_patch shape) is NOT a placeholder.
    """
    # Match `{identifier}` — a str.format slot — but not `{}` or `{"..."}`.
    named_placeholder = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", NARRATOR_OUTPUT_ONLY)
    assert named_placeholder is None, (
        f"output_only.md contains a str.format placeholder "
        f"{named_placeholder.group(0)!r} — the brief must be byte-static so it "
        f"rides the cached System prefix unchanged turn-to-turn (151-1)."
    )


@pytest.mark.asyncio
async def test_shrunk_output_contract_rides_cached_system_prefix(
    simple_turn_context,
) -> None:
    """AC2. Driven through the production SDK path, the shrunk contract must
    still ride the cache-marked ``system_blocks[0]`` prefix and must NOT ride the
    per-turn user message (consistent with 151-1's promotion).

    The cache-ride sentinel is ``private_segments`` — a token guaranteed to
    survive the shrink (AC1 retention) — deliberately NOT the 151-1 marker
    'You are running with NATIVE TOOLS.', which the rewrite may remove. See the
    Delivery Finding flagging that 151-1 coupling.
    """
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text='{"narration":"ok"}',
                stop_reason="end_turn",
                input_tokens=120,
                output_tokens=18,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("look around", simple_turn_context)

    assert len(fake.recorded_requests) == 1
    request = fake.recorded_requests[0]
    cached = request.system_blocks[0].text
    user_msg = "\n".join(
        m.content
        for m in request.messages
        if m.role == "user" and isinstance(m.content, str)
    )

    assert "private_segments" in cached, (
        "the perception-firewall brief must ride the cache-marked system_blocks[0] "
        "prefix (sent once, read every turn) — 'private_segments' was not found "
        "there. A shrink that re-bucketed the section out of the cached prefix "
        "regresses 151-1."
    )
    assert "private_segments" not in user_msg, (
        "the perception-firewall brief is on the per-turn user message (uncached). "
        "It must stay on the cached System prefix (151-1)."
    )


# ---------------------------------------------------------------------------
# AC3 — perception-firewall permanent guard (ADR-150, non-negotiable)
# ---------------------------------------------------------------------------


def test_private_segments_is_not_a_post_narration_extractor_field() -> None:
    """AC3 (THE non-negotiable guard, ADR-150 §Perception-firewall guard).

    ``private_segments`` must NOT be derivable post-hoc — it must never be one of
    the post-narration Haiku extractor's fields, or a future 'optimization' could
    sweep field C into the extractor and silently reopen the ADR-105 leak (a
    post-hoc reader cannot recover perception already leaked into PART 1).

    This interrogates the *real* extractor contract (``BUCKET_B_FIELDS`` and the
    ``SidecarExtraction`` pydantic model) — refactor-stable, not a source grep.
    """
    assert "private_segments" not in BUCKET_B_FIELDS, (
        f"private_segments was added to BUCKET_B_FIELDS {BUCKET_B_FIELDS!r} — the "
        f"post-narration extractor must NEVER own the perception-firewall field "
        f"(ADR-150 bucket C stays narrator-inline). This is the regression that "
        f"reopens the ADR-105 leak."
    )
    extractor_fields = set(SidecarExtraction.model_fields)
    assert "private_segments" not in extractor_fields, (
        f"SidecarExtraction now has a 'private_segments' field (fields: "
        f"{sorted(extractor_fields)}) — the post-narration extractor cannot be "
        f"allowed to produce private perception post-hoc (ADR-150 §Perception-"
        f"firewall guard)."
    )
    # The extractor's eleven fields are exactly bucket-B — assert it is non-empty
    # so the guard above is meaningful (an empty model would pass vacuously).
    assert extractor_fields == set(BUCKET_B_FIELDS), (
        f"SidecarExtraction fields drifted from BUCKET_B_FIELDS — expected exactly "
        f"the eleven bucket-B prose-readout fields, got {sorted(extractor_fields)}"
    )


def test_single_pc_perception_appears_only_in_private_segments_never_part1() -> None:
    """AC3. A turn carrying single-PC perception, correctly partitioned by the
    narrator (private reading in ``private_segments``, NOT duplicated into PART
    1), must yield public prose with ZERO trace of that perception while the
    structured private channel carries it with its ``anchor_pc`` intact.

    This is the durable proof of MOVE-not-COPY at the extraction seam: PART 1 is
    public-only; the reading itself is private.
    """
    private_reading = (
        "Two distinct arcane auras answer beyond the grate; one old and diffuse, "
        "the second bright, recent, and actively being drawn."
    )
    raw = (
        "**The Grate**\n\n"
        "Willes kneels at the chalk-cross, eyes closed. Narder sets his back to "
        "the wall, blade up, watching the dark.\n\n"
        "```game_patch\n"
        + json.dumps(
            {"private_segments": [{"text": private_reading, "anchor_pc": "Willes"}]}
        )
        + "\n```"
    )

    out = extract_structured_from_response(raw)
    public = out["prose"]

    # The publicly-observable action survives verbatim — the firewall MOVES, it
    # does not censor the public scene.
    assert "Willes kneels at the chalk-cross" in public
    assert "Narder sets his back to the wall" in public

    # ZERO trace of the single-PC reading in PART 1 — no distinctive token leaks.
    leak_tokens = ("arcane auras", "beyond the grate", "being drawn", "diffuse")
    for token in leak_tokens:
        assert token not in public.lower(), (
            f"single-PC perception token {token!r} leaked into PART 1 public prose "
            f"— the perception firewall (ADR-105 MOVE-not-COPY) is broken"
        )

    # The structured private channel carries the reading, routed to its owner.
    assert len(out["private_segments"]) == 1
    seg = out["private_segments"][0]
    assert seg["anchor_pc"] == "Willes"
    assert "auras" in seg["text"].lower()


def test_firewall_strips_leaked_perception_from_part1() -> None:
    """AC3. When the narrator (non-compliantly) DOES leak the private reading
    into PART 1 — as a self-labelled aside AND as an ordinary-narration near-
    duplicate of the private segment — the mechanical backstop strips both forms
    from the public prose while the structured ``private_segments`` still routes.

    Covers the 'label' and 'ordinary narration' leak forms named in AC3. (The
    Jaccard-overlap backstop catches a near-duplicate restatement; the prompt's
    MOVE-not-COPY rule is the primary defence against a terse paraphrase the
    token backstop cannot detect — see the brief retained in AC1.)
    """
    seg_text = (
        "The second aura is brighter and more recent than the first, and it is "
        "active, not dormant — something is drawing on it right now."
    )
    raw = (
        "**The Grate**\n\n"
        "Willes kneels at the chalk-cross.\n\n"
        # Ordinary-narration near-duplicate of the private reading (leak form 1).
        "The second aura is brighter and more recent than the first, active not "
        "dormant, and something is drawing on it right now.\n\n"
        # Self-labelled private aside (leak form 2).
        "Private (Willes only): " + seg_text + "\n\n"
        "```game_patch\n"
        + json.dumps({"private_segments": [{"text": seg_text, "anchor_pc": "Willes"}]})
        + "\n```"
    )

    out = extract_structured_from_response(raw)
    public = out["prose"]

    # The public scene-setting survives.
    assert "Willes kneels at the chalk-cross" in public
    # Neither leak form survives in PART 1.
    assert "Private (Willes only)" not in public
    assert "drawing on it right now" not in public.lower()
    assert "active not dormant" not in public.lower()
    # The structured channel still carries the reading for the owner.
    assert len(out["private_segments"]) == 1
    assert out["private_segments"][0]["anchor_pc"] == "Willes"


# ---------------------------------------------------------------------------
# AC4 — routing intact: private_segments survives assembly with anchor_pc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_result_carries_private_prose_segments_with_anchor(
    simple_turn_context,
) -> None:
    """AC4. Driven end-to-end through the production SDK assembler, a turn whose
    game_patch carries a ``private_segments`` entry must surface it on
    ``NarrationTurnResult.private_prose_segments`` with ``anchor_pc`` intact —
    the precondition the broadcast layer (websocket_session_handler) reads to
    route each segment to its owning PC (ADR-105 path).

    This is the behavioral wiring proof that the firewall field survives the real
    extraction → ``_presentation_and_untooled_fields`` → result path. A regression
    here drops the private channel on the live backend.
    """
    private_reading = "The lock answers your probe with a faint ward-heat — recent."
    raw = (
        "**The Vault Door**\n\n"
        "Kael runs gloved fingers along the brass plate, listening.\n\n"
        "```game_patch\n"
        + json.dumps(
            {"private_segments": [{"text": private_reading, "anchor_pc": "Kael"}]}
        )
        + "\n```"
    )
    fake = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text=raw,
                stop_reason="end_turn",
                input_tokens=140,
                output_tokens=40,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    orch = Orchestrator(client=fake)
    result = await orch.run_narration_turn("probe the lock", simple_turn_context)

    segments = result.private_prose_segments
    assert isinstance(segments, list) and len(segments) == 1, (
        f"expected exactly one private_prose_segment on the turn result, got "
        f"{segments!r} — the firewall field was dropped during assembly"
    )
    seg = segments[0]
    assert seg["anchor_pc"] == "Kael", (
        f"anchor_pc lost during assembly (got {seg.get('anchor_pc')!r}) — the "
        f"broadcast layer cannot route the segment to its owner without it"
    )
    assert "ward-heat" in seg["text"].lower()

    # The private reading must NOT be in the public narration the whole table sees.
    assert "ward-heat" not in result.narration.lower(), (
        "the single-PC reading leaked into the public narration — firewall broken "
        "on the end-to-end SDK turn path"
    )
    # The public action survives.
    assert "Kael runs gloved fingers" in result.narration
