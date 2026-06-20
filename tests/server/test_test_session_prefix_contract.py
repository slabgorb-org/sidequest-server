"""Story 125-10 (follow-up to 126-34) — lock the test-session prefix contract so
the server ``is_test_session`` and the ui ``isTestSession`` cannot silently drift.

This is the SERVER half of a two-repo contract. The canonical prefix set + golden
classification cases live in a fixture VENDORED BYTE-IDENTICAL in both repos:

    sidequest-server/tests/fixtures/test_session_prefix_contract.json
    sidequest-ui/src/components/Dashboard/__tests__/fixtures/test_session_prefix_contract.json

The matching ui contract test is
``sidequest-ui/src/components/Dashboard/__tests__/test-session-prefix-contract.test.ts``.

The lock: this test pins the production predicate's DECLARED prefix tuple
(``_TEST_SESSION_SLUG_PREFIXES``) to the fixture's ``prefixes``. Adding a prefix to
the predicate without updating the canonical fixture fails this test in CI — forcing
the change through the single canonical declaration that the ui side is pinned to as
well. (126-34 leak: stale ``test-*`` sessions reappearing on the live GM dashboard
when the two predicates disagree about what counts as a test run.)
"""

from __future__ import annotations

import json
from pathlib import Path

from sidequest.telemetry import watcher_hub as wh

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "test_session_prefix_contract.json"


def _contract() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_declared_prefix_set_matches_canonical_fixture() -> None:
    """The production predicate's prefix tuple is exactly the canonical set.

    This is the cross-repo tripwire: a prefix added to ``is_test_session`` but not
    to the fixture fails here, forcing the dev to the canonical declaration that
    the ui predicate is pinned to as well.
    """
    contract = _contract()
    assert tuple(contract["prefixes"]) == wh._TEST_SESSION_SLUG_PREFIXES, (
        "server _TEST_SESSION_SLUG_PREFIXES "
        f"{wh._TEST_SESSION_SLUG_PREFIXES} drifted from the canonical "
        f"test_session_prefix_contract.json prefixes {contract['prefixes']}; update the "
        "fixture (and the ui copy + ui predicate) together"
    )


def test_predicate_classifies_every_canonical_case() -> None:
    """``is_test_session`` agrees with each golden case the ui predicate also runs,
    so both repos classify the shared corpus of slugs identically."""
    contract = _contract()
    for case in contract["cases"]:
        slug = case["slug"]
        expected = case["is_test"]
        assert wh.is_test_session(slug) is expected, (
            f"is_test_session({slug!r}) != canonical {expected} "
            "(server/ui drift on a shared contract case)"
        )


def test_none_is_not_a_test_session() -> None:
    """Server-only case (the ui predicate is typed ``string``): session-less infra
    (``None``) is global, shown in every view — never a test run."""
    assert wh.is_test_session(None) is False
