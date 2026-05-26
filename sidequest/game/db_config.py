"""Database URL resolution for the Postgres substrate (ADR-115).

Single source of the connection string. The runtime pool (task-group 2)
and the Alembic migration runner both resolve through here so the URL is
never defined twice. No Silent Fallbacks: an unset URL raises rather than
defaulting to a localhost guess that masks a misconfigured deploy.
"""

from __future__ import annotations

import os

_ENV_VAR = "SIDEQUEST_DATABASE_URL"
_PLAIN_SCHEME = "postgresql://"
_PSYCOPG_SCHEME = "postgresql+psycopg://"


class MissingDatabaseUrlError(RuntimeError):
    """Raised when SIDEQUEST_DATABASE_URL is required but unset."""


def database_url() -> str:
    """Return the psycopg conninfo URL from the environment.

    Raises ``MissingDatabaseUrlError`` if unset (fail loud — never guess a
    localhost default that hides a deploy misconfiguration).
    """
    url = os.environ.get(_ENV_VAR)
    if not url:
        raise MissingDatabaseUrlError(
            f"{_ENV_VAR} is not set. The Postgres substrate requires an explicit "
            f"connection URL (e.g. postgresql://USER@localhost:5432/sidequest). "
            f"No silent localhost default (ADR-115 / No Silent Fallbacks)."
        )
    return url


def alembic_url() -> str:
    """The SQLAlchemy/Alembic form of the URL (``postgresql+psycopg://``).

    Alembic uses a SQLAlchemy Engine, which selects the driver from the URL
    scheme; the runtime psycopg pool uses the plain ``postgresql://`` form.
    """
    url = database_url()
    if url.startswith(_PSYCOPG_SCHEME):
        return url
    if url.startswith(_PLAIN_SCHEME):
        return _PSYCOPG_SCHEME + url[len(_PLAIN_SCHEME):]
    return url
