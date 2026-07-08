"""Per-site dungeon storage keying — two sites in one session must not collide
(Track B, Task 2 — story 164-1).

RED-phase tests. Until Dev adds ``DEFAULT_SITE_ID`` and threads a keyword-only
``site_id`` through ``PgDungeonRepository`` (+ the ``0003_dungeon_site_id``
migration re-keying the dungeon tables), these fail:
  * at import with ``ImportError: cannot import name 'DEFAULT_SITE_ID'`` (all three), and
  * (with a DB) ``commit_expansion() got an unexpected keyword argument 'site_id'``.

PLACEMENT DEVIATION (logged in the session): the plan specifies
``tests/game/pg/test_dungeon_site_keying.py`` and a ``pg_dungeon_repo`` fixture,
but neither ``tests/game/pg/conftest.py`` nor that fixture exist. The real
Postgres dungeon fixtures — the session-scoped ``migrated_db`` and the
``build_pg_dungeon_repo(monkeypatch, migrated_db) -> (pool, repo, sid)`` helper —
live in ``tests/dungeon/conftest.py``. This test sits beside them so the
fixtures resolve.

The ``Expansion`` shape also differs from the plan's sketch: the real dataclass
is ``sidequest.dungeon.region_graph.model.Expansion(expansion_id, new_nodes,
new_edges)`` — NOT ``materializer.Expansion(region_ids=...)``. Adapted per the
plan's own instruction ("read the dataclass and adapt — do not invent fields").

DB REQUIREMENT: the two-site tests need a live Postgres
(``SIDEQUEST_TEST_DATABASE_URL``; ``just pg-up``). Without it they SKIP loudly
via the ``migrated_db`` fixture. The DB-independent
``test_default_site_id_is_frontier`` still flips RED->GREEN on the constant
alone, so RED is verifiable even without Postgres (the module-level import fails
at collection today).
"""
from __future__ import annotations

from typing import Any

from sidequest.dungeon.region_graph.model import Expansion, RegionGraph, RegionNode
from sidequest.game.pg.dungeon import DEFAULT_SITE_ID


def _node(nid: str) -> RegionNode:
    return RegionNode(id=nid, expansion_id=0, theme="stone", depth_score=0.0)


def test_default_site_id_is_frontier() -> None:
    # AC-9 anchor: Sünden's legacy dungeon lands under this key after migration,
    # so every un-keyed write stays backward-compatible with the existing path.
    assert DEFAULT_SITE_ID == "frontier"


async def test_two_sites_do_not_collide(monkeypatch: Any, migrated_db: str) -> None:
    """AC-8: two sites keyed independently in one session — no collision.

    Commit one node into site ``gilded_boar`` and a DIFFERENT node into site
    ``frontier``; each site's ``load_map`` must return ONLY its own node.
    Without ``site_id`` keying, ``load_map`` selects every ``session_id`` row and
    both nodes leak into each site's map — this test would then see two nodes.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    n_boar = _node("gilded_boar:entrance")
    g_boar = RegionGraph(entrance_id="gilded_boar:entrance")
    g_boar.add_node(n_boar)
    repo.commit_expansion(
        Expansion(expansion_id=0, new_nodes=[n_boar], new_edges=[]),
        g_boar,
        site_id="gilded_boar",
    )

    n_deep = _node("frontier:entrance")
    g_deep = RegionGraph(entrance_id="frontier:entrance")
    g_deep.add_node(n_deep)
    repo.commit_expansion(
        Expansion(expansion_id=0, new_nodes=[n_deep], new_edges=[]),
        g_deep,
        site_id="frontier",
    )

    boar = repo.load_map(entrance_id="gilded_boar:entrance", site_id="gilded_boar")
    deep = repo.load_map(entrance_id="frontier:entrance", site_id="frontier")
    assert set(boar.nodes) == {"gilded_boar:entrance"}
    assert set(deep.nodes) == {"frontier:entrance"}


async def test_default_key_write_isolated_from_new_site(
    monkeypatch: Any, migrated_db: str
) -> None:
    """AC-9: a commit under the implicit DEFAULT_SITE_ID (the legacy path — no
    ``site_id`` kwarg) is invisible to an explicitly-keyed new site, and a
    default-keyed ``load_map`` returns only the legacy node. Proves the default
    key is a real partition, not a shared bucket the new sites spill into.
    """
    from tests.dungeon.conftest import build_pg_dungeon_repo

    _pool, repo, _sid = build_pg_dungeon_repo(monkeypatch, migrated_db)

    # Legacy write: NO site_id kwarg -> lands under DEFAULT_SITE_ID ("frontier").
    n_legacy = _node("frontier:entrance")
    g_legacy = RegionGraph(entrance_id="frontier:entrance")
    g_legacy.add_node(n_legacy)
    repo.commit_expansion(
        Expansion(expansion_id=0, new_nodes=[n_legacy], new_edges=[]), g_legacy
    )

    # A new, explicitly-keyed site in the same session.
    n_new = _node("gilded_boar:entrance")
    g_new = RegionGraph(entrance_id="gilded_boar:entrance")
    g_new.add_node(n_new)
    repo.commit_expansion(
        Expansion(expansion_id=0, new_nodes=[n_new], new_edges=[]),
        g_new,
        site_id="gilded_boar",
    )

    # The default-keyed load sees ONLY the legacy node, not the new site's node.
    legacy = repo.load_map(entrance_id="frontier:entrance")
    assert set(legacy.nodes) == {"frontier:entrance"}
