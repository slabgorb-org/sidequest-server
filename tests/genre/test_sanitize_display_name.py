"""Unit tests for ``sanitize_display_name`` (playtest 2026-06-10).

The Markov chain filters to letters, so clean generation can't emit bracket
junk — but the Monster Manual is a long-lived on-disk cache that can hold a
name minted by older code (``Vesper (version)`` in a stale coyote_star
manual). The sanitizer is the boundary guard. It must strip never-valid junk
(bracketed annotations, digits) while preserving the punctuation that culture
patterns use ON PURPOSE: Broken Drift's quoted callsigns (``Quija 'Salt'``)
and comma drift-markers (``Hush, off Tether``).
"""

from __future__ import annotations

import pytest

from sidequest.genre.names.generator import sanitize_display_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The reported bug and its class.
        ("Vesper (version)", "Vesper"),
        ("Unit 7 [decommissioned]", "Unit 7"),  # digit kept, bracket annotation gone
        ("Crawler {tier2}", "Crawler"),
        ("Vesper (v2),", "Vesper"),
        # Pure-junk names sanitize to empty (caller drops them).
        ("(version)", ""),
        ("[42]", ""),
        ("", ""),
    ],
)
def test_strips_bracketed_and_digit_junk(raw: str, expected: str) -> None:
    assert sanitize_display_name(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        # Plain authored / Markov names pass through untouched.
        "Demiloslava Chop",
        "Kanga Moana-Teru",
        "Teleetä of the Niisamän Root",
        # Intentional Broken Drift styling MUST survive.
        "Quija 'Salt'",
        "Hush, off Tether",
        # Apostrophes inside names are kept.
        "O'Brien",
    ],
)
def test_preserves_legitimate_names(name: str) -> None:
    assert sanitize_display_name(name) == name
