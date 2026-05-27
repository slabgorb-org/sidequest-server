"""Tests for the retirement of the ``begin_confrontation`` SDK tool
(Story 59-4, ADR-113).

In Story 59-1, ``begin_confrontation`` was the SDK-narrator's sidecar
signal for confrontation engagement: the narrator called the tool, the
SDK assembler at ``_assemble_turn_result_sdk`` lifted the type onto
``result.confrontation``, and ``narration_apply.py:2528`` instantiated
the encounter from the canonical snapshot. The mechanism worked, but it
left the narrator owning engagement signaling — the "convincing prose
with no mechanical backing" SOUL Illusionism failure mode the Epic 59
reframe (Houlihan 2026-05-23) exists to eliminate.

Story 59-4 atomically cuts that path over to the IntentRouter spine:
the router classifies intent pre-narrator, the new dispatch handler at
``sidequest/agents/subsystems/confrontation.py`` engages the encounter
engine, and the narrator narrates already-real state. With the router
live, ``begin_confrontation`` becomes dead infrastructure and must be
retired in the same PR (memory rule
``feedback_one_mechanism_per_problem`` — no parallel window).

These tests pin the AC3 retirement contract:

  1. ``begin_confrontation`` is not in the default tool registry — the
     barrel import at ``sidequest/agents/tools/__init__.py`` no longer
     re-exports it.
  2. The tool module has been relocated to ``_retired/`` with a
     deprecation marker — importing the original location fails loudly
     (no silent shim, per ``feedback_no_fallbacks_hard``).
  3. The relocated module's docstring acknowledges the retirement and
     points at the dispatch handler — so a developer who finds the file
     via grep is routed to the live mechanism, not left to wonder.

These tests FAIL TODAY by design — the tool is still live.
"""

from __future__ import annotations

import importlib

import pytest

# Importing the tools package wires the adapters onto default_registry.
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.tool_registry import default_registry


def _registered_tool_names() -> set[str]:
    return {d.name for d in default_registry.tool_definitions()}


# ---------------------------------------------------------------------------
# AC3: tool gone from the default registry
# ---------------------------------------------------------------------------


def test_begin_confrontation_not_in_default_registry() -> None:
    """The narrator's per-turn tool list (``default_registry.tool_definitions()``)
    must no longer surface ``begin_confrontation``. If it does, the narrator
    can still call it and the SDK assembler's lift (deleted in the same PR)
    would silently no-op — the worst kind of half-cutover failure.

    FAILS TODAY: the tool is registered via the barrel import at
    ``sidequest/agents/tools/__init__.py:18``.
    """
    registered = _registered_tool_names()
    assert "begin_confrontation" not in registered, (
        "begin_confrontation must be removed from the default registry in "
        "Story 59-4 atomically with the router cutover. Currently registered "
        f"tools include it: {sorted(t for t in registered if 'conf' in t)}"
    )


def test_begin_confrontation_not_in_tools_barrel_import() -> None:
    """Reflection-based wiring guard: the ``sidequest.agents.tools`` package
    namespace must not expose ``begin_confrontation`` as an attribute.

    CLAUDE.md "No Source-Text Wiring Tests" — this is the legitimate
    reflection-based shape (interrogates the live module, not source text).
    The barrel import is the load-bearing site (it's what populates the
    registry as a side effect of importing the package); checking the
    module attribute is equivalent to checking the barrel without grep.

    FAILS TODAY: the barrel imports begin_confrontation.
    """
    import sidequest.agents.tools as tools_pkg

    assert not hasattr(tools_pkg, "begin_confrontation"), (
        "sidequest.agents.tools exposes begin_confrontation as a module "
        "attribute — the barrel import at __init__.py:18 still references it. "
        "Story 59-4 must remove that import line."
    )


# ---------------------------------------------------------------------------
# AC3: importing the original location fails loudly (no silent shim)
# ---------------------------------------------------------------------------


def test_importing_begin_confrontation_from_original_location_raises() -> None:
    """Memory rule ``feedback_no_fallbacks_hard``: a runtime shim that
    silently no-ops or re-exports is exactly the kind of bridge-to-nowhere
    the project's no-fallbacks doctrine forbids. The retirement is a clean
    break: importing the original path must raise ``ImportError`` so
    callers fail at import time, not at runtime when a sidecar lift would
    have been triggered.

    The story description hints at "deprecation re-export", but TEA
    interprets this as a docstring/comment in the relocated module
    rather than a runtime shim — see Design Deviation #3 in the session
    file. A runtime shim is rejected on no-fallbacks grounds.

    FAILS TODAY: the module exists at the original path.
    """
    with pytest.raises(ImportError):
        importlib.import_module("sidequest.agents.tools.begin_confrontation")


def test_retired_begin_confrontation_module_carries_deprecation_marker() -> None:
    """The relocated module at ``sidequest/agents/tools/_retired/begin_confrontation.py``
    must exist and carry a docstring acknowledging the retirement and
    pointing at the live mechanism (``subsystems/confrontation.py``). This
    is the developer-facing breadcrumb — without it, a grep for
    ``begin_confrontation`` lands on a file that gives no context for why
    it's not wired anywhere.

    FAILS TODAY: the _retired/ directory does not exist.
    """
    try:
        retired = importlib.import_module("sidequest.agents.tools._retired.begin_confrontation")
    except ImportError as exc:
        pytest.fail(
            "expected relocated module at "
            "sidequest/agents/tools/_retired/begin_confrontation.py "
            f"with deprecation docstring; ImportError: {exc}"
        )

    doc = (retired.__doc__ or "").lower()
    assert "retired" in doc or "deprecated" in doc, (
        f"relocated module docstring must acknowledge retirement; got: {retired.__doc__!r}"
    )
    assert "subsystem" in doc or "intent_router" in doc or "dispatch" in doc, (
        "relocated module docstring must point at the live mechanism "
        "(subsystems/confrontation.py or intent_router); got: "
        f"{retired.__doc__!r}"
    )
