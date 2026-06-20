"""Story 126-34 — keep test-run sessions out of the live GM dashboard.

Two server-side concerns are exercised here:

1. **AC#3 — polling must not bump ``last_activity_ts``.** The GM dashboard's
   ``/api/debug/state`` poll loads each session through
   ``PgSaveRepository.for_slug`` → ``ensure_session``, whose
   ``ON CONFLICT (session_slug) DO UPDATE SET last_played`` rewrites
   ``last_played`` (the source of ``last_activity_ts``) on every read. So an
   idle test session floats to the top of auto-follow purely because the
   operator's dashboard polled it. A read-only poll must leave the timestamp
   untouched. Driven through the REAL FastAPI route (TestClient) so the fix is
   proven end-to-end, not at a helper seam.

2. **AC#4 / AC#5 — test-session activity is tagged so the per-session pin can
   filter it.** Every ``publish_event`` for a session whose slug is a test run
   (``test-*`` / ``tool-test*``) must carry a ``session_type: "test"`` marker on
   the broadcast envelope, so the GM-panel Live view can drop it while keeping a
   genuinely-driven session — the OTEL "lie-detector" separation the story
   requires. Real sessions must NOT be tagged. Driven through the production
   ``publish_event`` entry point (not an isolated helper) so the tag is proven
   reachable from the real publish path — the wiring test for this suite.

The ``session_type: "test"`` field is TEA's chosen contract; the AC offered
``span_type="infra"`` OR ``session_type="test"`` and this suite pins the latter
(see the session file's TEA deviation note).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sidequest.game import db_pool
from sidequest.game.pg import sessions
from sidequest.game.pg.forensic import PgForensicReader
from sidequest.server.app import create_app
from sidequest.telemetry import watcher_hub as wh

REAL_SLUG_PREFIX = "2026-06-16-annees_folles-"

# A fixed, far-past last_played so the post-poll comparison is deterministic:
# the buggy poll rewrites it to now() (~2026, a much larger ms value); a correct
# read-only poll leaves it exactly here. No sleeps, no wall-clock flake.
_OLD_LAST_PLAYED = "2020-01-01T00:00:00+00:00"
_OLD_ACTIVITY_MS = int(datetime.fromisoformat(_OLD_LAST_PLAYED).timestamp() * 1000)


def _slug(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


def _last_activity_ts(pool, slug: str) -> int | None:
    for row in PgForensicReader(pool).list_saves():
        if row["slug"] == slug:
            return int(row["last_activity_ts"])
    return None


# ---------------------------------------------------------------------------
# AC#3 — read-only poll must not advance last_activity_ts
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(monkeypatch, migrated_db: str):
    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield db_pool.get_pool()
    db_pool.close_pool()


def test_debug_state_poll_does_not_bump_last_activity_ts(pool, tmp_path) -> None:
    slug = _slug("test-poll-nobump")
    sessions.ensure_session(
        pool, slug=slug, mode="solo", genre_slug="pulp_noir", world_slug="annees_folles"
    )
    # Pin last_played to the far past so a bump is unmistakable.
    with pool.connection() as conn:
        conn.execute(
            "UPDATE sessions SET last_played = %s WHERE session_slug = %s",
            (_OLD_LAST_PLAYED, slug),
        )

    before = _last_activity_ts(pool, slug)
    assert before == _OLD_ACTIVITY_MS  # sanity: the pin took

    client = TestClient(create_app(genre_pack_search_paths=[tmp_path], save_dir=tmp_path))
    resp = client.get("/api/debug/state")
    assert resp.status_code == 200

    after = _last_activity_ts(pool, slug)
    # The poll is a read. It must NOT have rewritten last_played to now().
    assert after == before, (
        f"polling /api/debug/state bumped last_activity_ts {before} -> {after}; "
        "the read path must not call the last_played-bumping ensure_session"
    )


# ---------------------------------------------------------------------------
# AC#4 / AC#5 — test-session activity is tagged on the broadcast envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_publishes(monkeypatch) -> Iterator[list[dict]]:
    """Capture what ``publish_event`` hands to the hub, with all out-of-band
    persistence neutralized so the test needs no DB or bound loop."""
    events: list[dict] = []
    monkeypatch.setattr(wh.watcher_hub, "publish", lambda ev: events.append(ev))
    wh.bind_event_store(None)  # no out-of-frame sink → persistence is a no-op
    wh.bind_session_slug(None)
    try:
        yield events
    finally:
        wh.bind_session_slug(None)
        wh.bind_event_store(None)


def test_publish_event_tags_test_session_activity(captured_publishes) -> None:
    wh.bind_session_slug("test-pulp_noir-deadbeef")
    wh.publish_event("turn_complete", {"turn_id": "x"})

    assert len(captured_publishes) == 1
    ev = captured_publishes[0]
    assert ev["session_slug"] == "test-pulp_noir-deadbeef"
    assert ev.get("session_type") == "test", (
        "a test-run session's activity must be tagged session_type='test' so the "
        "GM-panel per-session pin can filter it out"
    )


def test_publish_event_tags_tool_test_session_activity(captured_publishes) -> None:
    wh.bind_session_slug("tool-test-fate-cafef00d")
    wh.publish_event("state_transition", {"field": "location", "kind": "move"})

    assert len(captured_publishes) == 1
    assert captured_publishes[0].get("session_type") == "test"


def test_publish_event_does_not_tag_real_session_activity(captured_publishes) -> None:
    real = f"{REAL_SLUG_PREFIX}5cbe9403"
    wh.bind_session_slug(real)
    wh.publish_event("turn_complete", {"turn_id": 1})

    ev = captured_publishes[0]
    assert ev["session_slug"] == real
    assert ev.get("session_type") != "test"


def test_test_and_real_activity_are_separable_in_the_live_stream(captured_publishes) -> None:
    """Wiring / lie-detector: a real turn and a parallel test-run turn flow
    through the SAME production publish_event path and stay distinguishable on
    the broadcast envelope — so the pin filter keeps one and drops the other."""
    real = f"{REAL_SLUG_PREFIX}5cbe9403"

    wh.bind_session_slug(real)
    wh.publish_event("turn_complete", {"turn_id": 1})

    wh.bind_session_slug("test-foo-12345678")
    wh.publish_event("turn_complete", {"turn_id": 2})

    by_slug = {e["session_slug"]: e for e in captured_publishes}
    assert by_slug[real].get("session_type") != "test"
    assert by_slug["test-foo-12345678"].get("session_type") == "test"
