"""Story 158-19 RED — per-expansion quests surface only one theme.

The 2026-06-23 beneath_sunden MP playtest showed every player-visible quest
reading "Below the Black Water" (drowned_cavern) across a 10-expansion descent,
even though the world ships five themes with distinct quest_templates.

Four root causes, each pinned by a test below:

  1. Quest binds the DEEPEST region's theme; drowned_cavern's depth_band {0,60}
     is the widest, so it keeps winning the deepest slot even at depth 42-52.
  2. Shallow zone is single-theme by construction: at depth_score 0-19 the
     eligible theme_pool is exactly [drowned_cavern].  ← HEADLINE (content authoring)
  3. Sparse projection: reconcile only projects the LANDED expansion, so
     multi-expansion jumps skip the diverse intermediate expansions.
  4. Static title: drowned_cavern's title is a literal with no varying slot, so
     same-theme repeats are byte-identical.

Plus AC-5 (OTEL: dungeon.quest.bound must carry the bound theme id) and AC-6
(no regression to the mint→project→complete path — guarded by the existing
tests/dungeon/test_expansion_quest_e2e.py, intentionally not duplicated here).

These tests are RED until the content (theme depth_bands + titles) and server
(projection + theme-on-span) fixes land.  Exact depth_band/title values are a
crunch/pacing call owned by Keith — the assertions enforce only the directional
invariants the playtest proved, never a specific number Keith hasn't chosen.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import sidequest.telemetry.spans as _spans_module
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.dungeon.expansion_quest import (
    make_expansion_quest_observer,
    seed_expansion_quest,
)
from sidequest.dungeon.persistence import ComplicationThread, DungeonStore
from sidequest.dungeon.region_graph.model import Expansion, RegionNode
from sidequest.dungeon.themes import (
    ThemePalette,
    load_theme_palette,
    theme_eligible_at_depth,
)
from sidequest.game.session import GameSnapshot
from sidequest.telemetry.spans.dungeon_quest import SPAN_QUEST_BOUND

# The depth at which the 2026-06-23 playtest DB showed drowned_cavern wrongly
# winning the deepest slot (exp005.r3@42.2, exp010.r0@52.8).  A region this deep
# must NOT be eligible for drowned once its band is narrowed.  Keith owns the
# exact max; this is only the bug-repro floor.
_DROWNED_DOMINATION_DEPTH = 50.0


# --------------------------------------------------------------------------- #
# Helpers (inlined per CLAUDE.md — no reaching across test modules)
# --------------------------------------------------------------------------- #


def _world_dir() -> Path:
    # tests/dungeon/<file> -> tests -> sidequest-server -> repo root;
    # sidequest-content is a sibling of sidequest-server (parents[3]).
    return (
        Path(__file__).resolve().parents[3]
        / "sidequest-content/genre_packs/caverns_and_claudes/worlds/beneath_sunden"
    )


def _palette() -> ThemePalette:
    return load_theme_palette(_world_dir())


def _store() -> tuple[sqlite3.Connection, DungeonStore]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _reach_deep_thread(exp_id: int, anchor: str, title: str) -> ComplicationThread:
    return ComplicationThread(
        thread_id=f"q.exp{exp_id}.x",
        origin_region_id=anchor,
        kind="quest",
        status="open",
        started_at_depth_score=float(exp_id) * 10.0,
        payload={
            "scope": "expansion",
            "expansion_id": exp_id,
            "signature_kind": "reach_deep",
            "ref_id": anchor,
            "anchor_region": anchor,
            "title": title,
            "objective": f"Reach the bottom of expansion {exp_id}.",
        },
    )


def _drowned_expansion(expansion_id: int, deepest_depth: float) -> Expansion:
    """A two-region drowned_cavern expansion whose deepest node is unique per
    expansion (distinct id + depth) — the natural distinguisher inputs."""
    r0 = f"exp{expansion_id:03d}.r0"
    r1 = f"exp{expansion_id:03d}.r1"
    return Expansion(
        expansion_id=expansion_id,
        new_nodes=[
            RegionNode(id=r0, expansion_id=expansion_id, theme="drowned_cavern", depth_score=5.0),
            RegionNode(
                id=r1,
                expansion_id=expansion_id,
                theme="drowned_cavern",
                depth_score=deepest_depth,
            ),
        ],
        new_edges=[],
    )


# --------------------------------------------------------------------------- #
# AC-2 (KEYSTONE — content authoring): shallow depth must offer variety
# --------------------------------------------------------------------------- #


def test_shallow_depth_has_at_least_two_eligible_themes() -> None:
    """AC-2 headline.  The opening descent (depth_score 0-19) currently has
    exactly one eligible theme — drowned_cavern — so every shallow expansion is
    forced to bind it.  Keith: "we are starved of templates in the easiest
    levels."  Floor is >1 (AC text); SM target is >=3 (his pacing call)."""
    pal = _palette()
    at_surface = pal.themes_for_depth(0.0)
    at_mid_shallow = pal.themes_for_depth(15.0)

    surface_ids = sorted(t.id for t in at_surface)
    assert len(at_surface) >= 2, (
        "shallow zone is single-theme by construction — depth_score 0 has only "
        f"{surface_ids}; author more shallow-eligible themes (target >=3) so the "
        "opening descent is not monotone"
    )
    assert len(at_mid_shallow) >= 2, (
        "depth_score 15 (still the opening descent) has only "
        f"{sorted(t.id for t in at_mid_shallow)} — shallow band must stay diverse "
        "across 0-19, not just at the surface"
    )


def test_drowned_cavern_no_longer_dominates_deep_slot() -> None:
    """AC-2 second half + root cause #1.  drowned_cavern's band {0,60} is the
    widest, so it keeps winning the deepest-region slot deep in the dungeon (the
    playtest DB showed it at depth 42.2 and 52.8 beside winding/bone siblings).
    Once narrowed, a depth-50 region must no longer be drowned-eligible.

    Keith owns the exact max — this asserts only that it drops below the
    bug-repro depth (50), not a specific value."""
    drowned = _palette().get("drowned_cavern")
    assert drowned.depth_band.max is not None, (
        "drowned_cavern must keep a bounded max so it stops spanning the whole "
        "dungeon and dominating the deepest slot"
    )
    assert not theme_eligible_at_depth(drowned, _DROWNED_DOMINATION_DEPTH), (
        f"drowned_cavern is still eligible at depth_score {_DROWNED_DOMINATION_DEPTH} "
        f"(band max={drowned.depth_band.max}); the 2026-06-23 playtest proved it "
        "wins the deepest slot at depth 42-52 — narrow its max below the deep zone"
    )


# --------------------------------------------------------------------------- #
# AC-1: a shallow descent can surface MULTIPLE DISTINCT quest titles
# --------------------------------------------------------------------------- #


def test_shallow_band_offers_at_least_two_distinct_quest_titles() -> None:
    """AC-1, grounded in content.  The player-visible promise is ">=2 distinct
    quest titles across a shallow descent."  That is only possible if the
    shallow-eligible themes carry >=2 DISTINCT quest_template titles.  Today the
    only shallow theme is drowned_cavern -> exactly one bindable title."""
    pal = _palette()
    shallow_themes = pal.themes_for_depth(0.0)
    titles = {
        t.quest_template.title.strip()
        for t in shallow_themes
        if t.quest_template is not None and t.quest_template.title.strip()
    }
    assert len(titles) >= 2, (
        "a shallow descent can surface at most one distinct quest title "
        f"({sorted(titles)}); add shallow-eligible themes whose quest_templates "
        "have distinct titles so the opening descent reads with variety"
    )


# --------------------------------------------------------------------------- #
# AC-3: expansions traversed/jumped-through still project their quest
# --------------------------------------------------------------------------- #


def test_traversed_expansions_project_into_quest_log() -> None:
    """AC-3 + root cause #3.  reconcile only projects the LANDED expansion, so a
    multi-expansion-per-turn jump (exp1 -> exp5) skips the diverse intermediate
    expansions (exp2-4) and their quests never reach quest_log.

    Drives the REAL frontier observer with a multi-expansion jump and asserts
    every traversed expansion projects.  The exact inference the observer uses
    (path range vs. all open threads <= current) is Dev's design choice — this
    asserts only the player-visible outcome.

    NOTE (AC-3 OR-clause): the AC permits an alternative resolution — a
    documented decision that only LANDED-in expansions mint quests.  If green
    takes that path, this test should be revised to assert the documented
    landed-only behavior.  See the TEA deviation log for this story."""
    conn, store = _store()
    # Threads for exp1..exp5 exist (minted when those expansions materialised).
    # Anchors are deep regions distinct from the jump destination so none
    # spuriously resolve on this transition.
    for exp_id in range(1, 6):
        store.open_thread(
            _reach_deep_thread(exp_id, f"exp{exp_id:03d}.r3", f"Quest for exp{exp_id}")
        )
    conn.commit()

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    observer = make_expansion_quest_observer(store)

    # The player was in exp1 and jumps straight to exp5, passing through 2-4.
    observer(snapshot=snap, pc_name="Rux", from_region="exp001.r0", to_region="exp005.r0")

    projected = set(snap.quest_log.keys())
    traversed = {"dungeon:exp2", "dungeon:exp3", "dungeon:exp4", "dungeon:exp5"}
    missing = traversed - projected
    assert not missing, (
        f"expansions jumped through were not projected into quest_log: {sorted(missing)} "
        f"(quest_log has {sorted(projected)}); a multi-expansion jump must surface the "
        "quests of the expansions it descended past, not just the landed one"
    )


# --------------------------------------------------------------------------- #
# AC-4: same-theme expansions produce distinguishable quest titles
# --------------------------------------------------------------------------- #


def test_same_theme_expansions_produce_distinct_quest_titles() -> None:
    """AC-4 + root cause #4.  Two expansions that both bind drowned_cavern (the
    real shipped template) produce byte-identical titles because the title is a
    static literal with no varying slot.  Distinct deepest regions/depths must
    yield distinct player-visible titles (region/depth/ordinal token).

    Seeds through the REAL drowned_cavern template and reads the stored payload
    titles, so it is robust to whether the distinguisher is a new {slot} in the
    YAML title or injected at seed time."""
    template = _palette().get("drowned_cavern").quest_template
    assert template is not None, "drowned_cavern must ship a quest_template"

    conn_a, store_a = _store()
    conn_b, store_b = _store()

    seed_expansion_quest(
        campaign_seed=99,
        expansion=_drowned_expansion(1, deepest_depth=12.0),
        manifests_by_region={},
        template=template,
        store=store_a,
        started_at_depth_score=5.0,
    )
    seed_expansion_quest(
        campaign_seed=99,
        expansion=_drowned_expansion(5, deepest_depth=48.0),
        manifests_by_region={},
        template=template,
        store=store_b,
        started_at_depth_score=40.0,
    )
    conn_a.commit()
    conn_b.commit()

    title_a = store_a.open_threads()[0].payload["title"]
    title_b = store_b.open_threads()[0].payload["title"]
    assert title_a != title_b, (
        f"two different drowned_cavern expansions produced identical quest titles "
        f"({title_a!r}); same-theme repeats must be distinguishable by a "
        "region/depth/ordinal token so the Quests tab does not read as duplicates"
    )


# --------------------------------------------------------------------------- #
# AC-5: the dungeon.quest.bound span carries the bound theme id
# --------------------------------------------------------------------------- #


def test_quest_bound_span_carries_bound_theme() -> None:
    """AC-5.  The GM panel (the lie-detector) must be able to verify which theme
    each quest bound.  seed_expansion_quest emits dungeon.quest.bound, but the
    span carries no theme today.  Capture the real span via the global-tracer
    override (same pattern as test_expansion_quest_e2e) and assert it does."""
    conn, store = _store()
    template = _palette().get("drowned_cavern").quest_template
    assert template is not None

    expansion = Expansion(
        expansion_id=4,
        new_nodes=[
            RegionNode(id="exp004.r0", expansion_id=4, theme="bone_crypt", depth_score=20.0),
            RegionNode(id="exp004.r1", expansion_id=4, theme="bone_crypt", depth_score=55.0),
        ],
        new_edges=[],
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("test")

    original_tracer_fn = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[assignment]
    try:
        seed_expansion_quest(
            campaign_seed=11,
            expansion=expansion,
            manifests_by_region={},
            template=template,
            store=store,
            started_at_depth_score=20.0,
        )
        conn.commit()
    finally:
        _spans_module.tracer = original_tracer_fn  # type: ignore[assignment]

    bound = [s for s in exporter.get_finished_spans() if s.name == SPAN_QUEST_BOUND]
    assert bound, "seed_expansion_quest did not emit a dungeon.quest.bound span"
    theme_attr = bound[0].attributes.get("theme")
    assert theme_attr == "bone_crypt", (
        "dungeon.quest.bound span does not carry the bound theme id "
        f"(theme attribute = {theme_attr!r}); the GM panel cannot verify which "
        "theme each quest used — emit the deepest region's theme on the span"
    )


def test_span_route_surfaces_bound_theme_to_gm_panel() -> None:
    """AC-5 wiring.  Emitting the attribute is not enough — the SPAN_ROUTES
    extractor is what the GM panel consumes, and it drops everything not in its
    projection dict.  The route for dungeon.quest.bound must surface `theme`."""
    import sidequest.telemetry.spans as spans_pkg

    route = spans_pkg.SPAN_ROUTES[SPAN_QUEST_BOUND]
    fake_span = SimpleNamespace(
        attributes={
            "expansion_id": 1,
            "signature_kind": "reach_deep",
            "ref_id": "exp001.r3",
            "degraded": False,
            "theme": "bone_crypt",
        }
    )
    projected = route.extract(fake_span)
    assert projected.get("theme") == "bone_crypt", (
        "SPAN_ROUTES[dungeon.quest.bound].extract drops the bound theme "
        f"(projected keys: {sorted(projected)}); add 'theme' so the GM panel can "
        "verify which theme each quest bound"
    )
