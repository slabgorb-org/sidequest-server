"""RED-phase contract tests — Story 100-2.

Phase 1, generic-YAML slice of the reference-pages → React migration
(spec: ``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

The map slice (Slice A, sidequest-server PR #762) already ships
``build_lore_map_section`` / ``build_lore_projection`` and its tests live in
``test_reference_projection.py``. This story adds the *generic-YAML* section
projection: the data-shaping analog of the HTML walk
(``render_node`` / ``_render_dict`` / ``_render_list`` in
``reference_renderer.py``) that turns a world-tier YAML file into a public-only
**node tree** of JSON, applying the SAME ``reference_visibility.classify()``
firewall the HTML renderer uses.

**Load-bearing constraint (spec C1):** no keeper field may cross the JSON
boundary. ``classify()`` is the gate; KEEPER fields drop silently, UNKNOWN
fields drop and fire a WARN span, and the renderer-layer suppressions
(``_is_devnote`` markers + leading-underscore keys) are preserved. The bulk of
this file is the security test set per the spec's Testing Strategy: "For every
page type and a representative spoiler-bearing world, assert the JSON payload
contains **no** keeper field." These are the load-bearing tests.

Contract this RED phase pins (to be implemented by Dev — none of these symbols
exist yet, so the import fails and every test below is RED):

  - ``build_generic_yaml_section(data, *, file_stem, pack, world) -> dict | None``
    Projects ONE parsed world YAML file into a public node-tree section dict, or
    ``None`` when nothing public survives the firewall. Section shape:
        {"id": <slug(file_stem)>, "label": <humanized stem>, "node": <node>}
    Node shapes (the normalized tree React walks):
        {"type": "dict",   "entries": [{"key": str, "label": str, "node": <node>}, ...]}
        {"type": "list",   "items":   [<node>, ...]}
        {"type": "scalar", "value":   <str|int|float|bool|None>}

  - ``build_lore_projection`` (extended) appends one generic-YAML section per
    present file in ``LORE_WORLD_FILES`` AFTER the map section, reusing the same
    ``classify()`` firewall.

The node-tree shape itself is a Dev/Architect call; if Dev lands a different but
equally public-only shape, the *security* assertions (no keeper value/key in the
JSON, ``classify`` actually consulted, UNKNOWN drops fire a span) are the
non-negotiable ones — do not weaken them to fit a shape change. The shape
assertions are marked below so they can be reconciled; the firewall assertions
cannot.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

# RED: this import fails until Dev adds the generic-YAML projection builder.
from sidequest.server.reference_projection import (
    build_generic_yaml_section,
    build_lore_projection,
)
from sidequest.server.reference_visibility import Visibility, classify
from sidequest.telemetry.spans.reference import SPAN_REFERENCE_UNKNOWN_FIELD
from tests.server.conftest import span_attrs_by_name

# ---------------------------------------------------------------------------
# Helpers — walk the projected node tree collecting every key and scalar value
# so the security assertions can scan the WHOLE structure, not just top level.
# ---------------------------------------------------------------------------


def _iter_nodes(node: object):
    """Yield every node dict in a projected node tree (depth-first)."""
    if isinstance(node, dict):
        yield node
        if node.get("type") == "dict":
            for entry in node.get("entries", []):
                yield from _iter_nodes(entry.get("node"))
        elif node.get("type") == "list":
            for item in node.get("items", []):
                yield from _iter_nodes(item)


def _all_dict_keys(section: dict) -> set[str]:
    """Every YAML-derived ``key`` that survived into the projected section."""
    keys: set[str] = set()
    for node in _iter_nodes(section.get("node")):
        if node.get("type") == "dict":
            for entry in node.get("entries", []):
                keys.add(entry["key"])
    return keys


def _all_scalar_values(section: dict) -> list[object]:
    values: list[object] = []
    for node in _iter_nodes(section.get("node")):
        if node.get("type") == "scalar":
            values.append(node.get("value"))
    return values


# ===========================================================================
# Group 1 — Non-keeper fields pass through (the happy path, then break it)
# ===========================================================================


def test_public_dict_projects_as_node_tree():
    # 'history' is a PUBLIC_STEMS stem with no KEEPER carve-out → all public.
    data = {
        "founding": "The city rose from salt and debt.",
        "ages": ["The Drowning", "The Reckoning"],
    }
    section = build_generic_yaml_section(data, file_stem="history", pack="p", world="w")
    assert section is not None
    assert section["id"] == "history"
    # [shape] humanized label — reconcilable if Dev picks a different casing.
    assert section["label"] == "History"
    keys = _all_dict_keys(section)
    assert "founding" in keys
    assert "ages" in keys
    values = _all_scalar_values(section)
    assert "The city rose from salt and debt." in values
    assert "The Drowning" in values
    assert "The Reckoning" in values


def test_scalar_types_survive_with_native_json_types():
    data = {"count": 3, "ratio": 1.5, "active": True, "missing": None}
    section = build_generic_yaml_section(data, file_stem="history", pack="p", world="w")
    values = _all_scalar_values(section)
    # Numbers/bools/None must cross as native JSON types so React renders them,
    # not as pre-stringified "Yes"/"(none)" HTML-isms.
    assert 3 in values
    assert 1.5 in values
    assert True in values
    assert None in values


def test_nested_dict_and_list_recurse():
    data = {
        "factions": [
            {"name": "The Combine", "stance": "hostile"},
            {"name": "Dock Union", "stance": "wary"},
        ]
    }
    section = build_generic_yaml_section(data, file_stem="factions", pack="p", world="w")
    keys = _all_dict_keys(section)
    assert {"factions", "name", "stance"} <= keys
    values = _all_scalar_values(section)
    assert "The Combine" in values
    assert "Dock Union" in values


# ===========================================================================
# Group 2 — THE FIREWALL (spec C1). Load-bearing. Do not weaken.
# ===========================================================================


def test_keeper_subtree_is_dropped_entirely():
    # ('beat_vocabulary', ('obstacles',)) is a KEEPER carve-out — narrator-only
    # obstacle stats. The whole subtree must NOT cross the JSON boundary.
    assert classify("beat_vocabulary", ("obstacles",)) is Visibility.KEEPER
    data = {
        "tags": ["combat", "social"],  # public sibling
        "obstacles": [
            {
                "name": "Locked Vault",
                "description": "DC 18, narrator escalates on failure.",
                "stat_check": "wits",
                "failure_penalty": "alarm raised",
            }
        ],
    }
    section = build_generic_yaml_section(data, file_stem="beat_vocabulary", pack="p", world="w")
    # Section may still exist for the public sibling, but the keeper subtree's
    # key and EVERY value beneath it must be gone.
    blob = _json.dumps(section)
    assert "obstacles" not in _all_dict_keys(section or {})
    assert "Locked Vault" not in blob
    assert "DC 18, narrator escalates on failure." not in blob
    assert "alarm raised" not in blob
    # The public sibling survived.
    assert "combat" in blob


def test_keeper_leaf_field_dropped_but_siblings_survive():
    # narrator_hint under a confrontation beat is KEEPER; the beat's other
    # fields are PUBLIC. Per-field firewall, not whole-node.
    assert (
        classify("rules", ("confrontations", "*", "beats", "*", "narrator_hint"))
        is Visibility.KEEPER
    )
    data = {
        "confrontations": [
            {
                "id": "standoff",
                "beats": [
                    {
                        "label": "Opening Move",
                        "narrator_hint": "Hint the villain's secret weakness here.",
                    }
                ],
            }
        ]
    }
    section = build_generic_yaml_section(data, file_stem="rules", pack="p", world="w")
    blob = _json.dumps(section)
    assert "narrator_hint" not in _all_dict_keys(section or {})
    assert "Hint the villain's secret weakness here." not in blob
    # Public siblings still present.
    assert "Opening Move" in blob
    assert "standoff" in blob


def test_whole_keeper_file_projects_to_none():
    # ('tropes', ()) and ('seed_tropes', ()) are file-root KEEPER — spoiler
    # trigger graphs. The whole file must project to nothing.
    assert classify("tropes", ()) is Visibility.KEEPER
    data = {"on_betrayal": {"trigger": "trust>5", "reveal": "the mole is Sten"}}
    section = build_generic_yaml_section(data, file_stem="tropes", pack="p", world="w")
    assert section is None
    section2 = build_generic_yaml_section(
        {"seed": "x"}, file_stem="seed_tropes", pack="p", world="w"
    )
    assert section2 is None


def test_unknown_stem_projects_to_none_and_fires_warn_span(otel_capture) -> None:
    # A stem the renderer never reads classifies UNKNOWN at root → dropped, and
    # a WARN span fires so content drift surfaces in the GM panel (No Silent
    # Fallbacks). This is the OTEL wiring assertion for the firewall.
    assert classify("archetype_constraints", ()) is Visibility.UNKNOWN
    section = build_generic_yaml_section(
        {"max_picks": 3}, file_stem="archetype_constraints", pack="p", world="w"
    )
    assert section is None
    spans = span_attrs_by_name(otel_capture, SPAN_REFERENCE_UNKNOWN_FIELD)
    assert spans, "UNKNOWN drop must fire a sidequest.reference.unknown_field WARN span"
    assert any(a.get("reference.file_stem") == "archetype_constraints" for a in spans)


def test_unknown_child_key_dropped_and_warns(otel_capture) -> None:
    # A PUBLIC stem can still carry an UNKNOWN child IF the stem default were
    # not PUBLIC — but for a PUBLIC_STEMS stem the child is PUBLIC. To exercise
    # the UNKNOWN child-drop path we rely on a stem-default-PUBLIC parent that
    # the firewall consults per key. The contract: classify() is consulted for
    # EVERY key_path, so a hypothetical UNKNOWN key drops + warns rather than
    # leaking. We assert classify() is actually invoked (Group 4 proves reuse);
    # here we assert no UNKNOWN value ever survives for a non-public stem child.
    section = build_generic_yaml_section(
        {"secret": "leak me"}, file_stem="not_a_real_stem", pack="p", world="w"
    )
    assert section is None
    spans = span_attrs_by_name(otel_capture, SPAN_REFERENCE_UNKNOWN_FIELD)
    assert any(a.get("reference.file_stem") == "not_a_real_stem" for a in spans)
    # The would-be-leaked value never crossed.
    assert section is None or "leak me" not in _json.dumps(section)


def test_leading_underscore_key_suppressed():
    # Private (leading-underscore) keys are dev-only and never reach the
    # player-facing surface (Story 63-9 parity in the HTML walk).
    data = {"setting": "A foggy port.", "_devnote": "TODO wire the docks"}
    section = build_generic_yaml_section(data, file_stem="world", pack="p", world="w")
    blob = _json.dumps(section)
    assert "_devnote" not in _all_dict_keys(section or {})
    assert "TODO wire the docks" not in blob
    assert "A foggy port." in blob


def test_devnote_marker_value_suppressed():
    # Dev-note marker values (TODO/FIXME/XXX/PLACEHOLDER/DEV NOTE as leading
    # token) are suppressed from the public projection.
    data = {
        "intro": "TODO write this section",
        "real": "The Drowned Quarter floods at every high tide.",
    }
    section = build_generic_yaml_section(data, file_stem="world", pack="p", world="w")
    blob = _json.dumps(section)
    assert "TODO write this section" not in blob
    assert "The Drowned Quarter floods at every high tide." in blob


def test_devnote_marker_inside_list_suppressed():
    # Dev-note markers leak through scalar-list items too (Story 63-9).
    data = {"rumors": ["The mole walks among us.", "FIXME add the third rumor"]}
    section = build_generic_yaml_section(data, file_stem="legends", pack="p", world="w")
    blob = _json.dumps(section)
    assert "FIXME add the third rumor" not in blob
    assert "The mole walks among us." in blob


# ===========================================================================
# Group 3 — Representative spoiler-bearing payload, end-to-end keeper scan.
#           The spec's "for a representative spoiler-bearing world, assert the
#           JSON contains NO keeper field" test.
# ===========================================================================


# Field tokens that are keeper-side per the spec C1 list. None may appear as a
# surviving KEY in any projected section. (ocean, history_seeds,
# initial_disposition, distinguishing_features, belief/clue data,
# seed_tropes/tropes.)
_KEEPER_TOKENS = (
    "ocean",
    "history_seeds",
    "initial_disposition",
    "distinguishing_features",
    "narrator_hint",
    "obstacles",
)


def test_spoiler_bearing_payload_leaks_no_keeper_token():
    # A 'beat_vocabulary' file mixing a public stem-default body with a KEEPER
    # obstacles subtree carrying spoiler stats. Nothing keeper crosses.
    data = {
        "tags": ["stealth"],
        "obstacles": [
            {
                "name": "Sten's Hidden Ledger",
                "description": "Reveals the smuggling ring on a 20.",
                "stat_check": "wits",
            }
        ],
    }
    section = build_generic_yaml_section(data, file_stem="beat_vocabulary", pack="p", world="w")
    keys = _all_dict_keys(section or {})
    for token in _KEEPER_TOKENS:
        assert token not in keys, f"keeper token {token!r} leaked as a key"
    blob = _json.dumps(section)
    assert "Reveals the smuggling ring on a 20." not in blob
    assert "Sten's Hidden Ledger" not in blob


# ===========================================================================
# Group 4 — classify() is REUSED, not reimplemented (story requirement).
# ===========================================================================


def test_classify_is_the_gate_not_a_reimplementation(monkeypatch):
    """The projection must call ``reference_visibility.classify`` for its
    firewall decisions. We monkeypatch the symbol the projection module
    imports and assert it is consulted with real (file_stem, key_path) tuples.

    This catches a Dev who copies the KEEPER/PUBLIC tables into a private
    helper instead of reusing the shipped firewall — a security regression
    because the two would drift.
    """
    import sidequest.server.reference_projection as proj

    calls: list[tuple[str, tuple[str, ...]]] = []
    real = classify

    def _spy(file_stem: str, key_path):
        calls.append((file_stem, tuple(key_path)))
        return real(file_stem, key_path)

    # The projection module must reference classify by name so this patch lands.
    assert hasattr(proj, "classify"), (
        "reference_projection must import classify from reference_visibility "
        "(reuse the firewall, do not reimplement it)"
    )
    monkeypatch.setattr(proj, "classify", _spy)

    build_generic_yaml_section(
        {"founding": "x", "obstacles": [{"name": "y"}]},
        file_stem="beat_vocabulary",
        pack="p",
        world="w",
    )
    # classify was consulted at least at the root and for each child key.
    assert calls, "classify() was never called — firewall not reused"
    seen_paths = {kp for _stem, kp in calls}
    assert ("obstacles",) in seen_paths, "classify() not consulted for the keeper key path"


# ===========================================================================
# Group 5 — Wiring: the generic-YAML sections appear in the full lore document
#           (CLAUDE.md: every test suite needs a wiring test).
# ===========================================================================


def _world_dir_with_files(tmp_path: Path) -> Path:
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    (world_dir / "world.yaml").write_text(
        "setting: A salt-choked port city.\n_devnote: TODO flesh out the docks\n",
        encoding="utf-8",
    )
    (world_dir / "history.yaml").write_text(
        "founding: Built on debt and brine.\nages: [The Drowning, The Reckoning]\n",
        encoding="utf-8",
    )
    return world_dir


def test_lore_projection_includes_generic_yaml_sections(tmp_path: Path):
    world_dir = _world_dir_with_files(tmp_path)
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    section_ids = [s["id"] for s in doc["sections"]]
    # No cartography here → no map section; the generic-YAML sections appear.
    assert "world" in section_ids
    assert "history" in section_ids
    # The suppressed dev-only key never crossed into the assembled document.
    blob = _json.dumps(doc)
    assert "_devnote" not in blob
    assert "TODO flesh out the docks" not in blob
    assert "A salt-choked port city." in blob
    assert "Built on debt and brine." in blob


def test_lore_projection_orders_map_before_generic_sections(tmp_path: Path):
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    (world_dir / "cartography.yaml").write_text(
        "starting_region: harbor\n"
        "regions:\n"
        "  harbor: {name: The Harbor, summary: Salt docks., description: Fog., adjacent: [market]}\n"
        "  market: {name: Night Market, summary: Lit stalls., description: Smoke., adjacent: [harbor]}\n",
        encoding="utf-8",
    )
    (world_dir / "history.yaml").write_text("founding: Salt and debt.\n", encoding="utf-8")
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    ids = [s["id"] for s in doc["sections"]]
    assert ids[0] == "map", "map section must lead the document"
    assert "history" in ids
    assert ids.index("map") < ids.index("history")


def test_lore_projection_excludes_keeper_files_entirely(tmp_path: Path):
    # tropes.yaml / seed_tropes.yaml are keeper files (EXCLUDED_FILES +
    # file-root KEEPER). Even if present on disk they must never project.
    world_dir = tmp_path / "worlds" / "w"
    world_dir.mkdir(parents=True)
    (world_dir / "world.yaml").write_text("setting: A port.\n", encoding="utf-8")
    (world_dir / "tropes.yaml").write_text(
        "on_betrayal: {reveal: the mole is Sten}\n", encoding="utf-8"
    )
    (world_dir / "seed_tropes.yaml").write_text("seed: spoiler\n", encoding="utf-8")
    doc = build_lore_projection("p", "w", pack_dir=tmp_path, world_dir=world_dir)
    blob = _json.dumps(doc)
    assert "the mole is Sten" not in blob
    assert "spoiler" not in blob
    section_ids = [s["id"] for s in doc["sections"]]
    assert "tropes" not in section_ids
    assert "seed_tropes" not in section_ids
