"""Tests for story 74-3 — world-tier lore guard + OTEL span + genre-lore deletion.

Epic 74 ("Genre tier = mechanics only"). Story 74-1 (DONE) made genre-tier lore
OPTIONAL and the world tier authoritative. Story 74-3 completes the move:
delete genre-tier lore, fail loud on a world with an empty LoreStore, and emit a
world-tier lore-load OTEL span.

**Test architecture (per Operator direction 2026-06-03):** unit tests test CODE
with synthetic inputs — they do NOT load real ``sidequest-content`` packs and
assert on their content. CONTENT invariants ("every live world has seedable
lore", "no genre-tier lore.yaml exists") live in the **pack validator**
(``sidequest.cli.validate.pack``); a single test RUNS that validator over the
real content as the regression lock. See feedback memory
``feedback_no_content_in_unit_tests`` and follow-up story 74-5.

Split:
  * ``TestSeedableCount`` / ``TestLoadGuard`` / ``TestLoreLoadSpan`` — loader
    BEHAVIOR on synthetic ``WorldLore`` / tmp paths (no content).
  * ``TestValidatorWorldLoreRule`` / ``TestValidatorGenreLoreForbidden`` — the
    validator RULES on synthetic tmp dirs (no real packs).
  * ``test_validator_reports_no_lore_errors_for_live_pack`` — runs the real
    validator over each live pack (content regression lock; the validator owns
    the content rule, the test just drives it).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sidequest.cli.validate.pack import (
    _find_default_schema,
    _validate_world_lore_seedable,
    validate_pack_structure,
)
from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import (
    _emit_world_lore_loaded,
    _require_seedable_world_lore,
    _world_lore_seedable_count,
    load_genre_pack,
)
from sidequest.genre.models.lore import Faction, WorldLore

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content"
PACKS_ROOT = CONTENT_ROOT / "genre_packs"

LIVE_PACKS = [
    "caverns_and_claudes",
    "elemental_harmony",
    "heavy_metal",
    "mutant_wasteland",
    "neon_dystopia",
    "pulp_noir",
    "road_warrior",
    "space_opera",
    "spaghetti_western",
    "tea_and_murder",
    "wry_whimsy",
]


def _faction() -> Faction:
    return Faction(name="The Testers", summary="QA guild", description="They write the tests.")


# --------------------------------------------------------------------------- #
# Loader BEHAVIOR — synthetic WorldLore, no content
# --------------------------------------------------------------------------- #


class TestSeedableCount:
    def test_empty_world_lore_counts_zero(self) -> None:
        assert _world_lore_seedable_count(WorldLore(world_name="X")) == 0

    def test_counts_each_seedable_field(self) -> None:
        lore = WorldLore(
            world_name="X",
            history="An age of testing.",
            geography="A land of fixtures.",
            cosmology="Layered assertions all the way down.",
            factions=[_faction()],
        )
        assert _world_lore_seedable_count(lore) == 4

    def test_content_only_under_extra_keys_counts_zero(self) -> None:
        # WorldLore has extra='allow'; the seeder ignores non-seedable keys, so a
        # file rich in extras seeds nothing. This is the empty-LoreStore trap.
        lore = WorldLore.model_validate(
            {"world_name": "X", "setting": "a vast and detailed setting", "themes": ["a", "b"]}
        )
        assert _world_lore_seedable_count(lore) == 0


class TestLoadGuard:
    def test_raises_on_empty_world_lore(self, tmp_path: Path) -> None:
        world_path = tmp_path / "worlds" / "ghost_town"
        with pytest.raises(GenreLoadError) as exc_info:
            _require_seedable_world_lore(WorldLore(world_name="Ghost"), world_path)
        msg = str(exc_info.value)
        assert "lore" in msg.lower(), "loud-fail must name the missing surface"
        assert "ghost_town" in msg, "loud-fail must be world-scoped (name the world)"

    def test_returns_count_when_seedable(self, tmp_path: Path) -> None:
        world_path = tmp_path / "worlds" / "ok"
        lore = WorldLore(world_name="Ok", history="A real history.", factions=[_faction()])
        assert _require_seedable_world_lore(lore, world_path) == 2


class TestLoreLoadSpan:
    @pytest.fixture
    def captured_events(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
        captured: list[dict[str, Any]] = []

        def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
            captured.append({"event_type": event_type, "fields": fields, "component": component})

        from sidequest.telemetry import watcher_hub as hub_mod

        monkeypatch.setattr(hub_mod, "publish_event", _capture)
        yield captured

    def test_emits_world_lore_state_transition(
        self, captured_events: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        _emit_world_lore_loaded(world_slug="ok_world", source=tmp_path, fragment_count=3)

        spans = [e for e in captured_events if e["event_type"] == "state_transition"]
        assert len(spans) == 1, "exactly one world_lore span expected"
        fields = spans[0]["fields"]
        assert fields["field"] == "world_lore"
        assert fields["op"] == "loaded"
        assert fields["world_slug"] == "ok_world"
        assert fields["lore_fragment_count"] == 3
        assert spans[0]["component"] == "genre"


# --------------------------------------------------------------------------- #
# Validator RULES — synthetic tmp dirs, no real packs
# --------------------------------------------------------------------------- #


class TestValidatorWorldLoreRule:
    def test_empty_lore_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "lore.yaml").write_text("world_name: X\n", encoding="utf-8")
        errors = _validate_world_lore_seedable(tmp_path, "world 'x'")
        assert errors and "seedable lore" in errors[0]

    def test_content_only_under_extra_keys_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "lore.yaml").write_text(
            "world_name: X\nsetting: lots of prose\nthemes: [a, b]\n", encoding="utf-8"
        )
        errors = _validate_world_lore_seedable(tmp_path, "world 'x'")
        assert errors and "seedable lore" in errors[0]

    def test_history_passes(self, tmp_path: Path) -> None:
        (tmp_path / "lore.yaml").write_text(
            "world_name: X\nhistory: A real, non-empty history.\n", encoding="utf-8"
        )
        assert _validate_world_lore_seedable(tmp_path, "world 'x'") == []

    def test_factions_pass(self, tmp_path: Path) -> None:
        (tmp_path / "lore.yaml").write_text(
            "world_name: X\nfactions:\n  - name: F\n    summary: s\n    description: d\n",
            encoding="utf-8",
        )
        assert _validate_world_lore_seedable(tmp_path, "world 'x'") == []

    def test_whitespace_only_history_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "lore.yaml").write_text('world_name: X\nhistory: "   "\n', encoding="utf-8")
        errors = _validate_world_lore_seedable(tmp_path, "world 'x'")
        assert errors and "seedable lore" in errors[0]

    def test_absent_file_is_not_this_rules_concern(self, tmp_path: Path) -> None:
        # Missing lore.yaml is the structural required-files check's job, not this one.
        assert _validate_world_lore_seedable(tmp_path, "world 'x'") == []


class TestValidatorGenreLoreForbidden:
    def test_genre_tier_lore_yaml_is_flagged(self, tmp_path: Path) -> None:
        """A genre-tier lore.yaml is forbidden (Epic 74). Validate a synthetic
        pack dir carrying one and assert the specific error fires (amid whatever
        structural errors the bare dir also produces)."""
        pack = tmp_path / "mechanics_only_pack"
        pack.mkdir()
        (pack / "lore.yaml").write_text("world_name: X\nhistory: h\n", encoding="utf-8")

        schema = _find_default_schema(PACKS_ROOT)
        errors, _warnings = validate_pack_structure(pack, schema)
        assert any("genre-tier lore.yaml is forbidden" in e for e in errors), (
            f"expected the genre-lore-forbidden error; got: {errors}"
        )


# --------------------------------------------------------------------------- #
# Guard WIRING — the load-time guard fires through the real load_genre_pack path
# (synthetic fixture pack, not real content). Falsifies the guard's wiring into
# _load_single_world: if the _require_seedable_world_lore call were removed, this
# test fails (per CLAUDE.md "Every Test Suite Needs a Wiring Test").
# --------------------------------------------------------------------------- #


def test_load_genre_pack_raises_on_empty_world_lore(minimal_pack_factory, tmp_path: Path) -> None:
    pack = minimal_pack_factory(tmp_path)
    world_lore_files = list((pack.path / "worlds").glob("*/lore.yaml"))
    assert world_lore_files, "fixture pack must ship at least one world lore.yaml"
    # Strand all content under a non-seedable key so the world seeds zero fragments.
    for lore_path in world_lore_files:
        lore_path.write_text(
            "world_name: Ghost Town\nsetting: prose stranded under a non-seedable key\n",
            encoding="utf-8",
        )

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack.path)

    msg = str(exc_info.value)
    assert "lore" in msg.lower(), "loud-fail must name the missing surface"
    assert any(p.parent.name in msg for p in world_lore_files), (
        "loud-fail must be world-scoped (name the empty-lore world)"
    )


# --------------------------------------------------------------------------- #
# Content regression lock — RUN the validator over the real live packs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pack_name", LIVE_PACKS)
def test_validator_reports_no_lore_errors_for_live_pack(pack_name: str) -> None:
    """Run the real pack validator over each live pack and assert ZERO
    lore-related errors. Content regression lock for story 74-3: all 11
    genre-tier lore.yaml deleted, and every live world's lore.yaml seeds a
    non-empty LoreStore (road_warrior/the_circuit was re-authored).

    The VALIDATOR owns the content rule; this test merely drives it over the
    real content (content invariants are not asserted ad-hoc in unit tests).
    Only lore-related errors are inspected so unrelated pre-existing validator
    findings don't make this a flaky catch-all.
    """
    pack = PACKS_ROOT / pack_name
    schema = _find_default_schema(PACKS_ROOT)
    errors, _warnings = validate_pack_structure(pack, schema)
    lore_errors = [e for e in errors if "lore.yaml" in e or "seedable lore" in e]
    assert lore_errors == [], f"{pack_name}: unexpected lore validator errors: {lore_errors}"
