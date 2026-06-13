"""Wiring gate — ADR-033 resource pools are wired on the REAL session-create path.

The behavior test in ``tests/game/test_resource_wiring.py`` proves
``wire_genre_resources`` works in isolation. This test keeps Dev honest
(CLAUDE.md "Verify Wiring, Not Just Existence" + "No Source-Text Wiring Tests"):
it drives a fresh character through the actual chargen-commit handler
(``_chargen_confirmation``, where the built PC is materialized onto the canonical
snapshot) and asserts that the shipping ``caverns_and_claudes`` pack's declared
``light`` resource pool was populated there — not that a helper merely exists.

Removing the ``wire_genre_resources`` call from the chargen seam makes this fail.

Reuses the chargen-commit harness from ``test_chargen_dispatch`` and the PG
isolation fixture pattern from ``test_chargen_quest_seed_wiring`` (the wiring
runs on the first-commit/materialize path; a fresh DB guarantees each run is a
first-commit rather than a slug-resume that skips materialization).
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


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, truncated per
    test, so each run is a first-commit (materialize) path rather than a
    slug-resume. Mirrors ``test_chargen_quest_seed_wiring::_pg_isolation``.
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


@pytest.mark.integration
def test_light_pool_wired_on_real_chargen_commit(
    handler: WebSocketSessionHandler,
) -> None:
    """The shipping caverns_and_claudes pack declares a `light` resource pool;
    driving the real chargen-commit path must leave it populated on the
    canonical snapshot."""

    async def body() -> None:
        await _connect(handler)
        await _walk_to_confirmation(handler, freeform_name="Rux")
        out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
        assert out, "confirmation must return at least the CHARACTER_CREATION{complete} message"

    run(body())

    sd = handler._session_data  # type: ignore[attr-defined]
    assert len(sd.snapshot.characters) == 1, "PC must be materialized on the snapshot"
    assert "light" in sd.snapshot.resources, (
        "caverns_and_claudes declares a `light` resource pool but it is absent "
        "from the snapshot after chargen commit — wire_genre_resources is not "
        "wired into the session-create path"
    )
