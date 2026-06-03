"""RED wiring test — Story 77-1 — seed-at-creation runs on the REAL path.

The behavior tests in ``tests/game/test_quest_seed.py`` prove
``seed_quest_spine`` works in isolation. This test keeps Dev honest
(CLAUDE.md "Verify Wiring, Not Just Existence"): it drives a fresh character
through the actual chargen-commit handler (``_chargen_confirmation``, where the
built PC is materialized onto the canonical snapshot) and asserts the seed
fired there — not that a helper merely exists.

It is deliberately content-agnostic about whether caverns_and_claudes populates
``drive``: the load-bearing wiring proof is that exactly one
``quest.seeded_at_creation`` span fires on commit, and that the span's verdict
is internally consistent with the resulting snapshot state (warning ⟺ empty
spine). That holds whether the chargen PC ends up with a drive or not, so the
test pins the wiring without coupling to a specific pack's drive content.

Reuses the chargen-commit harness from ``test_chargen_dispatch`` and the
``otel_capture`` span exporter from ``tests/server/conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.protocol.messages import CharacterCreationPayload
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import mock_claude_client_factory as _mock_claude_client_factory
from tests.server.test_chargen_dispatch import (
    _connect,
    _send_chargen,
    _walk_to_confirmation,
    run,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
SPAN_NAME = "quest.seeded_at_creation"


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


def test_quest_seed_fires_on_real_chargen_commit(
    handler: WebSocketSessionHandler, otel_capture
) -> None:
    async def body() -> None:
        await _connect(handler)
        await _walk_to_confirmation(handler, freeform_name="Rux")
        out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
        assert out, "confirmation must return at least the CHARACTER_CREATION{complete} message"

    run(body())

    sd = handler._session_data  # type: ignore[attr-defined]
    assert len(sd.snapshot.characters) == 1, "PC must be materialized on the snapshot"

    seed_spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_NAME]
    assert len(seed_spans) == 1, (
        "exactly one quest.seeded_at_creation span must fire on the real chargen-commit "
        f"path — the seed is not wired in (got {len(seed_spans)})"
    )

    attrs = dict(seed_spans[0].attributes or {})
    snap = sd.snapshot
    seeded_empty = not snap.quest_log and not snap.quest_anchors and snap.active_stakes == ""

    if attrs.get("severity") == "warning":
        # Loud-degrade path: span warned, so the spine MUST be empty (no
        # fabrication) and has_stakes False.
        assert seeded_empty, "warning span but a spine was fabricated — inconsistent"
        assert attrs.get("has_stakes") is False
    else:
        # Real seed: span clean, so the spine MUST be populated and consistent.
        assert not seeded_empty, "non-warning span but the spine is empty — seed produced nothing"
        assert attrs.get("has_stakes") is True
        assert len(snap.quest_anchors) >= 1
