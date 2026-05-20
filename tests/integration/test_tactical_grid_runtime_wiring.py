"""Story 52-5 — Server end-to-end wiring: runtime cavern PNG reaches the UI.

Epic 52's pipeline ends at the UI consumer. Stories 52-2/52-3/52-4 shipped
materializer → mask BLOB → PNG sidecar. This test drives the live
``_maybe_emit_tactical_grid`` dispatch site through the *runtime* branch
(``_maybe_build_runtime_cavern_payload``) and asserts:

1.  A ``TACTICAL_GRID`` message reaches ``emit_fn`` with a runtime-sourced
    ``cavern_image_url`` (prefix is ``artifacts/dungeon/.../regions/...``,
    not the static ``genre_packs/.../rooms/...`` shape).

2.  The watcher event ``tactical_grid.emitted`` carries an explicit
    ``source: "runtime"`` discriminator so the GM panel (Sebastien) can
    answer "is this cavern PNG runtime-generated or static?" without
    correlating with a sibling OTEL span. **This attribute does not yet
    exist on the published event** — that is the RED state this test
    exposes for Dev to close.

3.  No silent fallback: the URL is non-None and the message round-trips.

The test uses a fake ``DungeonStore`` whose ``load_masks()`` returns one
hand-crafted persisted mask — the same dict shape ``RegionMask.to_dict()``
produces. This is the minimal fixture that reaches the production
branch without spinning up the full Beneath Sünden materializer.

Project rules honoured:
  * **Verify Wiring, Not Just Existence** — the test calls the live
    ``_maybe_emit_tactical_grid`` (NOT the inner helper) so the static→
    runtime fallback edge in the wrapper is exercised.
  * **No Silent Fallbacks** — asserts the URL is non-None and the
    discriminator is published explicitly, not inferred.
  * **OTEL Observability Principle** — the source discriminator goes on
    the watcher event so the GM panel sees runtime vs static at a glance.
  * Python lang-review #5 (path handling): all paths use ``pathlib.Path``.
  * Python lang-review #6 (test quality): every assertion is a real value
    check, not ``assert result`` or ``assert x.is_not_none()``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CAVERNS_PACK = REPO_ROOT / "sidequest-content" / "genre_packs" / "caverns_and_claudes"
# ``beneath_sunden`` is the current live procedural world (the old
# ``caverns_sunden`` static hub was deprecated and moved to
# ``genre_workshopping/``). Per project memory: never re-render or re-add
# ``caverns_sunden`` to the live packs.
BENEATH_SUNDEN_WORLD = CAVERNS_PACK / "worlds" / "beneath_sunden"


def _packs_available() -> bool:
    return CAVERNS_PACK.exists() and BENEATH_SUNDEN_WORLD.exists()


def _runtime_mask_dict(rows: list[str], *, cell_width: int = 28) -> dict[str, Any]:
    """Build a persisted-mask dict matching ``RegionMask.to_dict()`` —
    the shape ``DungeonStore.load_masks()`` returns from the BLOB column."""
    mask_bytes = ("\n".join(rows)).encode("ascii")
    return {
        "mask_bytes_b64": base64.b64encode(mask_bytes).decode("ascii"),
        "mask_sha": hashlib.sha256(mask_bytes).hexdigest(),
        "block": {
            "cell_width": cell_width,
            "grid_width": len(rows[0]) if rows else 0,
            "grid_height": len(rows),
            "origin_x": 0,
            "origin_y": 0,
        },
    }


class _FakeDungeonStore:
    """Minimal stand-in for ``sidequest.dungeon.persistence.DungeonStore``.

    The runtime-payload helper only calls ``load_masks()``; nothing else
    on the store. A fake keeps the test independent of dungeon schema
    bootstrap (campaign_seed write-once, expansion commit, etc.).
    """

    def __init__(self, masks: dict[str, dict[str, Any]]) -> None:
        self._masks = masks

    def load_masks(self) -> dict[str, dict[str, Any]]:
        return dict(self._masks)


@pytest.mark.integration
def test_runtime_cavern_path_emits_tactical_grid_with_source_discriminator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a persisted runtime mask + a ``room_id`` matching that
    mask MUST emit a ``TACTICAL_GRID`` message via the runtime branch and
    publish a ``source: "runtime"`` discriminator on the watcher event.

    RED state expected before Dev's GREEN change:
      * ``_maybe_emit_tactical_grid`` emits the message correctly (52-4
        shipped that) — this assertion may pass on RED.
      * ``tactical_grid.emitted`` watcher event does NOT include a
        ``source`` attribute — this assertion FAILS on RED. Adding the
        attribute is the wire that closes Epic 52.
    """
    if not _packs_available():
        pytest.skip("caverns_and_claudes content pack not present")

    # noqa block: import order is load-bearing and must not be auto-sorted.
    # Importing session_handler FIRST resolves its bottom-of-file
    # ``WebSocketSessionHandler`` re-export before any other caller observes
    # ``websocket_session_handler`` mid-init. Reversing the order trips a
    # circular-import (websocket_session_handler imports session_handler at
    # the top, session_handler re-exports from websocket_session_handler at
    # the bottom — only one entry order works for fresh-import test
    # sessions). See ``tests/integration/test_room_enter_cavern.py`` for the
    # canonical entry order this mirrors. Auto-sort alphabetically would
    # place `sidequest.server` (bare module) before
    # `sidequest.server.session_handler` and re-introduce the cycle.
    from sidequest.agents.orchestrator import Orchestrator  # noqa: I001
    from sidequest.game.persistence import SqliteStore
    from sidequest.game.session import GameSnapshot
    from sidequest.genre.loader import load_genre_pack
    from sidequest.protocol.messages import TacticalGridMessage
    from sidequest.server.session_handler import _SessionData
    from sidequest.server import websocket_session_handler as wsh

    # Resolve URLs locally — easier to assert against a relative-path
    # prefix than against a CDN host. The asset_url module reads the env
    # var fresh on every call, so monkeypatch is sufficient.
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")

    # The runtime-payload helper writes a PNG sidecar via
    # ``emit_runtime_cavern_png`` to ``$SIDEQUEST_OUTPUT_DIR /
    # artifacts/dungeon/<save_id>/regions/<room_id>.cavern.png``.
    monkeypatch.setenv("SIDEQUEST_OUTPUT_DIR", str(tmp_path))

    # A 5x5 mask the runtime emitter knows how to render. Floor = '.',
    # wall = '#'. The exact glyphs match ``emit_runtime_cavern_png``'s
    # decoder (room_file_loader._decode_runtime_mask_grid).
    region_id = "region_runtime_alpha"
    mask = _runtime_mask_dict(
        rows=[
            "#####",
            "#...#",
            "#.#.#",
            "#...#",
            "#####",
        ]
    )

    pack = load_genre_pack(CAVERNS_PACK)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    snap.character_locations["Rux"] = region_id
    snap.discovered_rooms = [region_id]

    orchestrator = Orchestrator.__new__(Orchestrator)
    store = SqliteStore.open_in_memory()
    store.init_session("caverns_and_claudes", "caverns_sunden")

    sd = _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        player_name="Rux",
        player_id="player-runtime-wiring",
        snapshot=snap,
        store=store,
        genre_pack=pack,
        orchestrator=orchestrator,
    )
    # The Decision-N gate shape (`getattr(sd, "dungeon_store", None)` then
    # `if <var> is not None:`) is the production hook the runtime branch
    # reads. Attaching the fake store is the entire bridge into the
    # runtime branch.
    sd.dungeon_store = _FakeDungeonStore({region_id: mask})  # type: ignore[attr-defined]

    emitted_messages: list[Any] = []

    def capture_emit(msg: Any, kind: str) -> None:
        emitted_messages.append((kind, msg))

    watcher_events: list[tuple[str, dict[str, Any]]] = []
    original_publish = wsh._watcher_publish

    def capture_watcher(
        event: str,
        attrs: dict[str, Any],
        *,
        component: str | None = None,
        severity: str | None = None,
    ) -> Any:
        watcher_events.append((event, dict(attrs)))
        return original_publish(event, attrs, component=component, severity=severity)

    monkeypatch.setattr(wsh, "_watcher_publish", capture_watcher)

    wsh._maybe_emit_tactical_grid(
        None,
        sd=sd,
        snapshot=snap,
        actor="Rux",
        emit_fn=capture_emit,
    )

    # ── AC1: exactly one TACTICAL_GRID emitted via the runtime branch ──
    assert len(emitted_messages) == 1, (
        f"Expected 1 TACTICAL_GRID message via runtime branch; got "
        f"{len(emitted_messages)}. Runtime cavern path is not wired into "
        f"_maybe_emit_tactical_grid."
    )
    kind, msg = emitted_messages[0]
    assert kind == "TACTICAL_GRID", f"Expected kind 'TACTICAL_GRID'; got {kind!r}"
    assert isinstance(msg, TacticalGridMessage), (
        f"Expected TacticalGridMessage; got {type(msg).__name__}"
    )

    payload = msg.payload
    assert payload.room_id == region_id, (
        f"Expected payload.room_id={region_id!r}; got {payload.room_id!r}"
    )
    assert payload.room_type == "cavern", (
        f"Expected room_type='cavern' for runtime mask; got {payload.room_type!r}"
    )

    # ── AC2: cavern_image_url survives end-to-end and points at the
    # runtime sidecar path (artifacts/dungeon/...) not the static
    # (genre_packs/...) path. No silent fallback to None. ──
    assert payload.cavern_image_url is not None, (
        "cavern_image_url is None — the runtime branch silently dropped "
        "the URL between emit_runtime_cavern_png and the message payload"
    )
    assert "artifacts/dungeon/" in payload.cavern_image_url, (
        f"cavern_image_url {payload.cavern_image_url!r} does not contain "
        f"'artifacts/dungeon/' — the runtime branch produced a URL pointing "
        f"at the wrong location (static genre_packs/ prefix or unrelated)"
    )
    assert payload.cavern_image_url.endswith(f"{region_id}.cavern.png"), (
        f"cavern_image_url {payload.cavern_image_url!r} does not end with "
        f"{region_id}.cavern.png — the URL is for a different region"
    )

    # ── AC3: mask survives so the UI's cell-stepped math has truth ──
    assert payload.mask is not None, (
        "payload.mask is None — runtime branch failed to decode the BLOB"
    )
    assert payload.mask.count(".") > 0, (
        f"payload.mask {payload.mask!r} contains no floor cells — the "
        f"mask was either corrupted or the wrong cell glyphs were emitted"
    )
    assert payload.cell_size == 28, (
        f"Expected cell_size=28 (from block.cell_width); got {payload.cell_size}"
    )

    # ── AC4: the watcher event for the GM panel includes a runtime/static
    # discriminator so Sebastien can tell which path produced the PNG. ──
    grid_emit_events = [
        attrs for ev, attrs in watcher_events if ev == "tactical_grid.emitted"
    ]
    assert len(grid_emit_events) == 1, (
        f"Expected exactly 1 'tactical_grid.emitted' watcher event; "
        f"got {len(grid_emit_events)}. Events seen: "
        f"{[ev for ev, _ in watcher_events]}"
    )
    emit_attrs = grid_emit_events[0]
    assert "source" in emit_attrs, (
        "watcher event 'tactical_grid.emitted' is missing the 'source' "
        "attribute — the GM panel cannot distinguish runtime PNG from "
        "static PNG without correlating sibling OTEL spans (ADR-090 / "
        "CLAUDE.md OTEL Observability Principle). Add source: 'runtime' "
        "to the runtime-payload branch's _watcher_publish call."
    )
    assert emit_attrs["source"] == "runtime", (
        f"Expected source='runtime' on the watcher event from the runtime "
        f"branch; got source={emit_attrs.get('source')!r}. The runtime "
        f"branch must publish a distinct discriminator from the static branch."
    )

    # ── AC5: the runtime PNG sidecar actually lands on disk where
    # resolve_asset_url claims it lives. Closes the no-silent-fallback
    # loop — without this, the test could pass even if the URL points at
    # a missing file. ──
    expected_sidecar = tmp_path / "artifacts" / "dungeon"
    rendered = list(expected_sidecar.rglob("*.cavern.png")) if expected_sidecar.exists() else []
    assert len(rendered) == 1, (
        f"Expected exactly 1 runtime cavern PNG under {expected_sidecar}; "
        f"got {len(rendered)} ({rendered}). The runtime emitter wrote the "
        f"URL to the payload but failed to write the file — the UI would "
        f"render a broken image."
    )
    assert rendered[0].name == f"{region_id}.cavern.png", (
        f"Sidecar filename mismatch: expected {region_id}.cavern.png, "
        f"got {rendered[0].name}"
    )


# NOTE: a symmetric "static branch publishes source='static'" test is
# not written here because zero live packs currently ship an authored
# *.cavern.png + cavern-type room YAML pair (the deprecated
# caverns_sunden was moved to genre_workshopping/; beneath_sunden's
# rooms are all room_type=settlement). The only path from server to a
# UI-rendered cavern PNG today is the runtime branch — which is exactly
# why Epic 52 exists. Dev's GREEN diff for 52-5 SHOULD still add
# source="static" symmetrically at the static branch's
# _watcher_publish call so the attribute exists when the first
# authored cavern room lands; a follow-up story adds the matching test
# alongside that content. The runtime-side assertion above is the
# load-bearing discriminator for 52-5 RED.
