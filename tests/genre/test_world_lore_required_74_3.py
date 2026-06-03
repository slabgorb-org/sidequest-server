"""Tests for story 74-3 — world-tier lore for every live world; genre-lore
deletion + empty-LoreStore guard + OTEL load span (RED).

Epic 74 ("Genre tier = mechanics only"). Story 74-1 (DONE) made genre-tier lore
OPTIONAL and the world tier authoritative; ``seed_world_lore`` is now world-only.
Story 74-3 completes the move:

  1. **Author/verify** world lore for every live world so none ends up with an
     empty LoreStore once genre lore is gone. (SM audit 2026-06-03: all 20 live
     worlds already ship a substantial ``worlds/<world>/lore.yaml``, so this is a
     *regression lock*, not net-new authoring — see session Design Deviations.)
  2. **Delete** all 11 genre-tier ``genre_packs/*/lore.yaml`` files. (Operator
     decision 2026-06-03 brought deletion INTO scope — see session Design
     Deviations; the context file had it out of scope.)
  3. **Empty-LoreStore guard** — a world whose lore is present but content-empty
     (no history/geography/cosmology/factions, or content only under extra keys
     that the seeder ignores) must fail LOUD at load, not silently seed nothing
     (No Silent Fallbacks).
  4. **OTEL** — world-tier lore load emits a ``state_transition`` watcher span so
     the GM panel can prove the load fired (mirrors the existing ``world_items`` /
     ``world_theme`` spans).

Patterns mirror the sibling ``tests/genre/test_genre_flavor_world_tier.py``
(74-1): real/cloned packs ("relocate, don't fabricate"), monkeypatched
``publish_event`` for span capture, load-time loud-fail via ``GenreLoadError``.

Wiring (per "Every Test Suite Needs a Wiring Test"): the OTEL-span test and the
per-world seed test both drive the *real* production seams (``load_genre_pack`` /
``seed_world_lore``) and assert behavior — no source-text grep (forbidden by the
server CLAUDE.md "No Source-Text Wiring Tests" rule).
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sidequest.game.lore_seeding import seed_world_lore
from sidequest.game.lore_store import LoreStore
from sidequest.genre.error import GenreLoadError
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content"
PACKS_ROOT = CONTENT_ROOT / "genre_packs"

# The 10 wired live packs (mirror test_genre_flavor_world_tier.py) + wry_whimsy,
# which ships three authored worlds and is on the cusp of launch (epic-74 audit).
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

# All live (pack, world) pairs — the authoritative migration set (SM audit
# 2026-06-03). Every one of these must seed a non-empty LoreStore.
LIVE_WORLDS: list[tuple[str, str]] = [
    ("caverns_and_claudes", "beneath_sunden"),
    ("elemental_harmony", "burning_peace"),
    ("elemental_harmony", "shattered_accord"),
    ("heavy_metal", "evropi"),
    ("heavy_metal", "long_foundry"),
    ("mutant_wasteland", "flickering_reach"),
    ("neon_dystopia", "franchise_nations"),
    ("pulp_noir", "annees_folles"),
    ("road_warrior", "the_circuit"),
    ("space_opera", "aureate_span"),
    ("space_opera", "coyote_star"),
    ("space_opera", "perseus_cloud"),
    ("spaghetti_western", "dust_and_lead"),
    ("spaghetti_western", "five_points"),
    ("spaghetti_western", "the_real_mccoy"),
    ("tea_and_murder", "glenross"),
    ("tea_and_murder", "blackthorn_moor"),
    ("wry_whimsy", "gulliver"),
    ("wry_whimsy", "oz"),
    ("wry_whimsy", "wonderland"),
]

# A light, self-sufficient pack/world for the guard + span fixtures (matches the
# 74-1 file's choice — franchise_nations authors its own lore).
NEON_PACK = PACKS_ROOT / "neon_dystopia"
NEON_WORLD = "franchise_nations"


# --------------------------------------------------------------------------- #
# Watcher-event capture (mirror test_genre_flavor_world_tier.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.telemetry import watcher_hub as hub_mod

    monkeypatch.setattr(hub_mod, "publish_event", _capture)
    yield captured


def _loaded_spans(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``state_transition`` fields dicts for ``op == "loaded"`` events."""
    return [
        e["fields"]
        for e in events
        if e["event_type"] == "state_transition" and e["fields"].get("op") == "loaded"
    ]


def _clone_pack(src_pack: Path, dst_root: Path) -> Path:
    """copytree a pack (YAML only — the loader does not read binaries)."""
    pack_dst = dst_root / src_pack.name
    shutil.copytree(
        src_pack,
        pack_dst,
        ignore=shutil.ignore_patterns(
            "*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.ogg", "*.mp3", "*.wav", "__pycache__"
        ),
    )
    return pack_dst


# --------------------------------------------------------------------------- #
# AC1 — empty-LoreStore guard: content-empty world lore fails LOUD at load
# --------------------------------------------------------------------------- #


def test_content_empty_world_lore_fails_loud(tmp_path: Path) -> None:
    """A world whose ``lore.yaml`` exists but carries no seedable content
    (only ``world_name``) must fail LOUD at load, naming the world — not load
    fine and silently seed zero fragments (No Silent Fallbacks; mirror
    visibility_baseline / lethality_policy, and the 74-1 theme loud-fail).

    RED today: ``WorldLore`` makes every content field optional, so a
    ``world_name``-only file parses cleanly, the world loads, and
    ``seed_lore_from_world`` returns 0 with no error. The store is silently
    empty for that world.
    """
    pack_dir = _clone_pack(NEON_PACK, tmp_path)
    world_lore = pack_dir / "worlds" / NEON_WORLD / "lore.yaml"
    assert world_lore.exists(), "fixture precondition: world lore.yaml present"
    # Overwrite with a content-empty (but schema-valid) world lore.
    world_lore.write_text("world_name: franchise_nations\n", encoding="utf-8")

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack_dir)

    msg = str(exc_info.value)
    assert "lore" in msg.lower(), "loud-fail must name the missing surface (lore)"
    assert NEON_WORLD in msg, (
        "loud-fail must be world-scoped — name the world whose lore is empty"
    )


def test_content_only_in_extra_keys_fails_loud(tmp_path: Path) -> None:
    """A world lore.yaml that LOOKS substantial but puts all its content under
    extra keys the seeder never reads (``setting_anchor``, ``themes``, …) still
    seeds zero fragments — and must fail loud.

    This is the subtle empty-LoreStore trap: ``WorldLore`` has ``extra='allow'``
    but ``seed_lore_from_world`` only reads history/geography/cosmology/factions.
    A 100-line file of extras = an empty LoreStore. RED today (no guard).
    """
    pack_dir = _clone_pack(NEON_PACK, tmp_path)
    world_lore = pack_dir / "worlds" / NEON_WORLD / "lore.yaml"
    world_lore.write_text(
        "world_name: franchise_nations\n"
        "setting_anchor: A neon arcology of franchised micro-nations.\n"
        "themes:\n"
        "  - corporate feudalism\n"
        "  - brand loyalty as citizenship\n",
        encoding="utf-8",
    )

    with pytest.raises(GenreLoadError) as exc_info:
        load_genre_pack(pack_dir)

    assert NEON_WORLD in str(exc_info.value), (
        "a world with content only under extra (unseeded) keys must fail loud — "
        "it would otherwise reach a live session with an empty LoreStore"
    )


# --------------------------------------------------------------------------- #
# AC2 — OTEL: world-tier lore load emits a state_transition span
# --------------------------------------------------------------------------- #


def test_world_lore_load_emits_otel_span(captured_events: list[dict[str, Any]]) -> None:
    """Loading a world emits a ``state_transition`` watcher span with
    ``field='world_lore'`` so the GM panel can prove the world-tier lore load
    fired — mirroring the existing ``world_items`` / ``world_theme`` /
    ``world_audio`` spans (loader.py).

    RED today: no lore-load span exists at load time; only a seed-time
    ``lore_store_loaded`` span (chargen/connect) is emitted.
    """
    load_genre_pack(NEON_PACK)

    lore_spans = [
        f
        for f in _loaded_spans(captured_events)
        if f.get("field") == "world_lore" and f.get("world_slug") == NEON_WORLD
    ]
    assert lore_spans, (
        "missing world-tier lore OTEL span (field='world_lore', op='loaded', "
        f"world_slug='{NEON_WORLD}') — GM panel cannot prove the world lore load "
        f"fired (loaded spans seen: {sorted(str(f.get('field')) for f in _loaded_spans(captured_events))})"
    )


# --------------------------------------------------------------------------- #
# AC3 — genre-tier lore.yaml is DELETED (Operator scope decision)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pack_name", LIVE_PACKS)
def test_genre_tier_lore_not_loaded(pack_name: str) -> None:
    """No live pack carries genre-tier lore any more — ``pack.lore is None`` for
    every live pack after the genre-lore files are deleted.

    RED today: all 11 ``genre_packs/*/lore.yaml`` files still exist, so
    ``load_genre_pack`` populates ``pack.lore`` (a ``Lore`` instance). Behavioral
    assertion (not a filesystem grep) so it survives any loader refactor.
    """
    pack = load_genre_pack(PACKS_ROOT / pack_name)
    assert isinstance(pack, GenrePack)
    assert pack.lore is None, (
        f"{pack_name}: genre-tier lore must be deleted (pack.lore is None) — "
        "lore is world-only after epic 74"
    )


def test_no_genre_tier_lore_files_on_disk() -> None:
    """The deliverable: zero ``genre_packs/<pack>/lore.yaml`` files remain on
    disk (world-tier ``genre_packs/<pack>/worlds/<world>/lore.yaml`` are
    untouched). RED today: 11 genre-tier lore files exist.
    """
    genre_tier_lore = sorted(p for p in PACKS_ROOT.glob("*/lore.yaml"))
    assert genre_tier_lore == [], (
        "genre-tier lore.yaml files must be deleted; still present: "
        f"{[str(p.relative_to(PACKS_ROOT)) for p in genre_tier_lore]}"
    )


# --------------------------------------------------------------------------- #
# AC4 — every live world seeds a NON-EMPTY, world-sourced LoreStore
#        (the "avoid empty LoreStore" guarantee + the production wiring test)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pack_name,world_slug", LIVE_WORLDS, ids=[f"{p}/{w}" for p, w in LIVE_WORLDS])
def test_every_live_world_seeds_nonempty_world_lore(pack_name: str, world_slug: str) -> None:
    """For every live world, seeding the LoreStore through the real production
    seam (``seed_world_lore``) yields at least one WORLD fragment and ZERO genre
    fragments. This is the core "no empty LoreStore on un-migrated worlds"
    guarantee and the suite's wiring test (drives the real seam, asserts the
    store is populated — no source grep).

    Mostly a regression lock (content already exists), but genuinely RED for any
    world whose lore lives only under extra keys the seeder ignores — those seed
    zero and must be authored into seedable fields.
    """
    pack = load_genre_pack(PACKS_ROOT / pack_name)
    assert world_slug in pack.worlds, f"{pack_name}: world {world_slug!r} not loaded"

    store = LoreStore()
    genre_added, world_added = seed_world_lore(store, pack, world_slug)

    assert genre_added == 0, (
        f"{pack_name}/{world_slug}: genre lore must not be seeded (world-only after epic 74)"
    )
    assert world_added > 0, (
        f"{pack_name}/{world_slug}: world lore seeded ZERO fragments — this world "
        "would have an empty LoreStore once genre lore is gone. Author seedable "
        "world lore (history/geography/cosmology/factions)."
    )
