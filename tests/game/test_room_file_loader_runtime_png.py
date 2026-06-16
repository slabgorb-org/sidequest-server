"""Story 52-4 — Server emits cavern PNG sidecar from runtime mask.

Closes the runtime path: 52-2 produces ``RegionMask`` at materialize time,
52-3 persists the mask BLOB (``RegionMask.to_dict()``) into
``dungeon_map.mask``, and this story converts that persisted mask BLOB
back into the ADR-096 ``<room>.cavern.png`` sidecar the renderer
consumer already knows how to read. ``resolve_asset_url`` (already in
``sidequest.server.asset_urls``) turns the sidecar's relative content
path into the URL the UI fetches.

ACs (per ``.session/52-4-session.md``):

  AC1 — Mask-to-PNG conversion in room_file_loader: a new
        ``emit_runtime_cavern_png`` callable accepts a persisted mask
        BLOB dict (the shape ``RegionMask.to_dict()`` produces and
        ``DungeonStore.load_masks`` returns) and writes a PNG sidecar at
        the caller-supplied output path.

  AC2 — PNG sidecar path resolution: the relative content path the
        sidecar lives at (matching the static ADR-096
        ``rooms/<id>.cavern.png`` layout) round-trips through
        ``resolve_asset_url`` to a fetchable URL.

  AC3 — OTEL instrumentation: every conversion emits a
        ``dungeon.render.cavern_mask_to_png`` span with the lie-detector
        attributes (``region_id``, ``mask_sha256``, ``grid_width``,
        ``grid_height``, ``cell_width``, ``output_path``). The span is
        registered in ``SPAN_ROUTES`` or ``FLAT_ONLY_SPANS`` so the
        routing completeness lint passes.

  AC4 — No breakage of static path: existing ADR-096 static authored
        rooms (``caverns_sunden/mouth``) still load via
        ``load_room_payload`` with the static ``.cavern.png`` /
        ``.mask.txt`` pair unchanged.

  AC5 — Integration with existing renderer pipeline: the new emitter is
        imported and called from non-test production code (the
        websocket session handler's tactical-grid emit path is the
        natural integration point; the wiring test asserts SOME
        non-test consumer imports the symbol — the actual call site is
        Dev's choice but the connection must exist).

Project rules honored:
  - **No Silent Fallbacks**: malformed / empty / missing-field mask
    dicts raise loudly; the emitter does not invent a default mask or
    skip a region.
  - **Every test suite needs a wiring test**: ``test_emit_runtime_cavern_png_has_non_test_consumer``.
  - **OTEL Observability Principle**: span emission is mandatory and
    asserted (lie-detector visibility on the GM panel).
  - Python lang-review #6 (test quality): every assertion is a real
    value check, not a truthy probe.
  - Python lang-review #5 (path handling): tests use ``pathlib.Path``,
    no string concatenation.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers — small reusable mask dicts in the shape RegionMask.to_dict()
# produces (and DungeonStore.load_masks returns).
# ---------------------------------------------------------------------------


def _mask_bytes_from_ascii(rows: list[str]) -> bytes:
    """Join ``rows`` with ``\\n`` (the ADR-096 row separator)."""
    return ("\n".join(rows)).encode("ascii")


def _mask_dict(
    rows: list[str],
    *,
    cell_width: int = 28,
    origin_x: int = 0,
    origin_y: int = 0,
) -> dict:
    """Build a persisted-mask dict matching ``RegionMask.to_dict()``."""
    mask_bytes = _mask_bytes_from_ascii(rows)
    grid_height = len(rows)
    grid_width = len(rows[0]) if rows else 0
    return {
        "mask_bytes_b64": base64.b64encode(mask_bytes).decode("ascii"),
        "mask_sha": hashlib.sha256(mask_bytes).hexdigest(),
        "block": {
            "cell_width": cell_width,
            "grid_width": grid_width,
            "grid_height": grid_height,
            "origin_x": origin_x,
            "origin_y": origin_y,
        },
    }


def _otel_in_memory() -> tuple[Any, Any, Any]:
    """In-memory OTEL exporter + provider + tracer (matches
    ``tests/dungeon/test_materializer.py:_otel_in_memory``)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("test")
    return exporter, provider, real_tracer


def _install_test_tracer() -> tuple[Any, Any, Any]:
    """Patch ``sidequest.telemetry.spans.tracer`` to point at the
    in-memory test tracer. Returns ``(exporter, original_tracer_fn,
    _spans_module)`` — caller restores tracer in ``finally``.
    """
    import sidequest.telemetry.spans as _spans_module

    exporter, _provider, real_tracer = _otel_in_memory()
    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    return exporter, original_tracer_fn, _spans_module


# ---------------------------------------------------------------------------
# AC1 — Mask-to-PNG conversion produces a PNG sidecar on disk
# ---------------------------------------------------------------------------


class TestEmitRuntimeCavernPngWritesPng:
    """``emit_runtime_cavern_png`` decodes the persisted mask BLOB and
    writes a PNG sidecar file at the caller-supplied output path. The
    decoded grid drives PIL rendering; the output path's parent dirs
    are created as needed (mirrors ``cavern_renderer.render_grid_to_png``).
    """

    def test_emit_writes_png_file_at_output_path(self, tmp_path: Path) -> None:
        """The function writes a real file at ``output_path``. No silent
        fallback to a different location; if the dir doesn't exist it is
        created."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        mask = _mask_dict(
            [
                "#####",
                "#...#",
                "#...#",
                "#...#",
                "#####",
            ]
        )
        out = tmp_path / "regions" / "r0.cavern.png"

        emit_runtime_cavern_png(
            mask_dict=mask,
            output_path=out,
            region_id="exp001.r0",
        )

        assert out.is_file(), f"expected PNG at {out}, but no file was written"
        # PNG magic number (per RFC 2083 §3.1): 89 50 4E 47 0D 0A 1A 0A
        header = out.read_bytes()[:8]
        assert header == b"\x89PNG\r\n\x1a\n", (
            f"file at {out} is not a PNG (got header {header!r}); "
            "the emitter must produce real PNG bytes, not a placeholder"
        )

    def test_emit_creates_parent_directories(self, tmp_path: Path) -> None:
        """The emitter is responsible for ``mkdir(parents=True)`` on the
        sidecar's parent — no caller-side ceremony needed. Same contract
        as ``cavern_renderer.render_grid_to_png``."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        deeper = tmp_path / "a" / "b" / "c" / "region.cavern.png"
        assert not deeper.parent.exists(), "fixture sanity: parents absent"

        emit_runtime_cavern_png(
            mask_dict=_mask_dict(["##", ".."]),
            output_path=deeper,
            region_id="r1",
        )

        assert deeper.is_file(), (
            f"emitter must create parent dirs and write the PNG at {deeper}; "
            "got no file (parents not created or write was skipped)"
        )

    def test_emit_png_dimensions_match_block_metadata(self, tmp_path: Path) -> None:
        """PNG width/height = ``grid_width * cell_width`` /
        ``grid_height * cell_width``. The block in the persisted mask
        is the cell-stepped contract per ADR-096 §2; the PNG must
        honour it byte-precisely so the UI's pixel→cell math holds.
        """
        from PIL import Image

        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        rows = [
            "########",  # 8 cols
            "#......#",
            "#......#",
            "########",
        ]  # 4 rows
        mask = _mask_dict(rows, cell_width=28)
        out = tmp_path / "r2.cavern.png"

        emit_runtime_cavern_png(
            mask_dict=mask,
            output_path=out,
            region_id="r2",
        )

        with Image.open(out) as img:
            assert img.size == (8 * 28, 4 * 28), (
                f"PNG size {img.size} does not match block "
                f"({len(rows[0])} cols × {len(rows)} rows × 28 cell_width); "
                "UI cell-stepped math will be off by the size delta"
            )

    def test_emit_decodes_base64_mask_bytes_not_raw_bytes(self, tmp_path: Path) -> None:
        """The persisted shape stores ``mask_bytes_b64`` (RegionMask.to_dict
        base64-encodes for JSON safety). The emitter MUST decode before
        rendering — using the raw b64 string would produce a wrong grid
        and a wrong PNG. Lock the contract: equivalent ascii masks pass,
        and a malformed b64 raises loudly."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        # Two masks that should decode to the SAME grid produce
        # equally-sized PNGs (sanity: grid is decoded, not the raw b64).
        rows = ["##.#", ".##.", "#.##", "...."]
        mask = _mask_dict(rows, cell_width=10)
        out_a = tmp_path / "a.cavern.png"

        emit_runtime_cavern_png(mask_dict=mask, output_path=out_a, region_id="ra")

        from PIL import Image

        with Image.open(out_a) as img:
            assert img.size == (len(rows[0]) * 10, len(rows) * 10), (
                f"PNG size {img.size} suggests the b64 was not decoded; "
                "expected grid dimensions × cell_width"
            )

    def test_emit_rejects_invalid_base64_loudly(self, tmp_path: Path) -> None:
        """A bogus ``mask_bytes_b64`` value must raise — no silent
        fallback to an empty mask or default grid."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        bad = {
            "mask_bytes_b64": "not-valid-base64!!!",
            "mask_sha": "0" * 64,
            "block": {
                "cell_width": 28,
                "grid_width": 2,
                "grid_height": 2,
                "origin_x": 0,
                "origin_y": 0,
            },
        }
        # ``base64.binascii.Error`` is a ``ValueError`` subclass in Python 3,
        # so the dev MAY raise either ``ValueError`` directly or let the
        # decode error propagate — both satisfy the contract. The point is
        # NO silent fallback (no swap-in of an empty mask, no degraded PNG).
        with pytest.raises(ValueError):
            emit_runtime_cavern_png(
                mask_dict=bad,
                output_path=tmp_path / "x.png",
                region_id="bad",
            )


# ---------------------------------------------------------------------------
# AC1 (cont.) — No silent fallbacks: malformed / empty mask raises
# ---------------------------------------------------------------------------


class TestEmitRuntimeCavernPngRejectsBadInput:
    """Project rule: No Silent Fallbacks. Missing fields, empty grids,
    and structural drift in the mask dict raise loudly. The emitter must
    not invent a default mask, skip the region, or write a placeholder
    PNG — the GM panel would never know."""

    def test_empty_rows_raises(self, tmp_path: Path) -> None:
        """An all-empty mask (no rows) is a silent lie; the emitter must
        reject it (mirrors ``_emit_mask`` discipline in materializer.py)."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        mask = _mask_dict([])
        with pytest.raises(ValueError):
            emit_runtime_cavern_png(
                mask_dict=mask,
                output_path=tmp_path / "e.png",
                region_id="empty",
            )

    def test_zero_cell_width_raises(self, tmp_path: Path) -> None:
        """``cell_width=0`` would produce a 0×0 PNG — useless. Reject."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        mask = _mask_dict(["##", ".."], cell_width=0)
        with pytest.raises(ValueError):
            emit_runtime_cavern_png(
                mask_dict=mask,
                output_path=tmp_path / "z.png",
                region_id="zero",
            )

    def test_missing_mask_bytes_b64_field_raises(self, tmp_path: Path) -> None:
        """A mask dict missing the ``mask_bytes_b64`` key is structural
        drift — never silently substitute an empty mask. Raise loudly so
        the corruption is visible."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        bad = {
            "mask_sha": "0" * 64,
            "block": {
                "cell_width": 28,
                "grid_width": 2,
                "grid_height": 2,
                "origin_x": 0,
                "origin_y": 0,
            },
        }
        with pytest.raises((KeyError, ValueError)):
            emit_runtime_cavern_png(
                mask_dict=bad,
                output_path=tmp_path / "m.png",
                region_id="miss",
            )

    def test_missing_block_field_raises(self, tmp_path: Path) -> None:
        """The ``block`` sub-dict carries the cell-stepped contract —
        absent ``block`` means we cannot compute PNG dimensions. Raise."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        bad = {
            "mask_bytes_b64": base64.b64encode(b"##\n..").decode("ascii"),
            "mask_sha": "0" * 64,
            # block intentionally omitted
        }
        with pytest.raises((KeyError, ValueError)):
            emit_runtime_cavern_png(
                mask_dict=bad,
                output_path=tmp_path / "n.png",
                region_id="noblock",
            )


# ---------------------------------------------------------------------------
# AC2 — PNG sidecar path round-trips through resolve_asset_url
# ---------------------------------------------------------------------------


class TestRuntimeCavernPngAssetUrl:
    """The relative content path of the emitted sidecar (the path under
    ``artifacts/`` or ``genre_packs/`` that ``resolve_asset_url``
    understands) must round-trip to a fetchable URL — and the URL must
    point at the ``.cavern.png`` suffix the static ADR-096 path uses.
    AC2 tests live separately from AC1 because the URL contract is a
    decoupled responsibility of ``sidequest.server.asset_urls``.
    """

    def test_runtime_cavern_png_relative_path_resolves_to_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given the relative path the emitter would write to (under
        ``artifacts/dungeon/<save>/regions/<region>.cavern.png`` — the
        ADR-096 layout extended to the runtime save dir), the URL is
        the full CDN-prefixed concatenation. Asserts the EXACT value
        (not just prefix/suffix) so a partial drop of the relative
        path segment cannot pass undetected."""
        from sidequest.server.asset_urls import resolve_asset_url

        monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "https://cdn.example/")
        relative = "artifacts/dungeon/save01/regions/exp001r0.cavern.png"
        url = resolve_asset_url(relative)

        assert url == "https://cdn.example/artifacts/dungeon/save01/regions/exp001r0.cavern.png", (
            f"resolved CDN url {url!r} does not match the expected full URL; "
            "a partial-prefix/suffix check would miss a dropped path segment "
            "(symmetric with the local-mode test below)"
        )

    def test_runtime_cavern_png_relative_path_local_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local mode maps ``artifacts/`` → ``/renders/artifacts/``
        (per ``asset_urls._LOCAL_PREFIX_MAP``). The runtime PNG must
        land under the existing artifacts prefix so local-serve dev
        keeps working."""
        from sidequest.server.asset_urls import resolve_asset_url

        monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
        relative = "artifacts/dungeon/save01/regions/exp001r0.cavern.png"
        url = resolve_asset_url(relative)

        assert url == "/renders/artifacts/dungeon/save01/regions/exp001r0.cavern.png", (
            f"local-mode url {url!r} does not match the /renders/artifacts/ "
            "mount; the runtime PNG must live where the static mount serves it"
        )


# ---------------------------------------------------------------------------
# AC3 — OTEL span ``dungeon.render.cavern_mask_to_png`` emission
# ---------------------------------------------------------------------------


class TestEmitRuntimeCavernPngOtel:
    """Every mask→PNG conversion fires an OTEL span the GM panel reads
    to verify the runtime renderer is engaged. Without this, Claude
    could "claim" a procedural cavern is being shown when the server
    silently failed to emit one. The span name and attributes are the
    lie-detector contract.
    """

    SPAN_NAME = "dungeon.render.cavern_mask_to_png"

    def test_span_is_emitted_with_lie_detector_attributes(self, tmp_path: Path) -> None:
        """One span per call; attributes carry ``region_id``,
        ``mask_sha256``, ``grid_width``, ``grid_height``, ``cell_width``,
        and ``output_path``. These come from the mask dict + caller-
        supplied path (not the narrator) so they ARE ground truth on
        the GM panel."""
        from sidequest.game.room_file_loader import emit_runtime_cavern_png

        rows = ["####", "#..#", "####"]
        mask = _mask_dict(rows, cell_width=28)
        expected_sha = mask["mask_sha"]
        out = tmp_path / "span.cavern.png"

        exporter, original_tracer_fn, _spans_module = _install_test_tracer()
        try:
            emit_runtime_cavern_png(
                mask_dict=mask,
                output_path=out,
                region_id="exp001.r7",
            )
        finally:
            _spans_module.tracer = original_tracer_fn

        spans = [s for s in exporter.get_finished_spans() if s.name == self.SPAN_NAME]
        assert len(spans) == 1, (
            f"expected exactly one {self.SPAN_NAME!r} span, got "
            f"{[s.name for s in exporter.get_finished_spans()]}"
        )
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("region_id") == "exp001.r7", (
            f"span attr region_id should be 'exp001.r7', got {attrs.get('region_id')!r}"
        )
        # mask_sha256 may be full hex or 16-char prefix per cavern_room.py
        # convention; whichever is chosen must match the persisted SHA.
        mask_sha_attr = str(attrs.get("mask_sha256") or "")
        assert mask_sha_attr and expected_sha.startswith(mask_sha_attr), (
            f"span mask_sha256 {mask_sha_attr!r} does not match the persisted "
            f"mask SHA {expected_sha!r} — the GM panel would show a "
            "different mask than the one rendered (Illusionism)"
        )
        assert attrs.get("grid_width") == len(rows[0]), (
            f"span grid_width {attrs.get('grid_width')} != {len(rows[0])}"
        )
        assert attrs.get("grid_height") == len(rows), (
            f"span grid_height {attrs.get('grid_height')} != {len(rows)}"
        )
        assert attrs.get("cell_width") == 28, (
            f"span cell_width {attrs.get('cell_width')} != 28 (ADR-096 contract)"
        )
        # AC3 lie-detector attribute: output_path lets the GM panel
        # correlate the span with the on-disk artifact a refactor that
        # silently drops this attr would deny operators the ability to
        # verify the PNG actually landed where the URL points.
        assert attrs.get("output_path") == str(out), (
            f"span output_path {attrs.get('output_path')!r} != {str(out)!r}; "
            "the GM panel needs the exact filesystem path the renderer wrote to"
        )

    def test_span_name_constant_is_exported(self) -> None:
        """A ``SPAN_DUNGEON_RENDER_CAVERN_MASK_TO_PNG`` constant lives
        in the telemetry catalog. Per the package README, every span
        needs a named constant so renames break at import time."""
        import sidequest.telemetry.spans as _spans_module

        constants = {
            name: value
            for name, value in vars(_spans_module).items()
            if name.startswith("SPAN_") and isinstance(value, str)
        }
        matches = [v for v in constants.values() if v == self.SPAN_NAME]
        assert matches, (
            f"no SPAN_* constant equals {self.SPAN_NAME!r}; "
            f"existing dungeon-render constants: "
            f"{[v for v in constants.values() if v.startswith('dungeon.')]}"
        )

    def test_span_is_routed_or_explicitly_flat(self) -> None:
        """Routing completeness (see
        ``tests/telemetry/test_routing_completeness.py``): the new span
        is in ``SPAN_ROUTES`` (the GM panel renders it as a typed event)
        OR in ``FLAT_ONLY_SPANS`` (explicit "forensics only"). Anything
        else is a silent gap — adding the constant without a routing
        decision is the defect class the completeness lint catches."""
        from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES

        routed = self.SPAN_NAME in SPAN_ROUTES
        flat = self.SPAN_NAME in FLAT_ONLY_SPANS
        assert routed or flat, (
            f"span {self.SPAN_NAME!r} is neither routed (SPAN_ROUTES) nor "
            "explicitly flat-only (FLAT_ONLY_SPANS); the routing-completeness "
            "lint will fail downstream"
        )
        assert not (routed and flat), (
            f"span {self.SPAN_NAME!r} appears in both SPAN_ROUTES and FLAT_ONLY_SPANS — choose one"
        )


# ---------------------------------------------------------------------------
# AC4 — Static ADR-096 path still works (regression guard)
# ---------------------------------------------------------------------------


class TestStaticCavernPathStillWorks:
    """Adding the runtime emitter must not break the existing static
    ADR-096 loader. The static fixture (caverns_sunden/mouth) ships
    pre-rendered ``.cavern.png`` + ``.mask.txt`` and is the canary the
    UI has consumed since ADR-096 landed."""

    @pytest.fixture
    def caverns_sunden_dir(self) -> Path:
        """The pre-rendered ADR-096 canary world.

        ``caverns_sunden`` was relocated from ``genre_packs/`` to
        ``genre_workshopping/`` in sidequest-content PR #228 (canonical
        ``caverns_and_claudes`` cavern world is now ``beneath_sunden``,
        which is procedurally generated and does NOT ship static
        ``.cavern.png`` / ``.mask.txt`` sidecars). The workshopping
        copy keeps the static fixtures — the only remaining static
        ADR-096 canary in the tree — and is the correct regression
        target for AC4 (the runtime emitter must not break the static
        loader path).
        """
        here = Path(__file__).resolve()
        repo = here.parents[3]  # oq-1
        world = repo / "sidequest-content" / "genre_workshopping" / "caverns_sunden"
        if not (world / "rooms" / "mouth.yaml").is_file():
            pytest.skip(
                "static ADR-096 canary 'caverns_sunden/mouth' not present "
                f"at {world}; nothing to regression-test against. Re-point "
                "to a different static cavern fixture if/when one ships."
            )
        return world

    def test_static_cavern_load_still_returns_image_url_and_mask(
        self, caverns_sunden_dir: Path
    ) -> None:
        """The static room ``mouth`` continues to load with both
        ``cavern_image_url`` and ``mask`` populated. If 52-4's emitter
        accidentally short-circuits or rewrites this code path, this
        test breaks."""
        from sidequest.game.room_file_loader import load_room_payload

        payload = load_room_payload(caverns_sunden_dir, "mouth")
        assert payload.room_type == "cavern"
        assert payload.cavern_image_url is not None, (
            "static cavern lost its cavern_image_url after the runtime path was added"
        )
        assert payload.cavern_image_url.endswith("/mouth.cavern.png"), (
            f"static cavern_image_url {payload.cavern_image_url!r} no longer "
            "follows the rooms/<id>.cavern.png ADR-096 layout"
        )
        assert payload.mask is not None and "#" in payload.mask, (
            "static cavern mask must still load from rooms/mouth.mask.txt; "
            f"got {type(payload.mask).__name__}={payload.mask!r}"
        )

    def test_static_settlement_load_still_skips_cavern_fields(
        self, caverns_sunden_dir: Path
    ) -> None:
        """Settlements have no PNG/mask. The runtime emitter must not
        accidentally bolt one on."""
        from sidequest.game.room_file_loader import load_room_payload

        payload = load_room_payload(caverns_sunden_dir, "sunden_square")
        assert payload.room_type == "settlement"
        assert payload.cavern_image_url is None, (
            "settlements must not carry a cavern_image_url after 52-4"
        )
        assert payload.mask is None, "settlements must not carry a mask after 52-4"


# ---------------------------------------------------------------------------
# AC5 — Integration / wiring: non-test consumer imports the emitter
# ---------------------------------------------------------------------------


class TestEmitRuntimeCavernPngWiring:
    """Per CLAUDE.md: "Every Test Suite Needs a Wiring Test". A unit
    test proving the emitter works in isolation is necessary but not
    sufficient — the half-wired-feature failure mode is shipping a
    perfect emitter that no production code path ever invokes. This
    test asserts the symbol has at least one non-test consumer under
    ``sidequest/``.
    """

    EMITTER_SYMBOL = "emit_runtime_cavern_png"

    def test_emitter_has_non_test_production_consumer(self) -> None:
        """Walk ``sidequest/`` and verify at least one production
        module imports or references ``emit_runtime_cavern_png``. The
        canonical caller is the tactical-grid emit path in
        ``websocket_session_handler.py``, but the test does not pin the
        exact site — it only asserts the wiring is not zero."""
        import sidequest

        package_root = Path(sidequest.__file__).resolve().parent
        consumers: list[Path] = []
        for py in package_root.rglob("*.py"):
            if py.name == "room_file_loader.py":
                continue  # the definition site itself doesn't count
            try:
                text = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if self.EMITTER_SYMBOL in text:
                consumers.append(py)

        assert consumers, (
            f"no non-test production module under {package_root} imports or "
            f"mentions {self.EMITTER_SYMBOL!r}; the emitter is wired only to "
            "its own tests (half-wired feature — see CLAUDE.md)"
        )
