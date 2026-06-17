"""Fate-pack confirmation summary ([FATE/UX], playtest 2026-06-17).

The chargen confirmation card ("Your Character") rendered the native
Race/Class/Personality/Backstory fields even for a ``ruleset: fate`` pack —
e.g. "Class: I Talk My Way In", "Personality: Anxious" — which are category
errors in Fate and don't reflect the Fate sheet the player just authored via
the interactive aspects/pyramid/stunts flow (121-8). ``render_confirmation_summary``
was Fate-blind: it built ``character_preview`` from ``acc.*_hint`` regardless of
ruleset.

These tests pin the fix: for a fate pack whose interactive choices were
recorded, the confirmation preview is Fate-shaped (High Concept / Trouble /
Aspects / Skills pyramid / Stunts / Refresh / Fate Points) and carries NONE of
the native Class/Personality/Race/Backstory keys. The sheet is rebuilt from the
recorded choices via ``build_fate_sheet`` — the same constructor ``build()``
uses — so the preview matches the wired character by construction.

Synthetic fixtures only (a real ``GenrePack`` is never needed: the Fate branch
early-returns before any pack access), mirroring
``tests/server/test_121_8_fate_chargen_handler.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.builder import CharacterBuilder
from sidequest.genre.models.character import CharCreationScene, MechanicalEffects
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import RulesConfig
from sidequest.server.dispatch.chargen_summary import render_confirmation_summary

# ---------------------------------------------------------------------------
# Synthetic fate fixtures (copied from the 121-8 handler test so this stands alone)
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


def _walked_to_confirmation() -> CharacterBuilder:
    """A fate builder driven through the three interactive steps to Confirmation,
    with the player's choices recorded (the production path)."""
    builder = CharacterBuilder(
        scenes=[fate_step_scene("aspects"), fate_step_scene("pyramid"), fate_step_scene("stunts")],
        rules=fate_rules(),
    )
    builder.apply_fate_aspects(
        high_concept=HIGH_CONCEPT, trouble=TROUBLE, free_aspects=list(FREE_ASPECTS)
    )
    builder.apply_fate_pyramid(legal_pyramid())
    builder.apply_fate_stunts(list(STUNT_NAMES))
    assert builder.is_confirmation(), "the three fate steps must land at Confirmation"
    return builder


def _render(builder: CharacterBuilder, lobby_name: str | None = "Sam Spade") -> dict[str, str]:
    """Render the confirmation summary and return the preview dict (asserts present)."""
    pack = MagicMock(spec=GenrePack)  # never touched on the fate path
    msg = render_confirmation_summary(
        builder, pack, lobby_name=lobby_name, player_id="p1", world_slug=None
    )
    preview = msg.payload.character_preview
    assert preview is not None, "fate confirmation must emit a character_preview"
    return preview


# ---------------------------------------------------------------------------
# The fix: a fate pack's confirmation card shows the Fate sheet, not Class/etc.
# ---------------------------------------------------------------------------


class TestFateConfirmationPreview:
    def test_preview_carries_fate_sheet_keys(self) -> None:
        preview = _render(_walked_to_confirmation())
        for key in ("High Concept", "Trouble", "Aspects", "Skills", "Stunts", "Refresh", "Fate Points"):
            assert key in preview, f"fate confirmation preview missing {key!r}: {list(preview)}"

    def test_preview_drops_native_category_error_keys(self) -> None:
        # "Class"/"Personality"/"Race"/"Backstory" are category errors in Fate —
        # this is the exact regression ([FATE/UX]: "Class: I Talk My Way In",
        # "Personality: Anxious").
        preview = _render(_walked_to_confirmation())
        for key in ("Class", "Personality", "Race", "Backstory"):
            assert key not in preview, f"native key {key!r} leaked onto a fate confirmation card"

    def test_high_concept_and_trouble_are_the_authored_values(self) -> None:
        preview = _render(_walked_to_confirmation())
        assert preview["High Concept"] == HIGH_CONCEPT
        assert preview["Trouble"] == TROUBLE

    def test_free_aspects_listed(self) -> None:
        preview = _render(_walked_to_confirmation())
        for aspect in FREE_ASPECTS:
            assert aspect in preview["Aspects"]

    def test_skills_are_ladder_named_apex_first(self) -> None:
        # Player-facing math (Sebastien/Jade): adjective + signed rung, highest first.
        skills = _render(_walked_to_confirmation())["Skills"]
        assert skills.startswith("Investigate (Great +4)"), skills
        assert "Contacts (Good +3)" in skills
        assert "Will (Average +1)" in skills
        # Mediocre/+0 skills are unplaced and must not appear.
        assert "Athletics" not in skills

    def test_stunts_listed_by_name(self) -> None:
        stunts = _render(_walked_to_confirmation())["Stunts"]
        for name in STUNT_NAMES:
            assert name in stunts

    def test_refresh_and_fate_points_present(self) -> None:
        preview = _render(_walked_to_confirmation())
        # 3 stunts == free_stunts default ⇒ no refresh debit ⇒ refresh stays at base 3.
        assert preview["Refresh"] == "3"
        assert preview["Fate Points"] == "3"

    def test_name_from_lobby_is_shown(self) -> None:
        assert _render(_walked_to_confirmation(), lobby_name="Sam Spade")["Name"] == "Sam Spade"


# ---------------------------------------------------------------------------
# Lie-detector: the OTEL event fires with ruleset="fate"
# ---------------------------------------------------------------------------


def _capture_events(fn) -> list:  # type: ignore[no-untyped-def]
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test_harness"):
        fn()
    finished = exporter.get_finished_spans()
    assert finished, "no span was exported"
    return list(finished[-1].events)


class TestFateConfirmationTelemetry:
    def test_emits_confirmation_rendered_with_ruleset_fate(self) -> None:
        builder = _walked_to_confirmation()
        events = _capture_events(lambda: _render(builder))
        rendered = [e for e in events if e.name == "character_creation.confirmation_rendered"]
        assert rendered, "the fate confirmation must emit the lie-detector event"
        attrs = dict(rendered[-1].attributes or {})
        assert attrs["ruleset"] == "fate"
        assert attrs["aspect_count"] == 5  # HC + Trouble + 3 free
        assert attrs["skill_count"] == 10  # placed rungs of legal_pyramid()
        assert attrs["stunt_count"] == 3
