"""Track B, Task 6 (Story 164-3): movement dispatch wired to the site resolvers.

The RISKY cutover: ``run_movement_dispatch`` stops resolving Sünden's descent
through the inlined five-rung seam ladder and instead dispatches ``enter_site`` /
``exit_site`` by kind through the ``SiteRegistry`` × the Task-4 resolvers. This
suite is the WIRING PROOF (164-2 Dev finding: the resolvers were additive-ahead-
of-consumer — reachable only via the registry test). It drives the REAL handler
end-to-end and asserts the observable outcome (``resolved_via`` / ``to_region``)
+ the OTEL spans — never a source-text assertion (server CLAUDE.md: no
source-text wiring tests; prove wiring via behavior + spans).

Covers the two 164-2 carryover findings this story owns:
  * #2b — an unresolved enter emits ``site.enter_unresolved`` from the MOVEMENT
          catcher (the ``SeamCrossingError``/no-match catcher owns the fail span).
  * #2c — the ``site.enter`` span carries the player's coarse intent
          (``intent.exit_descriptor``) as ``movement.resolved`` does — the site
          spans omitted it on develop.

RED on ``develop``: the movement dispatch ignores ``action`` today, so an
``enter_site`` dispatch defers (``region_mode_deferred``) / fails and never fires
a ``site.*`` span. Dev wires the branches in GREEN (Task 6).
"""

from __future__ import annotations

import asyncio
import types

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.subsystems.movement import run_movement_dispatch
from sidequest.dungeon.region_graph.model import RegionGraph, RegionNode
from sidequest.dungeon.seed_bootstrap import ENTRANCE_ID
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    SiteDecl,
)
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _enter_dispatch(site_descriptor: str = "") -> SubsystemDispatch:
    """The router's ``enter_site`` shape (Task 5): action + a free-text descriptor,
    no ``direction``/``exit_descriptor`` (that vocabulary is in-scene navigation)."""
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "enter_site", "site_descriptor": site_descriptor},
        idempotency_key="mv-164-3-enter",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


def _exit_dispatch() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="movement",
        params={"action": "exit_site"},
        idempotency_key="mv-164-3-exit",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


class _FrontierLegacyRepo:
    """Models the REAL Sünden frontier store post-migration.

    The frontier site's NODE ids stay the un-namespaced legacy ``entrance`` /
    ``expNNN.rN`` for B1 (storage is ``(session, site_id)``-keyed; node-id
    namespacing is a B4 follow-up). So the loaded graph's entrance is
    ``ENTRANCE_ID`` (``entrance``), NOT the site's namespaced
    ``entrance_node_id`` (``frontier:entrance``). ``resolve_enter_site`` must
    prefer ``graph.entrance_id`` here (164-2 carryover #1) — this double is what
    exercises that fallback through the real dispatch. ``load_map`` accepts the
    site-keyed signature AND the movement dispatch's own bare call."""

    def load_map(self, *, entrance_id: str = ENTRANCE_ID, site_id: str = "frontier") -> RegionGraph:
        g = RegionGraph(entrance_id=ENTRANCE_ID)
        g.add_node(RegionNode(id=ENTRANCE_ID, expansion_id=0, theme="shaft_collar"))
        return g


class _FakePalette:
    def get(self, theme_id: str):
        return types.SimpleNamespace(
            display_name=theme_id,
            narrator=types.SimpleNamespace(register="grave", flavor="cold", motifs=["stone"]),
        )


def _cartography_with_site() -> CartographyConfig:
    """A clean site-only region-mode world: ``the_dropmouth`` owns the frontier
    site, ``ropefoot`` is the adjacent camp. NO ``deep_descent`` route — the site
    IS the descent path now, so on ``develop`` an ``enter_site`` dispatch defers
    cleanly (no legacy seam rung to muddy the RED)."""
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
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="the_dropmouth",
                extent="frontier",
            )
        ],
    )


def _cartography_no_site() -> CartographyConfig:
    """A region-mode world that declares NO frontier site (the misconfiguration
    the shim must fail loud on — Story 164-3 review finding)."""
    return CartographyConfig(
        starting_region="the_dropmouth",
        navigation_mode=NavigationMode.region,
        regions={
            "the_dropmouth": Region(
                name="The Dropmouth", summary="The lip.", description="The shaft mouth."
            ),
        },
        sites=[],
    )


def _cartography_dangling_owner() -> CartographyConfig:
    """The frontier site's ``attached_to`` names a region NOT on the map — so
    resolve_exit_site raises ``dangling_site_owner`` (the exit-failure path)."""
    return CartographyConfig(
        starting_region="the_dropmouth",
        navigation_mode=NavigationMode.region,
        regions={
            "the_dropmouth": Region(
                name="The Dropmouth", summary="The lip.", description="The shaft mouth."
            ),
        },
        sites=[
            SiteDecl(
                site_id="frontier",
                name="The Deep",
                archetype="megadungeon",
                attached_to="nowhere",  # not a region on the map
                extent="frontier",
            )
        ],
    )


def _pack(world_slug: str = "beneath_sunden"):
    world = types.SimpleNamespace(cartography=_cartography_with_site())
    return types.SimpleNamespace(worlds={world_slug: world})


def _pack_cart(cart: CartographyConfig, world_slug: str = "beneath_sunden"):
    world = types.SimpleNamespace(cartography=cart)
    return types.SimpleNamespace(worlds={world_slug: world})


def _snapshot(region: str) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        pc_regions={"Rux": region},
        player_seats={"p1": "Rux"},
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# ENTER — the movement dispatch resolves an enter_site by kind.
# ---------------------------------------------------------------------------


def test_enter_site_dispatch_resolves_via_registry() -> None:
    """``action=enter_site`` from a world node crosses into the site: ``resolved_via``
    is ``site_enter`` and the PC binds onto the site's entrance (the legacy
    ``ENTRANCE_ID`` via the frontier graph.entrance_id fallback)."""
    snap = _snapshot("the_dropmouth")
    out = _run(
        run_movement_dispatch(
            _enter_dispatch("the deep"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyRepo(),
            palette=_FakePalette(),
            pack=_pack(),
        )
    )
    assert out.data.get("resolved_via") == "site_enter", out.data
    assert out.data.get("to_region") == ENTRANCE_ID, out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID


def test_enter_site_stamps_intent_on_span(otel_capture) -> None:
    """Carryover #2c: the ``site.enter`` span carries the player's coarse intent
    (``intent.exit_descriptor``) — threaded dispatch → resolver → span, exactly as
    ``movement.resolved`` stamps it. On develop the site path never fires, so no
    ``site.enter`` span exists at all."""
    snap = _snapshot("the_dropmouth")
    _run(
        run_movement_dispatch(
            _enter_dispatch("down into the deep"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyRepo(),
            palette=_FakePalette(),
            pack=_pack(),
        )
    )
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "site.enter"]
    assert len(spans) == 1, f"exactly one site.enter span expected, got {[s.name for s in spans]}"
    attrs = spans[0].attributes or {}
    assert attrs.get("intent.exit_descriptor") == "down into the deep", attrs


def test_unmatched_enter_descriptor_emits_unresolved_span(otel_capture) -> None:
    """Carryover #2b: a descriptor that matches no enterable site fires
    ``site.enter_unresolved`` from the MOVEMENT catcher (reason ``no_matching_site``
    + the descriptor) and leaves the PC where they stood — a fail-loud the GM panel
    can see, never a silent swallow."""
    snap = _snapshot("the_dropmouth")
    out = _run(
        run_movement_dispatch(
            _enter_dispatch("the astral observatory"),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyRepo(),
            palette=_FakePalette(),
            pack=_pack(),
        )
    )
    # The PC did not cross — no site_enter resolution.
    assert out.data.get("resolved_via") != "site_enter", out.data
    assert snap.pc_regions["Rux"] == "the_dropmouth", "an unmatched enter must not move the PC"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "site.enter_unresolved"]
    assert len(spans) == 1, "an unmatched enter MUST emit site.enter_unresolved"
    attrs = spans[0].attributes or {}
    assert attrs.get("reason") == "no_matching_site", attrs
    assert attrs.get("descriptor") == "the astral observatory", attrs


# ---------------------------------------------------------------------------
# EXIT — the movement dispatch resolves an exit_site by kind (legacy-node shim).
# ---------------------------------------------------------------------------


def test_exit_site_dispatch_from_legacy_frontier_node() -> None:
    """A PC standing on the legacy frontier node (``entrance`` — un-namespaced, so
    ``site_owning_node`` cannot match it) exits via the ``is_procedural_region_id``
    shim: ``action=exit_site`` binds back to the site's ``attached_to`` surface
    region with ``resolved_via=site_exit``."""
    snap = _snapshot(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _exit_dispatch(),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyRepo(),
            palette=_FakePalette(),
            pack=_pack(),
        )
    )
    assert out.data.get("resolved_via") == "site_exit", out.data
    assert out.data.get("to_region") == "the_dropmouth", out.data
    assert snap.pc_regions["Rux"] == "the_dropmouth"


def test_exit_site_undeclared_frontier_fails_loud(otel_capture, caplog) -> None:
    """Review finding (HIGH, No Silent Fallbacks): a PC on a legacy procedural node
    in a region-mode world that declares NO frontier site is a cartography
    misconfiguration. An ``exit_site`` must FAIL LOUD — a clear
    ``frontier_site_undeclared`` reason + a ``site.exit_unresolved`` span + a
    warning log — NOT silently fall through to the §Q1 navigator's misleading
    ``no_candidate_edges``. The PC does not move."""
    snap = _snapshot(ENTRANCE_ID)  # a legacy procedural node
    with caplog.at_level("WARNING", logger="sidequest.agents.subsystems.movement"):
        out = _run(
            run_movement_dispatch(
                _exit_dispatch(),
                snapshot=snap,
                player_name="Rux",
                dungeon_store=_FrontierLegacyRepo(),
                palette=_FakePalette(),
                pack=_pack_cart(_cartography_no_site()),
            )
        )
    assert out.data.get("error") == "frontier_site_undeclared", out.data
    assert out.data.get("resolved_via") != "site_exit", out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID, "a misconfigured exit must not move the PC"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "site.exit_unresolved"]
    assert len(spans) == 1, "an undeclared-site exit MUST emit site.exit_unresolved (fail loud)"
    assert (spans[0].attributes or {}).get("reason") == "frontier_site_undeclared"
    assert any("frontier_site_undeclared" in r.getMessage() for r in caplog.records), (
        "the missing frontier-site config must surface as a loud warning, not a silent degrade"
    )


def test_exit_site_failure_emits_site_exit_unresolved_span(otel_capture) -> None:
    """Review finding (MEDIUM, OTEL parity): an ``exit_site`` whose resolver raises
    (here ``dangling_site_owner`` — the site's ``attached_to`` isn't on the map)
    must emit a ``site.exit_unresolved`` span from the movement catcher, mirroring
    the enter path, so the GM panel's ``sites`` component is not blind to exit
    failures. The PC does not move."""
    snap = _snapshot(ENTRANCE_ID)
    out = _run(
        run_movement_dispatch(
            _exit_dispatch(),
            snapshot=snap,
            player_name="Rux",
            dungeon_store=_FrontierLegacyRepo(),
            palette=_FakePalette(),
            pack=_pack_cart(_cartography_dangling_owner()),
        )
    )
    assert out.data.get("error") == "dangling_site_owner", out.data
    assert snap.pc_regions["Rux"] == ENTRANCE_ID, "a failed exit must not move the PC"

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "site.exit_unresolved"]
    assert len(spans) == 1, "an exit_site resolver failure MUST emit site.exit_unresolved"
    assert (spans[0].attributes or {}).get("reason") == "dangling_site_owner"
