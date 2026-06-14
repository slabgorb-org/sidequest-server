"""RED tests for Story 116-2 (F2b) — the `propose_fate_compel` narrator tool.

Plan §4 Step 3: a thin WRITE tool, ruleset-gated to Fate packs, that calls the EXISTING
`FateRulesetModule.offer_compel` and so fires the EXISTING `fate.compel.offered` span. No
new engine, no new span — F2b just makes the narrator able to fire the one already wired.

Covers:
  * AC-4 — dispatch through the REAL `default_registry` fires `fate.compel.offered`
    (OTEL-span assertion, the canonical wiring shape — not a source grep).
  * AC-5 — advertisement gate: present for `ruleset="fate"`, absent for `"wwn"`/`"native"`,
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

# Importing the tools package wires every adapter onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    default_registry,
)
from sidequest.agents.tooling_protocol import ToolUseBlock

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


def test_tool_hidden_from_wwn_and_native_packs() -> None:
    assert TOOL_NAME not in _names(default_registry.tool_definitions(ruleset="wwn"))
    assert TOOL_NAME not in _names(default_registry.tool_definitions(ruleset="native"))


def test_tool_present_in_unfiltered_catalog() -> None:
    """Back-compat: the full catalog (no slug) still lists it (dispatchable + self-guard)."""
    assert TOOL_NAME in _names(default_registry.tool_definitions())


def test_tool_is_a_write_tool() -> None:
    """A compel proposal mutates session intent (it fires a span / will store a pending
    offer) — it must be a WRITE tool, not a READ one (perception-filter + category routing)."""
    defs = {d.name: d for d in default_registry.tool_definitions()}
    assert defs[TOOL_NAME].category == ToolCategory.WRITE


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
