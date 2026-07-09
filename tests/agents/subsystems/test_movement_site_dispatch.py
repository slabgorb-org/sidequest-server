"""Track B, Task 6 (Story 164-3): the movement dispatch resolves SITE targets.

RED: ``run_movement_dispatch`` ignores ``params['action']`` today — descent is
reachable only through the five-rung ``direction``-keyed seam ladder
(``movement.py``:386-538). Task 6 REPLACES that ladder with SiteRegistry
descriptor-resolution × the enter_site/exit_site resolvers: a movement dispatch
carrying ``action='enter_site'`` / ``'exit_site'`` crosses via the registry,
``resolved_via`` becomes ``site_enter`` / ``site_exit``, and the DESTINATION
(``to_region``) is UNCHANGED from the ladder it replaces (ENTRANCE_ID on enter,
the owning region on exit).

These are the honest-RED contract for the risky cutover. The 164-2
characterization guard (``test_movement_sunden_characterization.py``) stays green
on ``develop`` and is retargeted by Dev in GREEN (it locks the SAME ``to_region``
destinations these tests assert).

Pure CODE test: a synthetic beneath_sünden-shaped region-mode cartography that
DECLARES a ``frontier`` site, plus a legacy-shaped store double whose graph is
keyed on the bare ``ENTRANCE_ID`` — the frontier-legacy case the entrance
fallback exists for (the site declares ``frontier:entrance`` but the bootstrapped
Sünden store uses ``entrance``). The real pack ``sites:`` block + the crossing
end-to-end are the content change + the ``sunden_descend_trace`` scenario's job.
"""

from __future__ import annotations

import asyncio
import types

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import sidequest.telemetry.spans as spans_module
from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    Route,
    SiteDecl,
)
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

# ---------------------------------------------------------------------------
# Doubles (mirror test_movement_sunden_characterization.py — single-PC form)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _site_move(action: str, site_descriptor: str = "") -> SubsystemDispatch:
    """A movement dispatch that names a SITE target (the shape Task 5's router
    emits), NOT the in-scene ``direction`` vocabulary."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": action, "site_descriptor": site_descriptor},
        idempotency_key="mv-164-3-site",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _LegacyFrontierStore:
    """The bootstrapped Sünden frontier store: its graph is keyed on the bare
    ``ENTRANCE_ID`` (``entrance``), NOT the site-namespaced ``frontier:entrance``
    the descriptor declares — the exact legacy shape the entrance fallback exists
    for. ``load_map`` accepts the ``site_id`` the resolver threads AND the
    site-less ``entrance_id``-only call the ``_in_dungeon`` probe makes."""

    def load_map(self, *, entrance_id: str, site_id: str = "frontier") -> RegionGraph:
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
        return g


class _FakePalette:
    """Duck-typed ThemePalette — the seam crossing never reaches projection, but
    the signature requires a non-None palette."""

    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _sunden_cart() -> CartographyConfig:
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
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="the_dropmouth",
                extent="frontier",
            ),
        ],
    )


def _pack_with_cartography(world_slug: str, cartography: CartographyConfig):
    world = types.SimpleNamespace(cartography=cartography)
    return types.SimpleNamespace(worlds={world_slug: world})


def _snapshot(region: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


def _install_recording_provider(monkeypatch) -> None:
    """Install an in-memory RECORDING provider so the site spans opened inside the
    dispatch carry ``.attributes`` — the sink ``_mirror`` only publishes for a
    recording span (matches ``tests/telemetry/test_site_spans_to_sink.py``)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    local = provider.get_tracer("test-movement-site-dispatch")
    monkeypatch.setattr(spans_module, "tracer", lambda: local)


def _capture_site_publish(monkeypatch) -> list[tuple]:
    """Capture every ``publish_event`` the site span module makes (the
    turn_telemetry sink feed)."""
    import sidequest.telemetry.spans.site as site_spans

    calls: list[tuple] = []
    monkeypatch.setattr(
        site_spans,
        "publish_event",
        lambda event_type, fields, **kw: calls.append((event_type, fields, kw)),
    )
    return calls


# ---------------------------------------------------------------------------
# enter_site — action-keyed crossing via the SiteRegistry (replaces the ladder).
# ---------------------------------------------------------------------------


def test_enter_site_from_owner_region_resolves_site_enter() -> None:
    """``action=enter_site`` from the seam-owner region crosses into the frontier via
    the registry: ``resolved_via == 'site_enter'`` and the destination is the
    graph's real entrance (the ladder's ENTRANCE_ID, preserved by the entrance
    fallback for the legacy frontier)."""
    snap = _snapshot("the_dropmouth")
    out = _run(
        run_movement_dispatch(
            _site_move("enter_site", "the deep"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_LegacyFrontierStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _sunden_cart()),
        )
    )
    assert out.data.get("resolved_via") == "site_enter", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


def test_enter_site_from_adjacent_camp_resolves_site_enter() -> None:
    """The one-action reach survives the cutover: ``action=enter_site`` from
    ``ropefoot`` (adjacent to the owner) still crosses — ``sites_for_node`` includes
    adjacent-owned sites, so the camp can descend in one deliberate action."""
    snap = _snapshot("ropefoot")
    out = _run(
        run_movement_dispatch(
            _site_move("enter_site", "down the rope"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_LegacyFrontierStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _sunden_cart()),
        )
    )
    assert out.data.get("resolved_via") == "site_enter", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


# ---------------------------------------------------------------------------
# exit_site — action-keyed reverse crossing back to the owning region.
# ---------------------------------------------------------------------------


def test_exit_site_from_inside_resolves_site_exit() -> None:
    """``action=exit_site`` while standing on the frontier's (legacy) entrance node
    binds back to the site's owning region (``the_dropmouth``):
    ``resolved_via == 'site_exit'``. Membership of the un-namespaced ``entrance``
    node is detected via the legacy ``is_procedural_region_id`` shim."""
    snap = _snapshot(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _site_move("exit_site"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_LegacyFrontierStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _sunden_cart()),
        )
    )
    assert out.data.get("resolved_via") == "site_exit", out.data
    assert out.data.get("to_region") == "the_dropmouth", out.data
    assert snap.pc_regions["Rux"] == "the_dropmouth"


# ---------------------------------------------------------------------------
# Unresolved enter — fail loud to the GM panel, never a silent no-op.
# ---------------------------------------------------------------------------


def test_enter_site_unmatched_descriptor_defers_and_mirrors_unresolved_span(monkeypatch) -> None:
    """``action=enter_site`` with a descriptor matching no enterable site defers to
    narration AND mirrors ``site.enter_unresolved`` to turn_telemetry (the GM-panel
    lie detector, OTEL Observability Principle) — never a silent no-op. The PC does
    NOT move.

    Uses ``ropefoot`` (adjacent, does not own the inert route) so the ONLY reason
    this fails on ``develop`` is the missing span: today's ladder ignores ``action``
    and simply defers here without emitting anything."""
    calls = _capture_site_publish(monkeypatch)
    _install_recording_provider(monkeypatch)
    snap = _snapshot("ropefoot")
    out = _run(
        run_movement_dispatch(
            _site_move("enter_site", "the silver moon"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_LegacyFrontierStore(),
            palette=_FakePalette(),
            pack=_pack_with_cartography("beneath_sunden", _sunden_cart()),
        )
    )
    assert snap.pc_regions["Rux"] == "ropefoot", (
        "an unmatched site must not move the PC",
        out.data,
    )
    ops = [fields.get("op") for (_event, fields, _kw) in calls]
    assert "site.enter_unresolved" in ops, (
        f"site.enter_unresolved MUST reach turn_telemetry via publish_event; got ops={ops}"
    )
