"""Story 59-1 RED — SDK confrontation ENGAGEMENT tool/prompt path.

See sprint/context/context-story-59-1.md (Architecture Decision, Houlihan
2026-05-22). The bug is NOT "narrator never calls advance_confrontation".
Engagement = the narrator setting the structured ``confrontation`` field
(orchestrator.py:304), consumed by the server at narration_apply.py:2531 to
create the StructuredEncounter. ``advance_confrontation`` only ADVANCES an
already-active encounter and errors if none is active.

On the default ``anthropic_sdk`` backend (ADR-101/102) the narrator gets the
full registry (orchestrator.py:3232/3322), so "registered == offered". The
real gap is that NO offered tool can WRITE ``result.confrontation``:
  - ``apply_world_patch`` carries the sibling game_patch fields but not this one
  - ``advance_confrontation`` can't start (and its description is combat-only)
  - ``generate_encounter`` — where ADR-111/57-4 stranded the social trigger
    criteria — is a stub that always returns a fatal error.

These tests pin the corrected ACs (2, 3, 4). They FAIL today by design.

NOTE on impl-agnosticism: the Architect's reuse-first recommendation is to
extend ``apply_world_patch`` with a ``confrontation`` field. A dedicated
``begin_confrontation`` tool is the documented fallback. The AC2 test accepts
either; AC3/AC4 are written against the recommended path — if Dev deviates,
update these + log a Design Deviation.
"""

from __future__ import annotations

from pathlib import Path

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import default_registry

# Social confrontation types tea_and_murder offers (rules.yaml). These are the
# types that must be reachable through the SDK engagement path.
_SOCIAL_TYPES = ("negotiation", "social_duel", "trial", "auction", "scandal")

# Property names a tool might expose to set the engagement TYPE. advance_confrontation's
# ``confrontation_id`` is explicitly NOT one of these (it's a forward-compat id, not a
# type that STARTS an encounter).
_ENGAGEMENT_FIELD_NAMES = ("confrontation", "confrontation_type")


def _defs_by_name() -> dict[str, object]:
    return {d.name: d for d in default_registry.tool_definitions()}


# ---------------------------------------------------------------------------
# AC2 — an SDK tool must be able to WRITE result.confrontation
# ---------------------------------------------------------------------------


def test_some_sdk_tool_can_write_the_confrontation_engagement_field() -> None:
    """AC2: There must be an offered SDK tool (other than advance_confrontation)
    whose input schema lets the narrator set the confrontation engagement type.

    FAILS today: only advance_confrontation carries a confrontation-ish arg
    (confrontation_id, a forward-compat id — NOT a starting type), and it is
    explicitly excluded. apply_world_patch carries no such field.
    """
    defs = _defs_by_name()
    writers = []
    for name, d in defs.items():
        if name == "advance_confrontation":
            continue
        props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
        if any(field in props for field in _ENGAGEMENT_FIELD_NAMES):
            writers.append(name)
    assert writers, (
        "No offered SDK tool exposes a confrontation engagement field "
        f"({_ENGAGEMENT_FIELD_NAMES}). The narrator cannot set result.confrontation, "
        "so social confrontations never engage. Recommended fix: add a "
        "`confrontation` field to apply_world_patch. Offered tools: "
        f"{sorted(defs)}"
    )


def test_advance_confrontation_is_not_the_engagement_writer() -> None:
    """AC2 (negative): advance_confrontation must NOT be treated as the way to
    START a confrontation — it advances an active encounter's dial and its args
    are axis/delta, not a starting type. This guards against a 'fix' that just
    points engagement back at the wrong tool.
    """
    d = _defs_by_name()["advance_confrontation"]
    props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
    assert "axis" in props and "delta" in props, (
        "advance_confrontation should remain the advance-an-active-encounter tool "
        f"(axis/delta). Got props: {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# AC3 — social trigger criteria must live on a LIVE engagement tool, not the
# always-erroring generate_encounter stub.
# ---------------------------------------------------------------------------


def test_live_engagement_tool_description_carries_social_triggers() -> None:
    """AC3: The social trigger criteria must ride on the LIVE engagement tool's
    description. SDK selection keys on the tool description (ADR-111).

    Dev deviated from the Architect's reuse-first recommendation (extend
    apply_world_patch): the engagement writer is the dedicated
    ``begin_confrontation`` tool instead, because apply_world_patch is a
    deprecation-targeted escape hatch (ADR-011, target = zero spans) and
    bolting a load-bearing engagement path onto it is the wrong home. See the
    59-1 session-file Design Deviation. The contract is unchanged: the social
    trigger criteria ride on whichever LIVE tool the narrator calls to START a
    confrontation.
    """
    d = _defs_by_name()["begin_confrontation"]
    desc = d.description.lower()  # type: ignore[attr-defined]
    missing = [t for t in _SOCIAL_TYPES if t not in desc]
    assert not missing, (
        "begin_confrontation (the engagement writer) description is missing "
        f"social trigger types {missing}. The narrator reads tool descriptions "
        "to decide engagement (ADR-111); the social criteria must ride on the "
        "live start-confrontation tool, not a dead stub."
    )


def test_generate_encounter_cannot_be_the_engagement_path() -> None:
    """AC3 (negative): generate_encounter is a stub that always returns a fatal
    error, so engagement must NOT route through it. Story 59-1 relocated the
    social trigger criteria OFF this stub onto begin_confrontation (the dead
    stub mis-routed the SDK narrator — that was the engagement-regression root
    cause). Assert structurally that engagement does not depend on it:
    generate_encounter exposes no confrontation-engagement field, so it is not
    among AC2's engagement writers.
    """
    d = _defs_by_name()["generate_encounter"]
    props = d.input_schema.get("properties", {})  # type: ignore[attr-defined]
    assert not any(field in props for field in _ENGAGEMENT_FIELD_NAMES), (
        "generate_encounter must NOT expose a confrontation engagement field "
        f"({_ENGAGEMENT_FIELD_NAMES}) — it is an always-erroring stub that "
        "cannot create an encounter. Engagement routes through "
        f"begin_confrontation. generate_encounter props: {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# AC4 — the SDK prompt must not route STARTING through advance_confrontation.
# ---------------------------------------------------------------------------

_SDK_PROMPT = (
    Path(__file__).resolve().parents[1].parent
    / "sidequest"
    / "agents"
    / "narrator_prompts"
    / "output_only_sdk.md"
)


def test_sdk_prompt_does_not_route_starting_through_advance_confrontation() -> None:
    """AC4: output_only_sdk.md section 4 must not instruct the narrator to START
    a confrontation by calling advance_confrontation (which errors with no active
    encounter). advance_confrontation is for ADVANCING an active encounter.

    FAILS today: section 4 reads "STARTING / ADVANCING A CONFRONTATION OR
    ENCOUNTER ... call advance_confrontation (when ANY structured encounter
    BEGINS this turn ...)".
    """
    # Normalize whitespace so markdown line-wrapping doesn't hide the phrase
    # (the source wraps "BEGINS this\n   turn" across lines).
    normalized = " ".join(_SDK_PROMPT.read_text(encoding="utf-8").split())
    assert (
        "advance_confrontation` (when ANY structured encounter BEGINS this turn" not in normalized
    ), (
        "output_only_sdk.md still tells the narrator to call advance_confrontation "
        "when an encounter BEGINS — but that tool cannot start one. STARTING must "
        "route to the engagement-field writer; advance_confrontation is advance-only."
    )


# ---------------------------------------------------------------------------
# C1 (rework) — SDK assembler wiring: a begin_confrontation tool call sets
# result.confrontation so narration_apply (not the tool's clobbered store
# write) creates the encounter on the canonical snapshot.
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from sidequest.agents.anthropic_sdk_client import AnthropicSdkClient  # noqa: E402
from sidequest.agents.orchestrator import Orchestrator, TurnContext  # noqa: E402
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock  # noqa: E402


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _TextBlock:
    type: str
    text: str


@dataclass
class _ToolUseSdkBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _Resp:
    content: list[Any]
    stop_reason: str
    usage: _Usage
    model: str


class _Msgs:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _Sdk:
    def __init__(self, responses: list[_Resp]) -> None:
        self.messages = _Msgs(responses)


class _FakeRegistry:
    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")


@pytest.mark.asyncio
async def test_sdk_begin_confrontation_call_sets_result_confrontation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK narrator calls begin_confrontation with a type the genre
    offers, the assembled NarrationTurnResult must carry that type on
    ``confrontation`` — so narration_apply's consumer creates the encounter on
    the CANONICAL snapshot (the tool's own ctx.store write would be clobbered
    by room.save). This is the load-bearing C1 wiring: tool call -> ledger ->
    result.confrontation.
    """
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(
        responses=[
            _Resp(
                content=[
                    _ToolUseSdkBlock(
                        type="tool_use",
                        id="toolu_bc",
                        name="begin_confrontation",
                        input={"confrontation_type": "negotiation"},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=200, output_tokens=12),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_TextBlock(type="text", text="The terms are named.")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=210, output_tokens=20),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))

    # Spy dispatch so the real handler (needs a live store/pack) isn't required —
    # the ledger still records the begin_confrontation call, which is what the
    # assembler reads.
    async def _spy_dispatch(block: ToolUseBlock, ctx: object) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    from sidequest.agents.tool_registry import default_registry as _dr

    monkeypatch.setattr(_dr, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Neil",
        genre="tea_and_murder",
        turn_number=3,
        # The assembler validates the requested type against the offered menu
        # before routing it to result.confrontation.
        available_confrontations=[("negotiation", "Negotiation", "social")],
    )

    result = await orch.run_narration_turn("name your client", ctx)

    assert result.confrontation == "negotiation", (
        "begin_confrontation tool call must set result.confrontation via the "
        f"assembler ledger scan; got {result.confrontation!r}. Without this the "
        "encounter never reaches narration_apply / the canonical snapshot."
    )


@pytest.mark.asyncio
async def test_sdk_unknown_begin_confrontation_type_is_not_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A begin_confrontation call with a type the genre does NOT offer must not
    set result.confrontation — otherwise narration_apply would raise on the
    unknown type. The assembler validates against the offered menu."""
    monkeypatch.delenv("SIDEQUEST_NARRATOR_STREAMING", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    sdk = _Sdk(
        responses=[
            _Resp(
                content=[
                    _ToolUseSdkBlock(
                        type="tool_use",
                        id="toolu_bad",
                        name="begin_confrontation",
                        input={"confrontation_type": "not_a_real_type"},
                    )
                ],
                stop_reason="tool_use",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            ),
            _Resp(
                content=[_TextBlock(type="text", text="...")],
                stop_reason="end_turn",
                usage=_Usage(input_tokens=10, output_tokens=2),
                model="claude-sonnet-4-6",
            ),
        ]
    )
    orch = Orchestrator(client=AnthropicSdkClient(sdk=sdk))

    async def _spy_dispatch(block: ToolUseBlock, ctx: object) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="err", is_error=True)

    from sidequest.agents.tool_registry import default_registry as _dr

    monkeypatch.setattr(_dr, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Neil",
        genre="tea_and_murder",
        turn_number=3,
        available_confrontations=[("negotiation", "Negotiation", "social")],
    )

    result = await orch.run_narration_turn("do a thing", ctx)
    assert result.confrontation is None
