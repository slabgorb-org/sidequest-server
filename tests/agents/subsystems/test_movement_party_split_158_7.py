"""Story 158-7 (RED): a co-located party advances to the SAME region node.

Playtest finding (``~/Projects/sq-playtest-pingpong.md`` — "[BUG] MP
party-split across the seam"): when a co-located party submits the SAME
descent (narrated as one anchored card), region advance is resolved
PER-ACTING-PC, so co-located players desync across hops. Repro session
``2026-06-21-beneath_sunden-mp-6c89369d`` turn 3: both players descended
together yet landed on DIFFERENT region nodes (``Groucho: the_dropmouth``,
``Harpo: exp001.r2``), and the split direction is NON-DETERMINISTIC.

The story DECIDED the semantic (title): **a co-located party advances to
the same region node.** These tests pin that contract.

DECIDED CONTRACT (see the 158-7 TEA Assessment in the session file):
``run_movement_dispatch`` gains an ``additional_player_names: list[str] |
None`` parameter — the SAME co-seated-party signal that
``run_confrontation_dispatch`` and ``run_dogfight_dispatch`` already
declare, and that ``run_dispatch_bank`` already threads into the bank
context (signature-filtered). When the acting PC advances across a region
hop, every peer in ``additional_player_names`` who is **co-located with the
acting PC** (same source region) advances to the **same destination node**.
A peer in a DIFFERENT region is NOT dragged (a genuinely split party stays
split). Each advanced PC emits its own ``movement.resolved`` span (the
per-PC span doctrine in ``telemetry/spans/movement.py``: "every span
carries pc_name so the panel sees each PC's move independently").

These tests FAIL today: ``run_movement_dispatch`` advances only the acting
PC (``§Q5 split-party — no party token``) and does not accept the
co-mover signal.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems import run_dispatch_bank
from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionEdge, RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
)
from sidequest.protocol.dispatch import (
    DispatchPackage,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)

# ---------------------------------------------------------------------------
# Helpers (mirror tests/agents/subsystems/test_movement_seam_crossing.py)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _movement(direction: str, descriptor: str = "") -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"direction": direction, "exit_descriptor": descriptor},
        idempotency_key="mv-158-7",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _StoreWithEntrance:
    """DungeonStore double: load_map returns a graph with the entrance node."""

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar"))
        return g


class _StoreWithDeepGraph:
    """DungeonStore double: entrance + one materialized deep region below it."""

    def load_map(self, *, entrance_id: str) -> RegionGraph:
        g = RegionGraph(entrance_id=entrance_id)
        g.add_node(
            RegionNode(id=entrance_id, expansion_id=0, theme="shaft_collar", depth_score=0.0)
        )
        g.add_node(
            RegionNode(id="exp001.r0", expansion_id=1, theme="shaft_collar", depth_score=7.9)
        )
        g.add_edge(RegionEdge(a=entrance_id, b="exp001.r0", kind="shaft"))
        return g


class _FakePalette:
    """Duck-typed ThemePalette — movement does not reach projection for a
    surface→deep crossing, but the signature requires a non-None palette."""

    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _hybrid_cartography() -> CartographyConfig:
    """beneath_sunden-shaped: region-mode with a registered seam route.

    ``the_dropmouth`` owns a route to ``deep_descent`` (a registered seam
    kind); ``ropefoot`` (the surface camp where the party spawns) is one step
    adjacent to it. A descent from EITHER crosses the seam onto ``entrance``.
    """
    return CartographyConfig(
        starting_region="ropefoot",
        navigation_mode=NavigationMode.region,
        regions={
            "ropefoot": Region(
                name="Ropefoot",
                summary="Surface camp.",
                description="The waiting camp above the shaft.",
                adjacent=["the_dropmouth"],
            ),
            "the_dropmouth": Region(
                name="The Dropmouth",
                summary="The lip of the shaft.",
                description="The mouth of the descent.",
                adjacent=["ropefoot"],
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


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    """Duck-typed GenrePack: exposes ``pack.worlds[slug].cartography``."""
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


def _party_snapshot(pc_regions: dict[str, str], seats: dict[str, str]) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions=dict(pc_regions),
        player_seats=dict(seats),
    )


@pytest.fixture
def capture_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-party-split-158-7")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)
    return exporter


def _resolved_spans(exporter: InMemorySpanExporter):
    return [s for s in exporter.get_finished_spans() if s.name == "movement.resolved"]


# ---------------------------------------------------------------------------
# AC-1 / AC-2: a co-located party moving together lands on the SAME node.
# ---------------------------------------------------------------------------


def test_colocated_party_descends_together_surface_adjacent(capture_spans):
    """The exact repro: both PCs start co-located at the surface camp
    (``ropefoot``) and descend together. Both must cross onto ``entrance``
    — not just the acting PC."""
    snap = _party_snapshot(
        {"Groucho": "ropefoot", "Harpo": "ropefoot"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    out = _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    assert out.data.get("resolved_via") == "surface_descent_adjacent", (
        f"expected an adjacent-seam crossing, got: {out.data}"
    )
    assert snap.pc_regions["Groucho"] == ENTRANCE_ID, "acting PC must cross to entrance"
    assert snap.pc_regions["Harpo"] == ENTRANCE_ID, (
        "co-located peer must descend WITH the party, not lag at ropefoot; "
        f"Harpo is at {snap.pc_regions.get('Harpo')!r}"
    )


def test_colocated_party_descends_together_owned_seam(capture_spans):
    """Both PCs co-located on the seam-owner region (``the_dropmouth``)
    descend together → both cross onto ``entrance``."""
    snap = _party_snapshot(
        {"Groucho": "the_dropmouth", "Harpo": "the_dropmouth"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    out = _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    assert out.data.get("resolved_via") == "surface_descent", f"expected seam crossing: {out.data}"
    assert snap.pc_regions["Groucho"] == ENTRANCE_ID
    assert snap.pc_regions["Harpo"] == ENTRANCE_ID, (
        f"co-located peer left behind at {snap.pc_regions.get('Harpo')!r}"
    )


def test_colocated_party_advances_together_in_dungeon(capture_spans):
    """The procedural-hop case (where the repro's non-determinism lived):
    both PCs co-located on the dungeon entrance node advance one hop deeper
    together to the SAME materialized node (``exp001.r0``)."""
    snap = _party_snapshot(
        {"Groucho": ENTRANCE_ID, "Harpo": ENTRANCE_ID},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    snap.discovered_regions.append(ENTRANCE_ID)
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    out = _run(
        run_movement_dispatch(
            _movement("deeper"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithDeepGraph(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    assert out.data.get("to_region") == "exp001.r0", f"acting PC must step deeper: {out.data}"
    assert snap.pc_regions["Groucho"] == "exp001.r0"
    assert snap.pc_regions["Harpo"] == "exp001.r0", (
        f"co-located peer must advance to the same node; Harpo at {snap.pc_regions.get('Harpo')!r}"
    )


def test_party_region_consensus_after_shared_hop(capture_spans):
    """``region_for()`` (no perspective) returns the consensus region only
    when all seated PCs agree, else None (party split). After a shared
    descent it must report the DESTINATION — proof the party did not desync.

    Guards against a false pass where nobody moved: asserts the consensus is
    the destination (``entrance``), not the unchanged source."""
    snap = _party_snapshot(
        {"Groucho": "ropefoot", "Harpo": "ropefoot"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    assert snap.region_for() == "ropefoot", "precondition: party starts in consensus at ropefoot"

    _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    assert snap.region_for() == ENTRANCE_ID, (
        "party must stay in consensus on the destination after a shared hop; "
        f"region_for() returned {snap.region_for()!r} (None = split party)"
    )


# ---------------------------------------------------------------------------
# AC-3: the destination must NOT depend on which PC anchors the beat.
# ---------------------------------------------------------------------------


def test_party_advance_is_order_independent(capture_spans):
    """Determinism: a shared descent yields the SAME end-state regardless of
    which co-located PC is the acting/anchor PC. The repro's split direction
    was non-deterministic — this pins it out."""
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    def _descend(anchor: str, peer: str) -> dict[str, str]:
        snap = _party_snapshot(
            {anchor: "ropefoot", peer: "ropefoot"},
            {"p1": anchor, "p2": peer},
        )
        _run(
            run_movement_dispatch(
                _movement("deeper", "down the rope"),
                snapshot=snap,
                player_name=anchor,
                additional_player_names=[peer],
                dungeon_store=_StoreWithEntrance(),
                palette=_FakePalette(),
                pack=pack,
            )
        )
        return dict(snap.pc_regions)

    groucho_leads = _descend("Groucho", "Harpo")
    harpo_leads = _descend("Harpo", "Groucho")

    assert groucho_leads == {"Groucho": ENTRANCE_ID, "Harpo": ENTRANCE_ID}, (
        f"Groucho-anchored descent split the party: {groucho_leads}"
    )
    assert harpo_leads == {"Groucho": ENTRANCE_ID, "Harpo": ENTRANCE_ID}, (
        f"Harpo-anchored descent split the party: {harpo_leads}"
    )
    assert groucho_leads == harpo_leads, (
        "destination must not depend on the acting-PC order; "
        f"Groucho-leads={groucho_leads} vs Harpo-leads={harpo_leads}"
    )


# ---------------------------------------------------------------------------
# Scope guard (Agency): a peer who is NOT co-located is NOT dragged.
# ---------------------------------------------------------------------------


def test_non_colocated_peer_is_not_dragged(capture_spans):
    """A genuinely split party stays split: a peer standing in a DIFFERENT
    region than the acting PC must not be teleported onto the destination.
    Only PCs co-located with the mover advance together."""
    # Groucho on the seam-owner; Harpo back on the surface camp (a real split).
    snap = _party_snapshot(
        {"Groucho": "the_dropmouth", "Harpo": "ropefoot"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    assert snap.pc_regions["Groucho"] == ENTRANCE_ID, "acting PC crosses"
    assert snap.pc_regions["Harpo"] == "ropefoot", (
        "a non-co-located peer must NOT be dragged across the seam; "
        f"Harpo moved to {snap.pc_regions.get('Harpo')!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 (OTEL): each advanced PC emits its own movement.resolved span.
# ---------------------------------------------------------------------------


def test_party_advance_emits_per_pc_movement_span(capture_spans):
    """OTEL Observability Principle + the per-PC span doctrine: the GM panel
    must see EACH PC's advance independently. A shared descent of two
    co-located PCs emits one ``movement.resolved`` span per advanced PC, each
    carrying the same destination — so the party-advance decision is legible
    (which PCs advanced together, to where)."""
    snap = _party_snapshot(
        {"Groucho": "ropefoot", "Harpo": "ropefoot"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    _run(
        run_movement_dispatch(
            _movement("deeper", "down the rope"),
            snapshot=snap,
            player_name="Groucho",
            additional_player_names=["Harpo"],
            dungeon_store=_StoreWithEntrance(),
            palette=_FakePalette(),
            pack=pack,
        )
    )

    resolved = _resolved_spans(capture_spans)
    pc_names = {(s.attributes or {}).get("pc_name") for s in resolved}
    assert pc_names == {"Groucho", "Harpo"}, (
        "expected one movement.resolved span per advanced PC (per-PC span "
        f"doctrine); got pc_names={pc_names} across {len(resolved)} span(s)"
    )
    to_regions = {(s.attributes or {}).get("to_region") for s in resolved}
    assert to_regions == {ENTRANCE_ID}, (
        f"every advanced PC's span must record the shared destination; got {to_regions}"
    )


# ---------------------------------------------------------------------------
# Wiring (SM-required): the synced advance is reachable through the REAL
# dispatch bank — additional_player_names is threaded by signature-filter,
# not just accepted by a hand-called helper.
# ---------------------------------------------------------------------------


def test_party_advance_wired_through_dispatch_bank(capture_spans):
    """Drive the production ``run_dispatch_bank`` with a movement dispatch and
    the same context shape the pre-narrator pass builds (``player_name`` +
    ``additional_player_names`` + store + palette). Proves the bank threads
    the co-seated party into movement (the signature-filter wiring), so a
    real MP shared-descent turn advances the whole co-located party — not a
    unit helper in isolation."""
    snap = _party_snapshot(
        {"Groucho": "ropefoot", "Harpo": "ropefoot"},
        {"p1": "Groucho", "p2": "Harpo"},
    )
    pack = _pack_with_cartography("beneath_sunden", _hybrid_cartography())

    package = DispatchPackage(
        turn_id="t-158-7-wiring",
        confidence_global=1.0,
        per_player=[
            PlayerDispatch(
                player_id="p1",
                raw_action="we climb down the rope together",
                dispatch=[_movement("deeper", "down the rope")],
            )
        ],
    )

    result = _run(
        run_dispatch_bank(
            package,
            context={
                "snapshot": snap,
                "pack": pack,
                "player_name": "Groucho",
                "additional_player_names": ["Harpo"],
                "dungeon_store": _StoreWithEntrance(),
                "palette": _FakePalette(),
                "lookahead_handle": None,
            },
        )
    )

    assert not result.errors, f"dispatch bank surfaced errors: {result.errors}"
    assert snap.pc_regions["Groucho"] == ENTRANCE_ID, "acting PC must cross via the real bank path"
    assert snap.pc_regions["Harpo"] == ENTRANCE_ID, (
        "the bank must thread additional_player_names into movement so the "
        f"co-located peer advances; Harpo at {snap.pc_regions.get('Harpo')!r}"
    )
