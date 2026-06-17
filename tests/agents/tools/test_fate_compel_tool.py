"""RED tests for Story 116-2 (F2b) — the `propose_fate_compel` narrator tool.

Plan §4 Step 3: a thin WRITE tool, ruleset-gated to Fate packs, that calls the EXISTING
`FateRulesetModule.offer_compel` and so fires the EXISTING `fate.compel.offered` span. No
new engine, no new span — F2b just makes the narrator able to fire the one already wired.

Covers:
  * AC-4 — dispatch through the REAL `default_registry` fires `fate.compel.offered`
    (OTEL-span assertion, the canonical wiring shape — not a source grep).
  * AC-5 — advertisement gate: present for `ruleset="fate"`, absent for `"wwn"`/`"dial"`,
    present in the unfiltered (full) catalog.
  * AC-6 — import-time registration verified in a SUBPROCESS (in-process autouse conftest
    can mask a forgotten barrel line; project memory
    `import-sideeffect-registry-wiring-needs-subprocess-test`).

All FAIL today: `propose_fate_compel` is not registered (RED).
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

# Importing the tools package wires every adapter onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock
from sidequest.agents.tools.fate_tools import ProposeFateCompelArgs

TOOL_NAME = "propose_fate_compel"


class _NoopFilter:
    def filter_result(
        self,
        *,
        tool_name: str,
        category: ToolCategory,
        result: ToolResult,
        perspective_pc: str | None,
    ) -> ToolResult:
        return result


def _fate_ctx() -> ToolContext:
    """A ToolContext on a Fate-bound session, mirroring tests/agents/test_73_15_*."""
    repository = SimpleNamespace(load=lambda *a, **k: SimpleNamespace())  # truthy session
    return ToolContext(
        world_id="w",
        session_id="s",
        perspective_pc="Vance",
        turn_number=1,
        repository=repository,
        otel_span=SimpleNamespace(),
        perception_filter=_NoopFilter(),
        genre_pack=SimpleNamespace(rules=SimpleNamespace(ruleset="fate")),
    )


def _names(defs) -> set[str]:
    return {d.name for d in defs}


# ---------------------------------------------------------------------------
# AC-4 — the tool fires fate.compel.offered through the REAL registry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_fate_compel_fires_compel_offered_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t1",
            name=TOOL_NAME,
            arguments={
                "actor": "Vance",
                "aspect_text": "Last Honest Cop in Vega",
                "compel_reason": "Internal Affairs wants your badge for this case",
            },
        ),
        _fate_ctx(),
    )
    assert out.is_error is False, f"dispatch errored: {getattr(out, 'content', out)}"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "fate.compel.offered"]
    assert len(spans) >= 1, "propose_fate_compel did not fire fate.compel.offered"
    attrs = dict(spans[0].attributes or {})
    assert attrs["actor"] == "Vance"
    assert attrs["aspect"] == "Last Honest Cop in Vega"


# ---------------------------------------------------------------------------
# AC-5 — advertisement gate (ruleset-declaration driven, like 73-15).
# ---------------------------------------------------------------------------


def test_tool_advertised_to_fate_pack() -> None:
    assert TOOL_NAME in _names(default_registry.tool_definitions(ruleset="fate"))


def test_tool_hidden_from_wwn_and_dial_packs() -> None:
    assert TOOL_NAME not in _names(default_registry.tool_definitions(ruleset="wwn"))
    assert TOOL_NAME not in _names(default_registry.tool_definitions(ruleset="dial"))


def test_tool_present_in_unfiltered_catalog() -> None:
    """Back-compat: the full catalog (no slug) still lists it (dispatchable + self-guard)."""
    assert TOOL_NAME in _names(default_registry.tool_definitions())


def test_tool_is_a_write_tool() -> None:
    """A compel proposal mutates session intent (it fires a span / will store a pending
    offer) — it must be a WRITE tool, not a READ one (perception-filter + category routing).

    Category is a SERVER-SIDE concern carried on the registered tool, not on the
    model-facing ``ToolDefinition`` (which is the JSON-schema descriptor the model sees).
    Assert it on the registry's registered tool — the surface that drives WRITE-vs-READ
    perception-filter routing in ``Registry.dispatch``."""
    registered = default_registry._tools[TOOL_NAME]
    assert registered.category == ToolCategory.WRITE


# ---------------------------------------------------------------------------
# AC-6 — import-time registration verified in a clean subprocess.
# ---------------------------------------------------------------------------


def test_tool_registered_at_import_in_subprocess() -> None:
    """Guards against a forgotten barrel import in agents/tools/__init__.py: a fresh
    interpreter that imports ONLY the tools package must register the tool. The
    in-process autouse conftest cannot mask a missing line here."""
    code = (
        "import sidequest.agents.tools;"
        "from sidequest.agents.tool_registry import default_registry;"
        "names={d.name for d in default_registry.tool_definitions()};"
        f"assert '{TOOL_NAME}' in names, 'tool not registered on fresh import';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"subprocess registration check failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


# ===========================================================================
# REWORK round 1 — Reviewer (Hermes) findings, RED. See ## Reviewer Assessment
# in .session/116-2-session.md. LLM-tool input boundary (lang-review #11) + OTEL.
# ===========================================================================


def test_compel_args_reject_empty_actor_and_aspect() -> None:
    """[SEC/MEDIUM] LLM-tool input boundary (lang-review #11). Empty actor/aspect_text
    would fire an unattributable `fate.compel.offered` span — they must be rejected at
    the Pydantic boundary (min_length=1), like every peer narrator tool. RED today: the
    fields are unbounded `str`, so empty strings construct fine."""
    with pytest.raises(ValidationError):
        ProposeFateCompelArgs(actor="", aspect_text="An Aspect", compel_reason="because")
    with pytest.raises(ValidationError):
        ProposeFateCompelArgs(actor="Vance", aspect_text="", compel_reason="because")


def test_compel_args_reject_overlong_aspect_text() -> None:
    """[SEC/MEDIUM] aspect_text is echoed back into the next turn's tool_result — an
    unbounded string is an echo-injection surface. It must be capped (Fate aspects are
    short phrases). RED today: no max_length, so a 5000-char aspect constructs fine."""
    with pytest.raises(ValidationError):
        ProposeFateCompelArgs(actor="Vance", aspect_text="x" * 5000, compel_reason="because")


@pytest.mark.asyncio
async def test_compel_reason_reaches_offered_span(
    otel_capture: InMemorySpanExporter,
) -> None:
    """[EDGE/MEDIUM] OTEL Observability: the narrator's stated complication
    (`compel_reason`) must reach the `fate.compel.offered` span so the GM panel sees WHAT
    was proposed, not merely THAT something was. RED today: `offer_compel` records only
    actor + aspect; the reason is collected, echoed in the ToolResult, then dropped."""
    reason = "Internal Affairs wants your badge for this case"
    out = await default_registry.dispatch(
        ToolUseBlock(
            id="t1",
            name=TOOL_NAME,
            arguments={
                "actor": "Vance",
                "aspect_text": "Last Honest Cop in Vega",
                "compel_reason": reason,
            },
        ),
        _fate_ctx(),
    )
    assert out.is_error is False, f"dispatch errored: {getattr(out, 'content', out)}"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "fate.compel.offered"]
    assert len(spans) >= 1, "propose_fate_compel did not fire fate.compel.offered"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("reason") == reason, (
        "compel_reason did not reach the fate.compel.offered span — "
        "the GM panel cannot see the substance of the proposed compel"
    )
