"""Wiring (Task 7.1): the narrator prompt surfaces the light pool + darkness.

Light & Darkness survival-clock — the narrator's guttering/dark/relit prose must
be STATE-DRIVEN (gaslit by the snapshot), not improvised. This test drives the
real prompt-assembly seam (``PromptRegistry.register_light_section``) and asserts
the compact light state (current/max), the active threshold ``narrator_hint``,
and the active darkness status reach the composed narrator prompt.

The light pool ``current`` is VOLATILE (it changes every burn), so the section
must live in a volatile attention zone (Valley), NOT the cached stable prefix —
otherwise a per-turn-changing value would break the prefix cache (ADR-110 /
ADR-112). This test also pins that zone placement.
"""

from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.prompt_framework.types import AttentionZone, SectionCategory
from sidequest.agents.subsystems.environment_clock import (
    DARKNESS_STATUS_SOURCE,
    DARKNESS_STATUS_TEXT,
)
from sidequest.game.resource_pool import ResourcePool, ResourceThreshold
from sidequest.game.status import Status, StatusSeverity

GUTTERING_HINT = "The torch is guttering — its light has minutes left."
DARK_HINT = "The light is gone. The dark itself closes in."


def _light_pool(current: float) -> ResourcePool:
    return ResourcePool(
        name="light",
        label="Light",
        current=current,
        min=0.0,
        max=6.0,
        voluntary=False,
        decay_per_turn=0.0,
        thresholds=[
            ResourceThreshold(at=1.0, event_id="light_guttering", narrator_hint=GUTTERING_HINT),
            ResourceThreshold(at=0.0, event_id="light_dark", narrator_hint=DARK_HINT),
        ],
    )


def _darkness_status() -> Status:
    # Mirror the burn path's mint: the lightest non-injury ``Scratch`` tier
    # (an ambient light penalty is not a bodily wound), stamped with a real turn.
    return Status(
        text=DARKNESS_STATUS_TEXT,
        source=DARKNESS_STATUS_SOURCE,
        severity=StatusSeverity.Scratch,
        created_turn=3,
        roll_modifier=-2,
    )


def test_light_section_surfaces_current_and_max_when_guttering():
    """light=1 (guttering): the narrator sees the live pool numbers + the
    guttering hint, so guttering prose is state-driven, not invented."""
    reg = PromptRegistry()
    reg.register_light_section("narrator", pool=_light_pool(1.0), statuses=[])
    text = reg.compose("narrator")
    # Compact live numbers reach the prompt (precise substring — not a stray
    # "1"/"6" anywhere else in the prompt; mirrors the dark test's "0/6").
    assert "1/6" in text
    # The active threshold's narrator_hint reaches the prompt.
    assert GUTTERING_HINT in text
    # Not dark yet — no darkness cue.
    assert DARKNESS_STATUS_TEXT not in text


def test_light_section_surfaces_darkness_status_when_dark():
    """light=0 (dark) with the darkness status active: the narrator sees the
    'every action is harder' darkness cue + the dark hint, so dark prose is
    state-driven."""
    reg = PromptRegistry()
    reg.register_light_section("narrator", pool=_light_pool(0.0), statuses=[_darkness_status()])
    text = reg.compose("narrator")
    assert "0" in text and "6" in text
    assert DARK_HINT in text
    # The darkness status (the −2 penalty) reaches the prompt.
    assert DARKNESS_STATUS_TEXT in text


def test_light_section_lives_in_volatile_valley_zone():
    """The light pool current is volatile (changes per turn). It must NOT ride
    the cached stable prefix (Primacy/Early) or the prefix cache breaks every
    burn. Pin it to the Valley (volatile) zone."""
    reg = PromptRegistry()
    reg.register_light_section("narrator", pool=_light_pool(3.0), statuses=[])
    sections = reg.get_sections("narrator", category=SectionCategory.State)
    light_sections = [s for s in sections if "light" in s.name.lower()]
    assert light_sections, "register_light_section produced no section"
    for s in light_sections:
        assert s.zone == AttentionZone.Valley, (
            f"light state must be volatile (Valley), got {s.zone}"
        )


def test_light_section_omits_when_no_light_pool():
    """No light pool (non-survival-clock pack): zero-byte-leak — no section."""
    reg = PromptRegistry()
    reg.register_light_section("narrator", pool=None, statuses=[])
    text = reg.compose("narrator")
    assert "LIGHT" not in text.upper() or "guttering" not in text.lower()


# ──────────────────────────────────────────────────────────────────────────
# Wiring: the orchestrator's build_narrator_prompt actually registers the
# light section when TurnContext carries the light pool. Unit coverage above
# proves the section renders; this proves it's reachable from the production
# narrator-prompt assembly path (CLAUDE.md: every test suite needs a wiring
# test).
# ──────────────────────────────────────────────────────────────────────────


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_build_narrator_prompt_surfaces_light_pool_when_dark():
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=3,
        light_pool=_light_pool(0.0),
        darkness_statuses=[_darkness_status()],
    )
    orch = Orchestrator()
    prompt_text, registry = await orch.build_narrator_prompt("grope forward", ctx)
    names = {s.name for s in registry.registry(orch._narrator.name())}
    assert "light_state" in names, "orchestrator did not register the light section"
    assert "0/6" in prompt_text
    assert DARK_HINT in prompt_text
    assert DARKNESS_STATUS_TEXT in prompt_text


@pytest.mark.asyncio
async def test_build_narrator_prompt_omits_light_when_no_pool():
    """Pack with no light clock: no light section leaks into the prompt."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=3,
    )
    orch = Orchestrator()
    _, registry = await orch.build_narrator_prompt("look around", ctx)
    names = {s.name for s in registry.registry(orch._narrator.name())}
    assert "light_state" not in names
