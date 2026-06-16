"""Consumer/bridge wiring for the per-session pacing hint (Story 81-3, ADR-025).

RED premise (audit 2026-06-03): ``TurnContext.pacing_hint`` is declared
(``orchestrator.py``) and consumed (``register_pacing_section`` fires the
``## Pacing Guidance`` section when it is non-None), but the sole construction
site — ``_build_turn_context`` in ``session_helpers.py`` — never sets it. So it
is ``None`` every turn, the ``if context.pacing_hint is not None`` guard never
passes, and the narrator improvises pacing with zero mechanical backing. 81-2
put a live, accumulating ``TensionTracker`` on ``_SessionData``; this story is
the bridge that reads it.

These tests pin the *consumer/bridge* half of ADR-024/025:

- AC1 — ``_build_turn_context`` derives ``pacing_hint`` from the session
  ``TensionTracker`` via ``pacing_hint(thresholds)`` using the active genre's
  ``DramaThresholds``; non-None when accumulated tension warrants it, and the
  neutral/fresh case is handled without error.
- AC1 (genre) — the thresholds come from the *pack*, not hardcoded defaults
  (proved by a drama_weight that crosses a delivery boundary differently under
  the pack's thresholds than under the type defaults).
- AC3 — the computed hint is emitted as a GM-panel-observable OTEL span.
- AC4 — end-to-end: tracker state → real ``_build_turn_context`` →
  ``Orchestrator.build_narrator_prompt`` → the ``## Pacing Guidance`` section is
  present in the rendered prompt. Fails on current ``develop`` (hint always
  None → section never present).

**What these tests do NOT cover (by design):** the orchestrator-side guard +
``register_pacing_section`` injection (AC2's "absent when None" half) is already
exhaustively covered by ``tests/agents/test_orchestrator_pacing_wiring.py``, and
the story forbids touching ``register_pacing_section``. AC4 strengthens the
present-case through the real bridge; the None-case guard stays with that suite.
See the TEA deviation entries in the session file.

**Wiring-test discipline.** Per the server's "No Source-Text Wiring Tests" rule
(CLAUDE.md), the proof is *behavioral* — drive the real ``_build_turn_context``
and observe (a) the populated ``TurnContext.pacing_hint`` object and (b) the
OTEL span — never a grep of handler source. AC4 additionally drives the real
``build_narrator_prompt`` so the section is observed in the rendered prompt, not
asserted via source shape.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator
from sidequest.game.session import GameSnapshot
from sidequest.game.tension_tracker import DeliveryMode, PacingHint
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import DEFAULT_GENRE_PACK_SEARCH_PATHS, GenreLoader
from sidequest.genre.models.ocean import DramaThresholds

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_client() -> ClaudeClient:
    """A canned ClaudeClient — build_narrator_prompt assembles, never spawns,
    so the fake process is only needed to construct the Orchestrator.
    """
    payload = json.dumps(
        {
            "type": "result",
            "result": "Narration.",
            "session_id": "sess-81-3-pacing-bridge",
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
    genre_slug: str,
    world_slug: str,
    *,
    stakes: tuple[int, int] | None = None,
):
    """Build a minimal real ``_SessionData`` with a loaded pack and (optionally)
    a tension tracker primed via ``update_stakes(current, max)``.

    ``update_stakes`` gives a *stable* drama_weight (no decay), so the derived
    hint is deterministic across the test.
    """
    from sidequest.server.session_handler import _SessionData

    pack = loader.load(genre_slug)
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug=world_slug,
        turn_manager=TurnManager(interaction=3),
    )
    snap.character_locations["Rux"] = "Main Hall"
    snap.player_seats["player:Rux"] = "Rux"
    repo = MagicMock()
    repo.recent_narrative.return_value = []
    sd = _SessionData(
        genre_slug=genre_slug,
        world_slug=world_slug,
        player_name="Rux",
        player_id="player:Rux",
        snapshot=snap,
        repository=repo,
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    sd.game_slug = f"2026-06-03-{genre_slug}_{world_slug}-1"
    if stakes is not None:
        sd.tension_tracker.update_stakes(*stakes)
    return sd


# ---------------------------------------------------------------------------
# AC1 — the bridge derives pacing_hint from the session tracker
# ---------------------------------------------------------------------------


def test_build_turn_context_derives_pacing_hint_from_session_tracker(_loader) -> None:
    """With accumulated tension on the session tracker, the real
    ``_build_turn_context`` must populate ``TurnContext.pacing_hint`` with the
    tracker's computed hint.

    Fails on current ``develop``: the construction site omits ``pacing_hint``,
    so it is None regardless of tracker state.
    """
    from sidequest.server.session_handler import _build_turn_context

    # update_stakes(1, 10) -> stakes 0.9 -> drama_weight 0.9 (high, stable).
    sd = _make_sd(_loader, "caverns_and_claudes", "sunken_keep", stakes=(1, 10))
    assert sd.tension_tracker.drama_weight() == pytest.approx(0.9), (
        "fixture sanity: primed tracker should carry high drama"
    )

    ctx = _build_turn_context(sd)

    assert ctx.pacing_hint is not None, (
        "_build_turn_context must derive TurnContext.pacing_hint from the session "
        "TensionTracker (ADR-025 / story 81-3) — it is None on current develop"
    )
    assert isinstance(ctx.pacing_hint, PacingHint)
    # caverns ships no pacing.yaml -> pack.drama_thresholds is None -> defaults.
    expected = sd.tension_tracker.pacing_hint(DramaThresholds())
    assert ctx.pacing_hint == expected, (
        "the derived hint must equal tracker.pacing_hint(thresholds): "
        f"got {ctx.pacing_hint}, expected {expected}"
    )
    # High drama -> streaming delivery, several sentences (not the trivial floor).
    assert ctx.pacing_hint.delivery_mode == DeliveryMode.Streaming
    assert ctx.pacing_hint.target_sentences >= 4


def test_pacing_hint_uses_genre_drama_thresholds_not_hardcoded(_loader) -> None:
    """The hint must be computed with the *pack's* DramaThresholds, not hardcoded
    type defaults.

    drama_weight 0.67 straddles a delivery boundary: under the type defaults
    (streaming_min 0.70) it is ``Sentence``; under mutant_wasteland's authored
    thresholds (streaming_min 0.65) it is ``Streaming``. Asserting Streaming
    proves the bridge read the pack — a hardcoded-defaults implementation would
    yield Sentence and fail here.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, "mutant_wasteland", "flickering_reach", stakes=(33, 100))
    assert sd.tension_tracker.drama_weight() == pytest.approx(0.67, abs=1e-9)
    assert sd.genre_pack.drama_thresholds is not None, (
        "fixture sanity: mutant_wasteland ships pacing.yaml"
    )

    ctx = _build_turn_context(sd)

    assert ctx.pacing_hint is not None
    assert ctx.pacing_hint == sd.tension_tracker.pacing_hint(sd.genre_pack.drama_thresholds)
    assert ctx.pacing_hint.delivery_mode == DeliveryMode.Streaming, (
        "delivery_mode must reflect the PACK's thresholds (streaming_min 0.65), "
        "not hardcoded defaults (which would give Sentence at drama_weight 0.67)"
    )
    # Guard against a false pass: the type defaults really would differ here.
    assert (
        sd.tension_tracker.pacing_hint(DramaThresholds()).delivery_mode == DeliveryMode.Sentence
    ), "discriminator sanity: defaults must give a different mode than the pack"


def test_pacing_hint_falls_back_to_defaults_when_pack_has_no_thresholds(_loader) -> None:
    """A pack with no authored pacing.yaml (``drama_thresholds is None``) must
    still produce a hint, using DramaThresholds() defaults — caverns_and_claudes
    is the default pack and the feature must work there without error.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, "caverns_and_claudes", "sunken_keep", stakes=(33, 100))
    assert sd.genre_pack.drama_thresholds is None, (
        "fixture sanity: caverns_and_claudes ships no pacing.yaml"
    )

    ctx = _build_turn_context(sd)  # must not raise

    assert ctx.pacing_hint is not None
    assert ctx.pacing_hint == sd.tension_tracker.pacing_hint(DramaThresholds())
    # drama_weight 0.67 under defaults (streaming_min 0.70) -> Sentence.
    assert ctx.pacing_hint.delivery_mode == DeliveryMode.Sentence


def test_neutral_tracker_is_handled_without_error(_loader) -> None:
    """A fresh session (tracker at zero, no meaningful signal) must build a
    TurnContext without raising. Per AC1 the hint "may legitimately be None";
    if the bridge does set it, it must equal the tracker's own computation.

    Regression guard (already green on develop where the field is None): pins
    that a future change can't crash on the neutral path or invent a hint that
    disagrees with the tracker.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, "caverns_and_claudes", "sunken_keep", stakes=None)
    assert sd.tension_tracker.drama_weight() == 0.0

    ctx = _build_turn_context(sd)  # must not raise

    if ctx.pacing_hint is not None:
        assert ctx.pacing_hint == sd.tension_tracker.pacing_hint(DramaThresholds()), (
            "a non-None neutral hint must still match the tracker's computation, not be fabricated"
        )


# ---------------------------------------------------------------------------
# AC3 — the computed hint is emitted as a GM-panel-observable OTEL span
# ---------------------------------------------------------------------------


def test_build_turn_context_emits_pacing_hint_span(_loader, otel_capture) -> None:
    """Driving the real ``_build_turn_context`` must emit a ``pacing.*`` OTEL
    span recording the computed drama_weight — the GM-panel lie detector for
    ADR-025 (mirrors how the same function emits ``npc.working_set``).

    Suggested contract for Dev: span ``pacing.hint_computed`` with attributes
    ``drama_weight`` / ``target_sentences`` / ``delivery_mode``. The assertion
    matches any ``pacing``-prefixed span carrying a numeric ``drama_weight`` so
    the exact suffix stays Dev's choice.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, "caverns_and_claudes", "sunken_keep", stakes=(1, 10))
    expected_dw = sd.tension_tracker.drama_weight()

    _build_turn_context(sd)

    spans = otel_capture.get_finished_spans()
    pacing_spans = [s for s in spans if s.name.startswith("pacing")]
    assert pacing_spans, (
        "AC3: _build_turn_context must emit a pacing.* OTEL span recording the "
        "computed hint (GM-panel lie detector, ADR-025); "
        f"span names seen: {sorted(s.name for s in spans)}"
    )
    attrs = dict(pacing_spans[0].attributes or {})
    assert attrs.get("drama_weight") == pytest.approx(expected_dw, abs=1e-6), (
        "the pacing span must carry the computed drama_weight so the GM panel can "
        f"verify the hint is real; span attributes: {attrs}"
    )


# ---------------------------------------------------------------------------
# AC4 — end-to-end: tracker -> real bridge -> rendered [PACING] section
# ---------------------------------------------------------------------------


async def test_end_to_end_tracker_drives_pacing_section_in_prompt(_loader) -> None:
    """The canonical wiring test (AC4): real tracker state flows through the real
    ``_build_turn_context`` into ``Orchestrator.build_narrator_prompt`` and lands
    as the ``## Pacing Guidance`` section in the rendered narrator prompt.

    Fails on current ``develop``: ``_build_turn_context`` leaves ``pacing_hint``
    None, the orchestrator guard skips ``register_pacing_section``, and the
    section never appears.
    """
    from sidequest.server.session_handler import _build_turn_context

    sd = _make_sd(_loader, "caverns_and_claudes", "sunken_keep", stakes=(1, 10))
    ctx = _build_turn_context(sd)
    assert ctx.pacing_hint is not None, "bridge must set the hint (precondition for AC4)"
    expected_directive = ctx.pacing_hint.narrator_directive()

    orch = Orchestrator(client=_make_client())
    prompt, _registry = await orch.build_narrator_prompt("I press deeper into the dark.", ctx)

    assert "## Pacing Guidance" in prompt, (
        "end-to-end: the tracker-derived hint must reach the rendered narrator "
        "prompt as the pacing section — absent on develop (hint always None)"
    )
    assert expected_directive in prompt, (
        "the rendered prompt must contain the hint's narrator_directive() text"
    )
