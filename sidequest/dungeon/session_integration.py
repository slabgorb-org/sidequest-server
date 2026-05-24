"""Beneath Sünden Plan 7 session-integration — the live wiring seam.

The only new production seam (spec Decision 5 / Approach A). Two
functions called from exactly two one-line incisions in the WS session
lifecycle: register the merged look-ahead worker for the session's life,
and bootstrap the Seed=Expansion-0 dungeon on the first open of a
campaign. All dungeon/bootstrap/dep-resolution complexity is isolated
here so the hot session subsystem stays thin.

No Silent Fallbacks: every unresolved dep raises loudly; the genre/world
gate returns None (a clean no-op) only for worlds this dungeon does not
apply to.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from sidequest.agents.llm_factory import build_llm_client
from sidequest.dungeon.lookahead_worker import (
    LookaheadWorkerHandle,
    register_lookahead_worker,
)
from sidequest.dungeon.materializer import materialize
from sidequest.dungeon.persistence import DungeonStore
from sidequest.dungeon.seed_bootstrap import (
    ENTRANCE_ID,
    build_entrance_seed_graph,
    build_expansion_one_request,
    select_entrance_theme_id,
)
from sidequest.dungeon.themes import load_theme_palette
from sidequest.game.cookbook.loader import load_cookbook
from sidequest.telemetry.spans import dungeon_attach_span

__all__ = [
    "attach_dungeon_to_session",
    "detach_dungeon_from_session",
]

_GENRE = "caverns_and_claudes"
_WORLD = "beneath_sunden"
# 63-bit seed: positive, fits a SQLite INTEGER, ample entropy.
_SEED_BITS = 63

# §14.D cross-session double-register guard. register_lookahead_worker
# builds a NEW handle -> NEW bound _observer each call, so frontier_hook's
# identity-dedup does NOT hold across sessions: two concurrent sessions on
# one save would double-register and double-materialize. The hard
# constraint forbids touching lookahead_worker.py/frontier_hook.py, so the
# guard lives here, in the seam we own — keyed by save identity. Concurrent
# attach for an already-attached save is a contract violation, not a silent
# upsert (No Silent Fallbacks). The real playgroup runs ONE shared session
# per save (submit-and-wait); sequential reopen clears the key in detach.
_ATTACHED_SAVES: dict[str, LookaheadWorkerHandle] = {}


def _save_key(conn: Any) -> str:
    """Stable per-save identity. Real saves: the sqlite main DB file path
    (two WS sessions on one save file open distinct connections to the
    SAME path -> same key -> guard fires). In-memory stores have no file
    -> fall back to the connection object's id (each in-memory store is a
    distinct connection, never sharing a file -> no false collision).
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception as exc:  # pragma: no cover - sqlite always supports this
        raise RuntimeError(
            f"could not resolve save identity for the dungeon attach guard: {exc}"
        ) from exc
    # PRAGMA database_list row: (seq, name, file). file is '' for :memory:.
    db_file = row[2] if row is not None and len(row) >= 3 else ""
    return db_file if db_file else f"mem:{id(conn)}"


def _theme_pack_root(world_dir: Path) -> Path:
    """The genre-pack dir holding ``themes/`` (Plan 4 layout
    ``genre_packs/<genre>/themes/``). ``world_dir`` is
    ``…/genre_packs/<genre>/worlds/<world>`` → parents[1] is the pack
    root. Verified loud by load_theme_palette (raises if themes/ absent).
    """
    return world_dir.parent.parent


async def attach_dungeon_to_session(
    *,
    store: Any,
    snapshot: Any,
    genre_pack: Any,
    genre_slug: str,
    world_slug: str,
    world_dir: Path,
) -> LookaheadWorkerHandle | None:
    """Register the look-ahead worker for this session; bootstrap the
    seed on a fresh campaign. Returns the handle (held by the session for
    teardown), or ``None`` for any non-beneath_sunden session (clean
    no-op — the gate lives here so the call site is unconditional).

    Every path emits exactly one routed ``dungeon.attach`` event so the
    GM panel can tell a legitimate skip from a misfire — the formerly
    silent ``return None`` here is what produced the 2026-05-17 playtest
    misdiagnosis (a live, fully-materialized dungeon read as "the gate
    skipped / dungeon never bootstrapped")."""
    with dungeon_attach_span(genre_slug=genre_slug, world_slug=world_slug) as _span:
        if genre_slug != _GENRE or world_slug != _WORLD:
            _span.set_attribute("outcome", "skipped_other_world")
            _span.set_attribute(
                "reason",
                f"not this dungeon's world (saw genre={genre_slug!r} "
                f"world={world_slug!r}; this dungeon is {_GENRE}/{_WORLD})",
            )
            return None

        conn = store.connection()
        save_key = _save_key(conn)
        if save_key in _ATTACHED_SAVES:
            # IDEMPOTENT re-attach (was a hard RuntimeError — it crashed
            # the connect for every MP join/reconnect). The deterministic
            # MP URL means N players + reconnects all connect to ONE save;
            # the original guard assumed strictly sequential reopen with a
            # detach between, which MP drop-in violates by design. The
            # seam's own docstring already calls it "idempotent" — the
            # raise contradicted that. Return the EXISTING handle: the
            # look-ahead worker is registered exactly once (no
            # double-register, no double-materialize — we return before
            # both), and additional sockets share the live session's one
            # worker. Loud, not silent: the span carries
            # outcome=already_attached so the GM panel sees the re-attach
            # (No Silent Fallbacks — observable, not a swallowed upsert).
            # KNOWN follow-up (post-game, not refcounted under fire):
            # detach is first-socket-wins; if a player leaves mid-session
            # the shared worker drains for the rest. Acceptable for the
            # submit-and-wait playgroup; refcounted teardown is the
            # proper fix and is tracked in the pingpong.
            existing = _ATTACHED_SAVES[save_key]
            _span.set_attribute("outcome", "already_attached")
            _span.set_attribute(
                "reason",
                f"save {save_key!r} already has a live look-ahead worker "
                "(MP join / reconnect on the shared save) — returning the "
                "existing handle idempotently",
            )
            _span.set_attribute(
                "regions",
                len(DungeonStore(conn).load_map(entrance_id=ENTRANCE_ID).nodes),
            )
            return existing
        persistence = DungeonStore(conn)
        persistence.ensure_schema()  # outside any txn (executescript implicit COMMIT)

        bundle = load_cookbook(world_dir)
        palette = load_theme_palette(_theme_pack_root(world_dir))
        claude_client = build_llm_client(purpose="tool")

        # Save-is-truth: reuse a frozen seed; only generate+persist on a
        # genuinely fresh save (a prior failed bootstrap left the seed but
        # no map → reuse it so the retry is deterministic).
        campaign_seed = persistence.get_campaign_seed()
        if campaign_seed is None:
            campaign_seed = secrets.randbits(_SEED_BITS)
            persistence.set_campaign_seed(campaign_seed)
            conn.commit()

        already_seeded = bool(persistence.load_map(entrance_id="entrance").nodes)
        if not already_seeded:
            entrance_theme = select_entrance_theme_id(palette)
            seed_graph = build_entrance_seed_graph(entrance_theme)
            request = build_expansion_one_request(
                campaign_seed=campaign_seed,
                # Story 55-1: thread the live slugs so the
                # materializer's post-commit YAML emit can resolve
                # <pack_root>/worlds/<world>.
                genre_slug=genre_slug,
                world_slug=world_slug,
            )
            # The merged commit stage seeds Expansion 0 (entrance) before
            # expansion 1 and rolls back on PersistError (Seed=Expansion-0,
            # spec §6). A bootstrap failure raises loudly here — the
            # connect handler must not start a beneath_sunden session with
            # a broken dungeon (No Silent Fallbacks, spec §9).
            await materialize(
                request,
                graph=seed_graph,
                bundle=bundle,
                palette=palette,
                persistence=persistence,
                snapshot=snapshot,
                pack_tropes=genre_pack,
                claude_client=claude_client,
            )
            _span.set_attribute("outcome", "bootstrapped")
        else:
            _span.set_attribute("outcome", "already_seeded")
        _span.set_attribute(
            "regions",
            len(persistence.load_map(entrance_id=ENTRANCE_ID).nodes),
        )

        # Bind the session's region position to the REAL materialized
        # dungeon entrance. Without this the procedural regions
        # (entrance / exp00N.rN) live only in dungeon_map and are never
        # bound to snap.current_region — region_init only handles static
        # world.yaml cartography, so beneath_sunden's location never
        # advances off "" and the narrator improvises geography (playtest
        # 2026-05-17). Save-is-truth: only bind on a session that has no
        # real region yet (fresh campaign); a resumed save keeps its
        # frozen position. dedup-append mirrors region_init /
        # frontier_hook discovered_regions semantics.
        if not snapshot.current_region:
            snapshot.current_region = ENTRANCE_ID
            if ENTRANCE_ID not in snapshot.discovered_regions:
                snapshot.discovered_regions.append(ENTRANCE_ID)
            _span.set_attribute("bound_current_region", ENTRANCE_ID)

        handle = register_lookahead_worker(
            persistence=persistence,
            bundle=bundle,
            palette=palette,
            pack_tropes=genre_pack,
            claude_client=claude_client,
            campaign_seed=campaign_seed,
            # Story 55-1: thread the session slugs so the
            # materializer's post-commit YAML emit can resolve
            # <pack_root>/worlds/<world>.
            genre_slug=genre_slug,
            world_slug=world_slug,
        )
        # Claim the save AFTER a successful register: a bootstrap/register
        # failure must leave no key behind (a later retry must be able to
        # attach). save-is-truth.
        _ATTACHED_SAVES[save_key] = handle
        return handle


async def detach_dungeon_from_session(
    handle: LookaheadWorkerHandle | None,
) -> None:
    """Teardown: unregister the observer and drain in-flight look-ahead
    tasks. Null-safe and unconditional-call-safe (handle is None for
    non-beneath_sunden sessions). Does NOT close the connection — the
    room owns the store lifecycle (spec §8 / dossier §9)."""
    if handle is None:
        return
    # Clear the §14.D save claim (reverse-lookup by handle identity — the
    # registry holds exactly one entry per live save; detach takes only the
    # handle, and LookaheadWorkerHandle is untouchable per the hard
    # constraint, so we cannot stash the key on it).
    for key, claimed in list(_ATTACHED_SAVES.items()):
        if claimed is handle:
            del _ATTACHED_SAVES[key]
    handle.unregister()
    await handle.drain()
