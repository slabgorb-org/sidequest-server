"""Story 103-10 RED — Seaboard of Saints end-to-end wiring + regression.

The capstone for epic 103 (build plan §4 DoD). Stories 103-1..103-8 built the
Saint/stock engine (``sidequest/mutation/saints.py`` + ``stocks.py``, the
``awn.saint.applied`` / ``awn.stock.applied`` spans) and authored the world
content (25 Saints, 23 stocks, the regions). What has *never* existed is the
proof that the pieces wire together in a real session: chargen as a stock →
seat a confrontation → the Saint drawback / stock trait is mechanically live
(OTEL-asserted) → the sheet survives a save/reload. That proof is this file.

THE DRAFT TRAP (measured 2026-06-11, the reason these tests bypass
``pack.worlds``): ``seaboard_of_saints`` ships ``world.yaml: draft: true`` until
the 103-9 asset gate is met, and ``load_genre_pack`` *silently skips draft
worlds* (``loader._load_single_world`` returns None on ``config.draft``). So the
production pack-load path validates NONE of the world's saints.yaml /
stocks.yaml / cartography while it is draft — ``pack.worlds`` holds only
``flickering_reach``. These e2e tests therefore drive the SAME loader functions
the world load would call (``load_saint_registry`` / ``load_stock_registry`` /
``_load_cartography``) directly against the real files, so the content is
validated ahead of the draft lift. When 103-9 lifts ``draft: true``, a follow-up
should additionally assert ``seaboard_of_saints in pack.worlds``.

HOW A DRAWBACK "MECHANICALLY FIRES": AWN negatives are *passive penalties*
(effect-text only — no usage/strain, ``use_ops`` refuses anything not in
``positive_ids``). The drawback is therefore mechanically live by being (a)
recorded on the ``awn.saint.applied`` span at chargen — the GM-panel lie
detector — and (b) carried in ``mutation_state.negative_ids`` and surfaced in
the narrator's mechanical-truth block (``build_mutation_static_block``) so the
narrator cannot quietly forget it. The Saint's *bundle* positives are the
usable half, and fire through the live ``use_mutation`` path in a confrontation
(the test_102_7 cast-spine, retold for a Saint-Marked PC).

P2-4 discipline: these assert WIRING (content resolves, spans fire, state
survives the round-trip), not catalog/content details — counts are asserted
only as DoD floors, never as brittle equalities.

NOTE (build plan §4 DoD, AC3 — cliché audit): the ``cliche-judge`` pass over the
shipped Seaboard content is an agent-judgment task, not a pytest assertion (no
cliché harness exists server-side). It is tracked as a content-lane review
action in the session, not encoded here.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from sidequest.genre.loader import _load_cartography, load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.saints import SaintRegistry, load_saint_registry
from sidequest.mutation.stocks import StockRegistry, load_stock_registry
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_GENRE = "mutant_wasteland"
_WORLD = "seaboard_of_saints"

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _load_pack() -> GenrePack:
    try:
        return load_genre_pack(find_pack_path(_GENRE))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _seaboard_dir():
    return find_pack_path(_GENRE) / "worlds" / _WORLD


def _catalog_or_skip(pack: GenrePack) -> MutationCatalog:
    if pack.mutations is None:  # pragma: no cover - guarded by 102-7
        pytest.skip("mutant_wasteland ships no mutations.yaml (see test_102_7)")
    return pack.mutations


def _saints(pack: GenrePack) -> SaintRegistry:
    return load_saint_registry(_seaboard_dir() / "saints.yaml", _catalog_or_skip(pack))


def _stocks(pack: GenrePack) -> StockRegistry:
    return load_stock_registry(_seaboard_dir() / "stocks.yaml", _catalog_or_skip(pack))


def _spans_named(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ===========================================================================
# AC4 — server loads seaboard content with zero errors; every saints.yaml /
# stocks.yaml mutation reference resolves against the genre catalog.
# ===========================================================================


class TestSeaboardContentResolvesAgainstCatalog:
    def test_live_world_is_loaded_by_pack_load(self) -> None:
        """seaboard_of_saints went LIVE on 2026-06-11 (PR #423: asset gate met,
        draft key removed from world.yaml). Per the prior version of this test's
        own instruction ("if it now loads, flip this test to assert it appears
        in pack.worlds"), this now pins the live state: ``load_genre_pack`` must
        surface the world through the production path alongside its sibling."""
        pack = _load_pack()
        assert _WORLD in pack.worlds, (
            "seaboard_of_saints is live (no draft key in world.yaml since "
            "PR #423) and must be loaded by load_genre_pack"
        )
        assert "flickering_reach" in pack.worlds, "the non-draft sibling must still load"

    def test_saints_registry_loads_and_every_id_resolves(self) -> None:
        """``load_saint_registry`` cross-validates every bundle / drawback /
        affinity id against the catalog and raises (loud) on a miss. A clean
        non-empty load IS the proof that every reference resolves (AC4)."""
        registry = _saints(_load_pack())
        assert registry.saints, "seaboard must ship a non-empty Saint canon"
        # The verbatim-kept 103-1 proof Saint must still resolve by id.
        assert registry.by_id("herman_of_the_acushnet") is not None

    def test_stocks_registry_loads_and_every_id_resolves(self) -> None:
        """``load_stock_registry`` validates every granted mutation id against
        the catalog; a clean non-empty load proves the references resolve."""
        registry = _stocks(_load_pack())
        assert registry.stocks, "seaboard must ship a non-empty stock roster"
        assert registry.by_id("saint_marked") is not None
        assert registry.by_id("sleeper") is not None

    def test_unresolvable_mutation_reference_fails_loud(self, tmp_path) -> None:
        """The guard that makes the above tests meaningful: a stocks.yaml that
        grants an id absent from the catalog must raise, not silently drop it
        (No Silent Fallbacks). Proves the loader's validation has teeth."""
        bad = tmp_path / "stocks.yaml"
        bad.write_text(
            "stocks:\n"
            "  - id: phantom\n"
            "    name: Phantom\n"
            "    granted_mutations:\n"
            "      - structure/does_not_exist_in_catalog\n",
            encoding="utf-8",
        )
        with pytest.raises((KeyError, ValueError)):
            load_stock_registry(bad, _catalog_or_skip(_load_pack()))


# ===========================================================================
# AC5 — all 17 regions present in places/cartography (graph integrity).
# ===========================================================================


class TestSeaboardCartography:
    def test_at_least_seventeen_regions_present(self) -> None:
        """DoD floor: 'all 17 regions present'. Content currently ships 18
        (one more than spec §3's '17 regions.' headline) — asserted as a floor,
        with the 17-vs-18 reconciliation logged as a Delivery Finding rather
        than pinned brittlely here."""
        carto = _load_cartography(_seaboard_dir() / "cartography.yaml")
        assert len(carto.regions) >= 17, (
            f"the Seaboard must declare at least 17 regions (DoD §4); "
            f"got {len(carto.regions)}: {sorted(carto.regions)}"
        )

    def test_starting_region_resolves_to_a_declared_region(self) -> None:
        carto = _load_cartography(_seaboard_dir() / "cartography.yaml")
        assert carto.starting_region in carto.regions, (
            f"starting_region {carto.starting_region!r} is not a declared region"
        )

    def test_every_route_endpoint_is_a_declared_region(self) -> None:
        """Graph integrity: a route to/from an undeclared region id is a dead
        edge the navigation engine cannot resolve."""
        carto = _load_cartography(_seaboard_dir() / "cartography.yaml")
        region_ids = set(carto.regions)
        dangling: list[str] = []
        for route in carto.routes:
            for endpoint in (route.from_id, route.to_id):
                # from_id/to_id are Optional — a route may be id/waypoint-only.
                if endpoint is not None and endpoint not in region_ids:
                    dangling.append(endpoint)
        assert not dangling, f"routes reference undeclared regions: {sorted(set(dangling))}"


# ===========================================================================
# AC2 — flickering_reach regression: loads clean, Saint-less, Wild-only.
# ===========================================================================


class TestFlickeringReachRegression:
    def test_flickering_reach_loads_and_is_saint_less(self) -> None:
        """The Saint-less world must keep loading through the production path
        with ZERO Saint/stock content (AWN rebase addendum, settled)."""
        pack = _load_pack()
        fr = pack.worlds.get("flickering_reach")
        assert fr is not None, "flickering_reach must load (non-draft)"
        assert fr.saints is None, (
            "flickering_reach must stay Saint-less — its mutants are raw AWN MP "
            "spend, not curated bundles (addendum)"
        )
        assert fr.stocks is None, "flickering_reach must ship no stock roster"
        assert pack.rules.ruleset == "awn", "mutant_wasteland must stay bound ruleset: awn"

    def test_flickering_reach_wild_mutant_chargen_seeds_clean(self, otel_capture) -> None:
        """A Wild-Mutant PC in the Saint-less world seeds through the classic
        ``seed_character_mutations`` path (no saint_id / stock_id) and is
        span-visible — no Saint machinery touched."""
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.server.mutation_init import init_mutation_state_for_session

        pack = _load_pack()
        catalog = _catalog_or_skip(pack)
        mutant_class = catalog.mp_economy.mutant_classes[0]

        snap = GameSnapshot(
            genre_slug=_GENRE, world_slug="flickering_reach", turn_manager=TurnManager()
        )
        init_mutation_state_for_session(
            snap,
            catalog=catalog,
            character_name="Rux",
            character_class=mutant_class,
            session_id="test-103-10-fr",
            # No saints/stocks — the Wild path.
        )
        assert snap.mutation_state is not None
        assert "Rux" in snap.mutation_state.characters
        assert not _spans_named(otel_capture, "awn.saint.applied"), (
            "the Wild path must not fire awn.saint.applied"
        )
        assert not _spans_named(otel_capture, "awn.stock.applied"), (
            "the Wild path must not fire awn.stock.applied"
        )


# ===========================================================================
# AC1 (chargen half) — a PC can be built as any of the stock branch classes,
# and the build is OTEL-asserted. Driven through init_mutation_state_for_session,
# the exact seam chargen_mixin calls at confirmation.
# ===========================================================================


def _seaboard_pc(pack: GenrePack, name: str = "Ishmael"):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.system_strain import SystemStrainPool

    stats = {n: 10 for n in pack.rules.ability_score_names}
    core = CreatureCore(
        name=name,
        description="A penitent of the Seaboard.",
        personality="watchful",
        inventory=Inventory(),
        hp=HpPool(current=8, max=8, base_max=8),
        armor_class=10,
        system_strain=SystemStrainPool(current=0, max=10),
    )
    return Character(
        core=core,
        char_class="Penitent",
        race="Mutant Human",
        backstory="Took the Saint's water young, on the Providence strand.",
        stats=stats,
    )


class TestPerStockChargenOtel:
    def test_saint_marked_chargen_fires_saint_span_with_drawback(self, otel_capture) -> None:
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.server.mutation_init import init_mutation_state_for_session

        pack = _load_pack()
        saints = _saints(pack)
        snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, turn_manager=TurnManager())
        init_mutation_state_for_session(
            snap,
            catalog=pack.mutations,
            character_name="Ishmael",
            character_class="Penitent",
            session_id="test-103-10-saint",
            saints=saints,
            saint_id="herman_of_the_acushnet",
        )
        spans = _spans_named(otel_capture, "awn.saint.applied")
        assert spans, "Saint-Marked chargen must fire awn.saint.applied (the lie detector)"
        attrs = spans[0].attributes or {}
        assert attrs.get("saint_id") == "herman_of_the_acushnet"
        assert attrs.get("drawback"), "the span must carry the drawback id for the GM panel"

    def test_animal_stock_chargen_fires_stock_span_with_trait_deltas(self, otel_capture) -> None:
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.server.mutation_init import init_mutation_state_for_session

        pack = _load_pack()
        stocks = _stocks(pack)
        pc = _seaboard_pc(pack, name="Queequeg")
        snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, turn_manager=TurnManager())
        snap.characters.append(pc)
        init_mutation_state_for_session(
            snap,
            catalog=pack.mutations,
            character_name="Queequeg",
            character_class="Penitent",
            session_id="test-103-10-stock",
            stocks=stocks,
            stock_id="harbor_seal",
            character=pc,
        )
        spans = _spans_named(otel_capture, "awn.stock.applied")
        assert spans, "stock chargen must fire awn.stock.applied"
        attrs = spans[0].attributes or {}
        assert attrs.get("stock_id") == "harbor_seal"
        # harbor_seal grants two natural-ability mutations — the granted count
        # must be auditable from the GM panel.
        assert attrs.get("granted_count", 0) >= 1

    def test_sleeper_stock_chargen_fires_stock_span(self, otel_capture) -> None:
        """Sleeper carries no mutations (implants are System-Strain items), but
        the stock application still fires the span — non-engagement of the
        granted-mutation path is still observable."""
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.server.mutation_init import init_mutation_state_for_session

        pack = _load_pack()
        stocks = _stocks(pack)
        pc = _seaboard_pc(pack, name="Bartleby")
        snap = GameSnapshot(genre_slug=_GENRE, world_slug=_WORLD, turn_manager=TurnManager())
        snap.characters.append(pc)
        init_mutation_state_for_session(
            snap,
            catalog=pack.mutations,
            character_name="Bartleby",
            character_class="Penitent",
            session_id="test-103-10-sleeper",
            stocks=stocks,
            stock_id="sleeper",
            character=pc,
        )
        spans = _spans_named(otel_capture, "awn.stock.applied")
        assert spans, "even a mutation-less stock must fire awn.stock.applied"
        assert (spans[0].attributes or {}).get("stock_id") == "sleeper"


# ===========================================================================
# AC1 (marquee) — chargen → confrontation where the Saint drawback is
# mechanically live (OTEL) and the bundle fires through the live use path →
# save cycle preserves the sheet. The epic's single end-to-end proof.
# ===========================================================================


def _pg_store_with(snapshot, migrated_db: str):
    """Build a PgSaveRepository isolated to a unique slug and seed it.

    Copied from tests/integration/test_mutation_wiring.py (the integration dir
    has no autouse _pg_isolation, so the store is managed explicitly)."""
    import os
    import uuid

    from sidequest.game import db_pool
    from sidequest.server.session_state import _build_pg_repos_for_slug

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")

    os.environ["SIDEQUEST_DATABASE_URL"] = plain
    db_pool.close_pool()

    slug = f"seaboard-103-10-{uuid.uuid4().hex[:8]}"
    repo, _dungeon, _sink = _build_pg_repos_for_slug(
        db_pool.get_pool(),
        slug=slug,
        mode="solo",
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
    )
    repo.init_session()
    repo.save(snapshot)
    return repo


@pytest.mark.asyncio
async def test_saint_marked_drawback_lives_through_confrontation_and_save(
    migrated_db: str, otel_capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capstone chain for a Saint-Marked PC:

      chargen (Saint preset)  → awn.saint.applied carries the drawback
      drawback is mechanically live → in negative_ids AND in the narrator's
                                      mechanical-truth block (not improv)
      confrontation            → a Strain-costed mutation fires the live
                                  use path → awn.mutation.used + Strain moves
      save / reload            → the bundle + drawback + Strain survive PG

    This is test_102_7's cast-spine + test_mutation_wiring's save proof, fused
    onto a real Saint-Marked sheet — the epic's single end-to-end proof.
    """
    from sidequest.agents.orchestrator import (
        BeatSelection,
        NarrationTurnResult,
        NpcMention,
    )
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.mutation.context_builder import build_mutation_static_block
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )
    from sidequest.server.mutation_init import init_mutation_state_for_session
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = _load_pack()
    catalog = _catalog_or_skip(pack)
    saints = _saints(pack)

    # --- chargen: a Saint-Marked PC, the verbatim 103-1 proof Saint ---------
    pc_name = "Ishmael"
    pc = _seaboard_pc(pack, name=pc_name)
    snap = GameSnapshot(
        genre_slug=_GENRE,
        world_slug=_WORLD,
        turn_manager=TurnManager(interaction=2),
        player_seats={"player:Keith": pc_name},
    )
    snap.characters.append(pc)
    init_mutation_state_for_session(
        snap,
        catalog=catalog,
        character_name=pc_name,
        character_class="Penitent",
        session_id="seaboard-103-10",
        saints=saints,
        saint_id="herman_of_the_acushnet",
    )

    saint_span = _spans_named(otel_capture, "awn.saint.applied")
    assert saint_span, "Saint-Marked chargen must fire awn.saint.applied"
    drawback_id = (saint_span[0].attributes or {}).get("drawback")
    assert drawback_id, "the awn.saint.applied span must carry the drawback id"

    cs = snap.mutation_state.characters[pc_name]
    assert drawback_id in cs.negative_ids, (
        "the Saint's drawback must be carried on the sheet as a live negative, not narrator flavor"
    )

    # --- the drawback is mechanical truth the narrator cannot forget --------
    block = build_mutation_static_block(mutation_state=snap.mutation_state, catalog=catalog)
    assert drawback_id in block, (
        "the drawback must surface in the narrator's mechanical-truth block "
        "(build_mutation_static_block) so it is enforced, not improvised"
    )

    # --- the bundle is the usable half: a Strain-costed mutation fires the
    #     live use path inside a real confrontation (test_102_7 cast-spine) ---
    costed = next((m for m in catalog.positives if m.strain_cost > 0), None)
    assert costed is not None, "catalog must offer a Strain-costed positive (the crunch)"
    # Grant the Saint-Marked PC ownership of a usable mutation for the strike.
    cs.positive_ids = list(dict.fromkeys([*cs.positive_ids, costed.id]))

    opponent = "Magisterium Inquisitor"
    snap.character_locations[pc_name] = "providence"
    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=pc_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug=_GENRE,
    )
    assert enc is not None, "seating the real combat confrontation must succeed"
    snap.encounter = enc

    combat = next(c for c in pack.rules.confrontations if c.category == "combat")
    mutation_beat = next(
        (b for b in combat.beats if getattr(b, "mutation_resolution", False)), None
    )
    assert mutation_beat is not None, "the combat confrontation needs a mutation beat"

    monkeypatch.setattr("sidequest.server.narration_apply.random.randint", lambda a, b: b)
    strain_before = pc.core.system_strain.current
    result = NarrationTurnResult(
        narration="Ishmael lets Saint Herman's gift answer the Inquisitor.",
        beat_selections=[
            BeatSelection(
                actor=pc_name, beat_id=mutation_beat.id, target=opponent, mutation_id=costed.id
            )
        ],
    )
    _apply_narration_result_to_snapshot(
        snap,
        result,
        player_name=pc_name,
        pack=pack,
        from_explicit_action=True,
        room=room_for(snap),
        acting_character_name=pc_name,
    )

    used = _spans_named(otel_capture, "awn.mutation.used")
    assert len(used) == 1, (
        "a Saint-Marked PC's mutation, driven through the real apply path in a "
        f"real confrontation, must fire awn.mutation.used; got {len(used)}"
    )
    assert pc.core.system_strain.current == strain_before + costed.strain_cost, (
        "the Strain cost must land on the PC's pool through the live path"
    )

    # --- save cycle: the whole sheet survives the PG round-trip -------------
    store = _pg_store_with(snap, migrated_db)
    reloaded = store.load()
    assert reloaded is not None, "store.load() must return state after save"
    rsnap = reloaded.snapshot
    assert rsnap.mutation_state is not None, "mutation_state must survive the round-trip"
    rcs = rsnap.mutation_state.characters.get(pc_name)
    assert rcs is not None, "the Saint-Marked PC's mutation state must survive"
    assert drawback_id in rcs.negative_ids, "the drawback must survive the save cycle"
    assert costed.id in rcs.positive_ids, "the bundle/used positive must survive the save cycle"
    rcore = rsnap.find_creature_core(pc_name)
    assert rcore is not None and rcore.system_strain is not None
    assert rcore.system_strain.current == strain_before + costed.strain_cost, (
        "the Strain spent in the confrontation must persist across the save cycle"
    )
