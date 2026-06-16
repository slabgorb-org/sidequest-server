"""RED (ADR-144 F4a3 / story 121-8): the Fate chargen SUBMISSION handler (client→server).

TEA RED discovery (2026-06-16, Operator-confirmed): 121-7 wired
``record_fate_chargen`` but it has ZERO production callers, and
``handlers/character_creation.py`` dispatches ``bones_*`` / ``arrange_*`` / ``stock``
phases yet has NO ``fate_*`` branch — a UI submission today falls through to
``_error_msg("Unknown chargen phase: ...")``. So the round-trip is half-wired: the
UI could render the three Fate steps but the server would drop every submission.
121-8 completes the round-trip.

**Fixtures only — a synthetic fate ``CharacterBuilder`` is injected into a Creating
session; no genre pack is loaded** (Operator directive 2026-06-16: do NOT test
against content; content is *validated* via the loader, never used as a fixture).

PINNED SUBMISSION CONTRACT (Dev implements to these phase + field names):
- phase ``fate_aspects_confirm``  → ``fate_high_concept`` + ``fate_trouble`` + ``fate_free_aspects``
- phase ``fate_pyramid_confirm``  → ``fate_allocation`` (``{skill: rating}``)
- phase ``fate_stunts_confirm``   → ``fate_selected_stunts`` (``[name]``)
After the three steps, ``builder.build()`` attaches the INTERACTIVE FateSheet (the
player's pyramid), not the F4a Menu seed. The server re-validates every submission
via ``validate_fate_sheet`` and fails loud on an illegal one (No Silent Fallbacks).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sidequest.game.builder import CharacterBuilder
from sidequest.game.fate_sheet import FateSheet
from sidequest.genre.models.character import CharCreationScene, MechanicalEffects
from sidequest.genre.models.rules import RulesConfig
from sidequest.handlers.character_creation import CharacterCreationHandler
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
)
from sidequest.server.session_handler import WebSocketSessionHandler, _SessionData, _State

# ---------------------------------------------------------------------------
# Synthetic fixtures (no pack)
# ---------------------------------------------------------------------------

NOIR_SKILLS = {
    "Investigate": 3, "Contacts": 2, "Notice": 2, "Deceive": 1, "Shoot": 1,
    "Rapport": 1, "Will": 1, "Stealth": 0, "Athletics": 0, "Fight": 0,
    "Burglary": 0, "Drive": 0, "Empathy": 0, "Provoke": 0,
}  # fmt: skip
HIGH_CONCEPT = "Hard-Boiled Private Eye"
TROUBLE = "Can't Walk Away From a Dame in Trouble"
FREE_ASPECTS = ["A Card With No Name On It", "Owes the Wrong People", "Never Carries a Gun"]
STUNT_NAMES = ["Gun Nut", "Quick on the Draw", "Streetwise"]


def legal_pyramid() -> dict[str, int]:
    """1 @Great(4) / 2 @Good(3) / 3 @Fair(2) / 4 @Average(1) — the SRD default."""
    return {
        "Investigate": 4,
        "Contacts": 3, "Notice": 3,
        "Deceive": 2, "Shoot": 2, "Rapport": 2,
        "Will": 1, "Stealth": 1, "Fight": 1, "Provoke": 1,
    }  # fmt: skip


def fate_rules() -> RulesConfig:
    fate_block: dict[str, object] = {
        "skills": dict(NOIR_SKILLS),
        "refresh": 3,
        "default_high_concept": HIGH_CONCEPT,
        "default_trouble": TROUBLE,
        "stunts": [{"name": n} for n in STUNT_NAMES],
    }
    return RulesConfig.model_validate({"ruleset": "fate", "fate": fate_block})


def fate_step_scene(step: str) -> CharCreationScene:
    return CharCreationScene(
        id=f"fate_{step}",
        title="T",
        narration="N",
        choices=[],
        mechanical_effects=MechanicalEffects(fate_chargen_step=step),  # type: ignore[call-arg]
    )


def _fate_builder() -> CharacterBuilder:
    return CharacterBuilder(
        scenes=[fate_step_scene("aspects"), fate_step_scene("pyramid"), fate_step_scene("stunts")],
        rules=fate_rules(),
    )


def _creating_session(tmp_path, builder: CharacterBuilder) -> WebSocketSessionHandler:
    """A WebSocketSessionHandler parked in Creating state with an injected fate
    builder. Heavy collaborators are mocks — the fate submission path reads only
    the builder (+ its rules) for legality, so the mocks are never touched."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    sd = _SessionData(
        genre_slug="noir_fixture",
        world_slug="",
        player_name="Sam",
        player_id="p1",
        snapshot=GameSnapshot(
            genre_slug="noir_fixture", world_slug="", turn_manager=TurnManager(interaction=1)
        ),
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=MagicMock(),
        orchestrator=MagicMock(),
    )
    sd.builder = builder
    handler = WebSocketSessionHandler(save_dir=tmp_path)
    handler._session_data = sd
    handler._state = _State.Creating
    return handler


def _msg(**fields) -> CharacterCreationMessage:
    return CharacterCreationMessage(payload=CharacterCreationPayload(**fields), player_id="p1")  # type: ignore[arg-type]


def _is_unknown_phase(out: list) -> bool:
    return any(
        isinstance(m, ErrorMessage) and "Unknown chargen phase" in str(m.payload.message)
        for m in out
    )


# ---------------------------------------------------------------------------
# The half-wired guard: every fate confirm phase must be dispatched
# ---------------------------------------------------------------------------


class TestFatePhaseDispatch:
    @pytest.mark.parametrize(
        "phase", ["fate_aspects_confirm", "fate_pyramid_confirm", "fate_stunts_confirm"]
    )
    async def test_fate_confirm_phase_is_not_unknown(self, tmp_path, phase: str) -> None:
        handler = _creating_session(tmp_path, _fate_builder())
        out = await CharacterCreationHandler().handle(handler, _msg(phase=phase))
        assert not _is_unknown_phase(out), (
            f"phase {phase!r} fell through to 'Unknown chargen phase' — the UI's "
            "submission is dropped; the fate dispatch branch is missing"
        )


# ---------------------------------------------------------------------------
# The round-trip: three submissions → build() attaches the INTERACTIVE sheet
# ---------------------------------------------------------------------------


class TestFateSubmissionRoundTrip:
    async def test_three_steps_attach_interactive_legal_sheet(self, tmp_path) -> None:
        handler = _creating_session(tmp_path, _fate_builder())
        h = CharacterCreationHandler()

        await h.handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept=HIGH_CONCEPT,
                fate_trouble=TROUBLE,
                fate_free_aspects=list(FREE_ASPECTS),
            ),
        )
        await h.handle(handler, _msg(phase="fate_pyramid_confirm", fate_allocation=legal_pyramid()))
        await h.handle(
            handler, _msg(phase="fate_stunts_confirm", fate_selected_stunts=list(STUNT_NAMES))
        )

        character = handler._session_data.builder.build("Sam Quaid")
        sheet = character.core.fate_sheet
        assert isinstance(sheet, FateSheet), (
            "after the three fate submissions the builder must attach the interactive "
            "FateSheet through the production build()"
        )
        # The discriminator that proves the INTERACTIVE path ran (not the F4a Menu
        # seed): the player's 10-skill pyramid, not the 14-skill cfg.skills default.
        assert sheet.skills == legal_pyramid()
        assert sheet.skills != NOIR_SKILLS
        assert any(a.kind == "high_concept" and a.text == HIGH_CONCEPT for a in sheet.aspects)
        assert any(a.kind == "trouble" and a.text == TROUBLE for a in sheet.aspects)

    async def test_illegal_pyramid_submission_fails_loud(self, tmp_path) -> None:
        # No Silent Fallbacks (Python rule #1 / SOUL): an illegal allocation must be
        # rejected with violations or raise — never silently accepted or corrected.
        handler = _creating_session(tmp_path, _fate_builder())
        h = CharacterCreationHandler()
        await h.handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept=HIGH_CONCEPT,
                fate_trouble=TROUBLE,
                fate_free_aspects=list(FREE_ASPECTS),
            ),
        )
        bad = legal_pyramid()
        bad["Notice"] = 4  # two skills at Great(4) → rung counts no longer [1,2,3,4]

        rejected = False
        try:
            out = await h.handle(handler, _msg(phase="fate_pyramid_confirm", fate_allocation=bad))
            # Accepted-with-rejection: an error/violations frame came back AND the
            # illegal allocation did not silently become the sheet.
            if any(isinstance(m, ErrorMessage) for m in out):
                rejected = True
            else:
                payload = getattr(out[0], "payload", None)
                violations = getattr(payload, "fate_violations", None)
                rejected = bool(violations)
        except ValueError:
            rejected = True  # fail-loud raise is also acceptable

        assert rejected, "an illegal pyramid submission must not be silently accepted"


# ---------------------------------------------------------------------------
# Reviewer-rework regression (121-8): the default flow must not dead-end, and an
# illegal sheet must surface as a structured error — never an uncaught exception.
# ---------------------------------------------------------------------------


class TestFateReworkRegression:
    async def test_default_flow_zero_free_aspects_builds_legal_sheet(self, tmp_path) -> None:
        # The HIGH the Reviewer caught: free aspects are OPTIONAL at chargen, so the
        # default flow (HC + Trouble seeded, free slots left blank) must produce a
        # LEGAL sheet — not a build() rejection.
        handler = _creating_session(tmp_path, _fate_builder())
        h = CharacterCreationHandler()
        await h.handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept=HIGH_CONCEPT,
                fate_trouble=TROUBLE,
                fate_free_aspects=[],  # default: no free aspects authored
            ),
        )
        await h.handle(handler, _msg(phase="fate_pyramid_confirm", fate_allocation=legal_pyramid()))
        await h.handle(handler, _msg(phase="fate_stunts_confirm", fate_selected_stunts=[]))
        character = handler._session_data.builder.build("Sam Quaid")
        sheet = character.core.fate_sheet
        assert isinstance(sheet, FateSheet)
        assert sheet.skills == legal_pyramid()
        assert sum(1 for a in sheet.aspects if a.kind == "character") == 0

    async def test_empty_high_concept_is_re_prompted_early(self, tmp_path) -> None:
        # An empty mandatory aspect must be rejected EARLY at the aspects step
        # (structured error), not silently advanced to a late build() failure.
        handler = _creating_session(tmp_path, _fate_builder())
        out = await CharacterCreationHandler().handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept="   ",
                fate_trouble=TROUBLE,
                fate_free_aspects=[],
            ),
        )
        assert any(isinstance(m, ErrorMessage) for m in out)
        # Did NOT advance — the builder is still on the aspects step.
        assert handler._session_data.builder.to_scene_message("p1").payload.input_type == (
            "fate_aspects"
        )

    async def test_out_of_catalog_stunt_rejected_at_boundary(self, tmp_path) -> None:
        # Server is the validation authority: an out-of-catalog stunt is rejected at
        # the stunts step, not deferred to build().
        handler = _creating_session(tmp_path, _fate_builder())
        h = CharacterCreationHandler()
        await h.handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept=HIGH_CONCEPT,
                fate_trouble=TROUBLE,
                fate_free_aspects=[],
            ),
        )
        await h.handle(handler, _msg(phase="fate_pyramid_confirm", fate_allocation=legal_pyramid()))
        out = await h.handle(
            handler, _msg(phase="fate_stunts_confirm", fate_selected_stunts=["Time Travel"])
        )
        assert any(isinstance(m, ErrorMessage) for m in out)

    async def test_confirmation_catches_illegal_fate_build(self, tmp_path) -> None:
        # Defense in depth: if an illegal sheet ever reaches build() (e.g. a poisoned
        # choices object), _chargen_confirmation must return a structured ERROR — the
        # FateChargenError must never leak uncaught through the handler.
        from opentelemetry import trace

        from sidequest.game.ruleset.fate_chargen import FateChargenChoices

        handler = _creating_session(tmp_path, _fate_builder())
        h = CharacterCreationHandler()
        # Walk to confirmation with a legal sheet...
        await h.handle(
            handler,
            _msg(
                phase="fate_aspects_confirm",
                fate_high_concept=HIGH_CONCEPT,
                fate_trouble=TROUBLE,
                fate_free_aspects=[],
            ),
        )
        await h.handle(handler, _msg(phase="fate_pyramid_confirm", fate_allocation=legal_pyramid()))
        await h.handle(handler, _msg(phase="fate_stunts_confirm", fate_selected_stunts=[]))
        builder = handler._session_data.builder
        # ...then poison the recorded choices with an illegal (empty HC) sheet.
        builder.record_fate_chargen(
            FateChargenChoices(
                high_concept="",
                trouble=TROUBLE,
                free_aspects=[],
                pyramid=legal_pyramid(),
                stunts=[],
            )
        )
        out = await handler._chargen_confirmation(
            builder, handler._session_data, "p1", trace.get_current_span()
        )
        assert any(isinstance(m, ErrorMessage) for m in out), (
            "an illegal Fate sheet at build() must surface as a structured ERROR, "
            "not leak an uncaught FateChargenError"
        )
