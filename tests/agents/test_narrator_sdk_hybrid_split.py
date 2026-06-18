"""Task E1.5-B — Hybrid-split the SDK-path NarrationTurnResult assembly.

The SDK narration path runs the 26 WRITE tools during the tool-dispatch
loop; those tools mutate AND persist (``ctx.store.save``) game state during
dispatch. The narrator ALSO emits a sidecar ``game_patch`` block (the prompt
still injects ``narrator_output_only``). Before this task,
``_run_narration_turn_sdk`` fed that sidecar through ``_assemble_turn_result``,
so ``narration_apply._apply_narration_result_to_snapshot`` re-applied every
tool-owned mutation a SECOND time (double-apply bug).

The hybrid split: on the SDK path only, build the ``NarrationTurnResult`` so
that

* tool-owned state categories (the 12 ``COVERAGE_MAP`` rows) are ZEROED on
  the result — the tools already applied + saved them during dispatch, so
  ``narration_apply`` must not re-apply them; and
* presentation/signal fields that have NO successor tool (scene_mood,
  visual_scene, npcs_present, footnotes, sfx_triggers, action_rewrite) are
  STILL sourced from the sidecar parse so images/audio/footnotes/perception
  keep working.

The ClaudeClient sync path (``_assemble_turn_result``) is
untouched — it keeps re-applying the sidecar exactly as before, because on
that path no tool ran during dispatch.

Tests live alongside ``test_narrator_uses_sdk_client.py`` and reuse its
fake-SDK shape (``_Sdk`` / ``_Resp`` / ``_Usage`` / ``_ToolUseSdkBlock`` /
``_TextBlock``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Importing the tools package wires the 26 adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import (
    _SDK_TOOL_OWNED_FIELDS,
    NarrationTurnResult,
    Orchestrator,
    TurnContext,
)
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# ---------------------------------------------------------------------------
# Story 119-3: this file tests the ORCHESTRATOR's SDK-path result assembler
# (``_assemble_turn_result_sdk`` — the hybrid split), NOT the transport. The
# right seam is therefore the whole-client ``ToolingLlmClient`` double
# (``FakeAnthropicSdkClient``), which returns a scripted ``ToolingResult`` with
# ``tool_calls`` accumulated from scripted ``tool_use`` responses — exactly what
# the assembler reads. The post-119-3 transport (``claude-agent-sdk`` ``query``)
# sits BELOW this seam and is covered by ``test_119_3_narrator_port.py``.
# ---------------------------------------------------------------------------


def _model_response(
    *,
    text: str,
    stop_reason: str,
    input_tokens: int,
    output_tokens: int,
    tool_uses: list[ToolUseBlock] | None = None,
) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_read_tokens=0,
        cached_input_write_tokens=0,
        model="claude-sonnet-4-6",
        tool_uses=tool_uses or [],
    )


class _FakeRegistry:
    """Stand-in for PromptRegistry.compose_split with the minimum API we use."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        # Story 60-2: the SDK path emits an enriched prompt_assembled post-call
        # that reads registry sections; this stub registers none.
        return []


# ---------------------------------------------------------------------------
# Shared sidecar fixture — a game_patch block that mixes TOOL-OWNED mutations
# (status_changes, location, magic_working, beat_selections, confrontation,
# days_advanced, affinity_progress) with PRESENTATION fields (visual_scene,
# scene_mood, sfx_triggers, footnotes, npcs_present, action_rewrite).
#
# On the SDK path the tool-owned mutations were already applied + saved by
# the WRITE tools during dispatch — so they MUST be empty on the result.
# The presentation fields have no successor tool — so they MUST survive.
# ---------------------------------------------------------------------------

_SIDECAR = {
    # ---- tool-owned (must be zeroed on the SDK-path result) ----
    "location": "The Drowned Vault",
    "status_changes": [{"actor": "Kael", "status": {"text": "Bleeding gash", "severity": "Wound"}}],
    "magic_working": {"working": "ward", "bar": "focus", "delta": -1.0},
    "confrontation": "tense_standoff",
    "beat_selections": [{"actor": "Kael", "beat_id": "press_attack", "outcome": "Success"}],
    "days_advanced": 3,
    "affinity_progress": [{"name": "Mara", "delta": 2}],
    "gold_change": -19,
    "quest_updates": {"q1": "advanced"},
    "lore_established": ["The vault drank the river."],
    "items_gained": [{"name": "Rusted Key", "category": "quest"}],
    "companions_added": [{"name": "Donut", "role": "torchbearer"}],
    # ---- presentation (must SURVIVE on the SDK-path result) ----
    "visual_scene": {
        "subject": "Kael wading waist-deep in a flooded stone vault",
        "mood": "ominous",
        "tags": ["water", "vault"],
    },
    "scene_mood": "claustrophobic dread",
    "sfx_triggers": ["water_drip", "distant_groan"],
    "footnotes": [{"summary": "The vault key is iron, not brass.", "category": "world"}],
    "npcs_present": ["The Drowned Warden"],
    "action_rewrite": {
        "you": "You wade into the dark water.",
        "named": "Kael wades into the dark water.",
        "intent": "explore",
    },
}


def _sidecar_text(prose: str) -> str:
    """Render prose + a fenced ```game_patch``` block, the narrator contract."""
    return f"{prose}\n\n```game_patch\n{json.dumps(_SIDECAR)}\n```\n"


def _make_client(prose: str) -> FakeAnthropicSdkClient:
    """Two-turn scripted ToolingLlmClient: a tool_use round (apply_status),
    then the final sidecar prose — the shape the SDK assembler consumes."""
    return FakeAnthropicSdkClient(
        responses=[
            _model_response(
                text="",
                stop_reason="tool_use",
                input_tokens=200,
                output_tokens=24,
                tool_uses=[
                    ToolUseBlock(
                        id="toolu_status_1",
                        name="apply_status",
                        arguments={
                            "actor": "Kael",
                            "text": "Bleeding gash",
                            "severity": "Wound",
                        },
                    )
                ],
            ),
            _model_response(
                text=_sidecar_text(prose),
                stop_reason="end_turn",
                input_tokens=250,
                output_tokens=48,
            ),
        ]
    )


async def _run_sdk_turn(
    monkeypatch: pytest.MonkeyPatch,
    prose: str,
    *,
    confrontation_def: Any = None,
) -> NarrationTurnResult:
    """Drive ``run_narration_turn`` through the SDK path with the fixture."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = _make_client(prose)
    orch = Orchestrator(client=client)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        # Don't exercise the real WRITE tool side effects in this unit; the
        # double-apply guard is about what the RESULT carries, not the
        # tool's own persistence (covered by the tools' own suites).
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Kael",
        genre="caverns_and_claudes",
        turn_number=2,
        confrontation_def=confrontation_def,
    )
    return await orch.run_narration_turn("wade in", ctx)


# ---------------------------------------------------------------------------
# 1. No double-apply — tool-owned categories are empty on the SDK result.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_zeros_tool_owned_state(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """The 12 COVERAGE_MAP categories were applied+saved by the WRITE tools
    during dispatch. The SDK-path result must NOT re-carry them, so
    narration_apply (and the session-handler trope/affinity/clue seams)
    do not double-apply.
    """
    result = await _run_sdk_turn(monkeypatch, "Black water laps at the stair.")

    # Prose survives — that's never tool-owned.
    assert result.narration == "Black water laps at the stair."

    # patches_status / apply_damage → status_changes
    assert result.status_changes == []
    # patches_other / apply_world_patch → location
    assert result.location is None
    # magic_effects / patches_resource_pool → magic_working
    assert result.magic_working is None
    # confrontation_advances / encounter_advances → confrontation + beats
    assert result.confrontation is None
    assert result.beat_selections == []
    # trope_tick → days_advanced
    assert result.days_advanced == 0
    # patches_resource_pool → affinity_progress
    assert result.affinity_progress == []
    # game_patch_dict carries patches_disposition / patches_other escape-hatch
    # intents (course/morale/world-patch) the tools own — must be empty.
    assert result.game_patch_dict == {}


# ---------------------------------------------------------------------------
# 2. Presentation survives — no-successor-tool fields stay sidecar-sourced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_keeps_presentation_fields(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """scene_mood / visual_scene / npcs_present / footnotes / sfx_triggers have
    NO successor tool, so they MUST still be parsed off the sidecar —
    images/audio/footnotes/perception depend on them.

    Story 151-3 / ADR-150 step 3: ``action_rewrite`` is NO LONGER a
    game_patch-sourced presentation field — it is produced by the pre-narrator
    IntentRouter and carried on ``TurnContext.dispatch_package``. With no
    dispatch_package on this SDK fixture turn, the result carries no rewrite; the
    pre-pass sourcing is covered by
    ``test_result_action_rewrite_sourced_from_pre_pass_not_game_patch`` in
    test_orchestrator.py. The other presentation fields move in 151-4/5, not here.
    """
    result = await _run_sdk_turn(monkeypatch, "Phosphor moss glows green.")

    assert result.scene_mood == "claustrophobic dread"
    assert result.visual_scene is not None
    assert "flooded stone vault" in (result.visual_scene.subject or "")
    assert result.sfx_triggers == ["water_drip", "distant_groan"]
    assert len(result.footnotes) == 1
    assert result.footnotes[0]["summary"] == "The vault key is iron, not brass."
    assert [m.name for m in result.npcs_present] == ["The Drowned Warden"]
    # action_rewrite intentionally NOT asserted here — retired from the sidecar
    # presentation bucket (now pre-pass-sourced; see 151-3 note above).


# ---------------------------------------------------------------------------
# 3. tool_calls observability ledger (ADR-103).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_path_populates_tool_calls_ledger(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """The SDK-path result must carry a tool-invocation ledger so the GM
    panel lie-detector (ADR-103) can show what the model actually called.
    """
    result = await _run_sdk_turn(monkeypatch, "The Warden lifts its head.")

    assert result.tool_calls == [
        {
            "id": "toolu_status_1",
            "name": "apply_status",
            "arguments": {
                "actor": "Kael",
                "text": "Bleeding gash",
                "severity": "Wound",
            },
        }
    ]


def test_tool_calls_field_defaults_empty_on_non_sdk_construction() -> None:
    """tool_calls is an SDK-path-only ledger — empty by default so the
    ClaudeClient sync paths never carry it.
    """
    ntr = NarrationTurnResult(narration="x")
    assert ntr.tool_calls == []


@pytest.mark.asyncio
async def test_sdk_tool_calls_ledger_emitted_to_narration_turn_span(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """ADR-103 / CLAUDE.md OTEL principle wiring test: the ledger must be
    EMITTED, not just stored. The GM panel lie-detector reads per-turn
    ``{id,name,arguments}`` off the ``narration.turn`` span — the existing
    ``tool_call_count`` attribute does not carry that detail. OTEL drops
    list/dict attributes, so the project convention (spans/magic.py) is a
    JSON-string attribute: ``narration.turn.tool_calls_json``.
    """
    await _run_sdk_turn(monkeypatch, "The Warden lifts its head.")

    narration_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(narration_spans) == 1
    attrs = dict(narration_spans[0].attributes or {})

    # Sanity: the count attribute still exists (unchanged contract).
    assert attrs["narration.turn.tool_call_count"] == 1

    # The new ledger attribute carries the full per-call detail as JSON.
    assert "narration.turn.tool_calls_json" in attrs
    ledger = json.loads(attrs["narration.turn.tool_calls_json"])
    assert ledger == [
        {
            "id": "toolu_status_1",
            "name": "apply_status",
            "arguments": {
                "actor": "Kael",
                "text": "Bleeding gash",
                "severity": "Wound",
            },
        }
    ]


@pytest.mark.asyncio
async def test_sdk_tool_calls_ledger_json_is_empty_list_when_no_tools(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """No tool calls this turn → the ledger attribute is present and is an
    empty JSON list (not absent, not null) so the GM panel can always parse
    it without a missing-key special case.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = FakeAnthropicSdkClient(
        responses=[
            _model_response(
                text=_sidecar_text("Silence."),
                stop_reason="end_turn",
                input_tokens=120,
                output_tokens=20,
            ),
        ]
    )
    orch = Orchestrator(client=client)

    async def _fake_build_prompt(
        self: Orchestrator, action: str, context: TurnContext
    ) -> tuple[str, _FakeRegistry]:
        return ("prompt-text", _FakeRegistry())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(character_name="Kael", turn_number=1)
    await orch.run_narration_turn("wait", ctx)

    narration_spans = [s for s in otel_capture.get_finished_spans() if s.name == "narration.turn"]
    assert len(narration_spans) == 1
    attrs = dict(narration_spans[0].attributes or {})
    assert attrs["narration.turn.tool_call_count"] == 0
    assert json.loads(attrs["narration.turn.tool_calls_json"]) == []


# ---------------------------------------------------------------------------
# 4. ClaudeClient (sync) path UNTOUCHED — regression guard.
# ---------------------------------------------------------------------------


def test_assemble_turn_result_still_applies_sidecar_on_non_sdk_path() -> None:
    """``_assemble_turn_result`` (sync callers) must keep its
    exact pre-task behavior: it re-applies the sidecar because no tool ran
    during dispatch on that path. This is the byte-for-byte regression
    guard for the non-SDK seam.
    """
    orch = Orchestrator(client=None)
    response = ClaudeResponse(
        text=_sidecar_text("The vault breathes."),
        input_tokens=10,
        output_tokens=20,
        session_id=None,
        backend="claude-cli",
    )
    ctx = TurnContext(character_name="Kael", turn_number=1)

    result = orch._assemble_turn_result(
        response=response,
        prompt_text="p",
        context=ctx,
        elapsed_ms=5,
        action="wade in",
    )

    # On the non-SDK path the tool-owned fields STILL come from the sidecar
    # (no tool ran; narration_apply is the only applier).
    assert result.location == "The Drowned Vault"
    assert len(result.status_changes) == 1
    assert result.magic_working == {"working": "ward", "bar": "focus", "delta": -1.0}
    assert result.confrontation == "tense_standoff"
    assert len(result.beat_selections) == 1
    assert result.days_advanced == 3
    assert result.affinity_progress == [("Mara", 2)]
    assert result.gold_change == -19
    assert result.game_patch_dict != {}
    # Presentation also present (parity — proves the SDK split didn't
    # regress the shared parse).
    assert result.scene_mood == "claustrophobic dread"
    # tool_calls ledger is empty on the non-SDK path.
    assert result.tool_calls == []


# ---------------------------------------------------------------------------
# 5. Wiring test — the SDK assembler is reachable from run_narration_turn,
#    and the partition constant is the single documented source of truth.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_assembler_is_wired_into_run_narration_turn(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """run_narration_turn → _run_narration_turn_sdk → the new SDK assembler.

    Proven by observing the split's effect end-to-end: a sidecar carrying
    BOTH tool-owned and presentation fields comes back with tool-owned
    zeroed and presentation kept — a behavior only the SDK assembler
    produces. This is the "every test suite needs a wiring test" gate.
    """
    result = await _run_sdk_turn(monkeypatch, "Reachability proof.")
    # Tool-owned zeroed AND presentation kept in the same result == the
    # SDK assembler ran (not _assemble_turn_result, which keeps both).
    assert result.location is None
    assert result.status_changes == []
    assert result.scene_mood == "claustrophobic dread"
    assert result.narration == "Reachability proof."


# ---------------------------------------------------------------------------
# 6. RW-2 opposed_check carve-out (playtest 2026-06-05, the_circuit chase).
#
# The opposed_check resolution engine (narration_apply._resolve_opposed_check_
# branch) consumes ``result.beat_selections`` — the narrator's OPPONENT-side
# beat pick. Zeroing beat_selections unconditionally on the SDK path made the
# entire engine structurally unreachable on the default backend: the player's
# stashed DICE_THROW d20 was silently discarded every turn and ALL dial
# movement came from the narrator free-handing advance_confrontation.
#
# The carve-out: when the ACTIVE confrontation resolves via opposed_check
# (``context.confrontation_def.resolution_mode``), the SDK assembler carries
# the sidecar beat_selections through so narration_apply can run the resolver.
# This is NOT a double-apply risk for that mode: no WRITE tool applies
# opposed_check beats during dispatch (advance_encounter_beat's apply path is
# the legacy DC mode; advance_confrontation is refused for opposed_check), and
# the SOUL gate in narration_apply still drops PC-side selections.
# ---------------------------------------------------------------------------


def _opposed_cdef() -> Any:
    from sidequest.genre.models.rules import ConfrontationDef

    return ConfrontationDef.model_validate(
        {
            "type": "chase",
            "label": "Chase",
            "category": "movement",
            "resolution_mode": "opposed_check",
            "opponent_default_stats": {"STR": 12},
            "player_metric": {"name": "separation", "starting": 0, "threshold": 7},
            "opponent_metric": {"name": "pursuit", "starting": 0, "threshold": 7},
            "beats": [
                {
                    "id": "press_attack",
                    "label": "Press Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ],
        }
    )


def _legacy_mode_cdef() -> Any:
    from sidequest.genre.models.rules import ConfrontationDef

    return ConfrontationDef.model_validate(
        {
            "type": "chase",
            "label": "Chase",
            "category": "movement",
            "resolution_mode": "beat_selection",
            "player_metric": {"name": "separation", "starting": 0, "threshold": 7},
            "opponent_metric": {"name": "pursuit", "starting": 0, "threshold": 7},
            "beats": [
                {
                    "id": "press_attack",
                    "label": "Press Attack",
                    "kind": "strike",
                    "base": 2,
                    "stat_check": "STR",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_sdk_path_carries_beat_selections_under_opposed_check(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """When the active confrontation is opposed_check, the SDK result MUST
    carry the sidecar beat_selections — they are the resolver's only input
    for the opponent's beat. Every OTHER tool-owned field stays zeroed.
    """
    result = await _run_sdk_turn(
        monkeypatch,
        "The patrol cuts across the median.",
        confrontation_def=_opposed_cdef(),
    )

    assert len(result.beat_selections) == 1, (
        "opposed_check carve-out must lift the sidecar beat_selections onto "
        "the SDK result — the opposed resolver is unreachable without them"
    )
    sel = result.beat_selections[0]
    assert sel.actor == "Kael"
    assert sel.beat_id == "press_attack"

    # The carve-out is surgical: every other tool-owned field stays zeroed
    # (the anti-double-apply guard holds for tool-applied categories).
    assert result.status_changes == []
    assert result.location is None
    assert result.magic_working is None
    assert result.days_advanced == 0
    assert result.affinity_progress == []
    assert result.game_patch_dict == {}


@pytest.mark.asyncio
async def test_sdk_path_zeros_beat_selections_under_non_opposed_confrontation(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Regression guard: a NON-opposed confrontation (legacy beat_selection
    mode) keeps the zeroed partition — beats on that mode flow through the
    advance_encounter_beat WRITE tool, so carrying the sidecar would
    double-apply.
    """
    result = await _run_sdk_turn(
        monkeypatch,
        "Steel rings on steel.",
        confrontation_def=_legacy_mode_cdef(),
    )
    assert result.beat_selections == []


@pytest.mark.asyncio
async def test_sdk_path_zeros_beat_selections_with_no_confrontation(
    monkeypatch: pytest.MonkeyPatch, otel_capture: InMemorySpanExporter
) -> None:
    """Regression guard: no active confrontation → zeroed, as before."""
    result = await _run_sdk_turn(monkeypatch, "A quiet road.")
    assert result.beat_selections == []


def test_sdk_tool_owned_partition_is_explicit_and_documented() -> None:
    """The zeroed partition must be a single module-level constant, not
    scattered literals — a future reader sees exactly which fields are
    zeroed and (via the constant's mapping) why.
    """
    # Every tool-owned NarrationTurnResult field name in the partition must
    # be a real dataclass field.
    valid_fields = set(NarrationTurnResult.__dataclass_fields__)
    for field_name in _SDK_TOOL_OWNED_FIELDS:
        assert field_name in valid_fields, (
            f"{field_name!r} in _SDK_TOOL_OWNED_FIELDS is not a NarrationTurnResult field"
        )
    # The categories that have a NarrationTurnResult home must be covered.
    assert "status_changes" in _SDK_TOOL_OWNED_FIELDS
    assert "location" in _SDK_TOOL_OWNED_FIELDS
    assert "magic_working" in _SDK_TOOL_OWNED_FIELDS
    # Story 59-1: ``confrontation`` (encounter START) is intentionally NOT in
    # the zeroed partition. A tool cannot create the encounter on the SDK path
    # (its ctx.store write is clobbered by room.save of the canonical snapshot),
    # so begin_confrontation is a signal and _assemble_turn_result_sdk SETS
    # result.confrontation from the tool-call ledger; narration_apply then
    # creates the encounter on the canonical snapshot. Zeroing it would discard
    # the signal.
    assert "confrontation" not in _SDK_TOOL_OWNED_FIELDS
    assert "beat_selections" in _SDK_TOOL_OWNED_FIELDS
    assert "days_advanced" in _SDK_TOOL_OWNED_FIELDS
    assert "affinity_progress" in _SDK_TOOL_OWNED_FIELDS
    # Presentation fields must NOT be in the zeroed partition.
    for keep in (
        "scene_mood",
        "visual_scene",
        "npcs_present",
        "footnotes",
        "sfx_triggers",
        "action_rewrite",
    ):
        assert keep not in _SDK_TOOL_OWNED_FIELDS
