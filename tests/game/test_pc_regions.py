"""Phase 1 — per-PC region data model (Movement subsystem §Q0/§Q2).

These tests drive the per-PC ``pc_regions`` map, the ``region_for`` accessor,
the ``seed_pc_regions`` helper, the ``WorldStatePatch.pc_region`` apply path,
the per-PC ``notify_region_transition`` signature, and the s4 migration.

No live pack is loaded (``feedback_no_content_coupled_tests``): every snapshot
is a fixture built in-process. Behavior + OTEL/model_fields reflection only —
never a source-text grep.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidequest.game.session import GameSnapshot, WorldStatePatch


def _seated_snapshot(*names: str, region: str = "") -> GameSnapshot:
    """A snapshot with ``names`` seated (player_seats) and an optional anchor."""
    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.player_seats = {f"pid_{n}": n for n in names}
    if region:
        snap.current_region = region
    return snap


# ---------------------------------------------------------------------------
# Test 24 — reflection tripwire (allowed exception per CLAUDE.md)
# ---------------------------------------------------------------------------


def test_reflection_tripwire_fields_exist() -> None:
    assert "pc_region" in WorldStatePatch.model_fields, (
        "WorldStatePatch.pc_region missing — the per-PC region delta field "
        "is the data-model spine of the movement subsystem"
    )
    assert "pc_regions" in GameSnapshot.model_fields, (
        "GameSnapshot.pc_regions missing — the per-PC region truth field"
    )


# ---------------------------------------------------------------------------
# Test 7a — a per-PC patch writes only the moving PC
# ---------------------------------------------------------------------------


def test_pc_region_patch_writes_only_moving_pc() -> None:
    snap = _seated_snapshot("Rux", "Gorm")
    snap.pc_regions = {"Rux": "r1", "Gorm": "r1"}

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))

    assert snap.pc_regions["Rux"] == "r2"
    assert snap.pc_regions["Gorm"] == "r1", "another PC's region was disturbed"


# ---------------------------------------------------------------------------
# Test 7b — region_for three-mode contract, never falls back to current_region
# ---------------------------------------------------------------------------


def test_region_for_perspective_returns_that_pc() -> None:
    snap = _seated_snapshot("Rux", "Gorm", region="anchor")
    snap.pc_regions = {"Rux": "r2", "Gorm": "r3"}
    assert snap.region_for(perspective="Rux") == "r2"
    assert snap.region_for(perspective="Gorm") == "r3"


def test_region_for_consensus_when_all_agree() -> None:
    snap = _seated_snapshot("Rux", "Gorm", region="anchor")
    snap.pc_regions = {"Rux": "r5", "Gorm": "r5"}
    assert snap.region_for() == "r5"


def test_region_for_party_split_returns_none() -> None:
    snap = _seated_snapshot("Rux", "Gorm", region="anchor")
    snap.pc_regions = {"Rux": "r2", "Gorm": "r3"}
    assert snap.region_for() is None


def test_region_for_never_falls_back_to_current_region() -> None:
    # A seated PC with NO pc_regions entry: mode-1 returns None (signal),
    # NEVER the singular current_region.
    snap = _seated_snapshot("Rux", region="anchor_region")
    assert snap.pc_regions == {}
    assert snap.region_for(perspective="Rux") is None, (
        "region_for silently fell back to current_region for a missing "
        "pc_regions entry — forbidden (No Silent Fallbacks)"
    )
    # consensus mode with a missing seated PC → None, not the anchor.
    assert snap.region_for() is None


def test_region_for_emits_party_split_span() -> None:
    from sidequest.telemetry.spans import SPAN_REGION_QUERY

    snap = _seated_snapshot("Rux", "Gorm", region="anchor")
    snap.pc_regions = {"Rux": "r2", "Gorm": "r3"}

    spans = _capture_spans(lambda: snap.region_for())
    region_spans = [s for s in spans if s.name == SPAN_REGION_QUERY]
    assert region_spans, "region_for emitted no snapshot.region_query span"
    assert (region_spans[-1].attributes or {}).get("party_split") is True


# ---------------------------------------------------------------------------
# seed_pc_regions helper
# ---------------------------------------------------------------------------


def test_seed_pc_regions_only_fills_missing_by_default() -> None:
    snap = _seated_snapshot("Rux", "Gorm")
    snap.pc_regions = {"Rux": "already"}

    seeded = snap.seed_pc_regions("entrance")

    assert seeded == 1, "should seed only the PC lacking an entry"
    assert snap.pc_regions["Rux"] == "already", "existing entry must not be overwritten"
    assert snap.pc_regions["Gorm"] == "entrance"


def test_seed_pc_regions_unconditional_overwrites() -> None:
    snap = _seated_snapshot("Rux")
    snap.pc_regions = {"Rux": "old"}
    seeded = snap.seed_pc_regions("entrance", only_missing=False)
    assert seeded == 1
    assert snap.pc_regions["Rux"] == "entrance"


def test_seed_pc_regions_falls_back_to_characters_when_unseated() -> None:
    # Mirror the location block: no player_seats → seed by character core name.
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")
    snap.characters = [
        Character.model_construct(core=CreatureCore.model_construct(name="Solo", statuses=[]))
    ]

    seeded = snap.seed_pc_regions("entrance")
    assert seeded == 1
    assert snap.pc_regions["Solo"] == "entrance"


# ---------------------------------------------------------------------------
# Test 13 — transition fires EXACTLY once per PC with pc_name
# ---------------------------------------------------------------------------


def test_pc_region_move_fires_transition_once_with_pc_name() -> None:
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = _seated_snapshot("Rux", region="entrance")
    snap.pc_regions = {"Rux": "entrance"}
    snap.discovered_regions = ["entrance"]

    seen: list[dict[str, Any]] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        seen.append({"pc_name": pc_name, "from_region": from_region, "to_region": to_region})

    register_frontier_observer(_spy)
    try:
        snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))
    finally:
        unregister_frontier_observer(_spy)

    assert len(seen) == 1, "transition fired more than once for one PC move"
    assert seen[0]["pc_name"] == "Rux"
    assert seen[0]["from_region"] == "entrance"
    assert seen[0]["to_region"] == "r2"
    assert snap.pc_regions["Rux"] == "r2"
    # to_region appended to the SHARED discovered set exactly once (no dup).
    assert snap.discovered_regions.count("r2") == 1


def test_pc_region_move_to_same_region_is_noop() -> None:
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = _seated_snapshot("Rux", region="entrance")
    snap.pc_regions = {"Rux": "r2"}

    fired: list[Any] = []
    obs = lambda **kw: fired.append(kw)  # noqa: E731
    register_frontier_observer(obs)
    try:
        snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))
    finally:
        unregister_frontier_observer(obs)
    assert fired == [], "no-op move (same region) must not fire a transition"


# ---------------------------------------------------------------------------
# Split party preview (Q5) — two distinct moves
# ---------------------------------------------------------------------------


def test_split_party_two_distinct_transitions() -> None:
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = _seated_snapshot("Rux", "Gorm", region="entrance")
    snap.pc_regions = {"Rux": "entrance", "Gorm": "entrance"}
    snap.discovered_regions = ["entrance"]

    seen: list[dict[str, Any]] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        seen.append({"pc_name": pc_name, "to_region": to_region})

    register_frontier_observer(_spy)
    try:
        snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))
        snap.apply_world_patch(WorldStatePatch(pc_region={"Gorm": "r3"}))
    finally:
        unregister_frontier_observer(_spy)

    assert snap.pc_regions == {"Rux": "r2", "Gorm": "r3"}
    assert len(seen) == 2
    assert {s["pc_name"] for s in seen} == {"Rux", "Gorm"}
    # The party is genuinely split: consensus accessor returns None.
    assert snap.region_for() is None
    assert snap.region_for(perspective="Rux") == "r2"
    assert snap.region_for(perspective="Gorm") == "r3"


# ---------------------------------------------------------------------------
# Anchor sync — sq-playtest 2026-06-12 (beneath_sunden current_region /
# pc_regions split-brain). A pc_region crossing that leaves the seated party
# in CONSENSUS must advance the singular ``current_region`` anchor too —
# otherwise every anchor consumer (region projection, forensics, render
# trigger) reads the stale surface region while the PCs stand in the dungeon,
# the narrator never receives the generated room manifest, and it improvises
# the whole crawl.
# ---------------------------------------------------------------------------


def test_pc_region_consensus_advances_current_region_solo() -> None:
    # The live repro shape: Pip descended the_dropmouth -> entrance ->
    # exp001.r0 via pc_region world patches; current_region stayed on the
    # surface forever.
    snap = _seated_snapshot("Pip", region="the_dropmouth")
    snap.pc_regions = {"Pip": "the_dropmouth"}

    snap.apply_world_patch(WorldStatePatch(pc_region={"Pip": "entrance"}))
    assert snap.current_region == "entrance", (
        "solo PC crossed regions but the current_region anchor did not "
        "follow — the projection/forensics split-brain"
    )

    snap.apply_world_patch(WorldStatePatch(pc_region={"Pip": "exp001.r0"}))
    assert snap.current_region == "exp001.r0"


def test_pc_region_split_party_does_not_move_anchor() -> None:
    snap = _seated_snapshot("Rux", "Gorm", region="entrance")
    snap.pc_regions = {"Rux": "entrance", "Gorm": "entrance"}

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))

    assert snap.current_region == "entrance", (
        "a split party has no consensus — the anchor must not jump to one PC's region"
    )


def test_pc_region_consensus_regained_advances_anchor() -> None:
    snap = _seated_snapshot("Rux", "Gorm", region="entrance")
    snap.pc_regions = {"Rux": "entrance", "Gorm": "entrance"}

    snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"}))
    assert snap.current_region == "entrance", "split party moved the anchor"

    snap.apply_world_patch(WorldStatePatch(pc_region={"Gorm": "r2"}))
    assert snap.current_region == "r2", (
        "party regained consensus on r2 but the anchor did not advance"
    )


def test_pc_region_anchor_sync_emits_span() -> None:
    from sidequest.telemetry.spans import SPAN_REGION_ANCHOR_SYNCED

    snap = _seated_snapshot("Pip", region="the_dropmouth")
    snap.pc_regions = {"Pip": "the_dropmouth"}

    spans = _capture_spans(
        lambda: snap.apply_world_patch(WorldStatePatch(pc_region={"Pip": "entrance"}))
    )
    synced = [s for s in spans if s.name == SPAN_REGION_ANCHOR_SYNCED]
    assert synced, (
        "anchor sync fired with no snapshot.region_anchor_synced span — "
        "invisible to the GM panel (OTEL Observability Principle)"
    )
    attrs = synced[-1].attributes or {}
    assert attrs.get("from_region") == "the_dropmouth"
    assert attrs.get("to_region") == "entrance"


def test_pc_region_no_consensus_change_emits_no_sync_span() -> None:
    from sidequest.telemetry.spans import SPAN_REGION_ANCHOR_SYNCED

    snap = _seated_snapshot("Rux", "Gorm", region="entrance")
    snap.pc_regions = {"Rux": "entrance", "Gorm": "entrance"}

    spans = _capture_spans(lambda: snap.apply_world_patch(WorldStatePatch(pc_region={"Rux": "r2"})))
    assert not [s for s in spans if s.name == SPAN_REGION_ANCHOR_SYNCED], (
        "split-party move must not emit an anchor-sync span (nothing synced)"
    )


# ---------------------------------------------------------------------------
# frontier_hook signature — pc_name is required
# ---------------------------------------------------------------------------


def test_notify_region_transition_requires_pc_name() -> None:
    from sidequest.dungeon.frontier_hook import notify_region_transition

    snap = _seated_snapshot("Rux", region="entrance")
    with pytest.raises(TypeError):
        notify_region_transition(snap, from_region="entrance", to_region="r2")  # type: ignore[call-arg]


def test_notify_region_transition_passes_pc_name_to_observer() -> None:
    from sidequest.dungeon.frontier_hook import (
        notify_region_transition,
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = _seated_snapshot("Rux", region="entrance")
    snap.pc_regions = {"Rux": "entrance"}

    captured: list[str] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        captured.append(pc_name)

    register_frontier_observer(_spy)
    try:
        notify_region_transition(snap, pc_name="Rux", from_region="entrance", to_region="r2")
    finally:
        unregister_frontier_observer(_spy)
    assert captured == ["Rux"]


# ---------------------------------------------------------------------------
# Test 7c — s4 migration: legacy current_region seeds pc_regions
# ---------------------------------------------------------------------------


def test_migration_seeds_pc_regions_from_current_region() -> None:
    from sidequest.game.migrations import migrate_legacy_snapshot

    legacy = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "current_region": "entrance",
        "player_seats": {"pid_a": "Rux", "pid_b": "Gorm"},
        # pc_regions absent — every pre-this-story save.
    }

    out = migrate_legacy_snapshot(legacy)

    assert out["pc_regions"] == {"Rux": "entrance", "Gorm": "entrance"}
    # current_region retained as the anchor — NOT dropped.
    assert out["current_region"] == "entrance"

    # The migrated dict round-trips through the model and keeps pc_regions.
    snap = GameSnapshot.model_validate(out)
    assert snap.pc_regions == {"Rux": "entrance", "Gorm": "entrance"}
    assert snap.region_for(perspective="Rux") == "entrance"


def test_migration_noop_when_pc_regions_already_present() -> None:
    from sidequest.game.migrations import migrate_legacy_snapshot

    data = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "current_region": "entrance",
        "player_seats": {"pid_a": "Rux"},
        "pc_regions": {"Rux": "r9"},
    }
    out = migrate_legacy_snapshot(data)
    assert out["pc_regions"] == {"Rux": "r9"}, "migration clobbered live pc_regions"


def test_migration_noop_when_no_current_region() -> None:
    from sidequest.game.migrations import migrate_legacy_snapshot

    data = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "player_seats": {"pid_a": "Rux"},
    }
    out = migrate_legacy_snapshot(data)
    assert not out.get("pc_regions"), "seeded pc_regions with no anchor to seed from"


# ---------------------------------------------------------------------------
# Legacy current_region (anchor) block still seeds pc_regions + fires
# ---------------------------------------------------------------------------


def test_current_region_anchor_seeds_pc_regions() -> None:
    snap = _seated_snapshot("Rux", "Gorm")
    snap.apply_world_patch(WorldStatePatch(current_region="entrance"))
    assert snap.current_region == "entrance", "anchor must still set current_region"
    assert snap.pc_regions == {"Rux": "entrance", "Gorm": "entrance"}, (
        "the spawn/teleport anchor must seed seated PCs lacking an entry"
    )


def test_current_region_anchor_fires_transition_per_seated_pc() -> None:
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = _seated_snapshot("Rux", "Gorm")

    seen: list[str] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        seen.append(pc_name)

    register_frontier_observer(_spy)
    try:
        snap.apply_world_patch(WorldStatePatch(current_region="entrance"))
    finally:
        unregister_frontier_observer(_spy)
    assert sorted(seen) == ["Gorm", "Rux"], (
        "the anchor bootstrap must fire one transition per newly-seeded PC"
    )


def test_current_region_anchor_no_seated_pc_fires_single_sentinel_transition() -> None:
    """No seated PCs AND no characters (the dungeon-bootstrap wiring-test shape,
    e.g. ``GameSnapshot(genre_slug=..., world_slug=...)`` before chargen): the
    spawn anchor must still fire EXACTLY ONE frontier transition (pc_name
    ``"__anchor__"``) so the look-ahead worker stays engaged — and must NOT
    leak a junk ``pc_regions["__anchor__"]`` entry. This is the content-free
    proxy for the materializer/lookahead wiring tests' single-fire assertion."""
    from sidequest.dungeon.frontier_hook import (
        register_frontier_observer,
        unregister_frontier_observer,
    )

    snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")

    seen: list[str] = []

    def _spy(*, snapshot: Any, pc_name: str, from_region: str | None, to_region: str) -> None:
        seen.append(pc_name)

    register_frontier_observer(_spy)
    try:
        snap.apply_world_patch(WorldStatePatch(current_region="entrance"))
    finally:
        unregister_frontier_observer(_spy)

    assert seen == ["__anchor__"], (
        "no-seated-PC spawn bootstrap must fire exactly one sentinel transition"
    )
    assert snap.current_region == "entrance"
    assert "__anchor__" not in snap.pc_regions, "the sentinel pc_name must NOT leak into pc_regions"
    assert snap.pc_regions == {}, "no seated PC / character → nothing to seed"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _capture_spans(fn: Any) -> list[Any]:
    """Run ``fn`` under an in-memory OTEL exporter; return finished spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import sidequest.telemetry.spans as _spans_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real_tracer = provider.get_tracer("test")
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        fn()
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
    return list(exporter.get_finished_spans())
