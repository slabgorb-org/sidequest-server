"""Cartography region projection — the frozen-Location-panel fix.

Playtest 2026-05-21 (tea_and_murder/glenross): the Location panel froze on
the chargen starting region while the narrator roamed in prose. Root cause
(Architect, Houlihan): procedural region-mode (beneath_sunden) gets a
``RegionProjection`` → "YOU ARE HERE" prompt section + MOVEMENT RULE that
makes the narrator emit ``current_region``; cartography region-mode worlds
got ``region_projection=None`` (``applies_to`` hard-gated to the dungeon),
so the narrator was never told the region ids and never emitted
``current_region`` — the per-turn LOCATION_DESCRIPTION emit branch never
re-fired.

These tests cover the reuse-first fix: build an equivalent RegionProjection
from ``cartography.yaml`` and feed it through the SAME render, with the
dungeon-only lethality directive gated off.

Fixtures, not live packs (project memory: no content-coupled unit tests).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region


def _region(name: str, summary: str, adjacent: list[str]) -> Region:
    return Region(name=name, summary=summary, description=summary, adjacent=adjacent)


def _glenrossish_world() -> SimpleNamespace:
    """A synthetic region-mode world with three connected regions."""
    regions = {
        "the_glenross_arms": _region(
            "The Glenross Arms",
            "The village inn, the fire going, the publican wiping the bar.",
            ["the_bridge", "the_post_office"],
        ),
        "the_bridge": _region(
            "The Bridge",
            "South parapet over the Allt Ross; chalk tally on the stone.",
            ["the_glenross_arms", "the_railway_halt"],
        ),
        "the_railway_halt": _region(
            "Glenross Halt",
            "One platform, a waiting room, the afternoon train from Inverness.",
            ["the_bridge"],
        ),
    }
    cart = CartographyConfig(navigation_mode=NavigationMode.region, regions=regions)
    return SimpleNamespace(cartography=cart)


# ---------------------------------------------------------------------------
# project_cartography_region — pure builder
# ---------------------------------------------------------------------------


class TestProjectCartographyRegion:
    def test_builds_projection_from_current_region(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        proj = project_cartography_region(_glenrossish_world(), "the_glenross_arms")
        assert proj is not None
        assert proj.region_id == "the_glenross_arms"
        # Display label is the region name; flavor is the region summary.
        assert proj.theme_display == "The Glenross Arms"
        assert "publican" in proj.flavor
        # Dungeon-only fields are inert for cartography.
        assert proj.is_dungeon is False
        assert proj.register == ""
        assert proj.motifs == []
        assert proj.depth_score is None

    def test_exits_are_the_adjacent_region_ids(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        proj = project_cartography_region(_glenrossish_world(), "the_bridge")
        assert proj is not None
        exit_ids = {e.to_region_id for e in proj.exits}
        assert exit_ids == {"the_glenross_arms", "the_railway_halt"}
        # Cartography exits are not hidden and carry a generic move kind.
        assert all(not e.hidden for e in proj.exits)
        assert all(e.kind for e in proj.exits)

    def test_returns_none_when_region_not_in_map(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        # A phantom the narrator improvised — never silently invented into
        # a projection (No Silent Fallbacks).
        assert project_cartography_region(_glenrossish_world(), "atlantis") is None

    def test_returns_none_on_blank_region(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        assert project_cartography_region(_glenrossish_world(), "") is None

    def test_returns_none_when_not_region_mode(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        cart = CartographyConfig(
            navigation_mode=NavigationMode.room_graph,
            regions={"a": _region("A", "s", [])},
        )
        world = SimpleNamespace(cartography=cart)
        assert project_cartography_region(world, "a") is None

    def test_returns_none_when_no_cartography(self) -> None:
        from sidequest.server.cartography_region_projection import (
            project_cartography_region,
        )

        assert project_cartography_region(SimpleNamespace(cartography=None), "x") is None
        assert project_cartography_region(None, "x") is None


# ---------------------------------------------------------------------------
# Render gating — the dungeon lethality directive must not reach a cosy world
# ---------------------------------------------------------------------------


async def _render_section(region_projection: Any) -> str:
    """Drive the real Orchestrator render path and return the prompt text."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext

    class _CannedClient:
        async def send(self, prompt: str, **_: Any) -> Any:
            from sidequest.agents.claude_client import ClaudeResponse

            return ClaudeResponse(text="ok", duration_ms=0)

    orch = Orchestrator(client=_CannedClient())
    ctx = TurnContext(
        character_name="Neil",
        genre="tea_and_murder",
        turn_number=2,
        region_projection=region_projection,
    )
    prompt_text, _registry = await orch.build_narrator_prompt("look around", ctx)
    return prompt_text


def _cartography_projection() -> Any:
    from sidequest.dungeon.region_projection import RegionExit, RegionProjection

    return RegionProjection(
        region_id="the_bridge",
        theme_id="",
        theme_display="The Bridge",
        register="",
        flavor="South parapet over the Allt Ross.",
        motifs=[],
        depth_score=None,
        exits=[
            RegionExit(to_region_id="the_glenross_arms", kind="path"),
            RegionExit(to_region_id="the_railway_halt", kind="path"),
        ],
        is_dungeon=False,
    )


class TestRenderGating:
    async def test_cartography_projection_renders_you_are_here_and_movement_rule(
        self,
    ) -> None:
        text = await _render_section(_cartography_projection())
        assert "YOU ARE HERE" in text
        assert "The Bridge" in text
        assert "MOVEMENT RULE" in text
        # Constrained move vocabulary: the real adjacent ids reach the prompt.
        assert "the_railway_halt" in text

    async def test_cartography_projection_omits_dungeon_lethality_directive(
        self,
    ) -> None:
        text = await _render_section(_cartography_projection())
        # The cosy-mystery narrator must NEVER be told it's in a lethal
        # megadungeon. These are the dungeon-only directive's signatures.
        assert "DUNGEON IS ALIVE" not in text
        assert "Moria" not in text

    async def test_empty_register_renders_no_dangling_register_line(self) -> None:
        text = await _render_section(_cartography_projection())
        assert "Register:" not in text

    async def test_dungeon_projection_keeps_lethality_directive(self) -> None:
        from sidequest.dungeon.region_projection import RegionExit, RegionProjection

        dungeon_proj = RegionProjection(
            region_id="entrance",
            theme_id="threshold",
            theme_display="The Threshold",
            register="grave, hushed",
            flavor="Cold stone and the smell of old water.",
            motifs=["dripping", "dark"],
            depth_score=0.3,
            exits=[RegionExit(to_region_id="the_dropmouth", kind="stairs")],
            is_dungeon=True,
        )
        text = await _render_section(dungeon_proj)
        assert "DUNGEON IS ALIVE" in text
        assert "Register:" in text


# ---------------------------------------------------------------------------
# Wiring — _project_current_region returns a cartography projection (the seam)
# ---------------------------------------------------------------------------


def _otel_in_memory() -> tuple[Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


class _FakeSessionData:
    def __init__(self, world_obj: Any, *, genre: str, world: str) -> None:
        self.genre_slug = genre
        self.world_slug = world
        self.player_id = "p1"
        self.genre_pack = SimpleNamespace(worlds={world: world_obj})


def test_project_current_region_returns_cartography_projection() -> None:
    """Wiring: a region-mode non-dungeon world flows through
    _project_current_region → cartography projection + observable span,
    instead of the dead outcome=no_dungeon path."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.game.session import GameSnapshot
    from sidequest.server.session_helpers import _project_current_region
    from sidequest.telemetry.spans.dungeon_region_projection import (
        SPAN_DUNGEON_REGION_PROJECTION,
    )

    exporter, real_tracer = _otel_in_memory()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: real_tracer  # type: ignore[method-assign]
    try:
        snap = GameSnapshot(genre_slug="tea_and_murder", world_slug="glenross")
        snap.current_region = "the_bridge"
        sd = _FakeSessionData(_glenrossish_world(), genre="tea_and_murder", world="glenross")

        proj = _project_current_region(sd, snap)

        assert proj is not None, (
            "cartography region-mode world produced no projection — the "
            "narrator gets no region ids and the Location panel freezes"
        )
        assert proj.region_id == "the_bridge"
        assert proj.is_dungeon is False

        spans = [
            s for s in exporter.get_finished_spans() if s.name == SPAN_DUNGEON_REGION_PROJECTION
        ]
        outcomes = {(s.attributes or {}).get("outcome") for s in spans}
        assert "cartography_projection" in outcomes, (
            "GM panel cannot distinguish 'projected from cartography' from "
            f"'no dungeon' — got outcomes {outcomes}"
        )
    finally:
        _spans_module.tracer = original  # type: ignore[method-assign]
