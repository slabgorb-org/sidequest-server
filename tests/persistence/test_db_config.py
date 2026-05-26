"""db_config.database_url resolver — fail-loud, no silent default (ADR-115 TG1)."""

from __future__ import annotations

import pytest

from sidequest.game.db_config import MissingDatabaseUrlError, alembic_url, database_url


def test_database_url_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://u@localhost:5432/sq")
    assert database_url() == "postgresql://u@localhost:5432/sq"


def test_database_url_unset_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIDEQUEST_DATABASE_URL", raising=False)
    with pytest.raises(MissingDatabaseUrlError):
        database_url()


def test_alembic_url_adds_psycopg_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql://u@localhost:5432/sq")
    assert alembic_url() == "postgresql+psycopg://u@localhost:5432/sq"


def test_alembic_url_idempotent_if_already_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", "postgresql+psycopg://u@localhost/sq")
    assert alembic_url() == "postgresql+psycopg://u@localhost/sq"
