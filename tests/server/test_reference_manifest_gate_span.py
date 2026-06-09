"""OTEL coverage for the surviving reference R2-manifest existence gate.

Story 100-12 (Phase 4 cutover) retired the HTML lore-page assembler, and with it
the HTML-route tests that asserted the ``sidequest.reference.manifest_loaded``
span. That span is NOT retired — it still fires in production from
``_gate_poi_slugs_on_manifest`` / ``_gate_cast_slugs_on_manifest``, which
``reference_projection.build_lore_projection`` calls to gate POI/Cast images on
R2 existence. Per the OTEL Observability Principle the GM/dev panel must still be
able to verify the gate engaged, so this suite re-pins the span (name, attrs,
flat-only registration) and asserts it fires end-to-end through the surviving
gate function on the projection path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sidequest.server.reference_renderer import _gate_poi_slugs_on_manifest
from tests.server.conftest import span_attrs_by_name

SPAN_MANIFEST_LOADED = "sidequest.reference.manifest_loaded"


def test_manifest_loaded_span_name_constant() -> None:
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_MANIFEST_LOADED

    assert SPAN_REFERENCE_MANIFEST_LOADED == SPAN_MANIFEST_LOADED


def test_manifest_loaded_span_helper_emits_attrs() -> None:
    from sidequest.telemetry.spans.reference import reference_manifest_loaded_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_manifest_loaded_span(
        path="tests/fixtures/r2_manifest.json",
        entry_count=1743,
        world_key_count=12,
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == SPAN_MANIFEST_LOADED
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.manifest_path"] == "tests/fixtures/r2_manifest.json"
    assert attrs["reference.manifest_entry_count"] == 1743
    assert attrs["reference.world_key_count"] == 12


def test_manifest_loaded_span_registered_flat_only() -> None:
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_MANIFEST_LOADED

    assert SPAN_REFERENCE_MANIFEST_LOADED in FLAT_ONLY_SPANS


def test_gate_fires_manifest_loaded_span_once(tmp_path: Path, otel_capture) -> None:
    """The surviving POI gate fires ``manifest_loaded`` exactly once per call,
    carrying the manifest census the GM panel reads — the projection path's
    observability is preserved after the HTML cutover.
    """
    # pack_dir.parent.parent is the content root where r2_manifest.json lives.
    content_root = tmp_path / "content"
    pack_dir = content_root / "genre_packs" / "demo"
    pack_dir.mkdir(parents=True)
    poi_key = "genre_packs/demo/worlds/demoworld/assets/poi/the_vicarage.png"
    (content_root / "r2_manifest.json").write_text(
        json.dumps([{"key": poi_key, "md5": "x", "size": 1}])
    )

    # {anchor: verbatim} — the verbatim slug builds the R2 key the gate matches.
    slug_map = {"the-vicarage": "the_vicarage"}
    survivors = _gate_poi_slugs_on_manifest(
        slug_map, pack="demo", world="demoworld", pack_dir=pack_dir
    )
    assert survivors == frozenset({"the-vicarage"})

    spans = span_attrs_by_name(otel_capture, SPAN_MANIFEST_LOADED)
    assert len(spans) == 1, "the R2 manifest gate must fire manifest_loaded exactly once"
    assert spans[0]["reference.manifest_entry_count"] == 1
    assert spans[0]["reference.world_key_count"] == 1
