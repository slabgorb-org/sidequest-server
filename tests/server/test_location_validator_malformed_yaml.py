"""Story 63-13 — Malformed world YAML becomes a clean Issue / graceful degrade
(RED phase).

Two defensive layers, both currently UNGUARDED on develop:

PRIMARY (dev tooling) — ``sidequest/cli/validate/locations.py``: every
``yaml.safe_load`` that reads a world file is unguarded, so a malformed world
YAML (tab/indent typo) raises a raw ``yaml.YAMLError`` straight out of
``validate_locations_in_world`` — ``pf validate locations`` dies with a
traceback on the exact authoring error it exists to report. The fix routes the
parse error into the existing ``Issue``/``ValidationResult`` channel
(``code="MALFORMED_YAML"``, ``severity="error"``, ``file`` + ``line`` from
``exc.problem_mark``). No OTEL — the validator is dev tooling, not a runtime
subsystem.

DEFENSE-IN-DEPTH (runtime, R1) — ``_maybe_emit_location_description``
(map_emit.py:514): the Story-63-6 region-anchor block calls
``load_poi_image_slugs(world_dir)`` OUTSIDE the function's two graceful
try/except guards (which end at :439). ``load_poi_image_slugs`` re-raises
``YAMLError`` as ``ValueError`` (reference_renderer.py:1105), so a malformed
``history.yaml`` crashes a live room-change emit despite the "must not crash a
turn" contract. The fix degrades to ``reference_url=None`` and fires
``reference_url_failed_span`` (ERROR — loud, GM-panel-visible).

AC→test map:
- AC1/AC2 PRIMARY: test_malformed_{history,locations,cartography,room,npcs,
  pack_allowlist,scenario}_yaml_records_issue_not_raise
- AC1 regression: test_valid_world_has_no_malformed_yaml_issue
- AC3/AC4/AC5 RUNTIME: test_runtime_emit_degrades_on_malformed_history_yaml
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.cli.validate.locations import validate_locations_in_world

# A tab in block-indentation position — yaml.safe_load raises ScannerError
# (a YAMLError subclass) carrying a populated ``problem_mark`` (line/col).
_MALFORMED_YAML = "regions:\n\tbad: value\n"

_VALID_HISTORY_WITH_POI = "points_of_interest:\n  - slug: x\n    name: X\n"


# ===========================================================================
# PRIMARY — validator: malformed world YAML → clean Issue, never a traceback
# ===========================================================================


def _make_world(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
    rooms: dict[str, str] | None = None,
    pack_files: dict[str, str] | None = None,
    scenarios: dict[str, str] | None = None,
    pack: str = "test_pack",
    world: str = "test_world",
) -> Path:
    """Build a synthetic ``<pack>/worlds/<world>/`` tree (no live content).

    Returns the world_dir for ``validate_locations_in_world``. ``pack_files``
    land at the pack root (e.g. ``pack.yaml``); ``scenarios`` under
    ``<world>/scenarios/``; ``rooms`` under ``<world>/rooms/``.
    """
    pack_dir = tmp_path / pack
    world_dir = pack_dir / "worlds" / world
    world_dir.mkdir(parents=True)
    for name, content in (files or {}).items():
        (world_dir / name).write_text(content)
    for name, content in (pack_files or {}).items():
        (pack_dir / name).write_text(content)
    if rooms:
        rdir = world_dir / "rooms"
        rdir.mkdir()
        for name, content in rooms.items():
            (rdir / name).write_text(content)
    if scenarios:
        sdir = world_dir / "scenarios"
        sdir.mkdir()
        for name, content in scenarios.items():
            (sdir / name).write_text(content)
    return world_dir


def _malformed_issues(result, *, file_contains: str) -> list:
    return [
        i
        for i in result.errors
        if i.code == "MALFORMED_YAML" and file_contains in i.file
    ]


def _assert_clean_malformed_issue(result, *, file_contains: str) -> None:
    """Shared assertion: a MALFORMED_YAML error Issue for the named file, with
    file + line populated, and the result is unsuccessful."""
    issues = _malformed_issues(result, file_contains=file_contains)
    assert issues, (
        f"expected a MALFORMED_YAML error Issue for {file_contains!r}; "
        f"got errors={[(i.code, i.file) for i in result.errors]}"
    )
    issue = issues[0]
    assert issue.severity == "error"
    assert file_contains in issue.file
    assert issue.line is not None, "Issue.line must be populated from exc.problem_mark.line"
    assert result.success is False


def test_malformed_history_yaml_records_issue_not_raise(tmp_path):
    """:420 _history_poi_slugs — malformed history.yaml → Issue, not traceback."""
    world_dir = _make_world(tmp_path, files={"history.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="history.yaml")


def test_malformed_locations_yaml_records_issue_not_raise(tmp_path):
    """:398 _location_card_slugs — reached only when history.yaml has a POI, so
    seed a valid history with one POI slug + a malformed locations.yaml."""
    world_dir = _make_world(
        tmp_path,
        files={
            "history.yaml": _VALID_HISTORY_WITH_POI,
            "locations.yaml": _MALFORMED_YAML,
        },
    )
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="locations.yaml")


def test_malformed_cartography_yaml_records_issue_not_raise(tmp_path):
    """:518 cartography.yaml load."""
    world_dir = _make_world(tmp_path, files={"cartography.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="cartography.yaml")


def test_malformed_room_yaml_records_issue_not_raise(tmp_path):
    """:534 rooms/*.yaml loop."""
    world_dir = _make_world(tmp_path, rooms={"broken_room.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="broken_room.yaml")


def test_malformed_npcs_yaml_records_issue_not_raise(tmp_path):
    """:116 _load_npc_tokens — npcs.yaml is in AC1's enumerated list."""
    world_dir = _make_world(tmp_path, files={"npcs.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="npcs.yaml")


def test_malformed_pack_allowlist_yaml_records_issue_not_raise(tmp_path):
    """:149 _load_allowlist — pack.yaml (at the pack root) is in AC1's list."""
    world_dir = _make_world(tmp_path, pack_files={"pack.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="pack.yaml")


def test_malformed_scenario_yaml_records_issue_not_raise(tmp_path):
    """:135 _load_clue_ids — scenarios/*.yaml is in AC1's list."""
    world_dir = _make_world(tmp_path, scenarios={"case.yaml": _MALFORMED_YAML})
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    _assert_clean_malformed_issue(result, file_contains="case.yaml")


def test_valid_world_has_no_malformed_yaml_issue(tmp_path):
    """Regression: a well-formed world records NO MALFORMED_YAML — the guard
    must not false-positive on valid YAML."""
    world_dir = _make_world(
        tmp_path,
        files={
            "history.yaml": _VALID_HISTORY_WITH_POI,
            "locations.yaml": "locations:\n  - id: x\n    name: X\n",
            "cartography.yaml": "regions: {}\n",
            "npcs.yaml": "npcs: []\n",
        },
        pack_files={"pack.yaml": "generic_allowlist: []\n"},
        scenarios={"case.yaml": "clues: []\n"},
    )
    result = validate_locations_in_world(world_dir)  # MUST NOT raise
    assert not _malformed_issues(result, file_contains=""), (
        "valid world must not produce a MALFORMED_YAML Issue; "
        f"got {[(i.code, i.file) for i in result.errors]}"
    )


# ===========================================================================
# DEFENSE-IN-DEPTH — runtime: malformed history.yaml degrades, never crashes
# ===========================================================================


def _seed_synthetic_world(tmp_path: Path) -> Path:
    """Minimal genre-pack/world tree with one settlement room so
    ``load_room_payload`` succeeds and the emit reaches the 63-6 POI block.
    Mirrors tests/server/test_location_description_emit.py::_seed_synthetic_world."""
    genre_root = tmp_path / "test_pack"
    rooms = genre_root / "worlds" / "test_world" / "rooms"
    rooms.mkdir(parents=True)
    (rooms / "test_room.yaml").write_text(
        "name: Test Square\n"
        "room_type: settlement\n"
        "description: A well at the centre.\n"
        "entities:\n"
        "  - id: square_well\n"
        "    label: the well\n"
        "    tier: flavor_only\n"
    )
    return genre_root


def _patch_genre_loader_find(monkeypatch, genre_root: Path) -> None:
    from sidequest.genre import loader as loader_mod

    def _fake_find(self, slug):  # noqa: ARG001
        return genre_root

    monkeypatch.setattr(loader_mod.GenreLoader, "find", _fake_find)


def test_runtime_emit_degrades_on_malformed_history_yaml(tmp_path, monkeypatch, otel_capture):
    """AC3/AC4/AC5 (R1): a malformed ``history.yaml`` must NOT crash the live
    room-change emit. ``load_poi_image_slugs`` re-raises YAMLError as ValueError
    at map_emit.py:514 (outside the function's graceful guards); the emit must
    catch it, still send a ``LocationDescriptionMessage`` with
    ``reference_url=None``, and fire ``reference_url_failed_span`` (loud, not a
    silent fallback).

    Drives the REAL graceful path (real load_room_payload sourcing + real
    load_poi_image_slugs against a malformed file) — no shim.
    """
    from sidequest.protocol.messages import LocationDescriptionMessage
    from sidequest.server.websocket_session_handler import (
        _maybe_emit_location_description,
    )

    genre_root = _seed_synthetic_world(tmp_path)
    world_dir = genre_root / "worlds" / "test_world"
    # The POI manifest the 63-6 block reads is malformed.
    (world_dir / "history.yaml").write_text(_MALFORMED_YAML)
    _patch_genre_loader_find(monkeypatch, genre_root)

    emit_fn = MagicMock()
    sd = MagicMock()
    sd.genre_slug = "test_pack"
    sd.world_slug = "test_world"
    sd.player_id = ""
    sd.genre_pack = MagicMock()
    sd.genre_pack.worlds = {"test_world": MagicMock()}
    snapshot = MagicMock()
    snapshot.character_locations = {"alice": "test_room"}

    # MUST NOT raise — the whole point of the defense-in-depth guard.
    _maybe_emit_location_description(
        MagicMock(),
        sd=sd,
        snapshot=snapshot,
        actor="alice",
        emit_fn=emit_fn,
    )

    # Graceful degrade: the location description still emits, with no URL.
    emit_fn.assert_called_once()
    sent_msg = emit_fn.call_args.args[0]
    assert isinstance(sent_msg, LocationDescriptionMessage)
    assert sent_msg.payload.reference_url is None

    # Loud, not silent: the failed span fires so the GM panel sees the degrade.
    span_names = {s.name for s in otel_capture.get_finished_spans()}
    assert "sidequest.reference.url_failed" in span_names, (
        "expected reference_url_failed_span on the malformed-YAML degrade path; "
        f"got spans={sorted(span_names)}"
    )
