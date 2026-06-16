"""Predicate catalog — the vocabulary of per-player asymmetry."""

from __future__ import annotations

from sidequest.game.projection.predicates import PREDICATES, PredicateContext
from sidequest.game.projection.view import SessionGameStateView


def _ctx(
    *,
    payload: dict,
    viewer_player_id: str,
    view: SessionGameStateView | None = None,
) -> PredicateContext:
    if view is None:
        view = SessionGameStateView(
            gm_player_id="gm",
            player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
            party_id="party_1",
        )
    return PredicateContext(
        view=view,
        payload=payload,
        viewer_player_id=viewer_player_id,
        viewer_character_id=view.character_of(viewer_player_id),
    )


def test_is_gm_no_args() -> None:
    pred = PREDICATES["is_gm"]
    assert pred(_ctx(payload={}, viewer_player_id="gm"), field_ref=None) is True
    assert pred(_ctx(payload={}, viewer_player_id="alice"), field_ref=None) is False


def test_is_self_matches_viewer_character() -> None:
    pred = PREDICATES["is_self"]
    ctx = _ctx(payload={"target": "alice_char"}, viewer_player_id="alice")
    assert pred(ctx, field_ref="target") is True

    ctx = _ctx(payload={"target": "bob_char"}, viewer_player_id="alice")
    assert pred(ctx, field_ref="target") is False


def test_is_self_returns_false_when_field_missing() -> None:
    pred = PREDICATES["is_self"]
    ctx = _ctx(payload={}, viewer_player_id="alice")
    assert pred(ctx, field_ref="missing") is False


def test_is_owner_of_checks_item_ownership() -> None:
    class _View(SessionGameStateView):
        def owner_of_item(self, item_id: str) -> str | None:
            return "alice" if item_id == "sword" else None

    view = _View(gm_player_id="gm", player_id_to_character={"alice": "alice_char"})
    pred = PREDICATES["is_owner_of"]

    ctx = _ctx(payload={"item_id": "sword"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="item_id") is True

    ctx = _ctx(payload={"item_id": "staff"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="item_id") is False


def test_in_same_zone_requires_both_zones_known() -> None:
    class _View(SessionGameStateView):
        def zone_of(self, character_id: str) -> str | None:
            return {"alice_char": "tavern", "bob_char": "tavern", "carol_char": "street"}.get(
                character_id
            )

    view = _View(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char", "bob": "bob_char", "carol": "carol_char"},
    )
    pred = PREDICATES["in_same_zone"]

    ctx = _ctx(payload={"target": "bob_char"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="target") is True

    ctx = _ctx(payload={"target": "carol_char"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="target") is False

    ctx = _ctx(payload={"target": "unknown_char"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="target") is False


def test_visible_to_delegates_to_view() -> None:
    class _View(SessionGameStateView):
        def visible_to(self, viewer: str, target: str) -> bool:
            return (viewer, target) == ("alice_char", "bob_char")

    view = _View(
        gm_player_id="gm",
        player_id_to_character={"alice": "alice_char", "bob": "bob_char"},
    )
    pred = PREDICATES["visible_to"]

    ctx = _ctx(payload={"target": "bob_char"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="target") is True

    ctx = _ctx(payload={"target": "carol_char"}, viewer_player_id="alice", view=view)
    assert pred(ctx, field_ref="target") is False


def test_in_same_party_compares_party_ids() -> None:
    pred = PREDICATES["in_same_party"]
    ctx = _ctx(payload={"revealer": "bob"}, viewer_player_id="alice")
    assert pred(ctx, field_ref="revealer") is True

    ctx = _ctx(payload={"revealer": "outsider"}, viewer_player_id="alice")
    assert pred(ctx, field_ref="revealer") is False


def test_unknown_predicate_name_is_not_in_catalog() -> None:
    assert "not_a_real_predicate" not in PREDICATES
