"""Story 158-17 — big_bad signature resolution for per-expansion quests (RED).

The per-expansion quest engine seeds three signature kinds: ``reach_deep``,
``set_piece``, ``big_bad``. ``reach_deep`` + ``set_piece`` resolve end-to-end
(covered by ``test_expansion_quest_resolve.py`` / ``test_expansion_quest_e2e.py``).
``big_bad`` was DEFERRED at feature build — see the e2e docstring: *"The test
uses reach_deep signature; big_bad is deferred per spec."* It is wired DEAD:

  AC1 (collection) — ``resolve_expansion_quests()`` is called from the turn
    handshake (``websocket_session_handler.py`` ~line 1569) AND the frontier
    observer (``expansion_quest.make_expansion_quest_observer``) with a
    HARDCODED ``defeated_npc_names=set()``. The big_bad branch in
    ``_beat_fired`` (``ref in defeated_npc_names``) can therefore never fire —
    nobody collects the names of NPCs defeated this turn. There is no
    collector seam at all.

  AC2 (name parity) — ``select_signature`` binds the quest ``ref_id`` to the
    manifest big_bad name via only ``.strip()``. But the Monster-Manual inject
    mints that same NPC into ``snapshot.npcs`` through
    ``sanitize_display_name()`` (``monster_manual_inject.py``). A bracket-bearing
    big_bad name therefore DIVERGES between the seeded ``ref_id`` and the minted
    actor name, so ``ref in defeated_npc_names`` can never match even once a
    collector exists.

  AC4 (OTEL) — ``quest_resolved_span`` carries no ``ref_id``, so a resolved
    big_bad quest is not GM-panel-verifiable by which antagonist closed it.

These tests pin the 158-17 contract. The new-behavior tests import
``collect_defeated_npc_names`` *locally* so that the not-yet-existing symbol
fails only those tests (RED), while the name-parity, span, and regression
guards still execute. RED until Story 158-17 lands.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.dungeon.expansion_quest import resolve_expansion_quests
from sidequest.dungeon.persistence import ComplicationThread, DungeonStore
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.session import GameSnapshot, Npc, QuestEntry

# ---------------------------------------------------------------------------
# Helpers (inlined — CLAUDE.md prohibits reaching across test modules into
# underscore-prefixed helpers; mirrors test_expansion_quest_resolve.py style)
# ---------------------------------------------------------------------------


def _store() -> tuple[sqlite3.Connection, DungeonStore]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = DungeonStore(conn)
    s.ensure_schema()
    return conn, s


def _seed_big_bad_thread(store: DungeonStore, exp_id: int, ref: str, region: str) -> None:
    store.open_thread(
        ComplicationThread(
            thread_id=f"q.exp{exp_id}.bb",
            origin_region_id=region,
            kind="quest",
            status="open",
            started_at_depth_score=10.0,
            payload={
                "scope": "expansion",
                "expansion_id": exp_id,
                "signature_kind": "big_bad",
                "ref_id": ref,
                "anchor_region": region,
                "title": "t",
                "objective": "o",
            },
        )
    )


def _npc(name: str, *, hp_current: int, hp_max: int = 8) -> Npc:
    """An NPC with an explicit HP pool. ``hp_current == 0`` == defeated."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="a creature",
            personality="hostile",
            hp=HpPool(current=hp_current, max=hp_max, base_max=hp_max),
        )
    )


def _snap() -> GameSnapshot:
    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _capture() -> tuple[InMemorySpanExporter, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


# ---------------------------------------------------------------------------
# AC1 — the defeated-NPC collector seam (the input the handshake must build
# instead of passing set()).
# ---------------------------------------------------------------------------


def test_collect_defeated_npc_names_returns_only_zero_hp_npcs() -> None:
    """The collector returns the names of NPCs defeated (HP depleted to 0)
    this turn — and ONLY those. This is the seam the turn handshake must call
    to feed ``resolve_expansion_quests`` instead of the hardcoded empty set."""
    from sidequest.dungeon.expansion_quest import (  # noqa: PLC0415  (RED: not yet defined)
        collect_defeated_npc_names,
    )

    snap = _snap()
    snap.npcs.append(_npc("Gormath the Drowned", hp_current=0))  # slain big_bad
    snap.npcs.append(_npc("Brecca Half-Hand", hp_current=8))  # untouched ally

    names = collect_defeated_npc_names(snap)

    assert names == {"Gormath the Drowned"}, (
        "collect_defeated_npc_names must return ONLY the names of NPCs at 0 HP; "
        f"got {names!r}"
    )


def test_collect_defeated_npc_names_empty_when_all_alive() -> None:
    """A wounded-but-alive NPC (HP > 0) is NOT defeated — the collector must
    not over-report, or unrelated quests would resolve on a near-miss."""
    from sidequest.dungeon.expansion_quest import (  # noqa: PLC0415  (RED: not yet defined)
        collect_defeated_npc_names,
    )

    snap = _snap()
    snap.npcs.append(_npc("Brecca Half-Hand", hp_current=8))
    snap.npcs.append(_npc("Ondre Drumhand", hp_current=1))  # bloodied, still up

    assert collect_defeated_npc_names(snap) == set()


# ---------------------------------------------------------------------------
# AC2 — seed-time ref_id must match the mint-time sanitized actor name.
# ---------------------------------------------------------------------------


def test_big_bad_ref_id_matches_mint_sanitized_name() -> None:
    """``select_signature`` binds ``ref_id`` to the big_bad name, but the
    Monster-Manual inject mints that NPC through ``sanitize_display_name``.
    A bracket-bearing name diverges between the two today (select uses only
    ``.strip()``), so the defeated-name comparison can never match. The seed
    must sanitize identically."""
    from sidequest.dungeon.expansion_quest import select_signature
    from sidequest.dungeon.region_graph.model import Expansion, RegionNode
    from sidequest.dungeon.themes import ExpansionQuestTemplate
    from sidequest.game.cookbook.models import RegionContentManifest
    from sidequest.genre.names.generator import sanitize_display_name

    raw = "Gormath the Drowned (the boss)"  # bracket junk a cached Manual can hold
    expected = sanitize_display_name(raw)  # -> "Gormath the Drowned"
    assert expected != raw, "fixture invalid: pick a name the sanitizer actually changes"

    def _manifest(big_bad: Any) -> RegionContentManifest:
        return RegionContentManifest(
            race="undead",
            cr_band="mid",
            size_budget={},
            wandering_table=[],
            loot_table=[],
            special_rooms=[],
            big_bad=big_bad,
        )

    exp = Expansion(
        expansion_id=1,
        new_nodes=[
            RegionNode(id="exp001.r0", expansion_id=1, theme="drowned_cavern", depth_score=10.0),
            RegionNode(id="exp001.r1", expansion_id=1, theme="drowned_cavern", depth_score=30.0),
        ],
        new_edges=[],
    )
    tpl = ExpansionQuestTemplate(
        signature="big_bad", title="The {theme} Stirs", objective="End the {big_bad}."
    )

    b = select_signature(
        expansion=exp,
        manifests_by_region={"exp001.r0": _manifest(None), "exp001.r1": _manifest({"name": raw})},
        template=tpl,
    )

    assert b.kind == "big_bad"
    assert "(" not in b.ref_id and ")" not in b.ref_id, (
        f"big_bad ref_id still carries bracket junk ({b.ref_id!r}); it must be "
        "sanitized the same way monster_manual_inject mints the actor name, or the "
        "defeated-name comparison can never match"
    )
    assert b.ref_id == expected, (
        f"seeded big_bad ref_id {b.ref_id!r} != mint-sanitized name {expected!r}; "
        "the quest can never resolve when the player kills the minted NPC"
    )
    assert b.objective == f"End the {expected}.", (
        "the player-facing objective must also use the clean name, not bracket junk"
    )


# ---------------------------------------------------------------------------
# AC1 + AC3 — composed: collect defeated names -> resolve completes the quest.
# This is the mandatory wiring test for the two new pieces (collector +
# resolver consuming it) on a REAL DungeonStore.
# ---------------------------------------------------------------------------


def test_big_bad_quest_resolves_when_defeated_name_collected() -> None:
    """End-to-end on a real store: a big_bad quest whose ref_id names an NPC
    that the collector reports as defeated this turn flips to ``completed`` and
    its ledger thread resolves — mirroring the working reach_deep path."""
    from sidequest.dungeon.expansion_quest import (  # noqa: PLC0415  (RED: not yet defined)
        collect_defeated_npc_names,
    )

    conn, store = _store()
    name = "Gormath the Drowned"
    _seed_big_bad_thread(store, 1, name, "exp001.r1")
    conn.commit()

    snap = _snap()
    snap.quest_log["dungeon:exp1"] = QuestEntry(
        title="The Drowned Cavern Stirs",
        objective=f"End the {name}.",
        status="active",
        anchor_id="exp001.r1",
    )
    snap.npcs.append(_npc(name, hp_current=0))  # the slain big_bad
    snap.npcs.append(_npc("A Bystander", hp_current=5))  # untouched

    defeated = collect_defeated_npc_names(snap)
    n = resolve_expansion_quests(
        snapshot=snap,
        store=store,
        reached_region_ids=set(),
        resolved_trope_ids=[],
        defeated_npc_names=defeated,
    )
    conn.commit()

    assert n == 1, f"expected the big_bad quest to resolve, got {n}"
    assert snap.quest_log["dungeon:exp1"].status == "completed", (
        "quest_log entry not flipped to 'completed' after the big_bad was defeated"
    )
    assert store.open_threads() == [], "ledger thread still open after big_bad resolution"


# ---------------------------------------------------------------------------
# AC4 — the dungeon.quest.resolved span carries ref_id (and the existing
# resolving_event / signature_kind) so the GM panel can attribute the close.
# ---------------------------------------------------------------------------


def test_big_bad_resolved_span_carries_ref_id() -> None:
    """The resolved span must carry the big_bad ``ref_id`` so the GM panel can
    verify WHICH antagonist closed the quest — not just that one did."""
    import sidequest.telemetry.spans as _spans_module
    from sidequest.telemetry.spans.dungeon_quest import SPAN_QUEST_RESOLVED

    conn, store = _store()
    name = "Bone Tyrant"
    _seed_big_bad_thread(store, 2, name, "exp002.r1")
    conn.commit()

    snap = _snap()
    snap.quest_log["dungeon:exp2"] = QuestEntry(
        title="t", objective="o", status="active", anchor_id="exp002.r1"
    )

    exporter, tracer = _capture()
    original = _spans_module.tracer
    _spans_module.tracer = lambda: tracer  # type: ignore[assignment]
    try:
        resolve_expansion_quests(
            snapshot=snap,
            store=store,
            reached_region_ids=set(),
            resolved_trope_ids=[],
            defeated_npc_names={name},
        )
    finally:
        _spans_module.tracer = original  # type: ignore[assignment]
    conn.commit()

    resolved = [s for s in exporter.get_finished_spans() if s.name == SPAN_QUEST_RESOLVED]
    assert resolved, "dungeon.quest.resolved span NOT emitted for the big_bad resolution"
    attrs = resolved[0].attributes or {}
    assert attrs.get("signature_kind") == "big_bad"
    assert attrs.get("resolving_event") == "hp_depletion"
    assert attrs.get("ref_id") == name, (
        f"resolved span ref_id={attrs.get('ref_id')!r}, expected {name!r}; the GM "
        "panel cannot verify which antagonist closed the quest without ref_id"
    )


# ---------------------------------------------------------------------------
# AC5 — guards: the fix must neither over-resolve nor regress reach_deep.
# (These drive resolve_expansion_quests directly — no collector — so they are
# green today and must stay green after the fix.)
# ---------------------------------------------------------------------------


def test_big_bad_does_not_resolve_when_other_npc_defeated() -> None:
    """A big_bad quest must NOT resolve when some OTHER NPC is defeated — only
    its named antagonist closes it. Guards against an over-eager collector
    resolving every big_bad quest on any kill."""
    conn, store = _store()
    _seed_big_bad_thread(store, 1, "Gormath the Drowned", "exp001.r1")
    conn.commit()

    snap = _snap()
    snap.quest_log["dungeon:exp1"] = QuestEntry(
        title="t", objective="o", status="active", anchor_id="exp001.r1"
    )

    n = resolve_expansion_quests(
        snapshot=snap,
        store=store,
        reached_region_ids=set(),
        resolved_trope_ids=[],
        defeated_npc_names={"Some Other Mook"},
    )

    assert n == 0
    assert snap.quest_log["dungeon:exp1"].status == "active"
    assert len(store.open_threads()) == 1


def test_reach_deep_unaffected_by_populated_defeated_names() -> None:
    """A reach_deep quest still resolves on arrival even when defeated names
    are present this turn — the new big_bad input must not perturb the
    existing signature paths."""
    conn, store = _store()
    store.open_thread(
        ComplicationThread(
            thread_id="q.exp1.rd",
            origin_region_id="exp001.r3",
            kind="quest",
            status="open",
            started_at_depth_score=10.0,
            payload={
                "scope": "expansion",
                "expansion_id": 1,
                "signature_kind": "reach_deep",
                "ref_id": "exp001.r3",
                "anchor_region": "exp001.r3",
                "title": "t",
                "objective": "o",
            },
        )
    )
    conn.commit()

    snap = _snap()
    snap.quest_log["dungeon:exp1"] = QuestEntry(
        title="t", objective="o", status="active", anchor_id="exp001.r3"
    )

    n = resolve_expansion_quests(
        snapshot=snap,
        store=store,
        reached_region_ids={"exp001.r3"},
        resolved_trope_ids=[],
        defeated_npc_names={"Gormath the Drowned"},  # noise — must not interfere
    )

    assert n == 1
    assert snap.quest_log["dungeon:exp1"].status == "completed"
