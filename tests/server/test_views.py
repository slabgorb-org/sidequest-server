"""Unit tests for sidequest/server/views.py.

Phase 2 of session_handler decomposition. After the post-epic cleanup
that dropped the seven thin delegates from WebSocketSessionHandler,
this file's surface is:

1. ``test_views_module_exposes_required_functions`` — smoke check that
   the seven canonical names are importable from ``sidequest.server.views``.
2. ``test_is_hidden_status_list_matches_hidden_tokens`` — behavioral
   coverage for the simplest function (the others are exercised by the
   broader integration tests in test_session_handler_view.py,
   test_visibility_wiring.py, test_multiplayer_party_status.py, and
   test_perception_rewriter_wiring.py — those tests now call the
   ``views.*`` functions directly rather than through delegates).
3. ``test_portrait_url_via_resolver_*`` — pin the contract that any
   future portrait_url emission MUST go through resolve_asset_url so
   the CDN/local seam stays single-pointed (R2 migration, Task 10).
"""

from __future__ import annotations

import pytest

from sidequest.server.asset_urls import resolve_asset_url


def test_views_module_exposes_required_functions() -> None:
    """The seven canonical names must be importable from
    sidequest.server.views."""
    from sidequest.server import views

    assert hasattr(views, "is_hidden_status_list")
    assert hasattr(views, "build_game_state_view")
    assert hasattr(views, "status_effects_by_player")
    assert hasattr(views, "backfill_last_narration_block")
    assert hasattr(views, "party_member_from_character")
    assert hasattr(views, "resolve_self_character")
    assert hasattr(views, "build_session_start_party_status")


def test_is_hidden_status_list_matches_hidden_tokens() -> None:
    """Behavioral test — each of the four hidden tokens triggers a True
    result; an unrelated token returns False; an empty list returns False."""
    from sidequest.game.status import Status, StatusSeverity
    from sidequest.server import views

    assert views.is_hidden_status_list([]) is False
    assert (
        views.is_hidden_status_list([Status(text="poisoned", severity=StatusSeverity.Scratch)])
        is False
    )
    for token in ("hidden", "invisible", "stealth", "concealed"):
        assert (
            views.is_hidden_status_list([Status(text=token, severity=StatusSeverity.Scratch)])
            is True
        )
    # Case-insensitive whole-token (the lowercase comparison is the
    # contract; substring matches are explicitly out of scope per
    # tests/server/test_session_handler_view.py:216).
    assert (
        views.is_hidden_status_list([Status(text="HIDDEN", severity=StatusSeverity.Scratch)])
        is True
    )
    assert (
        views.is_hidden_status_list([Status(text="hiddenly", severity=StatusSeverity.Scratch)])
        is False
    )


def test_portrait_url_via_resolver_default_is_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default config (no SIDEQUEST_ASSET_BASE_URL) emits a CDN URL for
    an artifacts-relative portrait path. This pins the contract that
    portrait_url emission must funnel through resolve_asset_url."""
    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    rel = "artifacts/dungeon_survivor/abc123/portraits/deadbeef.png"
    assert resolve_asset_url(rel) == f"https://cdn.slabgorb.com/{rel}"


def test_portrait_url_via_resolver_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local-serve mode rewrites artifacts/ to the /renders/artifacts/
    static mount — the rollback path for offline dev."""
    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    rel = "artifacts/dungeon_survivor/abc123/portraits/deadbeef.png"
    assert resolve_asset_url(rel) == f"/renders/{rel}"
