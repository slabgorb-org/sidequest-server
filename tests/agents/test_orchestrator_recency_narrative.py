"""Story 49-1 RED — Recency-zone recent-narrative window in narrator prompt.

ADR-098 dropped the ``claude -p --resume`` flag, removing the high-attention
recent-narration block from the narrator's prompt. Post-098 the narrator
loses prior-turn details because ``narrative_log`` lives in the
``<game_state>`` JSON dump (Valley zone, attention-decayed). 2026-05-11
Glenross playtest surfaced the regression:

- Turn 5 prose names a male patient ("Father", "him").
- Turn 6 invents a female "mother/her" with no state constraint to refuse.
- Prose-only facts ("secateurs on the blotter") drop between turns.

The fix this story drives: a dedicated Recency-zone section
(``recent_narrative_context``) carrying the last K=2 narrative_log entries
rendered as readable prose blocks (NOT JSON), labeled by author/round
prefix — alongside the existing Recency-zone constraints
(``player_action``, ``npc_intro_visual_constraint``,
``confrontation_trigger_constraint``).

These tests assert the FUTURE behavior — they will fail until Dev wires
``TurnContext.recent_narrative_log`` and the orchestrator's
``build_narrator_prompt`` registers the new section + emits the OTEL span.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory
from sidequest.game.session import NarrativeEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(*, round_: int, author: str, content: str) -> NarrativeEntry:
    """Construct a minimal NarrativeEntry for fixtures."""
    return NarrativeEntry(round=round_, author=author, content=content)


def _glenross_gender_flip_log() -> list[NarrativeEntry]:
    """Fixture mirroring the 2026-05-11 Glenross gender-flip scenario.

    Turn 5 prose names a male patient ("Father", "his", "him"). The next
    turn must reference the same patient consistently. The narrator can
    only do this if turn 5's prose rides into the prompt in a high-
    attention zone.

    Trimmed to K=2 entries in 57-1 (rounds 4-5 — the load-bearing
    male-patient continuity beat) so post-cap section content still
    contains every fixture entry. Pre-57-1 the fixture spanned rounds
    3-5 (4 entries) to match the K=4 cap.
    """
    return [
        _entry(
            round_=4,
            author="Player",
            content="I kneel beside him and check the wound.",
        ),
        _entry(
            round_=5,
            author="narrator",
            content=(
                "Father lies pale against the linen, his breath shallow. "
                "The secateurs rest on the blotter beside him where the "
                "physician set them down. You meet his eyes; he tries to speak."
            ),
        ),
    ]


def _section_by_name(registry, agent_name: str, name: str):
    """Return the registered PromptSection with the given name, or None."""
    for section in registry.registry(agent_name):
        if section.name == name:
            return section
    return None


# ---------------------------------------------------------------------------
# Section registration / zone / category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_section_registered_when_log_non_empty(
    simple_turn_context_turn_three,
):
    """When ``TurnContext.recent_narrative_log`` is non-empty, the
    orchestrator MUST register a ``recent_narrative_context`` section.

    This is the headline AC #1: a section in the Recency zone alongside
    the existing player_action / npc_intro_visual_constraint /
    confrontation_trigger_constraint sections.
    """
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_glenross_gender_flip_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("I lean closer.", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None, (
        "recent_narrative_context section was not registered. "
        "Existing Recency-zone neighbours: "
        f"{[s.name for s in registry.registry(orch._narrator.name()) if s.zone == AttentionZone.Recency]}"
    )


@pytest.mark.asyncio
async def test_recent_narrative_section_is_in_recency_zone(
    simple_turn_context_turn_three,
):
    """The whole point of the story: the section must land in
    ``AttentionZone.Recency``, not Valley/Late. Without this it gets the
    same attention-decay treatment that broke continuity in the first
    place (ADR-009 attention-aware prompt zones)."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_glenross_gender_flip_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    assert section.zone == AttentionZone.Recency, (
        f"recent_narrative_context registered in {section.zone}, "
        "must be Recency to ride high-attention with player_action."
    )


@pytest.mark.asyncio
async def test_recent_narrative_section_category_is_state(
    simple_turn_context_turn_three,
):
    """The section carries grounding state (prior turns), not a rule.
    Use SectionCategory.State so the dashboard Prompt-tab classification
    groups it alongside other state blocks rather than guardrails."""
    ctx = replace(
        simple_turn_context_turn_three,
        recent_narrative_log=_glenross_gender_flip_log(),
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    assert section.category == SectionCategory.State


# ---------------------------------------------------------------------------
# Prose rendering (NOT JSON)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_section_renders_as_prose_not_json(
    simple_turn_context_turn_three,
):
    """AC #2: content is rendered as readable prose blocks (NOT JSON).

    Bug pattern this prevents: the existing path serializes narrative_log
    as ``snapshot.model_dump_json()`` — long ``{"round": 5, "author":
    "narrator", ...}`` blobs. The narrator skims those poorly compared to
    flowing prose. Assert the section contains no JSON key syntax for
    ``NarrativeEntry`` fields.
    """
    log = _glenross_gender_flip_log()
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None

    body = section.content
    # Forbid JSON-shaped entries — these are the markers that the section
    # is dumping raw NarrativeEntry models instead of rendering prose.
    assert '"author":' not in body, "recent_narrative_context contains JSON keys, must be prose"
    assert '"round":' not in body, "recent_narrative_context contains JSON keys, must be prose"
    assert '"content":' not in body, "recent_narrative_context contains JSON keys, must be prose"

    # And — the round-tripped JSON of the log MUST NOT appear verbatim.
    blob = json.dumps([e.model_dump() for e in log])
    assert blob not in body

    # Sanity: the actual prose content of each entry must be present so
    # the section is not silently empty.
    for entry in log:
        assert entry.content in body, (
            f"narrative entry content missing from prose section: {entry.content[:40]!r}"
        )


@pytest.mark.asyncio
async def test_recent_narrative_section_labels_author_and_round(
    simple_turn_context_turn_three,
):
    """AC #2 cont'd: each entry is labeled by author/round prefix so the
    narrator can tell player-input from narrator-output and order by
    round. Without a label the prose blocks bleed into each other and
    speaker attribution drifts (Felix's 71-entry author='narrator'
    pattern from Playtest 3 — Story 45-22)."""
    log = _glenross_gender_flip_log()
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    body = section.content.lower()

    # Every entry's author must be visibly attributed.
    assert "player" in body, "recent_narrative_context omits Player author label"
    assert "narrator" in body, "recent_narrative_context omits narrator author label"

    # Every round number that appears in the log must be visible in the
    # prefix so the narrator can read entries in chronological order.
    for round_n in sorted({e.round for e in log}):
        assert str(round_n) in section.content, (
            f"recent_narrative_context omits round={round_n} label; narrator cannot order entries"
        )


# ---------------------------------------------------------------------------
# Cap / ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_section_caps_at_last_two_entries(
    simple_turn_context_turn_three,
):
    """AC #2: default K=2 (one player turn + one narrator turn) — tightened in 57-1.

    When the log is longer, the section MUST contain only the last 2
    entries. Without a cap the section grows unbounded, defeating the
    ADR-098 bounded-prompt invariant (every turn carries the same shape).
    """
    # Sigil-delimited, zero-padded markers — naive substring check above
    # (``"line 1" in "line 16"`` was True with the prior ``f"line {i}"``
    # fixture, making the negative assertion unsatisfiable regardless of
    # the cap behavior). The ``<<…>>`` brackets ensure ``"<<line-01>>"``
    # is NOT a substring of ``"<<line-19>>"``.
    long_log = [
        _entry(
            round_=i,
            author=("Player" if i % 2 == 0 else "narrator"),
            content=f"<<line-{i:02d}>>",
        )
        for i in range(20)
    ]
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=long_log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    body = section.content

    # Last 2 entries (rounds 18..19) must be present.
    for i in range(18, 20):
        marker = f"<<line-{i:02d}>>"
        assert marker in body, f"entry round={i} ({marker}) missing — cap dropped it accidentally"

    # Earlier entries (rounds 0..17) must NOT be present.
    for i in range(18):
        marker = f"<<line-{i:02d}>>"
        assert marker not in body, (
            f"entry round={i} ({marker}) leaked through cap — section should hold only last 2"
        )


# ---------------------------------------------------------------------------
# Partial windows (K_actual ∈ {1}) — Queen-of-Hearts review C1+C2+M2.
#
# (K_actual sweep tightened from {1,2,3} to {1} when 57-1 dropped K=4→K=2;
# k_actual=2 is the exact-cap case covered by the caps-at-last-two test
# above, and k_actual≥3 exceeds the cap and is no longer a partial window.)
#
# The first GREEN pass gated section registration on
# ``_recent_turn_count >= _recent_k`` — treating K as a FLOOR rather than
# a CAP. Turn 1 of every fresh save then carried zero high-attention
# recency context (exactly the scenario the story exists to fix). The
# OTEL span fires regardless, so on partial-window turns the span lies:
# ``turn_count=1 / total_tokens=13`` while ``section_registered=False`` —
# Sebastien's lie-detector flips a false-positive ("injector engaged")
# when the injector engaged with NOTHING (no high-attention bytes).
#
# These tests pin the corrected semantics:
#   - K=2 caps the window (already covered above).
#   - Any non-empty window registers the section with all available
#     entries.
#   - The OTEL span's ``total_tokens`` matches the actually-registered
#     body (or is 0 when no section is registered). No-lie invariant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("k_actual", [1])
async def test_partial_window_registers_section_with_available_entries(
    simple_turn_context_turn_three, k_actual: int
):
    """Partial window of 1 entry (turn 1 of a fresh save) MUST still
    register ``recent_narrative_context``. K=2 is a cap, not a floor —
    the whole point of the story is to make turn 2's narrator read
    turn 1's prose at high attention.

    Currently fails: GREEN at orchestrator.py:1622 gates on
    ``_recent_turn_count >= _recent_k``, leaving turn 1 with no
    high-attention recency at all.
    """
    log = [
        _entry(
            round_=i + 1,
            author=("Player" if i % 2 == 0 else "narrator"),
            # Sigil-delimited markers per the K=2 cap fixture lesson —
            # ``"slice-01"`` is not a substring of ``"slice-12"``.
            content=f"<<slice-{i + 1:02d}>> distinctive prose for partial-window test",
        )
        for i in range(k_actual)
    ]
    assert len(log) == k_actual

    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None, (
        f"recent_narrative_context NOT registered for K_actual={k_actual} — "
        "K is being treated as a floor instead of a cap. Turns 1-3 of every "
        "fresh save lose their high-attention recency window."
    )
    assert section.zone == AttentionZone.Recency

    body = section.content
    # Every entry in the available window must be rendered into the
    # section — partial means "give me what you've got", not "give me
    # nothing".
    for i in range(k_actual):
        marker = f"<<slice-{i + 1:02d}>>"
        assert marker in body, (
            f"partial-window section missing entry {marker} for K_actual={k_actual}"
        )

    # Author labels must still appear (consistent with the K=2 prose-
    # rendering contract).
    assert "Player" in body or "narrator" in body
    # Chronological order — first entry comes before last.
    if k_actual >= 2:
        first_marker = "<<slice-01>>"
        last_marker = f"<<slice-{k_actual:02d}>>"
        assert body.index(first_marker) < body.index(last_marker), (
            "partial-window entries are out of chronological order"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("k_actual", [1])
async def test_partial_window_otel_span_attrs_match_injected_body(
    simple_turn_context_turn_three, otel_capture, k_actual: int
):
    """The ``recent_narrative_context_injected`` span MUST NOT lie.

    Either:
      (a) turn_count == 0 AND total_tokens == 0 AND no section registered, OR
      (b) turn_count > 0 AND section registered AND total_tokens is the
          token estimate of the registered section's body.

    Currently fails on K_actual=1: the span fires with
    ``turn_count=K_actual`` and positive ``total_tokens`` while the
    section was never registered — Sebastien's GM panel sees
    "injector engaged" for a turn with zero high-attention recency
    bytes. The dashboard cannot distinguish "engaged + injected" from
    "engaged + silently dropped".
    """
    log = [
        _entry(
            round_=i + 1,
            author="Player" if i % 2 == 0 else "narrator",
            content=f"<<slice-{i + 1:02d}>> distinctive prose",
        )
        for i in range(k_actual)
    ]
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "recent_narrative_context_injected"
    ]
    assert len(spans) == 1, (
        f"expected exactly one recent_narrative_context_injected span, got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("turn_count") == k_actual

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None, (
        f"no section registered for K_actual={k_actual} — span/section "
        "agreement cannot be tested while the partial-window gate is broken"
    )

    # The "no-lie" invariant — span attrs reflect what was actually
    # injected. The token-count formula in the orchestrator (``len(body)
    # // 4`` with ``max(1, ...)``) is the contract; if Dev later swaps
    # in PromptSection.token_estimate() during GREEN, this assertion
    # still anchors them together.
    expected_total_tokens = max(1, len(section.content) // 4)
    assert attrs.get("total_tokens") == expected_total_tokens, (
        f"span lies on K_actual={k_actual}: span says total_tokens="
        f"{attrs.get('total_tokens')} but the registered section body "
        f"({len(section.content)} chars) computes to "
        f"{expected_total_tokens} tokens"
    )


@pytest.mark.asyncio
async def test_recent_narrative_span_truth_invariant_across_window_sizes(
    simple_turn_context,
    simple_turn_context_turn_three,
    otel_capture,
):
    """No-lie invariant, property form: across the K∈{0..5} sweep, the
    span's claims must MATCH the registry. Either everything zero AND
    no section, OR everything non-zero AND section registered. The
    asymmetric "engaged + silently dropped" state (span says
    ``turn_count=2, total_tokens=25`` while section is absent) is what
    the partial-window bug introduced; it makes Sebastien's GM panel
    structurally unable to distinguish a working injector from a broken
    one on turns 1-3 of a fresh save.

    Currently fails on the K∈{1,2,3} cases: span fires with positive
    counts while the section is unregistered.
    """
    cases = [
        ("empty", simple_turn_context),
        (
            "K=1",
            replace(
                simple_turn_context_turn_three,
                recent_narrative_log=[_entry(round_=1, author="Player", content="aaaa")],
            ),
        ),
        (
            "K=3",
            replace(
                simple_turn_context_turn_three,
                recent_narrative_log=[
                    _entry(round_=1, author="Player", content="aaaa"),
                    _entry(round_=1, author="narrator", content="bbbb"),
                    _entry(round_=2, author="Player", content="cccc"),
                ],
            ),
        ),
        (
            "K=5",
            replace(
                simple_turn_context_turn_three,
                recent_narrative_log=[
                    _entry(round_=i + 1, author="Player", content="long enough content here")
                    for i in range(5)
                ],
            ),
        ),
    ]

    spans_before = len(otel_capture.get_finished_spans())
    orch = Orchestrator()
    section_present_by_label: dict[str, bool] = {}
    for label, ctx in cases:
        _, registry = await orch.build_narrator_prompt(f"act {label}", ctx)
        section_present_by_label[label] = (
            _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
            is not None
        )

    spans = [
        s
        for s in otel_capture.get_finished_spans()[spans_before:]
        if s.name == "recent_narrative_context_injected"
    ]
    assert len(spans) == len(cases), f"expected one span per case ({len(cases)}); got {len(spans)}"

    for (label, _), span in zip(cases, spans, strict=True):
        attrs = dict(span.attributes or {})
        tc = attrs.get("turn_count", 0)
        tt = attrs.get("total_tokens", 0)
        section_present = section_present_by_label[label]

        # turn_count > 0 IFF section was registered. Anything else is
        # the asymmetric lie the partial-window bug introduced.
        if section_present:
            assert tc > 0, (
                f"[{label}] section IS registered but span turn_count={tc}; "
                "span under-reports a real injection"
            )
            assert tt > 0, (
                f"[{label}] section IS registered but span total_tokens={tt}; "
                "the registered body cost more than zero tokens"
            )
        else:
            assert tc == 0, (
                f"[{label}] section NOT registered but span turn_count={tc} — "
                "lying span: dashboard reads 'engaged' for a turn with zero "
                "high-attention recency bytes"
            )
            assert tt == 0, (
                f"[{label}] section NOT registered but span total_tokens={tt} — "
                "lying span: claims tokens that were never injected into the "
                "prompt"
            )


# ---------------------------------------------------------------------------
# Per-entry byte cap (Queen-of-Hearts review M4).
#
# K=2 caps the entry COUNT but the body has no byte cap. Reviewer
# reproduced a 40,086-char Recency section (4 × 10kB) inside a
# 72,218-char composed prompt — a single verbose narrator turn alone
# starves Late-zone sections of attention. ADR-009 (attention-aware
# prompt zones) treats Late as load-bearing for vocabulary / format
# guardrails; flooding Recency drops them out of the model's working
# memory.
#
# Pinned contract:
#   - Per-entry rendered body length is capped at ``PER_ENTRY_CAP_BYTES``
#     (2048 chars — Reviewer's recommendation). When an entry exceeds the
#     cap, the rendered version contains the marker ``… [truncated]`` (or
#     ``[truncated]`` — Dev may pick the surface text but the substring
#     must be present so Sebastien's GM panel can see the cut).
#   - Within-cap entries are NOT marked.
#   - Section total content stays bounded (≤ ``SECTION_BUDGET_BYTES``,
#     allowing 4 entries × per-entry cap + author/round label overhead).
#
# Numbers picked deliberately: 2kB × 4 entries = 8kB body, ~9kB after
# labels. The model has plenty of room for the recency window AND the
# Late-zone Format guardrails (verbosity / vocabulary blocks together
# weigh ~1.6kB).
# ---------------------------------------------------------------------------


PER_ENTRY_CAP_BYTES = 2048
SECTION_BUDGET_BYTES = 12_000  # K×2kB + label overhead + safety margin (sized for pre-57-1 K=4; still valid for K=2 with headroom)
TRUNCATION_MARKER = "[truncated]"


@pytest.mark.asyncio
async def test_oversized_entry_is_truncated_with_marker(
    simple_turn_context_turn_three,
):
    """Single 10kB entry — verbose narrator turn — MUST be truncated so it
    cannot eat the whole prompt budget on its own. Marker must be
    present so the cut is visible to anyone reading the prompt
    (Sebastien on the GM panel, Keith debugging a save)."""
    big_content = "X" * 10_000
    log = [_entry(round_=1, author="narrator", content=big_content)]

    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    body = section.content

    # The full 10kB must not ride into the prompt — that's the whole point.
    assert big_content not in body, (
        "10kB entry shipped verbatim — no per-entry cap. A single verbose "
        f"narrator turn produced a {len(body)}-char Recency section."
    )

    # Section body stays well under the budget envelope.
    assert len(body) <= SECTION_BUDGET_BYTES, (
        f"single 10kB entry rendered a {len(body)}-char section; budget is {SECTION_BUDGET_BYTES}"
    )

    # The truncation cut must be visible — silent truncation hides bugs.
    assert TRUNCATION_MARKER in body, (
        f"oversized entry was truncated but the marker {TRUNCATION_MARKER!r} "
        "is absent — a future reader cannot tell whether the prose was the "
        "narrator's actual output or a system cut"
    )


@pytest.mark.asyncio
async def test_oversized_entries_stay_within_section_budget(
    simple_turn_context_turn_three,
):
    """K=2 × 10kB stress: the section MUST stay within the byte budget.
    Reviewer originally reproduced 40,086-char section / 72,218-char full
    prompt at the pre-57-1 K=4 stress shape; the post-tightening K=2
    stress (2 × 10kB) is still well above SECTION_BUDGET_BYTES and pins
    the same per-entry-truncation contract. Late-zone Format guardrails
    (vocabulary, verbosity) fall out of the model's working memory at
    that scale.
    """
    big_content = "X" * 10_000
    log = [
        _entry(
            round_=i + 1,
            author=("Player" if i % 2 == 0 else "narrator"),
            content=big_content + f" <<entry-{i + 1}>>",
        )
        for i in range(2)
    ]

    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    prompt_text, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    body = section.content

    assert len(body) <= SECTION_BUDGET_BYTES, (
        f"2 × 10kB stress produced {len(body)}-char section; budget is "
        f"{SECTION_BUDGET_BYTES}. Reviewer reproduced 40,086 chars on the "
        "pre-57-1 K=4 shape — same disease, scaled to the K=2 cap."
    )

    # Truncation marker must be present (at least one — likely both).
    assert TRUNCATION_MARKER in body, "oversized entries were truncated silently — marker missing"

    # Even after truncation, every entry's sigil tail should ideally survive
    # so the narrator still sees BOTH turns rather than 2 truncated heads
    # of one. (Dev's choice: truncate from the tail or the middle. Either
    # way, the per-entry sigil at the END of each content tells us whether
    # truncation kept the start or the end.) Soft assertion: at minimum the
    # author/round labels for both entries must survive — pinning total
    # turn coverage, not specific truncation strategy.
    for round_n in (1, 2):
        assert f"Round {round_n}" in body, (
            f"entry round={round_n} lost its label after truncation — both "
            "turns must still be visible even if their bodies are cut"
        )

    # And the full composed prompt should be in a sane envelope too —
    # the byte cap is upstream defense for the bounded-prompt invariant.
    assert len(prompt_text) <= 60_000, (
        f"composed prompt is {len(prompt_text)} chars after the K=2 × 10kB "
        "stress; byte cap on the Recency section did not flow through to "
        "the bounded-prompt invariant"
    )


@pytest.mark.asyncio
async def test_within_cap_entries_are_not_truncated(
    simple_turn_context_turn_three,
):
    """Counterpart guard: entries under the per-entry cap MUST NOT carry
    the truncation marker. Marker-spam would erode its meaning and the
    GM panel could no longer tell a truncated turn from a clean one.
    """
    # Two entries (post-57-1 K=2) sized well under the per-entry cap.
    # The test asserts marker-absence on short content; whether the cap
    # drops earlier entries is orthogonal — what matters is that the
    # entries that DO survive into the section have no truncation marker.
    short_log = [
        _entry(round_=2, author="Player", content="Another short player turn."),
        _entry(round_=2, author="narrator", content="And another short narrator reply."),
    ]
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=short_log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    assert TRUNCATION_MARKER not in section.content, (
        "short entries got a truncation marker they didn't deserve — "
        "marker must only appear when an entry exceeded the cap"
    )


@pytest.mark.asyncio
async def test_recent_narrative_section_preserves_chronological_order(
    simple_turn_context_turn_three,
):
    """Entries must appear oldest → newest so the narrator reads them in
    the order they happened. Reversed ordering would invert cause and
    effect and is worse than no context at all."""
    # Two entries (post-57-1 K=2) — both survive the cap so the
    # chronological ordering check operates on a stable log.
    log = [
        _entry(round_=2, author="Player", content="bravo-action"),
        _entry(round_=2, author="narrator", content="bravo-response"),
    ]
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("act", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None
    body = section.content

    positions = [body.index(e.content) for e in log]
    assert positions == sorted(positions), (
        f"recent_narrative_context entries are out of chronological order: {positions}"
    )


# ---------------------------------------------------------------------------
# Empty log — zero-byte-leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_section_skipped_when_log_empty(
    simple_turn_context,
):
    """Zero-byte-leak discipline (matches state_summary pattern at
    orchestrator.py:1309): if there's no recent narrative (turn 0 or
    fresh save), the section must not be registered at all. An empty
    section pollutes the prompt and contradicts ADR-098's bounded-prompt
    invariant.
    """
    assert list(simple_turn_context.recent_narrative_log) == [], (
        "fixture should default to an empty narrative log on turn 0"
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("first action", simple_turn_context)

    assert _section_by_name(registry, orch._narrator.name(), "recent_narrative_context") is None


# ---------------------------------------------------------------------------
# Composed prompt — full wiring test (CLAUDE.md "Every Test Suite Needs a
# Wiring Test")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_context_appears_in_composed_prompt_text(
    simple_turn_context_turn_three,
):
    """Wiring: register_section is necessary but not sufficient. The
    composed ``prompt_text`` returned from build_narrator_prompt MUST
    contain the prose content — proving the section is registered AND
    the registry's compose step is actually emitting it.

    Without this assertion a future refactor could quietly drop the
    section from the composed output and the per-section unit tests
    would still pass."""
    log = _glenross_gender_flip_log()
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    prompt_text, _ = await orch.build_narrator_prompt("I lean closer.", ctx)

    # The prose content of every injected entry must ride into the
    # composed prompt; otherwise the narrator never sees it.
    for entry in log:
        assert entry.content in prompt_text, (
            f"composed prompt text missing entry content: {entry.content[:60]!r}"
        )


# ---------------------------------------------------------------------------
# OTEL span — Sebastien's lie-detector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_context_injected_span_emitted(
    simple_turn_context_turn_three, otel_capture
):
    """AC #4: emit ``recent_narrative_context_injected`` with ``turn_count``
    and ``total_tokens`` attributes.

    Sebastien's GM panel needs the span to tell whether the recency
    injector engaged at all — without it, broken wiring looks identical
    to "no recent narrative this turn". Audit fields are load-bearing.
    """
    log = _glenross_gender_flip_log()
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    await orch.build_narrator_prompt("act", ctx)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "recent_narrative_context_injected"
    ]
    assert len(spans) == 1, (
        f"expected exactly one recent_narrative_context_injected span, got "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )

    attrs = dict(spans[0].attributes or {})
    assert "turn_count" in attrs, "span missing required turn_count attribute"
    assert "total_tokens" in attrs, "span missing required total_tokens attribute"
    assert attrs["turn_count"] == len(log), (
        f"turn_count should equal injected-entry count; got {attrs['turn_count']}"
    )
    # Token estimate is a positive integer for a non-empty injection.
    assert isinstance(attrs["total_tokens"], int)
    assert attrs["total_tokens"] > 0, "total_tokens must be > 0 for a non-empty log"


@pytest.mark.asyncio
async def test_recent_narrative_context_injected_span_fires_with_zero_when_empty(
    simple_turn_context, otel_capture
):
    """OTEL no-op-fire discipline (matches ``room.state_injected``): the
    span fires on EVERY narrator turn — including the empty-log case
    with ``turn_count=0`` — so the GM panel can distinguish "feature
    engaged with nothing to inject" from "feature broken / not wired"."""
    orch = Orchestrator()
    await orch.build_narrator_prompt("first action", simple_turn_context)

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "recent_narrative_context_injected"
    ]
    assert len(spans) == 1, (
        "recent_narrative_context_injected must fire even when the log is empty "
        "(Sebastien's lie-detector contract)"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("turn_count") == 0
    assert attrs.get("total_tokens") == 0


# ---------------------------------------------------------------------------
# Regression: Glenross gender flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gender_flip_regression_father_appears_in_recent_narrative(
    simple_turn_context_turn_three,
):
    """AC #5: the regression test for the 2026-05-11 Glenross playtest.

    Turn 5 prose names a male patient ("Father", "his", "him"). The
    composed turn-6 prompt MUST surface that male-coded language in a
    high-attention zone so the narrator cannot invent a "mother / her"
    without contradicting visible context.

    This test does NOT call the LLM (RED phase — we cannot prove the
    narrator stops inventing); it proves the precondition: the gender
    cues survive into the prompt where the narrator can see them.
    """
    log = _glenross_gender_flip_log()
    ctx = replace(simple_turn_context_turn_three, recent_narrative_log=log)
    orch = Orchestrator()
    prompt_text, registry = await orch.build_narrator_prompt("I take Father's hand.", ctx)

    section = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
    assert section is not None, "recency section missing — gender cues will not reach the narrator"
    assert section.zone == AttentionZone.Recency

    # The male-coded language must appear inside the high-attention
    # section, not just somewhere in the prompt.
    body = section.content
    assert "Father" in body, "turn-5 'Father' missing from recency section"
    assert "his" in body.lower(), "turn-5 'his' missing from recency section"

    # And the prose-only fact ("secateurs on the blotter") must also
    # survive — that's the second regression from the playtest.
    assert "secateurs" in body.lower(), (
        "prose-only fact 'secateurs' missing — the 'set down twice' regression will recur"
    )


# ---------------------------------------------------------------------------
# Bounded-prompt invariant — ADR-098 cross-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_narrative_section_does_not_blow_bounded_prompt():
    """ADR-098 invariant: prompt size does not grow with turn count.

    Walks 30 simulated turns with an ever-growing narrative_log and
    pins TWO bounded-prompt invariants on the recency section:

    1. **Absolute byte envelope (M4 tie-in):** every turn's section is
       below ``SECTION_BUDGET_BYTES`` (12 kB). This is the load-bearing
       bound — without it, a 4-entry × 10 kB stress (per Queen-of-Hearts
       M4) silently produces a 40 kB section and starves Late-zone
       Format guardrails of attention regardless of turn count.

    2. **Steady-state shape stability:** once the K=2 window has filled
       (turn index 1+), section size plateaus. Turn 0 is legitimately
       smaller — the partial-window fix per C1 means a turn-0 prompt
       carries 1 entry while turn-1 onward carries the full 2.
       Asserting a flat ratio across the ramp-up would penalize that
       fix; the right invariant is "flat once full".

    Why both invariants? Either alone is insufficient:
    - Ratio alone: a section that oscillates 100 → 12 000 → 100 chars
      passes a 1.5 plateau ratio if measured at the right slice.
    - Envelope alone: a section that crept from 1 kB to 11 999 kB across
      turns 5..29 would satisfy the byte cap but break ADR-098.
    """
    from unittest.mock import AsyncMock

    from sidequest.agents.claude_client import ClaudeResponse

    # All section sizes per turn (None when the section was not
    # registered — should never happen post-C1 since turn 0 has 1
    # entry available).
    per_turn_sizes: list[int | None] = []

    async def capture(system_prompt: str, user_message: str, **kwargs):
        return ClaudeResponse(text='{"narration":"ok"}', session_id=None)

    client = AsyncMock()
    client.send_stateless = AsyncMock(side_effect=capture)

    orch = Orchestrator(client=client)
    log: list[NarrativeEntry] = []
    for turn_n in range(30):
        log.append(
            _entry(
                round_=turn_n,
                author="Player" if turn_n % 2 == 0 else "narrator",
                content=f"turn {turn_n} prose " + ("x" * 40),
            )
        )
        ctx = TurnContext(
            character_name="Kael",
            genre="caverns_and_claudes",
            turn_number=turn_n,
            recent_narrative_log=list(log),
        )
        _, registry = await orch.build_narrator_prompt(f"turn {turn_n} action", ctx)
        sec = _section_by_name(registry, orch._narrator.name(), "recent_narrative_context")
        per_turn_sizes.append(len(sec.content) if sec is not None else None)

    # Wiring: post-C1 every turn must register a section (even turn 0,
    # with a 1-entry partial window).
    assert all(s is not None for s in per_turn_sizes), (
        f"recency section unregistered on some turns: "
        f"{[i for i, s in enumerate(per_turn_sizes) if s is None]} — partial-"
        "window registration regressed (C1)"
    )
    section_sizes: list[int] = [s for s in per_turn_sizes if s is not None]

    # Invariant 1 — absolute byte envelope across ALL turns (M4 tie-in).
    assert max(section_sizes) <= SECTION_BUDGET_BYTES, (
        f"recency section blew the byte envelope: "
        f"max={max(section_sizes)} budget={SECTION_BUDGET_BYTES}. "
        "Per-entry truncation (M4) is what holds this line."
    )

    # Invariant 2 — steady-state ratio after the K=2 window fills.
    # Index K-1 = 1 is the first turn with all 2 entries available, so
    # the section reaches the steady-state shape from there on.
    K = 2
    steady_state = section_sizes[K - 1 :]
    assert len(steady_state) >= K, "not enough steady-state turns to measure"
    ratio = max(steady_state) / min(steady_state)
    assert ratio <= 1.5, (
        f"steady-state recency section size oscillated: "
        f"min={min(steady_state)} max={max(steady_state)} "
        f"ratio={ratio:.2f}. Post-K window fill, the section size should "
        "be essentially flat (entry content shape is fixed across the "
        "synthetic fixture)."
    )

    # Sanity: partial-window ramp-up is real but monotone. Turns 0..K-2
    # should grow strictly (more entries = more bytes) until the cap
    # kicks in. This guards against accidentally re-introducing the K-
    # floor gate (which would make turns 0..2 register zero-byte
    # sections or no section at all).
    ramp = section_sizes[: K - 1]
    assert ramp == sorted(ramp), (
        f"partial-window ramp-up is not monotone: {ramp}. Either the gate "
        "regressed or entry ordering is wrong."
    )
    if len(ramp) >= 2:
        assert ramp[0] < ramp[-1], (
            f"partial-window ramp-up is flat: {ramp}. Turn 0 (1 entry) "
            "should be smaller than turn K-2 (K-1 entries)."
        )
