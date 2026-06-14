"""RED tests for Story 116-2 (F2b) — the Fate narrator prompt section + agency directive.

Plan §4 Step 2: `build_narrator_prompt` registers a Late/State `fate_state` section that
renders the Fate projection (skills + fate points + character/scene aspects) plus an
invokable-aspect directive instructing the narrator to PROPOSE invokes, never auto-spend a
player's fate point (SOUL "Agency" / "The Test").

INJECTION CONTRACT (TEA Delivery Finding — see session file): `TurnContext` has NO
`snapshot` field (verified against orchestrator.py). The plan's draft
`build_fate_projection(context.snapshot)` cannot work as written. The narrator section must
read a PRE-BUILT projection injected onto `TurnContext`, exactly as the magic path injects
`magic_state` (`tests/magic/test_narrator_pre_prompt.py`). These tests pin that contract as
a new `TurnContext.fate_state: dict | None` field carrying `build_fate_projection`'s output,
populated by the session handler (which owns the snapshot). The Architect ratifies the field
name at spec-check; if it changes, only the construction lines here change.

Covers:
  * AC-2 — section present (with aspect text + fate points) when Fate state is injected.
  * AC-3 — no section when Fate state is absent (the non-Fate-pack turn).
  * AC-7 — the directive instructs propose/offer, never auto-spend.

All FAIL today: `TurnContext` has no `fate_state` field and no `fate_state` section /
`_build_fate_state_section` helper exists (RED).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sidequest.agents.claude_client import ClaudeClient
from sidequest.agents.orchestrator import Orchestrator, TurnContext

NARRATOR = "narrator"

_PROJECTION: dict[str, Any] = {
    "skills": {"Vance": {"Fight": 3, "Notice": 2}},
    "fate_points": {"Vance": 3},
    "character_aspects": {"Vance": ["Last Honest Cop in Vega"]},
    "scene_aspects": ["Overturned Table"],
    "active_conflict": True,
}


def _orch() -> Orchestrator:
    async def _spawn(*_a: Any, **_k: Any):
        class _P:
            returncode = 0

            async def communicate(self):
                return b'{"result":"x","session_id":"s","usage":{}}', b""

            def kill(self): ...
            async def wait(self):
                return 0

        return _P()

    return Orchestrator(client=ClaudeClient(spawn_fn=_spawn))


def _section_names(registry) -> list[str]:
    return [s.name for s in registry.registry(NARRATOR)]


def _section(registry, name: str):
    for s in registry.registry(NARRATOR):
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# AC-2 — section present when Fate state is injected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fate_state_section_present_when_projection_injected() -> None:
    context = TurnContext(
        character_name="Vance",
        genre="pulp_noir",
        pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
        fate_state=_PROJECTION,
    )
    prompt, registry = await _orch().build_narrator_prompt("I lean on the suspect", context)

    section = _section(registry, "fate_state")
    assert section is not None, f"no fate_state section registered; saw {_section_names(registry)}"

    # The section is dynamic per-turn → must NOT ride the cached primacy band.
    from sidequest.agents.prompt_framework.types import AttentionZone

    assert section.zone in (AttentionZone.Valley, AttentionZone.Late, AttentionZone.Recency), (
        f"fate_state must be a dynamic (non-cached) zone, got {section.zone}"
    )

    # The projected facts reach the composed prompt.
    assert "Last Honest Cop in Vega" in prompt
    assert "Overturned Table" in prompt
    assert "3" in prompt  # the fate-point count is surfaced


# ---------------------------------------------------------------------------
# AC-3 — no section when Fate state is absent (non-Fate-pack turn).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_fate_state_section_when_absent() -> None:
    context = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        pack=SimpleNamespace(rules=SimpleNamespace(ruleset="wwn")),
        fate_state=None,
    )
    prompt, registry = await _orch().build_narrator_prompt("look around", context)
    assert _section(registry, "fate_state") is None, (
        "fate_state section leaked onto a non-Fate turn"
    )
    assert "Last Honest Cop in Vega" not in prompt


# ---------------------------------------------------------------------------
# AC-7 — the directive instructs propose/offer, never auto-spend (SOUL Agency).
# ---------------------------------------------------------------------------


def test_fate_state_section_renders_facts_and_agency_directive() -> None:
    from sidequest.agents.orchestrator import _build_fate_state_section

    text = _build_fate_state_section(_PROJECTION)
    assert text, "section builder returned empty text for a populated projection"

    # Surfaces the invokable aspects + the live fate-point economy.
    assert "Last Honest Cop in Vega" in text
    assert "Overturned Table" in text

    lower = text.lower()
    # Surfaces invokes ...
    assert "invok" in lower, "directive does not surface invokable aspects"
    # ... as a PROPOSAL ...
    assert any(w in lower for w in ("propose", "offer", "suggest", "remind")), (
        "directive must frame invokes/compels as a proposal to the player"
    )
    # ... and forbids the narrator spending/applying on the player's behalf (The Test).
    assert any(
        phrase in lower
        for phrase in (
            "do not spend",
            "never spend",
            "not spend",
            "without spending",
            "player's choice",
            "player chooses",
            "do not invoke",
            "never invoke",
        )
    ), "directive must forbid the narrator from auto-spending a fate point / auto-invoking"


def test_fate_state_section_empty_for_empty_projection() -> None:
    """No PC has a Fate sheet → loud-absent (empty string → section skipped), never a
    blank header (plan §4 Step 2)."""
    from sidequest.agents.orchestrator import _build_fate_state_section

    empty = {
        "skills": {},
        "fate_points": {},
        "character_aspects": {},
        "scene_aspects": [],
        "active_conflict": False,
    }
    assert _build_fate_state_section(empty) == ""
