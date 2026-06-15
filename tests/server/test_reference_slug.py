"""Unit tests for the shared slugify helper.

The helper is imported by both the page renderer and the URL builder so the
two surfaces cannot drift.
"""

from __future__ import annotations

import pytest

from sidequest.foundation.reference_slug import slugify


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Burglar", "burglar"),
        ("Cosh & Run", "cosh-run"),
        ("   Aunt Pemberton   ", "aunt-pemberton"),
        ("Lady Of The Hall", "lady-of-the-hall"),
        ("history.yaml", "history-yaml"),
        ("naïve", "naive"),  # Story 101-8: NFKD-fold (was "na-ve"; ï→i)
        ("", ""),
    ],
)
def test_slugify_cases(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_slugify_collapses_runs() -> None:
    assert slugify("a   b---c__d") == "a-b-c-d"


def test_slugify_strips_edges() -> None:
    assert slugify("-leading and trailing-") == "leading-and-trailing"
