"""Regression guard for the session_handler ↔ websocket_session_handler import cycle.

Story 64-6. A back-compat re-export at ``session_handler.py`` pulls
``WebSocketSessionHandler`` from ``websocket_session_handler``, while
``websocket_session_handler`` imports shared leaf types (``_SessionData``,
``_State``, ``_build_pc_descriptor``, ``_hash_snapshot``,
``_shared_world_delta_to_state_delta``, ``_AUDIO_INTERPRETER``) back from
``session_handler``. Whichever module the interpreter imports first can observe
the other half-initialized, raising ``ImportError`` (most likely due to a
circular import).

These tests spawn a *fresh* interpreter per import order so the result is
independent of whatever is already cached in ``sys.modules`` for the pytest
process. This is a runtime import check, not a source-text grep — see the
project's "No Source-Text Wiring Tests" rule, which permits import/runtime-type
checks.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Both modules participate in the cycle; importing either FIRST in a clean
# interpreter must succeed once the cycle is broken.
CYCLE_MODULES = [
    "sidequest.server.websocket_session_handler",
    "sidequest.server.session_handler",
]


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess:
    """Import ``module`` first in a brand-new interpreter process."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("module", CYCLE_MODULES)
def test_module_imports_first_in_fresh_interpreter(module: str) -> None:
    """AC2: each cycle module imports cleanly when it is the FIRST import.

    Currently RED for ``websocket_session_handler`` — it raises
    ``ImportError: cannot import name 'WebSocketSessionHandler' from partially
    initialized module``. Goes green once the shared leaf symbols are relocated
    so neither module depends on the other at import time.
    """
    result = _import_in_fresh_interpreter(module)
    assert result.returncode == 0, (
        f"Importing {module} first in a fresh interpreter failed "
        f"(returncode={result.returncode}). Import cycle not broken.\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # The specific failure mode we are guarding against — assert it is absent
    # from stderr so a different (non-cycle) crash does not masquerade as a pass.
    assert "partially initialized module" not in result.stderr, (
        f"{module} hit a partially-initialized-module ImportError — the "
        f"session_handler/websocket_session_handler cycle is still present.\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_both_cycle_modules_import_regardless_of_order() -> None:
    """AC2: the cycle is order-independent — both orders succeed.

    A single assertion over both orders makes the regression obvious: if the
    cycle returns, exactly one order will fail and this names which one.
    """
    failures = {}
    for module in CYCLE_MODULES:
        result = _import_in_fresh_interpreter(module)
        if result.returncode != 0:
            failures[module] = result.stderr

    assert not failures, (
        "Import order matters — the cycle is present. Failing first-import "
        f"modules: {sorted(failures)}.\n"
        + "\n".join(f"=== {m} ===\n{err}" for m, err in failures.items())
    )
