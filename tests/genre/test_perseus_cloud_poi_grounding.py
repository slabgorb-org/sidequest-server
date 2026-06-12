"""Story 71-25 — perseus_cloud location grounding (AC1).

The ``perseus_cloud`` world names points of interest (New Kowloon, the
churnworld district of the ``yula`` region) in *prose lore* but does not
declare them as typed ``LocationEntity`` rows. With an empty
``region.entities`` manifest, the narrator improvises geography every
turn and ADR-109's resolve-location-entity contract has nothing
authoritative to anchor to — the Diamonds-and-Coal failure mode where a
durable place is treated as disposable coal.

This suite is the AC1 proof: the world's POIs (New Kowloon specifically)
are declared as typed ``entities`` under the correct region in
``cartography.yaml`` and the **real** production consumption path
(``_authored_entities_for`` over the loaded pack) surfaces them.

These tests intentionally bind to live content (``space_opera`` /
``perseus_cloud``) — the story is a content-grounding verification, so
pointing at the real pack is the requirement, not an anti-pattern. The
region that hosts New Kowloon is ``yula`` (read live from
``cartography.yaml``; the churnworld region whose summary names the neon
superblocks of New Kowloon).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.agents.narrator_perception_filter import NarratorPerceptionFilter
from sidequest.agents.tool_registry import ToolContext
from sidequest.agents.tools.resolve_location_entity import _authored_entities_for
from sidequest.game.location_resolver import _normalize
from sidequest.genre.loader import load_genre_pack
from sidequest.protocol.models import LocationEntity

_WORLD = "perseus_cloud"
_REGION = "yula"  # churnworld region hosting New Kowloon (cartography.yaml)
_NEW_KOWLOON = "new kowloon"  # _normalize("New Kowloon")


@pytest.fixture(scope="module")
def perseus_pack(content_dir: Path):
    """Load the real space_opera pack once for the module."""
    return load_genre_pack(content_dir / "genre_packs" / "space_opera")


@pytest.fixture
def yula_entities(perseus_pack) -> list[LocationEntity]:
    region = perseus_pack.worlds[_WORLD].cartography.regions[_REGION]
    return list(region.entities)


def _build_ctx_with_real_pack(perseus_pack) -> ToolContext:
    """A ToolContext carrying the *real* loaded pack — the production
    consumption path ``_authored_entities_for`` walks
    ``ctx.genre_pack.worlds[ctx.world_id].cartography.regions[region_id]``.
    No store is touched by ``_authored_entities_for`` so a MagicMock
    repository is sufficient (and keeps this test PG-free)."""
    return ToolContext(
        world_id=_WORLD,
        session_id="test-71-25",
        perspective_pc=None,
        turn_number=1,
        repository=MagicMock(),
        otel_span=MagicMock(),
        perception_filter=NarratorPerceptionFilter(),
        genre_pack=perseus_pack,
    )


# ---------------------------------------------------------------------------
# AC1 — declared world POIs appear as typed snapshot entities
# ---------------------------------------------------------------------------


def test_yula_region_declares_typed_entities(yula_entities: list[LocationEntity]) -> None:
    """The churnworld region must expose a non-empty typed ``entities``
    manifest. RED today: ``yula.entities == []`` (only legacy untyped
    ``landmarks`` are present)."""
    assert yula_entities, (
        "yula region declares no typed entities — POIs are still "
        "prose-only coal, not grounded diamonds"
    )


def test_declared_entities_are_valid_location_entity_rows(
    yula_entities: list[LocationEntity],
) -> None:
    """Every declared row parses as a ``LocationEntity`` (the model is
    ``extra='forbid'`` — a malformed declaration fails load loudly rather
    than silently dropping). Ids are unique and non-blank."""
    assert all(isinstance(e, LocationEntity) for e in yula_entities)
    ids = [e.id for e in yula_entities]
    assert all(i.strip() for i in ids), "every entity id must be non-blank"
    assert len(ids) == len(set(ids)), f"duplicate entity ids in yula: {ids}"


def test_new_kowloon_is_declared_as_a_typed_entity(
    yula_entities: list[LocationEntity],
) -> None:
    """New Kowloon — the named acceptance target — must be one of the
    declared entities, matchable on the resolver's normalized label
    (article-stripped, lowercased)."""
    labels = {_normalize(e.label) for e in yula_entities}
    assert _NEW_KOWLOON in labels, f"New Kowloon not declared in yula; have labels {sorted(labels)}"


def test_declared_entities_satisfy_tier_binding_invariant(
    yula_entities: list[LocationEntity],
) -> None:
    """Mirror the ``pf validate locations`` well-formedness invariant
    (a hard error there): ``real_object`` MUST carry a binding;
    ``flavor_only`` MUST NOT. Holds regardless of which tier the author
    chose for New Kowloon."""
    for e in yula_entities:
        if e.tier == "real_object":
            assert e.binding is not None, f"entity {e.id!r} is real_object but has no binding"
        if e.tier == "flavor_only":
            assert e.binding is None, f"entity {e.id!r} is flavor_only but carries a binding"


# ---------------------------------------------------------------------------
# AC1 — production consumption path surfaces the declared POIs
# ---------------------------------------------------------------------------


def test_authored_entities_for_surfaces_yula_pois(perseus_pack) -> None:
    """Wiring proof (CLAUDE.md: every suite needs a wiring test). The real
    production helper ``_authored_entities_for`` — given a ToolContext
    carrying the *loaded* pack — must return a non-empty list (not None)
    for the yula region. ``None`` here would surface to the narrator as
    NOT_FOUND, leaving New Kowloon ungrounded."""
    ctx = _build_ctx_with_real_pack(perseus_pack)
    resolved = _authored_entities_for(ctx, _REGION)
    assert resolved is not None, (
        "_authored_entities_for returned None — the consumption path cannot see yula's manifest"
    )
    assert resolved, "_authored_entities_for returned an empty manifest for yula"
    labels = {_normalize(e.label) for e in resolved}
    assert _NEW_KOWLOON in labels


def test_authored_entities_for_unknown_region_returns_none(perseus_pack) -> None:
    """No-silent-fallback guard: an undeclared region id must yield None
    (surfaced as NOT_FOUND), never an empty manifest that would let a
    player_initiated mint land in a region that does not exist."""
    ctx = _build_ctx_with_real_pack(perseus_pack)
    assert _authored_entities_for(ctx, "no_such_region_xyz") is None
