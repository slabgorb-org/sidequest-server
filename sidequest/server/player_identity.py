"""Authenticated player identity resolution (Story 67-6, ADR-119).

The app sits behind Cloudflare Zero Trust, which injects the authenticated
user's email as ``Cf-Access-Authenticated-User-Email``. Local dev distinguishes
players by per-player Host names (player1.local, player2.local). Identity is the
*human*, distinct from the seated character name (snapshot.player_seats).

No silent fallback: if neither header yields a non-blank value, raise.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

CF_ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"
HOST_HEADER = "host"


class MissingPlayerIdentityError(RuntimeError):
    """No player identity could be resolved from request headers."""


def _lowered(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): v for k, v in headers.items()}


def _strip_port(host: str) -> str:
    """Strip a trailing ``:port`` from a Host value, leaving the per-player hostname.

    IPv6-safe enough: a bracketed literal (``[::1]:8765``) contains ``]`` and is
    left untouched; a value with exactly one ``:`` is split on it; anything else
    (multiple colons, no colon) is returned as-is.
    """
    if "]" not in host and host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def resolve_player_identity(headers: Mapping[str, str]) -> str:
    """Resolve the authenticated player identity. Cf-Access email -> Host -> raise."""
    lowered = _lowered(headers)
    email = (lowered.get(CF_ACCESS_EMAIL_HEADER) or "").strip()
    if email:
        return email
    host = (lowered.get(HOST_HEADER) or "").strip()
    if host:
        return _strip_port(host)
    raise MissingPlayerIdentityError(
        "No player identity: neither Cf-Access-Authenticated-User-Email nor Host header present"
    )


def identity_source(headers: Mapping[str, str]) -> Literal["cf_access", "host"]:
    """Which header the resolved identity came from (for OTEL; never the value)."""
    lowered = _lowered(headers)
    if (lowered.get(CF_ACCESS_EMAIL_HEADER) or "").strip():
        return "cf_access"
    if (lowered.get(HOST_HEADER) or "").strip():
        return "host"
    raise MissingPlayerIdentityError(
        "No player identity: neither Cf-Access-Authenticated-User-Email nor Host header present"
    )
