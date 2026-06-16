"""resolve_location_entity core logic (Story 54-6 / ADR-109).

Pins the load-bearing resolver invariants:

* **Two-mode split** (ADR-109 §5.3) — ``narrator_proactive`` miss returns
  ``resolved=False`` with no mutation; ``player_initiated`` miss mints a
  new ``yes_and`` entity in ``location_promotions``.
* **flavor_only → yes_and promotion** on mechanical engagement
  (Diamonds-and-Coal), regardless of mode.
* **Authored-YAML immutability** — the authored entity list passed to
  ``resolve()`` is never mutated; promotions layer on top via
  ``model_copy``.
* **Label normalisation** — leading articles stripped, case-insensitive,
  applied to both authored and incoming labels.
* **Effective manifest** — promotions persisted across reloads are
  visible on subsequent ``resolve`` calls and surface as
  ``from_promotion=True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.game.location_resolver import resolve
from sidequest.game.persistence import SqliteStore
from sidequest.protocol.models import (
    LocationEntity,
    LocationEntityBinding,
    LocationEntityResolution,
)


def _entities() -> list[LocationEntity]:
    return [
        LocationEntity(
            id="bar",
            label="the bar",
            tier="real_object",
            binding=LocationEntityBinding(kind="location_feature", ref="glenross_arms_bar"),
        ),
        LocationEntity(id="cobwebs", label="cobwebs", tier="flavor_only"),
        LocationEntity(id="snug", label="the snug at the end", tier="yes_and"),
    ]


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "save.db")


# ---------------------------------------------------------------------------
# AC-2 / AC-3: two-mode miss behaviour
# ---------------------------------------------------------------------------


def test_proactive_match_real_object_returns_resolved(store: SqliteStore) -> None:
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the bar",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=1,
    )
    assert isinstance(res, LocationEntityResolution)
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "bar"
    assert res.mode_outcome == "matched"
    assert res.region_id == "the_glenross_arms"
    # No row written — a match on a real_object never mutates.
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


def test_proactive_miss_returns_no_match_and_does_not_mint(
    store: SqliteStore,
) -> None:
    """AC-2: narrator_proactive + miss = no-commit. The lie-detector path.

    The narrator's pending mechanical action does not commit (the tool layer
    surfaces this as NOT_FOUND); critically the resolver writes nothing."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the dragon",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is False
    assert res.entity is None
    assert res.mode_outcome == "no_match"
    assert res.region_id == "the_glenross_arms"
    assert res.from_promotion is False
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


def test_proactive_mechanical_miss_still_does_not_mint(
    store: SqliteStore,
) -> None:
    """The contract-violation behaviour is the same whether the narrator
    miss was a mention or mechanical claim — both must NOT mint."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the dragon",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=1,
    )
    assert res.resolved is False
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


def test_player_initiated_miss_mints_yes_and_entity(store: SqliteStore) -> None:
    """AC-3: player_initiated + miss = canonization."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the antique sextant",
        mode="player_initiated",
        engagement_kind="mention",
        turn_number=7,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.tier == "yes_and"
    assert res.entity.provenance == "yes_and_minted"
    assert res.mode_outcome == "minted"
    assert res.from_promotion is True
    rows = store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1
    assert rows[0].label == "the antique sextant"
    assert rows[0].provenance == "yes_and_minted"
    assert rows[0].new_tier == "yes_and"
    assert rows[0].promoted_at_turn == 7


def test_player_initiated_match_does_not_mint(store: SqliteStore) -> None:
    """A player-initiated label that matches an authored entity should NOT
    mint a duplicate row — match is match."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the bar",
        mode="player_initiated",
        engagement_kind="mechanical",
        turn_number=4,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "bar"
    assert res.mode_outcome == "matched"
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


# ---------------------------------------------------------------------------
# AC-4 / AC-5: flavor_only promotion vs. mention
# ---------------------------------------------------------------------------


def test_flavor_only_engaged_mechanically_promotes(store: SqliteStore) -> None:
    """AC-4: flavor_only entity engaged mechanically promotes to yes_and."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="cobwebs",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=11,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.tier == "yes_and"
    assert res.entity.provenance == "yes_and_promoted"
    assert res.mode_outcome == "promoted"
    assert res.from_promotion is True
    rows = store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1
    assert rows[0].entity_id == "cobwebs"
    assert rows[0].provenance == "yes_and_promoted"
    assert rows[0].new_tier == "yes_and"
    assert rows[0].promoted_at_turn == 11


def test_flavor_only_mechanical_promotes_in_player_initiated_mode_too(
    store: SqliteStore,
) -> None:
    """Promotion fires on mechanical engagement regardless of mode."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="cobwebs",
        mode="player_initiated",
        engagement_kind="mechanical",
        turn_number=11,
    )
    assert res.mode_outcome == "promoted"
    assert res.entity is not None
    assert res.entity.tier == "yes_and"


def test_flavor_only_mention_does_not_promote(store: SqliteStore) -> None:
    """AC-5: pure mention (engagement_kind='mention') is descriptive — no
    mutation. flavor_only stays flavor_only."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="cobwebs",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.tier == "flavor_only"
    assert res.mode_outcome == "matched"
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


def test_real_object_mechanical_engagement_does_not_promote(
    store: SqliteStore,
) -> None:
    """Only flavor_only entities promote on mechanical engagement.
    A real_object that is engaged mechanically stays real_object — promotion
    is a Diamonds-and-Coal upgrade for unbound flavor, not a churn signal."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the bar",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=8,
    )
    assert res.mode_outcome == "matched"
    assert res.entity is not None
    assert res.entity.tier == "real_object"
    assert store.list_location_promotions(save_id="default", region_id="the_glenross_arms") == []


# ---------------------------------------------------------------------------
# AC-7: label normalisation
# ---------------------------------------------------------------------------


def test_definite_article_stripped_when_matching(store: SqliteStore) -> None:
    """Authored "the bar" matches incoming "bar"."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="bar",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "bar"


def test_authored_unarticled_label_matches_articled_incoming(
    store: SqliteStore,
) -> None:
    """Authored "cobwebs" matches incoming "the cobwebs"."""
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="the cobwebs",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "cobwebs"


def test_indefinite_articles_stripped(store: SqliteStore) -> None:
    """Both 'a' and 'an' are stripped — not just 'the'."""
    authored = [LocationEntity(id="lamp", label="lamp", tier="flavor_only")]
    res = resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=authored,
        label="a lamp",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    res2 = resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=authored,
        label="an lamp",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res2.resolved is True


def test_match_is_case_insensitive(store: SqliteStore) -> None:
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="The Bar",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.id == "bar"


# ---------------------------------------------------------------------------
# AC-6: authored YAML immutability
# ---------------------------------------------------------------------------


def test_authored_yaml_never_mutates_on_promotion(store: SqliteStore) -> None:
    """The resolver must NEVER mutate the authored entity list it was passed,
    even when the resolution promotes the entity. Promotion is realised as a
    new ``LocationEntity`` value via ``model_copy``."""
    authored = _entities()
    authored_cobwebs_before = authored[1].model_copy(deep=True)
    resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=authored,
        label="cobwebs",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=11,
    )
    assert authored[1] == authored_cobwebs_before
    # All authored entities are equal to their pre-resolve copies.
    assert authored[1].tier == "flavor_only"
    assert authored[1].provenance == "authored"


def test_authored_yaml_never_mutates_on_mint(store: SqliteStore) -> None:
    """A player-initiated mint must not append to the authored list either —
    the mint lives in ``location_promotions``, not in memory."""
    authored = _entities()
    length_before = len(authored)
    resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=authored,
        label="the antique sextant",
        mode="player_initiated",
        engagement_kind="mention",
        turn_number=2,
    )
    assert len(authored) == length_before


# ---------------------------------------------------------------------------
# Effective manifest — promotions visible on re-resolve
# ---------------------------------------------------------------------------


def test_existing_promotion_layers_on_top_of_authored(store: SqliteStore) -> None:
    """An entity that was promoted on an earlier turn must be read with the
    new tier on subsequent resolves — and ``from_promotion=True`` so the GM
    panel can distinguish promoted entries from baseline authored ones."""
    # First touch promotes.
    resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="cobwebs",
        mode="narrator_proactive",
        engagement_kind="mechanical",
        turn_number=11,
    )
    # Second touch sees promoted tier.
    res = resolve(
        store=store,
        save_id="default",
        region_id="the_glenross_arms",
        authored_entities=_entities(),
        label="cobwebs",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=20,
    )
    assert res.resolved is True
    assert res.entity is not None
    assert res.entity.tier == "yes_and"
    assert res.entity.provenance == "yes_and_promoted"
    assert res.from_promotion is True
    # And the row is not duplicated on the no-op second touch.
    rows = store.list_location_promotions(save_id="default", region_id="the_glenross_arms")
    assert len(rows) == 1


def test_minted_entity_visible_on_subsequent_resolve(store: SqliteStore) -> None:
    """A previously-minted entity must be matchable on a later turn — the
    effective manifest = authored + promotions."""
    resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=_entities(),
        label="the antique sextant",
        mode="player_initiated",
        engagement_kind="mention",
        turn_number=2,
    )
    res = resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=_entities(),
        label="the antique sextant",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=10,
    )
    assert res.resolved is True
    assert res.mode_outcome == "matched"
    assert res.from_promotion is True
    assert res.entity is not None
    assert res.entity.provenance == "yes_and_minted"


# ---------------------------------------------------------------------------
# Edge: empty manifest, empty label
# ---------------------------------------------------------------------------


def test_empty_authored_manifest_narrator_proactive_miss(
    store: SqliteStore,
) -> None:
    res = resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=[],
        label="anything",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is False
    assert res.mode_outcome == "no_match"


def test_empty_authored_manifest_player_initiated_mints(
    store: SqliteStore,
) -> None:
    res = resolve(
        store=store,
        save_id="default",
        region_id="r",
        authored_entities=[],
        label="the brass key",
        mode="player_initiated",
        engagement_kind="mention",
        turn_number=1,
    )
    assert res.resolved is True
    assert res.mode_outcome == "minted"


def test_promotions_in_other_region_do_not_match(store: SqliteStore) -> None:
    """Region scoping — a promotion in region A must not be visible when
    resolving in region B."""
    resolve(
        store=store,
        save_id="default",
        region_id="region_a",
        authored_entities=[],
        label="the sextant",
        mode="player_initiated",
        engagement_kind="mention",
        turn_number=1,
    )
    res = resolve(
        store=store,
        save_id="default",
        region_id="region_b",
        authored_entities=[],
        label="the sextant",
        mode="narrator_proactive",
        engagement_kind="mention",
        turn_number=2,
    )
    assert res.resolved is False
