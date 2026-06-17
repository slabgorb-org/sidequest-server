"""Consumer/bridge wiring for player-chosen narrator verbosity + vocabulary
(Story 82-2, ADR-049).

RED premise (audit 2026-06-03): ADR-049's prompt plumbing fires every turn
(``_build_verbosity_section`` / ``_build_vocabulary_section`` in
``orchestrator.py``), but the player-control layer is inert. The sole
``TurnContext`` construction site — ``_build_turn_context`` in
``session_helpers.py`` — hardcodes ``narrator_verbosity="standard"`` /
``narrator_vocabulary="literary"`` regardless of what the session carries. So a
player's choice never reaches the narrator: the CONNECT payload field
(``SessionEventPayload.narrator_verbosity``) is dropped on the floor.

These tests pin the *consumer/bridge* half of ADR-049:

- AC2a — ``_build_turn_context`` reads the session-carried setting
  (``sd.narrator_verbosity`` / ``sd.narrator_vocabulary``) instead of the
  hardcoded literals. Fails on develop (always standard/literary).
- AC2b (No Silent Fallbacks) — when the session carries *no* explicit choice,
  the value comes from ``NarratorVerbosity.default_for_player_count(n)`` using
  the session's live player count — NOT a silent ``"standard"`` literal. A solo
  session (count=1) must default to ``verbose``; develop's hardcode gives
  ``standard`` and fails.
- AC4 (OTEL) — driving the real builder emits a GM-panel-observable span
  recording the active verbosity+vocabulary (lie detector per CLAUDE.md OTEL
  Observability Principle). No such span on develop.
- AC4 (wiring / e2e) — the canonical slider->session->TurnContext->prompt chain:
  a session set to ``concise`` + ``accessible`` flows through the real
  ``_build_turn_context`` into ``Orchestrator.build_narrator_prompt`` and lands
  the *concise* + *accessible* section text in the rendered prompt (and NOT the
  standard section). Fails on develop (renders standard/literary every turn).

**Wiring-test discipline** (CLAUDE.md "No Source-Text Wiring Tests"): the proof
is *behavioral* — drive the real ``_build_turn_context`` / ``build_narrator_prompt``
and observe the populated ``TurnContext`` field, the OTEL span, and the rendered
prompt text. Never a grep of handler source.

**Scope note (see TEA deviation in session file):** the UI half of AC1 — the
``VerbositySlider`` / ``VocabularySlider`` components and their onChange contract
— is covered by ``sidequest-ui`` ``verbosity-vocabulary-sliders.test.tsx``. This
server suite is the canonical slider->...->prompt wiring test (its session-side
input stands in for the CONNECT-delivered slider value).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from sidequest.protocol.enums import NarratorVerbosity, NarratorVocabulary

# Rendered-section discriminators (from orchestrator._build_*_section). These
# are the exact, distinguishing fragments each setting emits — picked so the
# concise/accessible markers can never coincide with the standard/literary ones.
_CONCISE_MARKER = "Maximum 4 sentences"  # verbosity == concise
_STANDARD_MARKER = "maximum 8 sentences"  # verbosity == standard (develop default)
_ACCESSIBLE_MARKER = "8th-grade reading level"  # vocabulary == accessible
_LITERARY_MARKER = "rich but clear prose"  # vocabulary == literary (develop default)


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
            "session_id": "sess-82-2-verbosity-vocab",
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
    loader: GenreLoader,
    *,
    verbosity: object = "__unset__",
    vocabulary: object = "__unset__",
):
    """Build a minimal real ``_SessionData`` with a loaded pack.

    ``verbosity`` / ``vocabulary`` default to the ``"__unset__"`` sentinel — we
    leave the attribute *unset* so the no-silent-fallback path is exercised.
    When a concrete value is passed, we stamp it on ``sd`` as the
    session-carried choice the bridge must read.
    """
    from sidequest.server.session_handler import _SessionData

    pack = loader.load("caverns_and_claudes")
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
    sd.game_slug = "2026-06-05-caverns_and_claudes_sunken_keep-1"
    if verbosity != "__unset__":
        sd.narrator_verbosity = verbosity  # type: ignore[attr-defined]
    if vocabulary != "__unset__":
        sd.narrator_vocabulary = vocabulary  # type: ignore[attr-defined]
    return sd


# ---------------------------------------------------------------------------
# AC2a — the bridge reads the session-carried choice
# ---------------------------------------------------------------------------


def test_build_turn_context_reads_session_verbosity_and_vocabulary(_loader) -> None:
    """An explicit, non-default choice on the session must flow into
    ``TurnContext`` verbatim.

    Fails on develop: ``_build_turn_context`` hardcodes standard/literary and
    ignores the session entirely.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, verbosity=NarratorVerbosity.concise, vocabulary=NarratorVocabulary.epic)
    ctx = _build_turn_context(sd)

    assert ctx.narrator_verbosity == NarratorVerbosity.concise, (
        "TurnContext.narrator_verbosity must reflect the session choice "
        f"(concise), not the hardcoded default; got {ctx.narrator_verbosity!r}"
    )
    assert ctx.narrator_vocabulary == NarratorVocabulary.epic, (
        "TurnContext.narrator_vocabulary must reflect the session choice "
        f"(epic), not the hardcoded default; got {ctx.narrator_vocabulary!r}"
    )


# ---------------------------------------------------------------------------
# AC2b — No Silent Fallbacks: absent choice resolves via the enum helper,
#        keyed to the live player count, NOT a hardcoded literal.
# ---------------------------------------------------------------------------


def test_absent_verbosity_defaults_via_helper_not_standard_literal(_loader) -> None:
    """With NO session choice, verbosity must default to ``verbose`` — the value
    ``default_for_player_count`` returns for a solo/unknown-count session — NOT
    the ``"standard"`` literal develop hardcodes.

    This is the No-Silent-Fallbacks discriminator: ``verbose != standard``, so a
    pass proves the bridge routed the absent value through the enum helper rather
    than snapping to a hardcoded literal. Fails on develop (hardcoded standard).

    Robustness: ``_build_turn_context`` with ``room=None`` reports player count 0
    (the documented safe-empty path), and a solo seat map reports 1 — and
    ``default_for_player_count`` returns ``verbose`` for BOTH 0 and 1. So the
    expected value is ``verbose`` regardless of which count source Dev wires.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader)  # verbosity/vocabulary unset
    assert not hasattr(sd, "narrator_verbosity") or sd.narrator_verbosity is None, (
        "fixture sanity: no explicit verbosity on the session"
    )

    ctx = _build_turn_context(sd)  # room=None -> solo/unknown count

    assert ctx.narrator_verbosity != NarratorVerbosity.standard, (
        "absent verbosity must NOT snap to the 'standard' literal — that is the "
        "hardcoded develop default and a No Silent Fallbacks violation"
    )
    assert ctx.narrator_verbosity == NarratorVerbosity.verbose, (
        "solo/unknown-count default is verbose per ADR-049 "
        "(default_for_player_count(0) == default_for_player_count(1) == verbose)"
    )
    # Same value the production helper yields for both candidate counts — pins
    # that the helper, not a literal, is the source.
    assert ctx.narrator_verbosity == NarratorVerbosity.default_for_player_count(1)
    assert ctx.narrator_verbosity == NarratorVerbosity.default_for_player_count(0)


def test_absent_vocabulary_resolves_via_helper(_loader) -> None:
    """Absent vocabulary resolves via ``NarratorVocabulary.default_for_player_count``
    (== literary for every count per ADR-049).

    Regression guard (literary == develop's hardcode, so GREEN on develop): once
    82-2 wires the bridge, this pins that vocabulary too routes through the helper
    rather than a literal. Paired with the AC2a explicit-``epic`` test above, which
    carries the RED teeth for the vocabulary axis.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader)
    ctx = _build_turn_context(sd)

    assert ctx.narrator_vocabulary == NarratorVocabulary.literary


# ---------------------------------------------------------------------------
# AC4 — OTEL span recording the active settings (GM-panel lie detector)
# ---------------------------------------------------------------------------


def test_build_turn_context_emits_active_settings_span(_loader, otel_capture) -> None:
    """Driving the real builder must emit a span carrying the active
    verbosity+vocabulary so the GM panel can confirm the setting is engaged
    rather than improvised (OTEL Observability Principle).

    Suggested contract for Dev: a span (any name) carrying string attributes
    ``narrator_verbosity`` and ``narrator_vocabulary``. The assertion matches by
    attribute presence so the exact span name stays Dev's choice. Fails on
    develop (no such span emitted).
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(
        _loader, verbosity=NarratorVerbosity.concise, vocabulary=NarratorVocabulary.accessible
    )
    _build_turn_context(sd)

    spans = otel_capture.get_finished_spans()
    settings_spans = [
        s
        for s in spans
        if (s.attributes or {}).get("narrator_verbosity") is not None
        and (s.attributes or {}).get("narrator_vocabulary") is not None
    ]
    assert settings_spans, (
        "AC4: _build_turn_context must emit an OTEL span carrying both "
        "narrator_verbosity and narrator_vocabulary attributes (GM-panel lie "
        f"detector); span names seen: {sorted(s.name for s in spans)}"
    )
    attrs = dict(settings_spans[0].attributes or {})
    assert attrs.get("narrator_verbosity") == "concise", (
        f"span must record the ACTIVE verbosity; got {attrs.get('narrator_verbosity')!r}"
    )
    assert attrs.get("narrator_vocabulary") == "accessible", (
        f"span must record the ACTIVE vocabulary; got {attrs.get('narrator_vocabulary')!r}"
    )


# ---------------------------------------------------------------------------
# AC4 — end-to-end: session choice -> real bridge -> rendered prompt section
# ---------------------------------------------------------------------------


async def test_end_to_end_session_choice_drives_prompt_sections(_loader) -> None:
    """Canonical wiring test (AC4): a session set to concise + accessible flows
    through the real ``_build_turn_context`` into ``build_narrator_prompt`` and
    lands the concise + accessible section text — and NOT the standard section —
    in the rendered narrator prompt.

    Fails on develop: ``_build_turn_context`` hardcodes standard/literary, so the
    rendered prompt always carries the standard + literary markers regardless of
    the player's choice.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(
        _loader,
        verbosity=NarratorVerbosity.concise,
        vocabulary=NarratorVocabulary.accessible,
    )
    ctx = _build_turn_context(sd)
    assert ctx.narrator_verbosity == NarratorVerbosity.concise, "precondition for AC4"

    orch = Orchestrator(client=_make_client())
    prompt, _registry = await orch.build_narrator_prompt("I press deeper into the dark.", ctx)

    assert _CONCISE_MARKER in prompt, (
        "end-to-end: the concise verbosity section must reach the rendered prompt "
        f"(marker {_CONCISE_MARKER!r}) — absent on develop (always standard)"
    )
    assert _ACCESSIBLE_MARKER in prompt, (
        "end-to-end: the accessible vocabulary section must reach the rendered "
        f"prompt (marker {_ACCESSIBLE_MARKER!r}) — absent on develop (always literary)"
    )
    assert _STANDARD_MARKER not in prompt, (
        "the standard verbosity section must NOT render when the player chose "
        "concise — its presence means the hardcoded default is still winning"
    )
    assert _LITERARY_MARKER not in prompt, (
        "the literary vocabulary section must NOT render when the player chose "
        "accessible — its presence means the hardcoded default is still winning"
    )
