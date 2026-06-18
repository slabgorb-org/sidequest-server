"""Drama-aware narrator ``<length-limit>`` cap (Story 126-11, SOUL
"Cost Scales with Drama").

RED premise (narrator-prompt deep-dive 2026-06-18): the narrator's hard
``<length-limit>`` cap is *flat*. ``_build_verbosity_section`` (orchestrator.py)
renders a fixed ``maximum 8 sentences and 800 characters`` for every
``standard`` turn — a climactic reveal and a walk to the tavern get the exact
same ceiling. It never widens for drama, so it strangles plot beats; and it
never tightens for a quiet turn, so it spends prose budget on nothing.

Meanwhile the engine ALREADY computes the signal this cap should ride:
``TensionTracker.drama_weight()`` (0.0-1.0) lands on every turn as
``TurnContext.pacing_hint.drama_weight`` (session_helpers.py). Today that
weight only drives a *soft* ``[PACING]`` "target approximately N sentences"
directive — it never touches the HARD cap. Story 126-11 wires the existing
drama weight into the hard cap so high-weight turns get room and quiet turns
stay cheap. This is "wire up what exists", not new signal plumbing.

These tests pin the contract:

- AC1/AC2 — the rendered ``<length-limit>`` cap SCALES with
  ``drama_weight``: high drama widens it beyond the 8/800 baseline, quiet drama
  tightens it below, and the scaling is monotonic. (Exact tier magic-numbers —
  AC2's "e.g. 12/1200, 6/600" — are left a Dev tuning choice; the tests pin the
  *direction* + the baseline anchor, not the literals.)
- AC5 / regression — when the drama weight cannot be derived (``pacing_hint is
  None`` on a legacy / bare ``TurnContext``), the cap stays the EXACT 8/800
  develop baseline. No silent change for drama-less call sites.
- AC3 — a ``narrator.verbosity_tier`` OTEL span records the chosen tier, the
  weight, and the cap (sentences + chars) so the GM panel can confirm the tier
  matches the turn's drama (lie detector per CLAUDE.md OTEL Observability
  Principle). The span's caps MUST match the cap the prompt actually renders.
- AC4 (wiring / e2e) — the drama weight flows from the real
  ``TensionTracker`` through the real ``_build_turn_context`` into
  ``build_narrator_prompt`` and lands a scaled cap in the rendered prompt. A
  high-drama session widens the cap; a quiet session tightens it.
- Verbosity + language (Mortal's directive 2026-06-18) — the player's
  verbosity mode and vocabulary ("language") setting are first-class here, not
  an afterthought. Drama scaling composes with BOTH; see the design decision
  below.

**Wiring-test discipline** (CLAUDE.md "No Source-Text Wiring Tests"): every
assertion is *behavioral* — drive the real ``_build_turn_context`` /
``build_narrator_prompt`` and read the rendered ``<length-limit>`` text and the
emitted OTEL span. Never a grep of orchestrator.py source.

**Design decision — "Base + scale all modes" (Mortal, 2026-06-18; see TEA
deviation in session file).** The player's verbosity mode sets the BASE cap and
drama scales quiet<->climax AROUND that base, for EVERY mode (not just
``standard``):

    drama ->      quiet    normal   climax
    concise        <4/400    4/400    >4/400
    standard       <8/800    8/800    >8/800
    verbose       <10/1000  10/1000  >10/1000

The per-mode *normal/base* cap is the current develop literal (concise 4/400,
standard 8/800, verbose 10/1000) and is what an underivable-drama turn renders
(regression anchor). The mode ordering concise < standard < verbose is
preserved at every fixed drama level — the mode sets the relative position, the
drama scales it. The exact scaled magic-numbers (the preview's 3/300, 6/600,
14/1400, ...) are a Dev tuning choice; these tests pin *direction*, *per-mode
base anchor*, and *mode ordering*, not the literals.

The vocabulary / "language" axis (accessible / literary / epic) is ORTHOGONAL:
drama changes length only and never alters which vocabulary section renders —
pinned by ``test_drama_does_not_change_vocabulary_section``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.game.session import GameSnapshot
from sidequest.game.tension_tracker import PacingHint, TensionTracker
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from sidequest.genre.models.ocean import DramaThresholds
from sidequest.protocol.enums import NarratorVerbosity, NarratorVocabulary

# The flat develop cap for the ``standard`` mode — the anchor everything scales
# around. ``_STANDARD_MARKER`` is the exact develop section fragment (shared with
# test_verbosity_vocabulary_turn_context_wiring.py).
_BASELINE_SENTENCES = 8
_BASELINE_CHARS = 800
_STANDARD_MARKER = "maximum 8 sentences"  # develop standard section

# Per-mode NORMAL/base caps = current develop literals. Drama scales around
# these; an underivable-drama turn renders exactly these (regression anchor).
_CONCISE_BASE = (4, 400)
_STANDARD_BASE = (_BASELINE_SENTENCES, _BASELINE_CHARS)
_VERBOSE_BASE = (10, 1000)

# Vocabulary / "language" section discriminators (from _build_vocabulary_section)
# — orthogonal to length; drama must never flip which one renders.
_ACCESSIBLE_MARKER = "8th-grade reading level"  # vocabulary == accessible
_LITERARY_MARKER = "rich but clear prose"  # vocabulary == literary (default)
_EPIC_MARKER = "mythic diction"  # vocabulary == epic


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_client() -> ClaudeClient:
    """Canned ClaudeClient — build_narrator_prompt only assembles, never spawns;
    the fake process exists solely to construct the Orchestrator."""
    payload = json.dumps(
        {
            "type": "result",
            "result": "Narration.",
            "session_id": "sess-126-11-drama-cap",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "total_cost_usd": 0.0,
            "num_turns": 1,
        }
    )

    class _FakeProcess:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.stdout.encode(), b""

    async def spawn_fn(
        command: str, *args: str, env: object = None, **kwargs: object
    ) -> _FakeProcess:
        return _FakeProcess(stdout=payload)

    return ClaudeClient(spawn_fn=spawn_fn)


@pytest.fixture(scope="module")
def _loader() -> GenreLoader:
    return GenreLoader(DEFAULT_GENRE_PACK_SEARCH_PATHS)


def _make_sd(
    _loader: GenreLoader,
    *,
    drama: float = 0.0,
    verbosity: NarratorVerbosity = NarratorVerbosity.standard,
    vocabulary: NarratorVocabulary | None = None,
):
    """Build a minimal real ``_SessionData`` with a loaded pack, the tension
    tracker seeded to ``drama`` (via the action track), and the player's
    ``verbosity`` mode (default ``standard``) + optional ``vocabulary``.

    Seeding through ``TensionTracker.with_values`` means the resulting
    ``ctx.pacing_hint.drama_weight`` is the real, tracker-derived weight — so
    the e2e tests prove the *wiring* (tracker -> TurnContext -> prompt), not a
    hand-set hint.
    """
    from sidequest.server.session_handler import _SessionData

    pack = _loader.load("caverns_and_claudes")
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Rux"] = "Main Hall"
    snap.player_seats["player:Rux"] = "Rux"
    repo = MagicMock()
    repo.recent_narrative.return_value = []
    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        player_name="Rux",
        player_id="player:Rux",
        snapshot=snap,
        repository=repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.game_slug = "2026-06-18-caverns_and_claudes_sunken_keep-1"
    # Pin the player's verbosity mode explicitly (a solo session would otherwise
    # default to ``verbose`` via default_for_player_count, masking which mode a
    # test means to exercise).
    sd.narrator_verbosity = verbosity  # type: ignore[attr-defined]
    if vocabulary is not None:
        sd.narrator_vocabulary = vocabulary  # type: ignore[attr-defined]
    # Seed the real per-session tracker so drama_weight == ``drama``.
    sd.tension_tracker = TensionTracker.with_values(drama, 0.0)
    return sd


def _hint(drama: float) -> PacingHint:
    """A real ``PacingHint`` carrying ``drama_weight == drama`` (the other
    fields are computed by the tracker and are irrelevant to the hard cap)."""
    return TensionTracker.with_values(drama, 0.0).pacing_hint(DramaThresholds())


def _length_limit_block(prompt: str) -> str:
    """Extract the REAL verbosity ``<length-limit> ... </length-limit>`` block —
    the only true tag pair (rendered by ``_build_verbosity_section``).

    ``output_style.md`` line 1 mentions the literal token ``<length-limit>`` as
    a prose reference ("The <length-limit> is a HARD CAP ...") with NO matching
    closer, and its line 3 says "2-3 sentences". A naive ``find('<length-limit>')``
    grabs that reference and spans to the verbosity closer, so the cap parse
    reads "3" from output_style instead of the real cap. Anchor on the closer,
    then take the NEAREST opener before it — that is the verbosity section's real
    tag. This also keeps the soft ``[PACING]`` "Target approximately N
    sentence(s)" directive (which lives outside the block) out of the parse."""
    end = prompt.find("</length-limit>")
    assert end != -1, (
        "rendered narrator prompt must contain a </length-limit> closer (the verbosity section)"
    )
    start = prompt.rfind("<length-limit>", 0, end)
    assert start != -1 and start < end, f"no opening <length-limit> before the closer; end={end}"
    return prompt[start:end]


def _parse_caps(prompt: str) -> tuple[int, int]:
    """Parse the (sentences, characters) hard cap out of the length-limit block.

    Reads the cap the narrator is actually TOLD — the behavioral surface of the
    feature — rather than grepping source. The develop standard block parses to
    (8, 800)."""
    block = _length_limit_block(prompt)
    s = re.search(r"(\d+)\s+sentence", block)
    c = re.search(r"(\d+)\s+character", block)
    assert s is not None, f"no '<n> sentence' cap in length-limit block: {block!r}"
    assert c is not None, f"no '<n> character' cap in length-limit block: {block!r}"
    return int(s.group(1)), int(c.group(1))


async def _render(ctx: TurnContext) -> str:
    orch = Orchestrator(client=_make_client())
    prompt, _registry = await orch.build_narrator_prompt("I press deeper into the dark.", ctx)
    return prompt


def _tier_spans(otel_capture) -> list:
    """Spans carrying the structured verbosity-tier caps. Matched by attribute
    presence (cap_sentences + cap_chars) so the exact span name stays Dev's
    choice — suggested: ``narrator.verbosity_tier``."""
    return [
        s
        for s in otel_capture.get_finished_spans()
        if (s.attributes or {}).get("cap_sentences") is not None
        and (s.attributes or {}).get("cap_chars") is not None
    ]


def _span_weight(attrs: dict) -> float | None:
    """The recorded drama weight — accept ``weight`` or ``drama_weight`` (the
    sibling pacing span already uses ``drama_weight``)."""
    raw = attrs.get("weight", attrs.get("drama_weight"))
    return None if raw is None else float(raw)


# ---------------------------------------------------------------------------
# AC5 / regression — underivable weight keeps the EXACT 8/800 baseline.
# (GREEN on develop; must STAY green — proves drama-less call sites unchanged.)
# ---------------------------------------------------------------------------


async def test_underivable_weight_renders_baseline_cap(_loader) -> None:
    """A standard-mode TurnContext with NO drama signal (``pacing_hint is
    None`` — the legacy / bare-context path) renders the exact develop baseline:
    8 sentences / 800 characters. No silent widening or tightening when drama
    can't be derived (AC5)."""
    sd = _make_sd(_loader, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    ctx = _build_turn_context(sd)
    # Null the drama signal to model a context where weight cannot be derived.
    ctx = dataclasses.replace(ctx, pacing_hint=None)

    prompt = await _render(ctx)

    assert _STANDARD_MARKER in prompt, (
        "underivable drama must fall back to the develop standard section "
        f"(marker {_STANDARD_MARKER!r})"
    )
    assert _parse_caps(prompt) == (_BASELINE_SENTENCES, _BASELINE_CHARS), (
        f"underivable drama must render the EXACT 8/800 baseline; got {_parse_caps(prompt)}"
    )


# ---------------------------------------------------------------------------
# AC1/AC2 — the cap scales with drama_weight (RED on develop: always 8/800).
# ---------------------------------------------------------------------------


async def test_high_drama_widens_cap_beyond_baseline(_loader) -> None:
    """A high-drama turn (climactic / confrontation) must get MORE room than the
    flat baseline — both the sentence cap and the character cap exceed 8/800.

    Fails on develop: the standard section is fixed at 8/800 regardless of
    drama, so the climactic reveal is strangled."""
    sd = _make_sd(_loader)
    from sidequest.server.session_handler import _build_turn_context

    ctx = dataclasses.replace(_build_turn_context(sd), pacing_hint=_hint(0.9))
    sentences, chars = _parse_caps(await _render(ctx))

    assert sentences > _BASELINE_SENTENCES, (
        f"high drama must widen the sentence cap above the {_BASELINE_SENTENCES} "
        f"baseline (SOUL Cost-Scales-with-Drama); got {sentences}"
    )
    assert chars > _BASELINE_CHARS, (
        f"high drama must widen the character cap above the {_BASELINE_CHARS} baseline; got {chars}"
    )


async def test_low_drama_tightens_cap_below_baseline(_loader) -> None:
    """A quiet / exploration turn (low drama) must stay CHEAPER than the flat
    baseline — tighter sentence and character caps. "A quiet walk through town
    is cheap" (SOUL). This is the distinct sibling of the *underivable* case
    above: here the weight IS derived and is low, so the cap tightens (not the
    8/800 fallback).

    Fails on develop: the standard section is fixed at 8/800 regardless."""
    sd = _make_sd(_loader)
    from sidequest.server.session_handler import _build_turn_context

    ctx = dataclasses.replace(_build_turn_context(sd), pacing_hint=_hint(0.05))
    sentences, chars = _parse_caps(await _render(ctx))

    assert sentences < _BASELINE_SENTENCES, (
        f"quiet drama must tighten the sentence cap below the {_BASELINE_SENTENCES} "
        f"baseline; got {sentences}"
    )
    assert chars < _BASELINE_CHARS, (
        f"quiet drama must tighten the character cap below the {_BASELINE_CHARS} "
        f"baseline; got {chars}"
    )


async def test_cap_scales_monotonically_with_drama(_loader) -> None:
    """Across rising drama the cap is monotonically non-decreasing, and the
    extremes are strictly ordered (high gets strictly more room than quiet) on
    BOTH axes. Pins the *scaling* contract without coupling to exact tier
    literals.

    Fails on develop: all three levels render the same 8/800."""
    sd = _make_sd(_loader)
    from sidequest.server.session_handler import _build_turn_context

    base = _build_turn_context(sd)
    low = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.05))))
    mid = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.5))))
    high = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.95))))

    # Strict ordering at the extremes (the load-bearing claim).
    assert high[0] > low[0] and high[1] > low[1], (
        f"high-drama cap must strictly exceed quiet-drama cap on both axes; low={low} high={high}"
    )
    # Monotonic non-decreasing through the middle (no inversion).
    assert low[0] <= mid[0] <= high[0], (
        f"sentence caps must not invert: {low[0]},{mid[0]},{high[0]}"
    )
    assert low[1] <= mid[1] <= high[1], f"char caps must not invert: {low[1]},{mid[1]},{high[1]}"


# ---------------------------------------------------------------------------
# AC3 — OTEL verbosity-tier span (GM-panel lie detector). RED on develop.
# ---------------------------------------------------------------------------


async def test_verbosity_tier_span_emitted_with_caps(_loader, otel_capture) -> None:
    """Driving the real bridge + prompt for a high-drama turn must emit a span
    recording the chosen verbosity tier so the GM panel can confirm the tier
    tracks the turn's drama (OTEL Observability Principle).

    Contract (attributes; span name is Dev's choice, suggested
    ``narrator.verbosity_tier``):
      - ``tier``          : non-empty string tier name
      - ``weight``        : the drama weight (float; ``drama_weight`` also accepted)
      - ``cap_sentences`` : int sentence cap chosen
      - ``cap_chars``     : int character cap chosen
    For a high-drama turn the recorded caps must exceed the 8/800 baseline —
    the span can't claim a wide tier the cap doesn't reflect.

    Fails on develop: no such span exists."""
    sd = _make_sd(_loader, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    # Drive BOTH the bridge and the prompt so the span is captured regardless of
    # which one Dev chooses as the emit site.
    ctx = _build_turn_context(sd)
    await _render(ctx)

    spans = _tier_spans(otel_capture)
    assert spans, (
        "AC3: a verbosity-tier span carrying cap_sentences + cap_chars must be "
        f"emitted; span names seen: {sorted(s.name for s in otel_capture.get_finished_spans())}"
    )
    attrs = dict(spans[0].attributes or {})
    assert isinstance(attrs.get("tier"), str) and attrs["tier"], (
        f"tier attribute must be a non-empty string; got {attrs.get('tier')!r}"
    )
    weight = _span_weight(attrs)
    assert weight is not None and weight > 0.5, (
        f"span must record the high drama weight (~0.9); got {weight!r}"
    )
    assert int(attrs["cap_sentences"]) > _BASELINE_SENTENCES, (
        f"high-drama tier span must record a sentence cap above {_BASELINE_SENTENCES}; "
        f"got {attrs['cap_sentences']!r}"
    )
    assert int(attrs["cap_chars"]) > _BASELINE_CHARS, (
        f"high-drama tier span must record a char cap above {_BASELINE_CHARS}; "
        f"got {attrs['cap_chars']!r}"
    )


async def test_tier_span_caps_match_rendered_prompt(_loader, otel_capture) -> None:
    """Lie-detector cross-check: the caps the tier span CLAIMS must equal the
    caps the narrator prompt actually renders. A span that reports 12/1200 while
    the prompt still says 8/800 is exactly the prose/mechanics disagreement the
    OTEL principle exists to catch.

    Fails on develop: no span (and the prompt is the flat baseline)."""
    sd = _make_sd(_loader, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    ctx = _build_turn_context(sd)
    prompt = await _render(ctx)

    spans = _tier_spans(otel_capture)
    assert spans, "AC3 cross-check requires the verbosity-tier span to be emitted"
    attrs = dict(spans[0].attributes or {})
    rendered = _parse_caps(prompt)
    assert (int(attrs["cap_sentences"]), int(attrs["cap_chars"])) == rendered, (
        "tier span caps must match the cap rendered in the prompt; "
        f"span=({attrs['cap_sentences']},{attrs['cap_chars']}) prompt={rendered}"
    )


# ---------------------------------------------------------------------------
# AC4 (wiring / e2e) — drama_weight flows tracker -> bridge -> prompt cap.
# ---------------------------------------------------------------------------


async def test_end_to_end_high_drama_session_widens_prompt_cap(_loader) -> None:
    """Canonical wiring test: a session whose real ``TensionTracker`` carries a
    high drama_weight flows through the real ``_build_turn_context`` into
    ``build_narrator_prompt`` and lands a cap wider than 8/800. Proves the
    existing drama signal actually reaches the hard cap — not a hand-set hint.

    Fails on develop: the bridge never feeds drama into the cap."""
    sd = _make_sd(_loader, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    ctx = _build_turn_context(sd)
    assert ctx.pacing_hint is not None and ctx.pacing_hint.drama_weight > 0.5, (
        "precondition: the seeded tracker must yield a high drama_weight on the ctx"
    )
    sentences, chars = _parse_caps(await _render(ctx))
    assert sentences > _BASELINE_SENTENCES and chars > _BASELINE_CHARS, (
        f"high-drama session must widen the rendered cap beyond 8/800; got {(sentences, chars)}"
    )


async def test_end_to_end_quiet_drama_session_tightens_prompt_cap(_loader) -> None:
    """The mirror: a quiet session (fresh tracker, drama_weight 0.0) flows
    through the real bridge and lands a cap tighter than 8/800. Quiet turns stay
    cheap (SOUL Cost-Scales-with-Drama cuts both ways).

    Fails on develop: the bridge renders the flat 8/800."""
    sd = _make_sd(_loader, drama=0.0)
    from sidequest.server.session_handler import _build_turn_context

    ctx = _build_turn_context(sd)
    sentences, chars = _parse_caps(await _render(ctx))
    assert sentences < _BASELINE_SENTENCES and chars < _BASELINE_CHARS, (
        f"quiet-drama session must tighten the rendered cap below 8/800; got {(sentences, chars)}"
    )


# ---------------------------------------------------------------------------
# Verbosity modes — "base + scale all modes" (Mortal 2026-06-18). Each mode's
# base = current develop literal; drama scales around it; ordering preserved.
# ---------------------------------------------------------------------------


async def test_verbose_base_unchanged_when_drama_underivable(_loader) -> None:
    """Regression anchor: ``verbose`` with NO derivable drama renders its exact
    develop base, 10/1000. Drama-less call sites in verbose mode are unchanged.

    Green on develop; must stay green."""
    sd = _make_sd(_loader, verbosity=NarratorVerbosity.verbose, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    ctx = dataclasses.replace(_build_turn_context(sd), pacing_hint=None)
    assert _parse_caps(await _render(ctx)) == _VERBOSE_BASE, (
        f"verbose base must be {_VERBOSE_BASE} when drama underivable; got {_parse_caps(await _render(ctx))}"
    )


async def test_concise_base_unchanged_when_drama_underivable(_loader) -> None:
    """Regression anchor: ``concise`` with NO derivable drama renders its exact
    develop base, 4/400.

    Green on develop; must stay green."""
    sd = _make_sd(_loader, verbosity=NarratorVerbosity.concise, drama=0.9)
    from sidequest.server.session_handler import _build_turn_context

    ctx = dataclasses.replace(_build_turn_context(sd), pacing_hint=None)
    assert _parse_caps(await _render(ctx)) == _CONCISE_BASE, (
        f"concise base must be {_CONCISE_BASE} when drama underivable"
    )


async def test_verbose_mode_scales_with_drama(_loader) -> None:
    """``verbose`` PARTICIPATES in drama scaling (Model A): a high-drama verbose
    turn exceeds the 10/1000 verbose base on both axes, and a quiet verbose turn
    falls below it. The "let it breathe" mode breathes even more at a climax and
    stays cheaper in a lull.

    Fails on develop: verbose is a fixed 10/1000 regardless of drama."""
    sd = _make_sd(_loader, verbosity=NarratorVerbosity.verbose)
    from sidequest.server.session_handler import _build_turn_context

    base = _build_turn_context(sd)
    high = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.95))))
    low = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.05))))

    assert high[0] > _VERBOSE_BASE[0] and high[1] > _VERBOSE_BASE[1], (
        f"high-drama verbose must widen beyond {_VERBOSE_BASE}; got {high}"
    )
    assert low[0] < _VERBOSE_BASE[0] and low[1] < _VERBOSE_BASE[1], (
        f"quiet verbose must tighten below {_VERBOSE_BASE}; got {low}"
    )


async def test_concise_mode_scales_with_drama(_loader) -> None:
    """``concise`` PARTICIPATES in drama scaling (Model A): a high-drama concise
    turn exceeds the 4/400 concise base, a quiet one falls below. Even a
    terse-preferring player gets a little more room for a climax — but concise
    stays the tightest mode (pinned by the ordering test below).

    Fails on develop: concise is a fixed 4/400 regardless of drama."""
    sd = _make_sd(_loader, verbosity=NarratorVerbosity.concise)
    from sidequest.server.session_handler import _build_turn_context

    base = _build_turn_context(sd)
    high = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.95))))
    low = _parse_caps(await _render(dataclasses.replace(base, pacing_hint=_hint(0.05))))

    assert high[0] > _CONCISE_BASE[0] and high[1] > _CONCISE_BASE[1], (
        f"high-drama concise must widen beyond {_CONCISE_BASE}; got {high}"
    )
    assert low[0] < _CONCISE_BASE[0] and low[1] < _CONCISE_BASE[1], (
        f"quiet concise must tighten below {_CONCISE_BASE}; got {low}"
    )


async def test_mode_base_ordering_preserved_under_drama(_loader) -> None:
    """The mode sets the relative base: at ANY fixed drama level the caps stay
    ordered concise < standard < verbose on both axes. Drama scales each mode;
    it never lets a tighter mode overtake a looser one at the same drama.

    Green on develop (the base literals 4<8<10 are already ordered); turns RED
    if Dev's scaling inverts the modes at a shared drama level."""
    from sidequest.server.session_handler import _build_turn_context

    async def _cap(verbosity: NarratorVerbosity, drama: float) -> tuple[int, int]:
        ctx = dataclasses.replace(
            _build_turn_context(_make_sd(_loader, verbosity=verbosity)),
            pacing_hint=_hint(drama),
        )
        return _parse_caps(await _render(ctx))

    for drama in (0.05, 0.95):
        c = await _cap(NarratorVerbosity.concise, drama)
        s = await _cap(NarratorVerbosity.standard, drama)
        v = await _cap(NarratorVerbosity.verbose, drama)
        assert c[0] < s[0] < v[0], (
            f"sentence caps must stay ordered concise<standard<verbose at drama={drama}; "
            f"got concise={c[0]} standard={s[0]} verbose={v[0]}"
        )
        assert c[1] < s[1] < v[1], (
            f"char caps must stay ordered concise<standard<verbose at drama={drama}; "
            f"got concise={c[1]} standard={s[1]} verbose={v[1]}"
        )


# ---------------------------------------------------------------------------
# Language / vocabulary axis — orthogonal to drama (Mortal 2026-06-18).
# ---------------------------------------------------------------------------


async def test_drama_does_not_change_vocabulary_section(_loader) -> None:
    """Drama scales LENGTH only. The vocabulary ("language") section the player
    chose renders identically regardless of drama — a high-drama turn does not
    silently flip ``epic`` to ``literary`` or alter the vocabulary directive.

    Green on develop (drama is ignored entirely there); the teeth are for the
    *feature*: once drama scales the length cap, this guard ensures it left the
    orthogonal language axis untouched. Uses ``epic`` so the marker can never
    coincide with the ``literary`` default."""
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, vocabulary=NarratorVocabulary.epic)
    base = _build_turn_context(sd)
    assert base.narrator_vocabulary == NarratorVocabulary.epic, "precondition: epic on the ctx"

    quiet = await _render(dataclasses.replace(base, pacing_hint=_hint(0.05)))
    climax = await _render(dataclasses.replace(base, pacing_hint=_hint(0.95)))

    for label, prompt in (("quiet", quiet), ("climax", climax)):
        assert _EPIC_MARKER in prompt, (
            f"chosen epic vocabulary must render at {label} drama (marker {_EPIC_MARKER!r})"
        )
        assert _LITERARY_MARKER not in prompt and _ACCESSIBLE_MARKER not in prompt, (
            f"drama must not flip the vocabulary axis at {label} drama "
            "(literary/accessible markers leaked into an epic turn)"
        )

    # And the length cap DID move between the two — proving drama acted on length
    # while leaving language alone.
    assert _parse_caps(climax)[0] > _parse_caps(quiet)[0], (
        "sanity: drama must still scale the length cap while vocabulary is held fixed"
    )
