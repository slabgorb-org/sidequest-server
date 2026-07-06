"""RED tests for Story 162-8 — dead spawn-path cleanup (No Stubbing).

Two unwired stubs from the NPC-origin-consolidation sweep (Epic 162) are
*removed*, not left dormant. Both are dead paths the live engine routed around:

1. ``resolve_encounter_from_trope`` (``server/dispatch/encounter_lifecycle.py``)
   — a self-described IOU helper whose own docstring admits "no Python caller as
   of this commit ... hook this function at the completion site when the trope
   tick/resolve path lands." That path has since landed (``game/trope_tick.py``)
   and deliberately does NOT resolve encounters through this helper — the
   resolved-trope handshake is chapter-promotion (``_handshake_resolved_tropes``,
   Story 45-20). The helper is the sole non-test caller of
   ``Encounter.resolve_from_trope`` and is stranded dead code. Delete it.
   (The ``Encounter.resolve_from_trope`` *method* keeps its own direct unit
   tests in ``tests/game/test_encounter.py`` and is out of scope here.)

2. The ``generate_name`` tool + ``ToolContext.name_generators`` field
   (``agents/tools/generate_name.py`` / ``agents/tool_registry.py``) — the one
   Phase-E ``ToolContext`` seam never wired at the production call site.
   ``orchestrator.py``'s ``ToolContext(...)`` construction wires every sibling
   seam (lore_store, monster_manual, genre_pack, weather_state, ...) but never
   ``name_generators``, so ``ctx.name_generators`` is permanently ``None`` and
   the tool always returns an empty list. The live naming path is
   ``narration_apply.py``'s ``build_from_culture`` route (Story 83-2), which
   makes the tool a redundant, permanently-empty parallel path. Remove the tool
   and the dangling field.

These assertions FAIL until the code is removed (RED) and pass once Dev deletes
it (GREEN). They are reflection-based per the server "No Source-Text Wiring
Tests" rule — they interrogate runtime module namespaces, dataclass fields, and
registry state, never grepping source text.
"""

from __future__ import annotations

import dataclasses
import importlib

import pytest

# Force the tools barrel to import so every @tool self-registers on the shared
# default_registry. Without this, the "unregistered" assertion below could pass
# for the WRONG reason (the adapter was simply never imported) and mask a tool
# that is in fact still present.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import ToolContext, default_registry


def test_resolve_encounter_from_trope_helper_deleted() -> None:
    """The no-caller IOU helper must be gone from the dispatch module namespace."""
    module = importlib.import_module("sidequest.server.dispatch.encounter_lifecycle")
    assert not hasattr(module, "resolve_encounter_from_trope"), (
        "resolve_encounter_from_trope is a no-caller IOU stub (its own docstring "
        "admits no Python caller); the trope engine landed without wiring it, so "
        "it must be deleted, not left dormant."
    )


def test_generate_name_tool_unregistered() -> None:
    """The redundant generate_name tool must not be advertised to the narrator."""
    assert "generate_name" not in default_registry.list_names(), (
        "generate_name is a permanently-unwired tool — ctx.name_generators is "
        "never set at the production ToolContext call site, so it always returns "
        "an empty list. The live namer is narration_apply.build_from_culture; the "
        "tool is a redundant dead path and must be de-registered."
    )


def test_generate_name_module_deleted() -> None:
    """The tool adapter module itself must be removed (no dead shell)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sidequest.agents.tools.generate_name")


def test_toolcontext_has_no_name_generators_field() -> None:
    """The dangling Phase-E ToolContext seam must be excised, not just the tool."""
    field_names = {f.name for f in dataclasses.fields(ToolContext)}
    assert "name_generators" not in field_names, (
        "ToolContext.name_generators is the one Phase-E seam never wired at the "
        "production call site; removing the tool while leaving the field keeps a "
        "dead shell (No Stubbing). Remove the field too."
    )
