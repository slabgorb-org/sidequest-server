"""RED wiring test — Story 153-19 — Oddity 2 cleanup runs on the REAL path.

The behavior tests in ``tests/game/test_153_19_character_location_cleanup.py``
prove ``prune_orphan_character_locations`` works in isolation. This test keeps
Dev honest (CLAUDE.md "Verify Wiring, Not Just Existence"): it drives a fresh
character through the actual chargen-commit handler (``_chargen_confirmation``,
where the chargen-built PC replaces the materialized placeholder) and asserts the
orphan-location cleanup fired THERE — not merely that a helper exists.

Content-agnostic by design (mirrors ``test_chargen_quest_seed_wiring``): the
load-bearing wiring proof is that exactly one ``character_locations.orphan_pruned``
span fires on commit — the cleanup is always-fire (it emits even when it prunes
nothing). That holds whether caverns_and_claudes' fresh chapter happens to author
a placeholder or not, so the test pins the wiring without coupling to pack
content. (The deterministic orphan-removal proof lives in the game-level unit
test, which constructs the placeholder state directly.)

Additionally asserts the post-commit invariant: ``character_locations`` carries no
key that isn't a current character — defense against a future regression that
re-introduces a stale key on the real path.

Reuses the chargen-commit harness from ``test_chargen_dispatch`` and the
``otel_capture`` span exporter from ``tests/server/conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.protocol.messages import CharacterCreationPayload
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import mock_claude_client_factory as _mock_claude_client_factory
from tests.server.test_chargen_dispatch import (
    _connect,
    _send_chargen,
    _walk_to_confirmation,
    run,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
SPAN_NAME = "character_locations.orphan_pruned"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, truncated per test.
    The cleanup seam only runs on the first-commit (materialize) path; a fresh DB
    guarantees each run is a first-commit, not a slug-resume that skips it.
    (Mirrors ``test_chargen_quest_seed_wiring::_pg_isolation``.)
    """
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


def test_orphan_location_cleanup_fires_on_real_chargen_commit(
    handler: WebSocketSessionHandler, otel_capture
) -> None:
    async def body() -> None:
        await _connect(handler)
        await _walk_to_confirmation(handler, freeform_name="Rux")
        out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
        assert out, "confirmation must return at least the CHARACTER_CREATION{complete} message"

    run(body())

    sd = handler._session_data  # type: ignore[attr-defined]
    assert len(sd.snapshot.characters) == 1, "PC must be materialized on the snapshot"

    prune_spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_NAME]
    assert len(prune_spans) == 1, (
        "exactly one character_locations.orphan_pruned span must fire on the real "
        "chargen-commit path — the cleanup is not wired into _chargen_confirmation "
        f"(got {len(prune_spans)})."
    )

    # Post-commit invariant: no character_locations key without a current character.
    snap = sd.snapshot
    real_names = {c.core.name for c in snap.characters}
    orphans = set(snap.character_locations) - real_names
    assert not orphans, (
        f"character_locations carries orphan key(s) {sorted(orphans)} with no "
        "matching character after chargen commit (153-19 oddity 2)."
    )
