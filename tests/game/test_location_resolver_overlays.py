"""Resolver honours encounter overlays in the effective manifest (Story 54-7)."""

from __future__ import annotations

import uuid

import pytest

from sidequest.game import db_pool
from sidequest.game.location_resolver import _build_effective_manifest, resolve
from sidequest.game.pg import sessions
from sidequest.game.pg.promotions import PgPromotionStore
from sidequest.protocol.models import (
    EncounterLocationOverlay,
    LocationEntity,
    LocationEntityBinding,
)


def _authored() -> list[LocationEntity]:
    return [
        LocationEntity(
            id="bar",
            label="the bar",
            tier="real_object",
            binding=LocationEntityBinding(kind="location_feature", ref="glenross_arms_bar"),
        ),
        LocationEntity(id="cobwebs", label="cobwebs", tier="flavor_only"),
    ]


@pytest.fixture
def store(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    pool = db_pool.get_pool()
    slug = f"locres_ov_{uuid.uuid4().hex[:8]}"
    sid = sessions.ensure_session(pool, slug=slug, mode="solo", genre_slug="g", world_slug="w")
    yield PgPromotionStore(pool, session_id=sid)
    db_pool.close_pool()


def test_build_effective_manifest_accepts_empty_overlays_default():
    """Backward-compatible call signature: no overlays kwarg = old behavior."""
    out = _build_effective_manifest(authored=_authored(), promotions=[])
    ids = [e.id for e, _ in out]
    assert ids == ["bar", "cobwebs"]


def test_overlay_entity_delta_appends_to_manifest():
    overlay = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(
                id="overturned_table",
                label="an overturned table",
                tier="yes_and",
            ),
        ],
    )
    out = _build_effective_manifest(authored=_authored(), promotions=[], overlays=[overlay])
    ids = [e.id for e, _ in out]
    assert ids == ["bar", "cobwebs", "overturned_table"]


def test_overlay_entities_tagged_not_from_promotion():
    """Overlay entities are encounter-scoped, never promotions — flag stays False."""
    overlay = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(
                id="overturned_table",
                label="an overturned table",
                tier="yes_and",
            ),
        ],
    )
    out = _build_effective_manifest(authored=_authored(), promotions=[], overlays=[overlay])
    by_id = {e.id: from_promo for e, from_promo in out}
    assert by_id["overturned_table"] is False


def test_multiple_overlays_concatenated_in_arrival_order():
    overlay_a = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(id="a", label="a", tier="yes_and"),
        ],
    )
    overlay_b = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(id="b", label="b", tier="yes_and"),
        ],
    )
    out = _build_effective_manifest(
        authored=_authored(), promotions=[], overlays=[overlay_a, overlay_b]
    )
    # base ("bar", "cobwebs") + overlay_a ("a") + overlay_b ("b"), in that order.
    assert [e.id for e, _ in out] == ["bar", "cobwebs", "a", "b"]


def test_overlay_entity_matches_via_resolver(store):
    """Resolve a label that only exists in the overlay — must resolve."""
    overlay = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(
                id="overturned_table",
                label="an overturned table",
                tier="yes_and",
            ),
        ],
    )
    res = resolve(
        store=store,
        region_id="the_glenross_arms",
        authored_entities=_authored(),
        label="the overturned table",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=5,
        overlays=[overlay],
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "overturned_table"
    assert res.mode_outcome == "matched"


def test_overlay_entity_does_not_persist_to_promotions_table(store):
    """Overlay entities are encounter-scoped, never written to durable storage."""
    overlay = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(
                id="overturned_table",
                label="an overturned table",
                tier="yes_and",
            ),
        ],
    )
    resolve(
        store=store,
        region_id="the_glenross_arms",
        authored_entities=_authored(),
        label="the overturned table",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=5,
        overlays=[overlay],
    )
    rows = store.list_location_promotions(region_id="the_glenross_arms")
    assert rows == []


def test_proactive_miss_when_label_not_in_authored_promotion_or_overlay(store):
    overlay = EncounterLocationOverlay(
        bound_room_id="the_glenross_arms",
        entity_delta=[
            LocationEntity(id="a", label="a", tier="yes_and"),
        ],
    )
    res = resolve(
        store=store,
        region_id="the_glenross_arms",
        authored_entities=_authored(),
        label="the dragon",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
        overlays=[overlay],
    )
    assert res.resolved is False
    assert res.mode_outcome == "no_match"
