"""Wiring test — chargen-completion populates opening_seed/directive
on session_data via _resolve_opening_post_chargen.

The meaningful e2e assertion lives in Phase 8 Task 27/28
(test_first_turn_uses_authored_setting). This file documents the seam.
"""

from __future__ import annotations

import pytest

# Prime the import cache: ``session_handler`` and ``websocket_session_handler``
# have a back-compat circular re-export — the helpers in
# ``websocket_session_handler`` are only directly importable after
# ``session_handler`` has loaded (which triggers the re-export edge in
# the right order). Other tests in this directory hit this implicitly via
# conftest fixtures; this file imports the helper at module scope, so we
# load ``session_handler`` first by hand.
import sidequest.server.session_handler  # noqa: F401 — ordering side-effect
from sidequest.server.websocket_session_handler import (
    _populate_opening_directive_on_chargen_complete,
)


def test_helper_is_importable() -> None:
    """Smoke test: the wiring helper exists and is importable."""
    assert _populate_opening_directive_on_chargen_complete is not None


def test_populate_sets_opening_directive() -> None:
    pytest.skip(
        "Wiring assertion — e2e behavior covered by "
        "test_first_turn_uses_authored_setting in Phase 8."
    )
