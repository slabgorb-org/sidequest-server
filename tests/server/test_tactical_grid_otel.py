"""Story 158-18 — Task 6: tactical_grid.emitted watcher span carries counts.

Asserts that _maybe_emit_tactical_grid publishes token_count, feature_count,
exit_count, and poi_count on the tactical_grid.emitted watcher event.

These counts let the GM panel prove the payload is non-hollow without
correlating with sibling spans (OTEL Observability Principle / ADR-090).

Capture pattern mirrors tests/integration/test_tactical_grid_runtime_wiring.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.server.tactical_emit_fixtures import build_sd_with_tactical_region


@pytest.mark.integration
def test_emitted_span_carries_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """End-to-end: tactical_grid.emitted must carry token/feature/exit/poi counts.

    RED state before Step 3: the watcher event is missing these four fields —
    KeyError / assertion failure on 'token_count' not in fields.
    GREEN state after Step 3: all four fields present, feature_count >= 1.
    """
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))

    sd, snapshot, room_id = build_sd_with_tactical_region()

    import sidequest.server.websocket_handlers.map_emit as map_emit

    events: list[tuple[str, dict[str, Any]]] = []

    def _capture(
        event: str,
        attrs: dict[str, Any],
        *,
        component: str | None = None,
        severity: str | None = None,
    ) -> Any:
        events.append((event, dict(attrs)))

    monkeypatch.setattr(map_emit, "_watcher_publish", _capture)

    from sidequest.server.websocket_handlers.map_emit import _maybe_emit_tactical_grid

    _maybe_emit_tactical_grid(
        object(),
        sd=sd,
        snapshot=snapshot,
        actor="Rux",
        emit_fn=lambda *a, **k: None,
        room_id_override=room_id,
    )

    emitted = [attrs for ev, attrs in events if ev == "tactical_grid.emitted"]
    assert emitted, (
        "tactical_grid.emitted watcher event did not fire — "
        "_maybe_emit_tactical_grid did not reach the publish site"
    )
    fields = emitted[0]
    assert "token_count" in fields, (
        f"'token_count' missing from tactical_grid.emitted fields: {list(fields)}"
    )
    assert "feature_count" in fields, (
        f"'feature_count' missing from tactical_grid.emitted fields: {list(fields)}"
    )
    assert "exit_count" in fields, (
        f"'exit_count' missing from tactical_grid.emitted fields: {list(fields)}"
    )
    assert "poi_count" in fields, (
        f"'poi_count' missing from tactical_grid.emitted fields: {list(fields)}"
    )
    assert fields["feature_count"] >= 1, (
        f"feature_count={fields['feature_count']} — fixture tactical block has "
        f"at least one TacticalFeatureCell; the emit path failed to populate features"
    )
