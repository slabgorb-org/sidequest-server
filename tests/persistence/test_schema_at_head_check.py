"""Startup schema-version guard — fail loud when the DB is behind alembic head.

Story 71-20 / playtest finding #G4. ADR-115 already fails loud when Postgres is
UNREACHABLE (``MissingDatabaseUrlError``, 10s pool-wait timeout). This extends
that same fail-loud startup contract to the case the playtest actually hit:
Postgres reachable, but the schema stamped behind alembic head (dev DB at 0001
while head was 0002 — the ``asset_ledger`` migration never applied). The server
booted fine and only exploded mid-turn on the first write to the missing table.
The guard moves that failure to boot, where it is obvious and cheap to fix.

These tests provision a REAL Postgres DB upgraded only to the base revision (a
behind-head DB, via the ``behind_head_db`` fixture) and assert
``assert_schema_at_head()`` fails loud, names current vs head, gives an actionable
message, and logs the check — while a head-current DB (``migrated_db``, already at
head) passes clean. Head/current revision identifiers are read dynamically from
the alembic scripts so the assertions survive a future 0003+ migration.
"""

from __future__ import annotations

import logging

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_CHECK_LOGGER = "sidequest.game.db_schema_check"


def _alembic_head() -> str:
    """The single current head revision id, read from the alembic scripts."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    return ScriptDirectory.from_config(cfg).get_current_head()


def _check_log_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _CHECK_LOGGER]


# --- AC#1 / AC#5: behind-head fails loud AT THE CHECK (not at a later write) ---


def test_behind_head_db_fails_loud(monkeypatch, behind_head_db: str) -> None:
    """A DB stamped behind head raises ``SchemaBehindHeadError`` — and the error
    names BOTH the current revision and the head revision so the operator sees
    exactly how far behind the schema is."""
    from sidequest.game.db_schema_check import (
        SchemaBehindHeadError,
        assert_schema_at_head,
    )

    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", behind_head_db)

    with pytest.raises(SchemaBehindHeadError) as exc_info:
        assert_schema_at_head()

    message = str(exc_info.value)
    assert "0001" in message, f"current revision (0001) not named in error: {message!r}"
    head = _alembic_head()
    assert head in message, f"head revision ({head}) not named in error: {message!r}"


# --- AC#1 / AC#5 control: head-current DB passes clean (no false positive) ---


def test_head_current_db_passes(monkeypatch, migrated_db: str) -> None:
    """A DB at head passes the check without raising and returns ``None``.

    ``migrated_db`` ran ``alembic upgrade head``, so this is the clean-boot path.
    Without this control a check that always raised would still pass the
    behind-head test — this proves the guard does not false-positive."""
    from sidequest.game.db_schema_check import assert_schema_at_head

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)

    assert assert_schema_at_head() is None


# --- AC#2: the failure message is actionable ---


def test_error_message_is_actionable(monkeypatch, behind_head_db: str) -> None:
    """AC#2: the failure tells the operator how to fix it — by name, the
    ``alembic upgrade head`` step. A loud error with no remediation is half a
    fix; the whole point is to point the operator at the migrate command."""
    from sidequest.game.db_schema_check import (
        SchemaBehindHeadError,
        assert_schema_at_head,
    )

    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", behind_head_db)

    with pytest.raises(SchemaBehindHeadError) as exc_info:
        assert_schema_at_head()

    assert "alembic upgrade head" in str(exc_info.value), (
        "error must name the remediation command (`alembic upgrade head`); "
        f"got: {str(exc_info.value)!r}"
    )


# --- AC#6: the check logs the schema-version result (current rev, head rev) ---


def test_check_logs_result_when_behind(monkeypatch, behind_head_db, caplog) -> None:
    """AC#6: even on the failure path the check logs current + head revision
    BEFORE it raises, so the operator can see the schema-version mismatch in the
    boot log (the GM-panel / log is the only place a behind-head boot is now
    visible)."""
    from sidequest.game.db_schema_check import (
        SchemaBehindHeadError,
        assert_schema_at_head,
    )

    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", behind_head_db)
    head = _alembic_head()

    with caplog.at_level(logging.INFO, logger=_CHECK_LOGGER):
        with pytest.raises(SchemaBehindHeadError):
            assert_schema_at_head()

    messages = _check_log_messages(caplog)
    assert any("0001" in m and head in m for m in messages), (
        f"expected a {_CHECK_LOGGER} log naming current (0001) and head ({head}); "
        f"got: {messages!r}"
    )


def test_check_logs_result_at_head(monkeypatch, migrated_db, caplog) -> None:
    """AC#6: the success path also logs the schema-version check, so a clean boot
    positively records "DB is at head" rather than logging only on failure."""
    from sidequest.game.db_schema_check import assert_schema_at_head

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    head = _alembic_head()

    with caplog.at_level(logging.INFO, logger=_CHECK_LOGGER):
        assert_schema_at_head()

    messages = _check_log_messages(caplog)
    assert any(head in m for m in messages), (
        f"expected a {_CHECK_LOGGER} log naming head ({head}) on the clean path; "
        f"got: {messages!r}"
    )


# --- AC#4: reuses the ADR-115 fail-loud contract, not a parallel one ---


def test_unset_url_reuses_missing_url_contract(monkeypatch) -> None:
    """AC#4: with ``SIDEQUEST_DATABASE_URL`` unset the guard raises the SAME
    ``MissingDatabaseUrlError`` ADR-115 already defines — proving it extends the
    existing fail-loud contract instead of inventing a parallel resolver."""
    from sidequest.game.db_config import MissingDatabaseUrlError
    from sidequest.game.db_schema_check import assert_schema_at_head

    monkeypatch.delenv("SIDEQUEST_DATABASE_URL", raising=False)

    with pytest.raises(MissingDatabaseUrlError):
        assert_schema_at_head()
