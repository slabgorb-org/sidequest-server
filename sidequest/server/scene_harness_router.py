"""Scene-harness routes (ADR-092).

``POST /dev/scene/{name}`` — hydrate a fixture into a playable save.
``GET /dev/scenes`` — list available fixture metadata for the UI picker.

Always registered — Cloudflare Zero Trust gates access at the tunnel layer.

Wires together the existing pieces:

* :func:`sidequest.game.scene_harness.hydrate_fixture` — YAML → GameSnapshot
* :func:`sidequest.game.game_slug.generate_slug` — mints a fresh slug
* :class:`sidequest.game.persistence.SqliteStore` — persists the snapshot
* :func:`sidequest.game.persistence.upsert_game` — registers the games row
* :func:`sidequest.telemetry.watcher_hub.publish_event` — emits OTEL spans
  for the GM panel (CLAUDE.md OTEL Observability Principle)

Span vocabulary (asserted by ``tests/server/test_scene_harness.py``):

* ``scene_harness.intent.load`` — fixture name + slug, fires before hydration
* ``scene_harness.hydrate.ok`` — field counts (npcs, characters) on success
* ``scene_harness.hydrate.error`` — fixture name + error class on failure
* ``scene_harness.persist.ok`` — slug + save path after commit
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date_cls
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request

from sidequest.game.game_slug import generate_slug
from sidequest.game.persistence import (
    GameMode,
    SqliteStore,
    db_path_for_slug,
    upsert_game,
)
from sidequest.game.scene_harness import (
    FixtureNotFoundError,
    FixtureValidationError,
    hydrate_fixture,
)
from sidequest.telemetry import watcher_hub as _hub

_FIXTURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

logger = logging.getLogger(__name__)


def create_scene_harness_router() -> APIRouter:
    """Build the dev-gated scene-harness router.

    Handlers pull config from ``request.app.state``:

    * ``save_dir`` — SQLite root, set by ``create_app``.
    * ``fixtures_dir`` — directory containing fixture YAMLs, set by
      ``create_app`` from ``SIDEQUEST_FIXTURES_DIR`` (with a cwd-relative
      fallback to ``scenarios/fixtures``).
    * ``today_fn`` — injectable clock, defaults to ``date.today``.
    """
    router = APIRouter()

    @router.post("/dev/scene/{name}")
    async def load_scene(name: str, request: Request) -> dict[str, str]:
        save_dir: Path = request.app.state.save_dir
        fixtures_dir: Path = request.app.state.fixtures_dir
        today_fn = getattr(request.app.state, "today_fn", _date_cls.today)

        # Intent span first — even before hydration. The GM panel must see
        # the request landed regardless of whether it succeeded; an
        # absent intent span on a known-issued POST is itself a signal.
        _hub.publish_event(
            "scene_harness.intent.load",
            {"fixture_name": name, "fixtures_dir": str(fixtures_dir)},
            component="scene_harness",
        )

        try:
            snapshot = hydrate_fixture(name=name, fixtures_dir=fixtures_dir)
        except FixtureNotFoundError as exc:
            _hub.publish_event(
                "scene_harness.hydrate.error",
                {
                    "fixture_name": name,
                    "error_class": "FixtureNotFoundError",
                    "message": str(exc),
                },
                component="scene_harness",
                severity="warning",
            )
            logger.warning("scene_harness.fixture_not_found name=%s err=%s", name, exc)
            raise HTTPException(
                status_code=404,
                detail={
                    "fixture_name": name,
                    "message": str(exc),
                },
            ) from exc
        except FixtureValidationError as exc:
            _hub.publish_event(
                "scene_harness.hydrate.error",
                {
                    "fixture_name": name,
                    "error_class": "FixtureValidationError",
                    "message": str(exc),
                },
                component="scene_harness",
                severity="warning",
            )
            logger.warning("scene_harness.fixture_invalid name=%s err=%s", name, exc)
            raise HTTPException(
                status_code=422,
                detail={
                    "fixture_name": name,
                    "field": "genre"
                    if "genre" in str(exc).lower()
                    else "world"
                    if "world" in str(exc).lower()
                    else "snapshot",
                    "message": str(exc),
                },
            ) from exc

        _hub.publish_event(
            "scene_harness.hydrate.ok",
            {
                "fixture_name": name,
                "genre_slug": snapshot.genre_slug,
                "world_slug": snapshot.world_slug,
                "character_count": len(snapshot.characters),
                "npc_count": len(snapshot.npcs),
            },
            component="scene_harness",
        )

        # Slug minting reuses the production helper. Every fixture load
        # mints a fresh slug — never collide with a "real" save.
        slug = generate_slug(
            world_slug=snapshot.world_slug,
            today=today_fn(),
            mode=GameMode.SOLO,
        )
        # Disambiguate against same-day-same-world collisions so two
        # scene-harness loads in one session don't overwrite each other.
        slug = _disambiguate(save_dir, slug)

        db = db_path_for_slug(save_dir, slug)
        db.parent.mkdir(parents=True, exist_ok=True)

        store = SqliteStore(db)
        store.initialize()
        store.init_session(snapshot.genre_slug, snapshot.world_slug)
        store.save(snapshot)
        upsert_game(
            store,
            slug=slug,
            mode=GameMode.SOLO,
            genre_slug=snapshot.genre_slug,
            world_slug=snapshot.world_slug,
        )

        # ADR-115 D1: also register the session in Postgres so the pg
        # repositories are available when the connect handler resumes
        # from this slug.  The SQLite write above is kept intact (D2 will
        # migrate room.bind_world; D3-D7 migrate remaining consumers).
        from sidequest.game import db_pool as _db_pool
        from sidequest.server.session_state import _build_pg_repos_for_slug

        _build_pg_repos_for_slug(
            _db_pool.get_pool(),
            slug=slug,
            mode="solo",
            genre_slug=snapshot.genre_slug,
            world_slug=snapshot.world_slug,
        )

        _hub.publish_event(
            "scene_harness.persist.ok",
            {
                "fixture_name": name,
                "game_slug": slug,
                "save_path": str(db),
            },
            component="scene_harness",
        )

        return {"slug": slug}

    @router.get("/dev/scenes")
    async def list_scenes(request: Request) -> list[dict[str, str | None]]:
        fixtures_dir: Path = request.app.state.fixtures_dir
        results: list[dict[str, str | None]] = []

        if fixtures_dir.is_dir():
            for yaml_path in sorted(fixtures_dir.glob("*.yaml")):
                stem = yaml_path.stem
                if not _FIXTURE_NAME_RE.match(stem):
                    continue
                try:
                    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning("scene_harness.list.skip_invalid path=%s", yaml_path)
                    continue
                if not isinstance(raw, dict) or "genre" not in raw or "world" not in raw:
                    continue
                results.append(
                    {
                        "name": stem,
                        "genre": raw["genre"],
                        "world": raw["world"],
                        "description": raw.get("description"),
                    }
                )

        _hub.publish_event(
            "scene_harness.list",
            {"fixture_count": len(results), "fixtures_dir": str(fixtures_dir)},
            component="scene_harness",
        )
        return results

    return router


_MAX_DISAMBIGUATE_ATTEMPTS = 1000


def _disambiguate(save_dir: Path, base_slug: str) -> str:
    """Pick the next free slug under ``save_dir`` by appending ``-2``, ``-3``...

    Same-day same-world scene loads must not silently overwrite each
    other; the scene harness is for iteration and devs reload the
    same fixture many times in a session.

    Bounded at :data:`_MAX_DISAMBIGUATE_ATTEMPTS` so a pathological save
    directory (or a misconfigured tests that points the harness at a
    populated production save tree) fails loudly per CLAUDE.md
    "No Silent Fallbacks" rather than spinning an O(n) filesystem scan.
    """
    candidate = base_slug
    n = 1
    while db_path_for_slug(save_dir, candidate).exists():
        n += 1
        if n > _MAX_DISAMBIGUATE_ATTEMPTS:
            raise RuntimeError(
                f"scene_harness._disambiguate: {n - 1} consecutive same-day same-world "
                f"saves already exist under {save_dir!s} for base_slug={base_slug!r}. "
                "Either prune old scene-harness saves or rename the fixture."
            )
        candidate = f"{base_slug}-{n}"
    return candidate
