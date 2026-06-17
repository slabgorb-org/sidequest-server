"""Pinning tests for resolve_player_identity (Story 67-6, ADR-119).

Resolution order: Cf-Access-Authenticated-User-Email (non-blank,
case-insensitive) -> Host header -> raise. No silent default.
"""

import pytest

from sidequest.server.player_identity import (
    MissingPlayerIdentityError,
    identity_source,
    resolve_player_identity,
)

CF = "cf-access-authenticated-user-email"


def test_cf_access_email_wins():
    assert (
        resolve_player_identity({CF: "alice@example.com", "host": "p1.local"})
        == "alice@example.com"
    )


def test_cf_access_header_is_case_insensitive():
    headers = {"Cf-Access-Authenticated-User-Email": "bob@example.com"}
    assert resolve_player_identity(headers) == "bob@example.com"


def test_blank_cf_access_falls_through_to_host():
    assert resolve_player_identity({CF: "   ", "host": "player1.local"}) == "player1.local"


def test_missing_cf_access_falls_through_to_host():
    assert resolve_player_identity({"host": "player2.local"}) == "player2.local"


def test_host_is_trimmed():
    assert resolve_player_identity({"host": "  player1.local  "}) == "player1.local"


def test_host_port_is_stripped():
    assert resolve_player_identity({"host": "player1.local:8765"}) == "player1.local"


def test_host_without_port_unchanged():
    assert resolve_player_identity({"host": "player1.local"}) == "player1.local"


def test_cf_access_email_is_trimmed():
    assert resolve_player_identity({CF: "  alice@example.com  "}) == "alice@example.com"


def test_raises_when_neither_present():
    with pytest.raises(MissingPlayerIdentityError):
        resolve_player_identity({})


def test_raises_when_both_blank():
    with pytest.raises(MissingPlayerIdentityError):
        resolve_player_identity({CF: "", "host": "   "})


def test_identity_source_reports_cf_access():
    assert identity_source({CF: "alice@example.com", "host": "p1.local"}) == "cf_access"


def test_identity_source_reports_host_when_email_blank():
    assert identity_source({CF: "  ", "host": "player1.local"}) == "host"


def test_identity_source_raises_when_neither_present():
    with pytest.raises(MissingPlayerIdentityError):
        identity_source({})


def test_identity_source_raises_when_both_blank():
    with pytest.raises(MissingPlayerIdentityError):
        identity_source({CF: "", "host": "  "})
