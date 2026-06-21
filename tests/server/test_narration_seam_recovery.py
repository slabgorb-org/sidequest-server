"""Seam recovery at the narration guard (Story 105-2 Task 5 + spec-review fixes).

The turn-3 repro: router emitted NO movement dispatch; the narrator patched
location to a confabulated deep. Two heading shapes, two guards:

* Heading resolves to NO cartography region ("The Deep Below") + PC on a
  seam-owning region → the unresolvable-heading guard performs the REAL
  crossing via the seam registry, or rejects the patch loud
  (``region.entry_rejected`` reason ``seam_crossing_unresolvable``).
* Heading resolves to the CURRENT seam region via its leading segment
  ("The Dropmouth — The Deep") → the same-region-drift branch STRIPS the
  sub-title back to the region's canonical display name (Architect decision:
  strip, don't cross — firing a crossing off a drifted title would be
  text-classification teleportation). Span reason
  ``seam_region_sub_location_stripped``.

Seam-less region-mode worlds (oz) keep the 90-6 behavior unchanged on both
shapes. Both guards keep the ``character_locations`` ledger consistent with
the decision (the pre-resolution ledger write stamps the confab first).

Fixture shapes:
- ``hybrid_apply_kit``             — beneath_sunden-shaped: region-mode + seam + live
  store + a real tmp world_dir with ``rooms/entrance.yaml`` (name "Under the Rope");
  the kit monkeypatches ``sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS``
  at the module attribute ``_entrance_room_name`` imports at call time (shadowing
  the conftest autouse fixture-pack guard for this test only).
- ``hybrid_apply_kit_empty_store`` — same, but the store has no entrance node.
- ``oz_apply_kit``                 — wry_whimsy/oz-shaped: region-mode, NO seam routes.

Apply-call shape mirrors ``test_region_drift_encounter_continue.py``'s ``_apply()``
helper. Span assertions use the ``otel_capture`` fixture from conftest.py; watcher
assertions monkeypatch ``narration_apply._watcher_publish`` (same file's pattern).
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot, WorldStatePatch
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# ---------------------------------------------------------------------------
# Store doubles (identical to test_movement_seam_crossing.py)
# ---------------------------------------------------------------------------


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with the entrance node."""

    def load_map(self, *, entrance_id):
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _EmptyStore:
    """DungeonStore double: load_map returns a graph with NO nodes (corrupt seed)."""

    def load_map(self, *, entrance_id):
        return RegionGraph(entrance_id=entrance_id)


# ---------------------------------------------------------------------------
# Cartography helpers
# ---------------------------------------------------------------------------


def _hybrid_cartography() -> CartographyConfig:
    """beneath_sunden-shaped: region-mode with a registered seam route.

    the_dropmouth owns a route to ``deep_descent`` (a registered seam kind).
    """
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
            ),
        },
        routes=[
            Route(
                name="Down the Rope",
                description="The one-way descent.",
                from_id="the_dropmouth",
                to_id="deep_descent",
            ),
        ],
    )


def _oz_cartography() -> CartographyConfig:
    """wry_whimsy/oz-shaped: region-mode, NO seam routes."""
    return CartographyConfig(
        starting_region="emerald_city",
        navigation_mode=NavigationMode.region,
        regions={
            "emerald_city": Region(
                name="Emerald City",
                summary="The green hub.",
                description="A shimmering emerald hub.",
            ),
        },
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: exposes pack.worlds[slug].cartography."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


# ---------------------------------------------------------------------------
# Encounter helper (cribbed from test_region_drift_encounter_continue.py)
# ---------------------------------------------------------------------------


def _active_combat() -> StructuredEncounter:
    """An anchored, unfinished combat: dials BELOW threshold (no dial win),
    no opponent yield, ``category="combat"`` (non-mobile). The ONLY way this
    survives a location change is a same-region-drift continue."""
    return StructuredEncounter(
        encounter_type="combat",
        category="combat",
        player_metric=EncounterMetric(name="momentum", current=2, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="menace", current=1, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Groucho", role="combatant", side="player"),
            EncounterActor(name="Hollow Stalker", role="aggressor", side="opponent"),
        ],
    )


# ---------------------------------------------------------------------------
# LookaheadWorkerHandle double
# ---------------------------------------------------------------------------


class _FakeLookaheadHandle:
    """Minimal LookaheadWorkerHandle double carrying persistence + slugs."""

    def __init__(self, store, *, genre_slug: str = "", world_slug: str = ""):
        self.persistence = store
        self.genre_slug = genre_slug
        self.world_slug = world_slug


# ---------------------------------------------------------------------------
# Authored entrance room (item 4: real rooms/entrance.yaml under tmp_path)
# ---------------------------------------------------------------------------

ENTRANCE_ROOM_NAME = "Under the Rope"


def _build_authored_world(tmp_path: Path, monkeypatch) -> None:
    """Create ``<tmp>/caverns_and_claudes/worlds/beneath_sunden/rooms/entrance.yaml``
    (settlement shape — the minimal valid form ``load_room_payload`` accepts
    without mask sidecars) and point the loader's module-level search-path
    constant at the tmp tree. ``_entrance_room_name`` reads
    ``sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS`` at call time,
    so the monkeypatch (shadowing the conftest autouse fixture-pack guard,
    LIFO) is exactly the seam the production helper resolves through — no
    helper-level stubbing.
    """
    rooms_dir = tmp_path / "caverns_and_claudes" / "worlds" / "beneath_sunden" / "rooms"
    rooms_dir.mkdir(parents=True)
    (rooms_dir / f"{ENTRANCE_ID}.yaml").write_text(
        "room_type: settlement\n"
        f"name: {ENTRANCE_ROOM_NAME}\n"
        "description: The shaft collar where the rope ends.\n"
    )
    monkeypatch.setattr(
        "sidequest.genre.loader.DEFAULT_GENRE_PACK_SEARCH_PATHS",
        [tmp_path],
    )


# ---------------------------------------------------------------------------
# Apply kit helper class
# ---------------------------------------------------------------------------


class _ApplyKit:
    """All the moving parts for a narration seam-recovery test.

    ``apply(result)`` wraps ``_apply_narration_result_to_snapshot`` the same
    way the production handler calls it (lookahead_handle threaded).
    """

    def __init__(
        self,
        snapshot: GameSnapshot,
        pack,
        player_name: str,
        world_slug: str,
        handle: _FakeLookaheadHandle | None,
        captured_spans,
    ):
        self.snapshot = snapshot
        self.pack = pack
        self.player_name = player_name
        self.world_slug = world_slug
        self.handle = handle
        self._captured_spans = captured_spans

    def narration_result(self, *, location: str) -> NarrationTurnResult:
        return NarrationTurnResult(
            narration="The rope ends and the dark swallows you whole.",
            location=location,
        )

    def apply(self, result: NarrationTurnResult) -> None:
        _apply_narration_result_to_snapshot(
            snapshot=self.snapshot,
            result=result,
            player_name=self.player_name,
            room=room_for(snapshot=self.snapshot),
            pack=self.pack,
            world=self.world_slug,
            lookahead_handle=self.handle,
        )

    def assert_span(self, span_name: str, **attrs: Any) -> None:
        """Assert at least one finished span with ``span_name`` + matching attrs."""
        finished = self._captured_spans.get_finished_spans()
        matching = [s for s in finished if s.name == span_name]
        assert matching, (
            f"Expected span {span_name!r} but none found. "
            f"Finished spans: {[s.name for s in finished]}"
        )
        for key, expected_val in attrs.items():
            attr_hits = [s for s in matching if (s.attributes or {}).get(key) == expected_val]
            assert attr_hits, (
                f"Span {span_name!r} found but no instance had "
                f"{key}={expected_val!r}. "
                f"Seen attrs: {[(s.attributes or {}) for s in matching]}"
            )

    def assert_no_span_reason(self, reason: str) -> None:
        """Assert NO region.entry_rejected span carries ``reason``."""
        finished = self._captured_spans.get_finished_spans()
        hits = [
            s
            for s in finished
            if s.name == "region.entry_rejected" and (s.attributes or {}).get("reason") == reason
        ]
        assert not hits, f"Unexpected region.entry_rejected reason={reason!r}: {hits}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hybrid_snapshot() -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Groucho": "the_dropmouth"},
        player_seats={"p1": "Groucho"},
    )
    snap.current_region = "the_dropmouth"
    snap.character_locations["Groucho"] = "The Dropmouth"
    return snap


@pytest.fixture
def hybrid_apply_kit(otel_capture, tmp_path, monkeypatch):
    """beneath_sunden-shaped apply kit: hybrid cartography + live store + a
    real authored ``rooms/entrance.yaml`` so the re-anchor resolves the
    authored name 'Under the Rope'."""
    _build_authored_world(tmp_path, monkeypatch)
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    handle = _FakeLookaheadHandle(
        _StoreWithEntrance(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    return _ApplyKit(_hybrid_snapshot(), pack, "Groucho", "beneath_sunden", handle, otel_capture)


@pytest.fixture
def hybrid_apply_kit_empty_store(otel_capture, tmp_path, monkeypatch):
    """beneath_sunden-shaped apply kit: hybrid cartography + dead store."""
    _build_authored_world(tmp_path, monkeypatch)
    cart = _hybrid_cartography()
    pack = _pack_with_cartography("beneath_sunden", cart)
    handle = _FakeLookaheadHandle(
        _EmptyStore(),
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
    )
    return _ApplyKit(_hybrid_snapshot(), pack, "Groucho", "beneath_sunden", handle, otel_capture)


@pytest.fixture
def oz_apply_kit(otel_capture):
    """oz-shaped apply kit: region-mode world with NO seam routes."""
    cart = _oz_cartography()
    pack = _pack_with_cartography("oz", cart)
    snap = GameSnapshot(
        genre_slug="wry_whimsy",
        world_slug="oz",
        pc_regions={"Groucho": "emerald_city"},
        player_seats={"p1": "Groucho"},
    )
    snap.character_locations["Groucho"] = "Emerald City"
    snap.current_region = "emerald_city"
    return _ApplyKit(snap, pack, "Groucho", "oz", None, otel_capture)


@pytest.fixture
def captured_watcher_events(monkeypatch) -> list[dict[str, Any]]:
    """Capture every ``_watcher_publish`` on the narration-apply path
    (pattern from test_region_drift_encounter_continue.py)."""
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

    monkeypatch.setattr(narration_apply, "_watcher_publish", _capture)
    return captured


# ---------------------------------------------------------------------------
# Unresolvable-heading guard: recover / re-anchor / reject
# ---------------------------------------------------------------------------


def test_unresolved_heading_on_seam_region_recovers_crossing(hybrid_apply_kit):
    """AC1: a heading resolving to NO cartography region, PC on a seam region
    → real crossing; PC ends at the procedural entrance; ledger agrees."""
    kit = hybrid_apply_kit
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"PC must be rebound to entrance after seam recovery; "
        f"still at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # The confabulated heading must NOT pollute the surface graph.
    assert "The Deep Below" not in kit.snapshot.discovered_regions, (
        "confabulated deep heading must never enter discovered_regions"
    )
    # Item 2 (ledger consistency): the pre-resolution write stamped the confab;
    # the success path must rewrite it to the re-anchored room name.
    assert kit.snapshot.character_locations["Groucho"] == ENTRANCE_ROOM_NAME, (
        f"ledger must carry the re-anchored entrance room name; "
        f"got {kit.snapshot.character_locations['Groucho']!r}"
    )


def test_recovery_reanchors_scene_to_authored_room_name(hybrid_apply_kit):
    """AC2 (exact assertion restored): result.location is the AUTHORED entrance
    room name from rooms/entrance.yaml — not the confab, not the region id."""
    kit = hybrid_apply_kit
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    assert result.location == ENTRANCE_ROOM_NAME, (
        f"result.location must be the authored room name {ENTRANCE_ROOM_NAME!r}; "
        f"got {result.location!r}"
    )


def test_dead_store_rejects_patch_loud(hybrid_apply_kit_empty_store):
    """AC3: dead store (no entrance node) → patch rejected loudly, PC stays
    put, ledger restored to the prior location."""
    kit = hybrid_apply_kit_empty_store
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        "PC must stay at the_dropmouth when seam crossing fails (no silent fallback)"
    )
    assert "The Deep Below" not in kit.snapshot.discovered_regions, (
        "confabulated heading must NOT enter discovered_regions even on failure"
    )
    kit.assert_span("region.entry_rejected", reason="seam_crossing_unresolvable")
    # Item 2 (ledger consistency): the pre-resolution write stamped the confab;
    # the reject path must restore the prior value.
    assert kit.snapshot.character_locations["Groucho"] == "The Dropmouth", (
        f"ledger must be restored to the prior location on rejection; "
        f"got {kit.snapshot.character_locations['Groucho']!r}"
    )


def test_dead_store_rejection_keeps_combat_and_scratch(
    hybrid_apply_kit_empty_store, captured_watcher_events
):
    """Item 3: a rejected confab is NOT a scene boundary. Pre-105-2 this
    heading shape set drift=True and a seated anchored combat continued —
    rejection must preserve that: combat NOT abandoned, scratch sweep
    SKIPPED (the skip event fires; the abandon event does not)."""
    kit = hybrid_apply_kit_empty_store
    kit.snapshot.encounter = _active_combat()
    result = kit.narration_result(location="The Deep Below")
    kit.apply(result)

    enc = kit.snapshot.encounter
    assert enc is not None and not enc.resolved, (
        "a rejected seam-confab patch must NOT abandon an anchored combat; "
        f"got resolved={enc.resolved!r} outcome={enc.outcome!r}"
    )
    events = {e["event_type"] for e in captured_watcher_events}
    assert "confrontation_deactivated_on_location_change" not in events, (
        f"rejection must not fire the abandon event; got {sorted(events)}"
    )
    assert "confrontation_continued_same_region_drift" in events, (
        f"the drift-continue keep event must fire; got {sorted(events)}"
    )
    assert "scratch_sweep_skipped_same_region_drift" in events, (
        f"the scratch sweep must be SKIPPED (skip event present); got {sorted(events)}"
    )
    # Lie-detector hygiene: a rejected patch is NOT a move — the GM panel
    # must not see a ``state.location_update`` (state_transition
    # field=location, after="") on a turn where the engine refused the
    # relocation. The rejection's own region.entry_rejected span is the
    # only location-shaped telemetry for this turn.
    location_updates = [
        e
        for e in captured_watcher_events
        if e["event_type"] == "state_transition" and e["fields"].get("field") == "location"
    ]
    assert location_updates == [], (
        f"no location state_transition may fire on the reject path; got {location_updates!r}"
    )


# ---------------------------------------------------------------------------
# Drift-strip: heading resolves to the CURRENT seam region (the literal
# "The Dropmouth — The Deep" turn-3 repro shape)
# ---------------------------------------------------------------------------


def test_drift_strip_on_seam_region(hybrid_apply_kit):
    """Item 1: a sub-title over a seam-owning region is stripped to the
    region's canonical display name — no crossing, no confab accepted."""
    kit = hybrid_apply_kit
    result = kit.narration_result(location="The Dropmouth — The Deep")
    kit.apply(result)

    # Strip, don't cross: PC stays on the surface seam region.
    assert kit.snapshot.region_for(perspective="Groucho") == "the_dropmouth", (
        f"drift-strip must NOT cross the seam; "
        f"PC at {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    # Re-anchored to the cartography region's canonical display name.
    assert result.location == "The Dropmouth", (
        f"sub-title must be stripped to the canonical region name; got {result.location!r}"
    )
    assert kit.snapshot.character_locations["Groucho"] == "The Dropmouth", (
        f"ledger must match the stripped canonical name; "
        f"got {kit.snapshot.character_locations['Groucho']!r}"
    )
    # The confabulated sub-title must not survive anywhere.
    assert "The Dropmouth — The Deep" not in kit.snapshot.discovered_regions
    kit.assert_span("region.entry_rejected", reason="seam_region_sub_location_stripped")


def test_oz_drift_retitle_unchanged(oz_apply_kit):
    """Item 5 non-regression: in a SEAM-LESS region-mode world a sub-title
    that resolves to the current region keeps the 90-6 cosmetic re-title —
    the strip must NOT fire."""
    kit = oz_apply_kit
    result = kit.narration_result(location="Emerald City — Inside the Gates")
    kit.apply(result)

    assert result.location == "Emerald City — Inside the Gates", (
        f"seam-less worlds keep the cosmetic re-title; got {result.location!r}"
    )
    assert kit.snapshot.character_locations["Groucho"] == "Emerald City — Inside the Gates", (
        "the cosmetic re-title still lands on the ledger in seam-less worlds"
    )
    assert kit.snapshot.region_for(perspective="Groucho") == "emerald_city"
    kit.assert_no_span_reason("seam_region_sub_location_stripped")


# ---------------------------------------------------------------------------
# 90-6 non-regression: unresolvable heading, seam-less world
# ---------------------------------------------------------------------------


def test_seamless_region_mode_world_unchanged(oz_apply_kit):
    """90-6 non-regression: an UNRESOLVABLE POI heading in a seam-less
    region-mode world still hits entry_skipped_sub_location
    (reason=sub_location_in_region_mode_world). The seam guards must NOT fire."""
    kit = oz_apply_kit
    result = kit.narration_result(location="The Throne Room")
    kit.apply(result)

    assert kit.snapshot.region_for(perspective="Groucho") == "emerald_city", (
        f"PC region must stay emerald_city in a seam-less world; "
        f"got {kit.snapshot.region_for(perspective='Groucho')!r}"
    )
    assert "The Throne Room" not in kit.snapshot.discovered_regions
    kit.assert_span("region.entry_rejected", reason="sub_location_in_region_mode_world")
    kit.assert_no_span_reason("seam_crossing_unresolvable")
    kit.assert_no_span_reason("seam_region_sub_location_stripped")


# ---------------------------------------------------------------------------
# Story 153-21: don't-clobber a same-turn crossing receipt.
#
# The movement subsystem already crossed the deep_descent seam EARLIER this
# turn (ADR-113 engine-first dispatch runs before the narrator), leaving a
# same-turn crossing receipt on the snapshot: PC at ENTRANCE_ID +
# a this-turn region_transitions entry to "entrance". The bug: apply then
# resolves the sticky heading "The Dropmouth — First Chamber" back to
# known_region_id="the_dropmouth" (leading-segment match) and the static
# latch (narration_apply.py ~4291) CLOBBERS current_region/pc_regions back
# to "the_dropmouth", dragging the PC out of the dungeon. The fix is a guard
# that refuses to clobber a same-turn crossing.
# ---------------------------------------------------------------------------


def _seed_same_turn_crossing_receipt(kit: _ApplyKit) -> None:
    """Simulate what ``run_movement_dispatch`` → ``resolve_deep_descent``
    already did earlier this turn (ADR-113 engine-first): the faithful
    production receipt is a ``pc_region`` world patch onto ENTRANCE_ID
    (``deep_descent.py:54`` calls exactly this). ``apply_world_patch``
    (``session.py:1537``) then sets ``pc_regions["Groucho"]="entrance"``,
    appends a same-turn ``RegionTransition(to_region="entrance")``, fires the
    frontier hook (which dedup-appends "entrance" to ``discovered_regions``),
    and anchor-syncs ``current_region`` to "entrance"."""
    kit.snapshot.apply_world_patch(WorldStatePatch(pc_region={"Groucho": ENTRANCE_ID}))


def test_seam_receipt_precondition_holds(hybrid_apply_kit):
    """Sanity gate for the RED below: confirm the seeded receipt mirrors a
    real movement crossing (PC at entrance, same-turn transition, current
    anchor advanced, discovered counter bumped) BEFORE apply runs. If this
    precondition ever breaks the AC1 test would red for the wrong reason."""
    kit = hybrid_apply_kit
    _seed_same_turn_crossing_receipt(kit)

    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID
    assert kit.snapshot.current_region == ENTRANCE_ID
    this_turn = kit.snapshot.turn_manager.interaction
    crossings = [
        t
        for t in kit.snapshot.region_transitions
        if t.to_region == ENTRANCE_ID and t.turn == this_turn and t.pc_name == "Groucho"
    ]
    assert crossings, (
        "the seed must leave a same-turn region_transitions entry to entrance "
        f"(turn={this_turn}); got {kit.snapshot.region_transitions!r}"
    )
    # AC4 baseline: the crossing's frontier hook bumps discovered_regions.
    assert ENTRANCE_ID in kit.snapshot.discovered_regions


def test_resolved_seam_heading_honors_same_turn_crossing(
    hybrid_apply_kit, captured_watcher_events
):
    """153-21 AC1/AC2/AC3/AC4/AC6 — the don't-clobber guard.

    Given a same-turn deep_descent crossing receipt (PC already at the
    procedural ``entrance``), a sticky narrator heading that resolves BACK to
    the seam-owning surface region ("The Dropmouth — First Chamber" →
    ``the_dropmouth``) must NOT drag the PC back to the surface. Apply must
    detect the same-turn crossing and DECLINE to clobber it.

    FAILS against current code: the static latch at narration_apply.py ~4291
    fires (``current_region != known_region_id`` is ``"entrance" !=
    "the_dropmouth"`` → True), re-sets current_region/pc_regions to
    ``the_dropmouth``, publishes ``region_current_advanced`` with
    ``new_region="the_dropmouth"``, and logs
    ``region.entry_resolved_to_cartography`` — the clobber-back.
    """
    kit = hybrid_apply_kit
    _seed_same_turn_crossing_receipt(kit)

    result = kit.narration_result(location="The Dropmouth — First Chamber")
    kit.apply(result)

    # AC1: the PC stays on the procedural entrance node — NOT clobbered back
    # to the static surface region.
    assert kit.snapshot.region_for(perspective="Groucho") == ENTRANCE_ID, (
        f"AC1: PC must stay on the procedural entrance after a same-turn "
        f"crossing; got {kit.snapshot.region_for(perspective='Groucho')!r} "
        f"(clobbered back to the seam-owning surface region)"
    )
    assert kit.snapshot.current_region == ENTRANCE_ID, (
        f"AC1: current_region must stay at the crossed entrance; got "
        f"{kit.snapshot.current_region!r} (latch re-anchored the surface region)"
    )

    # AC3: the scene + ledger re-anchor to the AUTHORED entrance room name.
    assert result.location == ENTRANCE_ROOM_NAME, (
        f"AC3: scene must re-anchor to the authored entrance room "
        f"{ENTRANCE_ROOM_NAME!r}; got {result.location!r}"
    )
    assert kit.snapshot.character_locations["Groucho"] == ENTRANCE_ROOM_NAME, (
        f"AC3: ledger must agree with the re-anchored scene; got "
        f"{kit.snapshot.character_locations['Groucho']!r}"
    )

    # AC4: the discovered counter reflects deep entry on THIS descent (the
    # crossing's frontier hook added it). Apply must not regress it.
    assert ENTRANCE_ID in kit.snapshot.discovered_regions, (
        "AC4: the deep entrance must be in discovered_regions after the "
        "first descent"
    )

    # AC6: the apply decision is observable — a movement.resolved span fired
    # recording that apply HONORED the same-turn crossing (narration-driven),
    # reusing the 105-2 vocabulary.
    kit.assert_span("movement.resolved", resolved_via="narration_seam_recovery")

    # AC6: the static-region latch span MUST NOT fire for the crossed descent.
    cartography_latches = [
        s
        for s in kit._captured_spans.get_finished_spans()
        if s.name == "region.entry_canonicalized_dedup"
        and (s.attributes or {}).get("resolution") == "cartography"
        and (s.attributes or {}).get("existing_surface_form") == "the_dropmouth"
    ]
    assert not cartography_latches, (
        "AC6: region.entry_resolved_to_cartography (the static latch) must "
        f"NOT fire for the crossed descent; got {cartography_latches!r}"
    )

    # AC6: the watcher region-advance lane must carry "entrance", and must NOT
    # publish a clobber-back to the seam-owning surface region.
    advances = [e for e in captured_watcher_events if e["event_type"] == "region_current_advanced"]
    clobber_backs = [e for e in advances if e["fields"].get("new_region") == "the_dropmouth"]
    assert not clobber_backs, (
        f"AC6: no region_current_advanced may carry new_region='the_dropmouth' "
        f"(that is the clobber); got {clobber_backs!r}"
    )
    entrance_advances = [e for e in advances if e["fields"].get("new_region") == ENTRANCE_ID]
    assert entrance_advances, (
        f"AC6: a region_current_advanced with new_region={ENTRANCE_ID!r} must "
        f"fire so the GM panel sees the engine honor the crossing; "
        f"got advances={advances!r}"
    )
