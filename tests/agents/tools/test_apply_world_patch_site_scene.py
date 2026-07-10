"""Single-writer for site scenes — Track B, Task 12 (story 164-6).

The engine owns navigation wherever a scene is engine-driven. Task 12 extends
TWO existing single-writer guards from the region-mode/frontier case to the new
SITE case:

  1. ``apply_world_patch`` — the narrator escape hatch must NOT write
     ``/current_region`` when the acting PC is inside a SITE scene (its region
     is a site-owned node). Today this is denied only for region-mode worlds
     (``apply_world_patch.py:182``); the site scene is a fresh hole.
  2. ``_honors_same_turn_seam_crossing`` (``narration_apply.py:261``) — the
     same-turn seam-crossing clobber guard is keyed to the single global
     ``ENTRANCE_ID``; it must recognise a same-turn ``site.enter`` crossing onto
     a SITE's entrance node too, parameterised per site.

RED: neither extension exists yet. The apply_world_patch site denial does not
fire (the write succeeds), and ``_honors_same_turn_seam_crossing`` returns
``False`` for a site entrance because it compares against the global
``ENTRANCE_ID`` only.

The apply_world_patch tests use a real ``PgSaveRepository`` (via
``pg_store_with``, same as ``test_apply_world_patch.py``) because the tool loads
the session; they skip loudly without a test DB. The narration_apply test uses
content-free duck-typed doubles (house style, ``test_scene_context.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import (
    ToolContext,
    ToolResult,
    ToolResultStatus,
    default_registry,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.world import (
    CartographyConfig,
    NavigationMode,
    Region,
    SiteDecl,
)

_SITE_ID = "gilded_boar"
_SITE_ENTRANCE = f"{_SITE_ID}:entrance"
_ATTACHED_TO = "square"


# ---------------------------------------------------------------------------
# Helpers (mirror test_apply_world_patch.py's real-PG + registered-handler path)
# ---------------------------------------------------------------------------


def _build_snapshot(*, pc_region: str) -> GameSnapshot:
    """A snapshot with Alice seated and her graph region set to ``pc_region``
    (``region_for`` reads ``pc_regions`` — never ``current_region``)."""
    snap = GameSnapshot(
        genre_slug="tea_and_murder",
        world_slug="testworld",
        turn_manager=TurnManager(interaction=1),
        characters=[],
    )
    snap.player_seats = {"p1": "Alice"}
    snap.pc_regions = {"Alice": pc_region}
    return snap


def _store_with(snapshot: GameSnapshot):
    from tests.agents.tools.conftest import pg_store_with

    return pg_store_with(snapshot)


def _site_cartography() -> CartographyConfig:
    """A room_graph-mode world with one plain region and one bounded site
    attached to it — so the region-mode denial does NOT fire and ONLY the new
    site-scene check can deny."""
    return CartographyConfig(
        navigation_mode=NavigationMode.room_graph,
        regions={
            _ATTACHED_TO: Region(
                name="Village Square",
                summary="The square.",
                description="A muddy village square, the Gilded Boar on its north side.",
                adjacent=[],
            )
        },
        sites=[
            SiteDecl(
                site_id=_SITE_ID,
                name="The Gilded Boar",
                archetype="tavern",
                attached_to=_ATTACHED_TO,
                extent="bounded",
            )
        ],
    )


@dataclass
class _FakeWorld:
    cartography: Any


@dataclass
class _FakePack:
    worlds: dict[str, _FakeWorld] = field(default_factory=dict)


def _make_site_scene_ctx(store, *, pc_region: str) -> ToolContext:
    """ToolContext whose world carries the site cartography; the acting PC's
    region is ``pc_region`` (a site node when we want the denial)."""
    world = _FakeWorld(cartography=_site_cartography())
    pack = _FakePack(worlds={"testworld": world})
    return ToolContext(
        world_id="testworld",
        session_id="s-site",
        perspective_pc="Alice",
        turn_number=3,
        repository=store,
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=pack,
    )


async def _call(arguments: dict, ctx: ToolContext) -> ToolResult:
    registered = default_registry._tools["apply_world_patch"]
    args = registered.args_model.model_validate(arguments)
    return await registered.handler(args, ctx)


# ---------------------------------------------------------------------------
# 1. apply_world_patch — /current_region denied inside a site scene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_region_write_denied_in_site_scene() -> None:
    """RED: the narrator escape hatch must not write /current_region while the
    PC is inside a site scene (the engine owns the site's interior graph). The
    rejection is RECOVERABLE (narrator re-plans), not a fatal abort, and the
    denial fires even though the world is room_graph mode (so it is the SITE
    check, not the region-mode check, doing the work)."""
    snap = _build_snapshot(pc_region=_SITE_ENTRANCE)
    store = _store_with(snap)
    ctx = _make_site_scene_ctx(store, pc_region=_SITE_ENTRANCE)

    r = await _call(
        {
            "path": "/current_region",
            "value": "somewhere_else",
            "reason": "narrator tries to move the PC out of the tavern",
        },
        ctx,
    )
    assert r.status is ToolResultStatus.ERROR_RECOVERABLE
    assert r.message is not None
    # The region must NOT have been clobbered.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.snapshot.pc_regions.get("Alice") == _SITE_ENTRANCE


@pytest.mark.asyncio
async def test_current_region_allowed_when_not_in_site_scene() -> None:
    """Control (must stay green): when the PC is on a plain cartography region
    — not a site node — in a room_graph world, the /current_region escape hatch
    stays open. The site-scene denial must be scoped to actual site nodes, not
    blanket-deny every write."""
    snap = _build_snapshot(pc_region=_ATTACHED_TO)
    store = _store_with(snap)
    ctx = _make_site_scene_ctx(store, pc_region=_ATTACHED_TO)

    r = await _call(
        {
            "path": "/current_region",
            "value": "somewhere_else",
            "reason": "ordinary region set in a non-site scene",
        },
        ctx,
    )
    assert r.status is not ToolResultStatus.ERROR_RECOVERABLE
    assert r.status is not ToolResultStatus.ERROR_FATAL


# ---------------------------------------------------------------------------
# 2. narration_apply — same-turn seam-crossing guard is site-aware
# ---------------------------------------------------------------------------


def _snapshot_double_on_site_entrance() -> Any:
    """Duck-typed snapshot: Alice's region is a SITE entrance and there is a
    this-turn region_transitions receipt landing her there (the movement
    receipt for a site.enter crossing)."""
    this_turn = 5
    transition = SimpleNamespace(
        turn=this_turn, pc_name="Alice", to_region=_SITE_ENTRANCE
    )
    return SimpleNamespace(
        region_for=lambda *, perspective=None: _SITE_ENTRANCE,
        turn_manager=SimpleNamespace(interaction=this_turn),
        region_transitions=[transition],
    )


def test_same_turn_site_crossing_is_honored() -> None:
    """RED: a same-turn crossing onto a SITE's entrance node must be honored
    (the clobber declined) exactly as a frontier-entrance crossing is. Today
    the guard compares only against the global ENTRANCE_ID, so a site entrance
    is not recognised and the function returns False."""
    from sidequest.server.narration_apply import _honors_same_turn_seam_crossing

    honored = _honors_same_turn_seam_crossing(
        snapshot=cast(GameSnapshot, _snapshot_double_on_site_entrance()),
        player_name="Alice",
        known_region_id=_ATTACHED_TO,
        is_region_mode_world=True,
        region_cart=_site_cartography(),
    )
    assert honored is True


def test_non_region_mode_world_never_honors() -> None:
    """Control (must stay green): the guard only applies to region-mode worlds;
    a non-region-mode world always returns False regardless of site state."""
    from sidequest.server.narration_apply import _honors_same_turn_seam_crossing

    honored = _honors_same_turn_seam_crossing(
        snapshot=cast(GameSnapshot, _snapshot_double_on_site_entrance()),
        player_name="Alice",
        known_region_id=_ATTACHED_TO,
        is_region_mode_world=False,
        region_cart=_site_cartography(),
    )
    assert honored is False
