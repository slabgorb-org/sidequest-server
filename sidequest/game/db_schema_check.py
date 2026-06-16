"""Startup schema-version guard — fail loud when the DB is behind alembic head.

ADR-115 already fails loud when Postgres is UNREACHABLE
(``db_config.MissingDatabaseUrlError`` on an unset URL, plus the 10s pool-wait
timeout in ``app._open_db_pool``). This module extends that same fail-loud
startup contract to the case playtest finding #G4 actually hit: Postgres
reachable, but the schema stamped BEHIND alembic head (the dev DB sat at ``0001``
while head was ``0002`` — the ``asset_ledger`` migration had never been applied).
The server booted fine and only exploded mid-turn on the first write to the
missing table, far from the root cause.

**Decision (Story 71-20, AC#3): (A) fail-loud-assert.** At boot we assert the
connected DB's alembic revision == head and raise ``SchemaBehindHeadError`` if
not. We deliberately do NOT (B) auto-run ``alembic upgrade head`` at boot:
silently mutating a (possibly shared) schema during startup is itself a silent
action and violates No Silent Fallbacks. Migration stays an explicit, visible
operator step — the error names the exact remediation command.
"""

from __future__ import annotations

import logging

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory

from sidequest.game.db_config import database_url

logger = logging.getLogger(__name__)


class SchemaBehindHeadError(RuntimeError):
    """Raised at startup when the connected DB's alembic revision is not at head.

    Extends the ADR-115 fail-loud startup contract from "Postgres unreachable"
    (``MissingDatabaseUrlError``) to "reachable but schema behind head".
    """


def _current_revision(conninfo: str) -> str | None:
    """The DB's stamped alembic revision, read from ``alembic_version``.

    Returns ``None`` when the table is absent — i.e. the DB was never migrated,
    which is itself "behind head" and must fail loud. A genuine connection or
    query error (unreachable host, permission denied) is deliberately NOT caught
    here: it propagates and fails the boot loudly, which is the intended
    No-Silent-Fallbacks behaviour — relabelling it as "schema behind head" would
    be a misleading error.
    """
    with psycopg.connect(conninfo) as conn:
        # The EXISTS guard makes a never-migrated DB (no alembic_version table)
        # return zero rows -> None, instead of raising UndefinedTable. That puts
        # "table absent" on the same fail-loud path as "behind head" without a
        # try/except that could accidentally swallow a real error.
        row = conn.execute(
            "SELECT version_num FROM alembic_version WHERE EXISTS "
            "(SELECT 1 FROM information_schema.tables "
            " WHERE table_schema = 'public' AND table_name = 'alembic_version')"
        ).fetchone()
    return row[0] if row else None


def _head_revision() -> str:
    """The single current head revision id, from the alembic scripts."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "alembic")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        # No migrations on disk, or the script tree is misconfigured — there is
        # no head to compare against, so we cannot vouch for the schema.
        raise SchemaBehindHeadError(
            "alembic has no head revision — the migration scripts are missing or "
            "misconfigured (checked alembic.ini / the `alembic` script tree)."
        )
    return head


def assert_schema_at_head() -> None:
    """Assert the configured DB is migrated to the alembic head, or fail loud.

    Resolves the connection URL through ``db_config.database_url()`` (so an unset
    ``SIDEQUEST_DATABASE_URL`` raises the SAME ``MissingDatabaseUrlError`` ADR-115
    already defines — one contract, not a parallel one). Logs the current and
    head revisions, then raises :class:`SchemaBehindHeadError` if they differ.

    Returns ``None`` when the schema is at head.
    """
    conninfo = database_url()
    head = _head_revision()
    current = _current_revision(conninfo)

    if current == head:
        logger.info("schema check OK: db at alembic head %s", head)
        return

    current_label = current if current is not None else "(unmigrated — no alembic_version)"
    logger.error(
        "schema check FAILED: db at %s, alembic head is %s — the schema is behind "
        "head and would explode on the first write to an unmigrated table",
        current_label,
        head,
    )
    raise SchemaBehindHeadError(
        f"Postgres schema is behind alembic head: db is at revision "
        f"{current_label}, but head is {head}. The server will not boot on a "
        f"stale schema (a behind-head DB is a silent landmine — ADR-115 / No "
        f"Silent Fallbacks). Fix it with an explicit migration: `alembic upgrade "
        f"head` (or `just pg-up` for a fresh local DB)."
    )
