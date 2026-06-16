"""Scenario-binding integration — Story 2.3 Slice D.

Two layers:

- Unit-level: :func:`bind_scenario` against a handcrafted
  :class:`GenrePack` + :class:`GameSnapshot`, asserting belief seeding
  and OTEL emission independently of the chargen pipeline.
- Dispatch-level: full chargen walk through caverns_and_claudes with
  a ScenarioPack injected into the active world's
  ``pack.worlds[world_slug].scenarios`` before confirmation (Story 71-32:
  binding is world-aware), asserting the bind wires into
  ``_chargen_confirmation`` and populates both ``snapshot.scenario_state``
  and ``sd.active_scenario``.
- No-scenarios path: the default caverns pack (no scenarios) leaves
  both holders at their defaults after confirmation.
"""

from __future__ import annotations

import asyncio
import copy
import random
import textwrap
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.loader import _load_single_world, load_genre_pack
from sidequest.genre.models.pack import GenrePack, World
from sidequest.genre.models.scenario import (
    AssignmentMatrix,
    InitialBeliefs,
    Pacing,
    ScenarioNpc,
    ScenarioPack,
    Suspect,
    Suspicion,
    WhenGuilty,
    WhenInnocent,
)
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.dispatch.scenario_bind import bind_scenario
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, clean per test.

    ADR-115 D2: chargen confirmation persists the authoritative snapshot
    (incl. ``scenario_state``) to Postgres via ``db_pool.get_pool()``, and the
    slug-connect path reloads from there — NOT the SQLite save_dir store. The
    TRUNCATE-per-test keeps each test reading only its own seeded session row
    in the shared, session-scoped ``sidequest_test`` db. (Story 97-6 also made
    ``seed_slug_for_test`` return a unique slug per call, so dispatch tests no
    longer share the old fixed ``"test-slug"`` row; the TRUNCATE remains as
    defence-in-depth against any other cross-test row bleed.)
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


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _npc_snap(name: str) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="placeholder",
            personality="placeholder",
        ),
    )


def _scenario_npc(
    npc_id: str,
    name: str,
    *,
    facts: list[str] | None = None,
    suspicions: list[Suspicion] | None = None,
) -> ScenarioNpc:
    return ScenarioNpc(
        id=npc_id,
        archetype_ref="witness",
        name=name,
        initial_beliefs=InitialBeliefs(
            facts=facts or [],
            suspicions=suspicions or [],
        ),
        when_guilty=WhenGuilty(truth="", cover_story="", breaking_evidence=[]),
        when_innocent=WhenInnocent(actual_activity=""),
    )


def _scenario_pack(
    *,
    npcs: list[ScenarioNpc],
    suspects: list[Suspect] | None = None,
) -> ScenarioPack:
    return ScenarioPack(
        name="Test Whodunit",
        version="1.0",
        description="",
        duration_minutes=90,
        max_players=3,
        pacing=Pacing(scene_budget=5),
        assignment_matrix=AssignmentMatrix(suspects=suspects or []),
        npcs=npcs,
    )


def _fresh_otel() -> tuple[TracerProvider, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _attach_world_scenario(
    pack: GenrePack, world_slug: str, scenario_id: str, scenario: ScenarioPack
) -> None:
    """Place ``scenario`` at ``pack.worlds[world_slug].scenarios`` (the Story
    71-32 world-aware contract — bind reads from the world, not pack root).

    If the scaffold pack lacks ``world_slug``, synthesize that world from an
    existing one: the caverns scaffold authors ``beneath_sunden`` while the
    bind tests address ``flickering_reach``.
    """
    world = pack.worlds.get(world_slug)
    if world is None:
        world = copy.deepcopy(next(iter(pack.worlds.values())))
        pack.worlds[world_slug] = world
    world.scenarios = {scenario_id: scenario}


# ---------------------------------------------------------------------------
# Unit: bind_scenario against handcrafted pack + snapshot
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def caverns_pack() -> GenrePack:
    """Load caverns as a fully-assembled GenrePack — used by the unit
    tests that only care about ``bind_scenario``'s behavior, not the
    rest of the pack content. Each test deep-copies it and injects
    scenarios (world- or pack-level) as needed."""
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


class TestBindScenarioUnit:
    def test_returns_none_when_pack_has_no_scenarios(self, caverns_pack: GenrePack) -> None:
        # caverns ships without scenarios — copy and ensure it stays empty.
        pack = copy.deepcopy(caverns_pack)
        pack.scenarios = {}
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="flickering_reach")
        result = bind_scenario(
            pack, snap, genre_slug="caverns_and_claudes", world_slug="flickering_reach"
        )
        assert result is None
        assert snap.scenario_state is None

    def test_seeds_matching_npc_beliefs_and_emits_event(self, caverns_pack: GenrePack) -> None:
        snap = GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="flickering_reach",
            npcs=[_npc_snap("Ada"), _npc_snap("Bert"), _npc_snap("Cleo")],
        )
        scenario = _scenario_pack(
            npcs=[
                _scenario_npc(
                    "a",
                    "Ada",
                    facts=["Was at the library", "Carries the key"],
                ),
                _scenario_npc(
                    "b",
                    "Bert",
                    suspicions=[
                        Suspicion(
                            target="Ada", confidence=0.7, basis="Saw her argue with the victim"
                        )
                    ],
                ),
                _scenario_npc("d", "Daisy"),  # Not present in snapshot → no-op
            ],
            suspects=[Suspect(id="a", archetype_ref="r", can_be_guilty=True)],
        )
        pack = copy.deepcopy(caverns_pack)
        # Story 71-32: scenarios bind from the active world, not pack root.
        _attach_world_scenario(pack, "flickering_reach", "whodunit", scenario)

        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("outer"):
            result = bind_scenario(
                pack,
                snap,
                genre_slug="caverns_and_claudes",
                world_slug="flickering_reach",
                rng=random.Random(0),
            )

        assert result is not None
        scenario_id, bound_pack = result
        assert scenario_id == "whodunit"
        assert bound_pack is scenario

        # ScenarioState wired onto snapshot
        assert snap.scenario_state is not None
        assert snap.scenario_state.guilty_npc == "a"
        assert snap.scenario_state.npc_roles["Ada"] == "guilty"

        # Belief seeding on matching NPCs only
        ada = next(n for n in snap.npcs if n.core.name == "Ada")
        bert = next(n for n in snap.npcs if n.core.name == "Bert")
        cleo = next(n for n in snap.npcs if n.core.name == "Cleo")

        assert len(ada.belief_state.beliefs) == 2
        assert all(b.variant == "fact" for b in ada.belief_state.beliefs)
        assert {b.content for b in ada.belief_state.beliefs} == {
            "Was at the library",
            "Carries the key",
        }

        assert len(bert.belief_state.beliefs) == 1
        bert_belief = bert.belief_state.beliefs[0]
        assert bert_belief.variant == "suspicion"
        assert bert_belief.subject == "Ada"
        assert bert_belief.content == "Saw her argue with the victim"
        # confidence round-tripped (clamped path covered separately)
        assert bert_belief.confidence == 0.7  # type: ignore[attr-defined]

        # Cleo is in snapshot but not in scenario — untouched.
        assert cleo.belief_state.beliefs == []

        # OTEL: scenario.initialized fired once, with the expected fields.
        events = [
            e
            for span in exporter.get_finished_spans()
            for e in span.events
            if e.name == "scenario.initialized"
        ]
        assert len(events) == 1
        attrs = dict(events[0].attributes or {})
        assert attrs["scenario_id"] == "whodunit"
        assert attrs["genre"] == "caverns_and_claudes"
        assert attrs["world"] == "flickering_reach"
        assert attrs["guilty_npc"] == "a"
        # Belief-added events fired during seeding
        belief_events = [
            e
            for span in exporter.get_finished_spans()
            for e in span.events
            if e.name == "belief_state.belief_added"
        ]
        # 2 facts for Ada + 1 suspicion for Bert = 3
        assert len(belief_events) == 3


# ---------------------------------------------------------------------------
# Dispatch-level: full chargen walk with scenario injected into pack
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    if not (CONTENT_ROOT / "caverns_and_claudes").is_dir():
        pytest.skip("content pack not found")
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[CONTENT_ROOT],
        save_dir=tmp_path,
    )


async def _connect(handler: WebSocketSessionHandler) -> None:
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(
        handler._save_dir,
        genre="caverns_and_claudes",
        world="flickering_reach",
    )
    attach_default_room_context(handler)
    payload = SessionEventPayload(
        event="connect",
        player_name="Tester",
        game_slug=slug,
    )
    out = await handler.handle_message(SessionEventMessage(payload=payload, player_id=""))
    assert isinstance(out[0], SessionEventMessage)


async def _walk_and_confirm(handler: WebSocketSessionHandler) -> list:
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    builder = sd.builder
    assert builder is not None

    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Rux")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")

    return await handler.handle_message(
        CharacterCreationMessage(
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )


class TestDispatchIntegration:
    def test_confirmation_binds_injected_scenario(self, handler: WebSocketSessionHandler) -> None:
        async def body() -> None:
            await _connect(handler)
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None

            # Inject a scenario into the ACTIVE WORLD before confirmation
            # (Story 71-32: confirmation binds from pack.worlds[sd.world_slug],
            # not pack root). The confirmation path passes world_slug=sd.world_slug.
            _attach_world_scenario(
                sd.genre_pack,
                sd.world_slug,
                "test_whodunit",
                _scenario_pack(
                    npcs=[
                        _scenario_npc(
                            "suspect",
                            "A Person Not In The World",
                        )
                    ],
                    suspects=[Suspect(id="suspect", archetype_ref="r", can_be_guilty=True)],
                ),
            )

            out = await _walk_and_confirm(handler)
            assert len(out) >= 1
            assert isinstance(out[0], CharacterCreationMessage)
            assert out[0].payload.phase == "complete"

            # Scenario state bound onto snapshot; pack stashed on session.
            assert sd.snapshot.scenario_state is not None
            assert sd.snapshot.scenario_state.guilty_npc == "suspect"
            assert sd.active_scenario is not None
            assert sd.active_scenario.name == "Test Whodunit"

        asyncio.run(body())

    def test_confirmation_noop_when_pack_has_no_scenarios(
        self, handler: WebSocketSessionHandler
    ) -> None:
        async def body() -> None:
            await _connect(handler)
            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd is not None
            # Default caverns has no scenarios; confirm that directly
            # so the test stays honest if content later adds one.
            assert sd.genre_pack.scenarios == {}

            out = await _walk_and_confirm(handler)
            assert isinstance(out[0], CharacterCreationMessage)
            assert out[0].payload.phase == "complete"

            assert sd.snapshot.scenario_state is None
            assert sd.active_scenario is None

        asyncio.run(body())


# ===========================================================================
# Story 71-32 — World-level scenario discovery + world-aware binding
#
# These tests assert the world-aware contract:
#   - World model declares a ``scenarios`` field.
#   - The loader discovers ``worlds/<world>/scenarios/``.
#   - ``bind_scenario`` binds ONLY the active world's scenario via
#     ``pack.worlds[world_slug].scenarios`` (NOT pack-level
#     ``next(iter(pack.scenarios))`` — that would be world-agnostic).
#   - Absence is explicit: a world with no scenario binds NOTHING and emits a
#     ``scenario.bind_skipped`` watcher event — NO silent fallback to pack-level
#     scenarios (SOUL.md / server CLAUDE.md "No Silent Fallbacks").
#
# Wiring is asserted by BEHAVIOR (OTEL events + real bind/load results), never
# by grepping source (server CLAUDE.md "No Source-Text Wiring Tests"). The one
# model-shape check uses ``World.model_fields`` — the blessed reflection
# tripwire, NOT a ``hasattr`` check (``World`` is ``extra="allow"``, so
# ``hasattr`` would pass spuriously on any extra attribute).
# ===========================================================================


# Synthetic world slugs for the two-world fixture. Deliberately NOT any real
# authored world name — the fixture re-keys a real World object under these so
# the tests never depend on which world the scaffold pack happens to ship.
_WORLD_A = "manor_house"
_WORLD_B = "garden_estate"


def _two_world_pack(
    base_pack: GenrePack,
    *,
    world_a_scenario: tuple[str, ScenarioPack] | None,
    world_b_scenario: tuple[str, ScenarioPack] | None,
    pack_level_scenario: tuple[str, ScenarioPack] | None,
) -> GenrePack:
    """Build a two-world pack from a real (scaffold) GenrePack.

    Takes one real, fully-populated ``World`` object as a structural scaffold
    (it needs valid config/lore/cartography, which are tedious to hand-build)
    and re-keys deep copies under the synthetic slugs ``_WORLD_A`` / ``_WORLD_B``
    — so the fixture is independent of the scaffold pack's authored world names.
    Each world (and optionally the pack root) carries a DISTINCT scenario so a
    world-agnostic binder is caught selecting the wrong one.
    """
    pack = copy.deepcopy(base_pack)
    template_world = next(iter(pack.worlds.values()))
    pack.worlds = {}

    world_a = copy.deepcopy(template_world)
    world_a.scenarios = {world_a_scenario[0]: world_a_scenario[1]} if world_a_scenario else {}
    pack.worlds[_WORLD_A] = world_a

    world_b = copy.deepcopy(template_world)
    world_b.scenarios = {world_b_scenario[0]: world_b_scenario[1]} if world_b_scenario else {}
    pack.worlds[_WORLD_B] = world_b

    pack.scenarios = {pack_level_scenario[0]: pack_level_scenario[1]} if pack_level_scenario else {}
    return pack


def _guilty_scenario(guilty_id: str, guilty_name: str) -> ScenarioPack:
    """A scenario whose single can-be-guilty suspect is deterministic under
    ``random.Random(0)`` — its ``scenario_state.guilty_npc`` equals ``guilty_id``."""
    return _scenario_pack(
        npcs=[_scenario_npc(guilty_id, guilty_name)],
        suspects=[Suspect(id=guilty_id, archetype_ref="r", can_be_guilty=True)],
    )


class TestWorldModelScenarioField:
    def test_world_declares_scenarios_field(self) -> None:
        # Reflection tripwire: `scenarios` must be a DECLARED field, not an
        # `extra="allow"` attribute (a `hasattr` check would pass spuriously).
        # (The empty-default behavior is covered hermetically by
        # TestLoaderWorldLevelDiscovery.test_world_without_scenarios_dir_defaults_empty.)
        assert "scenarios" in World.model_fields


class TestBindSelectsActiveWorld:
    def test_binds_only_the_active_worlds_scenario(self, caverns_pack: GenrePack) -> None:
        """AC2 — binding World A binds World A's scenario, NOT World B's and
        NOT the pack-level decoy. This is the core regression guard against
        cross-world scenario bleed."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=("train_mystery", _guilty_scenario("porter", "The Porter")),
            world_b_scenario=("garden_party", _guilty_scenario("gardener", "The Gardener")),
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug=_WORLD_A)

        result = bind_scenario(
            pack,
            snap,
            genre_slug="caverns_and_claudes",
            world_slug=_WORLD_A,
            rng=random.Random(0),
        )

        assert result is not None
        scenario_id, _ = result
        assert scenario_id == "train_mystery"  # not "garden_party", not "decoy_case"
        assert snap.scenario_state is not None
        assert snap.scenario_state.guilty_npc == "porter"

    def test_binds_the_other_worlds_scenario_when_that_world_is_active(
        self, caverns_pack: GenrePack
    ) -> None:
        """AC2 — the same pack, binding World B, must select World B's
        scenario. Proves selection follows ``world_slug``, not load order."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=("train_mystery", _guilty_scenario("porter", "The Porter")),
            world_b_scenario=("garden_party", _guilty_scenario("gardener", "The Gardener")),
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug=_WORLD_B)

        result = bind_scenario(
            pack,
            snap,
            genre_slug="caverns_and_claudes",
            world_slug=_WORLD_B,
            rng=random.Random(0),
        )

        assert result is not None
        scenario_id, _ = result
        assert scenario_id == "garden_party"
        assert snap.scenario_state is not None
        assert snap.scenario_state.guilty_npc == "gardener"


class TestBindNoSilentFallback:
    def test_world_without_scenario_binds_nothing_despite_pack_level(
        self, caverns_pack: GenrePack
    ) -> None:
        """AC3 — a scenario-less world returns ``None`` EVEN WHEN the pack root
        carries a scenario. No silent fallback to pack-level scenarios."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=None,  # World A has no scenario
            world_b_scenario=None,
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug=_WORLD_A)

        result = bind_scenario(
            pack,
            snap,
            genre_slug="caverns_and_claudes",
            world_slug=_WORLD_A,
            rng=random.Random(0),
        )

        assert result is None
        assert snap.scenario_state is None

    def test_unknown_world_slug_binds_nothing(self, caverns_pack: GenrePack) -> None:
        """AC3 edge — an unknown ``world_slug`` returns ``None`` (no KeyError),
        and does NOT fall back to the pack-level scenario."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=("train_mystery", _guilty_scenario("porter", "The Porter")),
            world_b_scenario=None,
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="no_such_world")

        result = bind_scenario(
            pack,
            snap,
            genre_slug="caverns_and_claudes",
            world_slug="no_such_world",
            rng=random.Random(0),
        )

        assert result is None
        assert snap.scenario_state is None


class TestBindOtelWorldAware:
    def test_initialized_event_carries_active_world(self, caverns_pack: GenrePack) -> None:
        """AC4 — the success span event records the ACTIVE world and that
        world's scenario id (proves the GM panel sees the right binding)."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=("train_mystery", _guilty_scenario("porter", "The Porter")),
            world_b_scenario=("garden_party", _guilty_scenario("gardener", "The Gardener")),
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug=_WORLD_B)

        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("outer"):
            bind_scenario(
                pack,
                snap,
                genre_slug="caverns_and_claudes",
                world_slug=_WORLD_B,
                rng=random.Random(0),
            )

        events = [
            e
            for span in exporter.get_finished_spans()
            for e in span.events
            if e.name == "scenario.initialized"
        ]
        assert len(events) == 1
        attrs = dict(events[0].attributes or {})
        assert attrs["world"] == _WORLD_B
        assert attrs["scenario_id"] == "garden_party"
        assert attrs["guilty_npc"] == "gardener"

    def test_skip_emits_watcher_event_for_scenario_less_world(
        self, caverns_pack: GenrePack
    ) -> None:
        """AC4 — the absence decision is observable. Binding a scenario-less
        world emits a ``scenario.bind_skipped`` event carrying the world,
        genre, and reason, so the GM panel can distinguish "no scenario here"
        from a silently-improvised mystery."""
        pack = _two_world_pack(
            caverns_pack,
            world_a_scenario=None,
            world_b_scenario=None,
            pack_level_scenario=("decoy_case", _guilty_scenario("decoy", "Pack Decoy")),
        )
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug=_WORLD_A)

        provider, exporter = _fresh_otel()
        tracer = provider.get_tracer("t")
        with tracer.start_as_current_span("outer"):
            bind_scenario(
                pack,
                snap,
                genre_slug="caverns_and_claudes",
                world_slug=_WORLD_A,
                rng=random.Random(0),
            )

        skip_events = [
            e
            for span in exporter.get_finished_spans()
            for e in span.events
            if e.name == "scenario.bind_skipped"
        ]
        assert len(skip_events) == 1
        attrs = dict(skip_events[0].attributes or {})
        assert attrs["world"] == _WORLD_A
        assert attrs["genre"] == "caverns_and_claudes"
        # _WORLD_A exists in pack.worlds (via _two_world_pack) but has no
        # scenario → the reason is the "world present, empty" branch, not
        # "unknown_world". Guards against the two reason strings being swapped.
        assert attrs["reason"] == "no_world_scenario"


# ---------------------------------------------------------------------------
# Hermetic loader fixtures — a synthetic world on tmp_path, NO real content.
# Mirrors tests/genre/test_loader_world_plumbing.py's minimal-world pattern.
# ---------------------------------------------------------------------------

_WORLD_YAML = textwrap.dedent(
    """\
    name: Testworld
    description: Synthetic test world for world-level scenario discovery.
    starting_location: testtown
    """
)

_LORE_YAML = textwrap.dedent(
    """\
    world_name: Testworld
    history: A brief history of testing.
    geography: Flat. Featureless. Test-shaped.
    cosmology: Two suns, no moons, deterministic stars.
    """
)

_CARTOGRAPHY_YAML = textwrap.dedent(
    """\
    world_name: Testworld
    starting_region: testtown
    navigation_mode: region
    regions:
      testtown:
        name: Testtown
        summary: A region for tests.
        description: A flat plain with one inn and a notional river.
        terrain: settlement
        adjacent: []
    """
)

_OPENINGS_YAML = textwrap.dedent(
    """\
    version: "1.0.0"
    world: testworld
    genre: testgenre
    openings:
      - id: solo_default
        triggers:
          mode: either
          min_players: 1
          max_players: 6
          backgrounds: []
        setting:
          location_label: testtown
          situation: Standing in the square at noon.
        establishing_narration: |
          The square is empty. The sun is high. You stand alone.
    """
)

# Minimal valid ScenarioPack — only scenario.yaml is required by
# _load_single_scenario; the matrix/clue/atmosphere/npc files are optional.
_SCENARIO_YAML = textwrap.dedent(
    """\
    name: The Morning Train
    version: "1.0"
    description: A synthetic whodunit for loader discovery.
    duration_minutes: 90
    max_players: 3
    pacing:
      scene_budget: 5
    assignment_matrix:
      suspects: []
    npcs: []
    """
)


def _make_world_tree(tmp_path: Path, *, with_scenario: bool) -> tuple[Path, Path]:
    """Construct ``<tmp>/genre/worlds/testworld/`` + minimal required files.

    When ``with_scenario`` is set, also drops
    ``worlds/testworld/scenarios/the_morning_train/scenario.yaml``.
    Returns ``(genre_root, world_path)``.
    """
    genre_root = tmp_path / "genre"
    world_path = genre_root / "worlds" / "testworld"
    world_path.mkdir(parents=True)
    (world_path / "world.yaml").write_text(_WORLD_YAML, encoding="utf-8")
    (world_path / "lore.yaml").write_text(_LORE_YAML, encoding="utf-8")
    (world_path / "cartography.yaml").write_text(_CARTOGRAPHY_YAML, encoding="utf-8")
    (world_path / "openings.yaml").write_text(_OPENINGS_YAML, encoding="utf-8")
    if with_scenario:
        scn_dir = world_path / "scenarios" / "the_morning_train"
        scn_dir.mkdir(parents=True)
        (scn_dir / "scenario.yaml").write_text(_SCENARIO_YAML, encoding="utf-8")
    return genre_root, world_path


class TestLoaderWorldLevelDiscovery:
    """AC1/AC5 — the loader discovers ``worlds/<world>/scenarios/`` and attaches
    them to the World model. Fully hermetic: a synthetic world on ``tmp_path``,
    no real content pack. Exercises ``_load_single_world`` directly — the unit
    Dev modifies. Fails in RED (no World.scenarios field; loader ignores the
    world-level scenarios dir) and passes once both land.
    """

    def test_world_level_scenarios_dir_is_discovered(self, tmp_path: Path) -> None:
        genre_root, world_path = _make_world_tree(tmp_path, with_scenario=True)

        world = _load_single_world(world_path, [], genre_root)

        assert world is not None
        assert "the_morning_train" in world.scenarios
        assert world.scenarios["the_morning_train"].name == "The Morning Train"

    def test_world_without_scenarios_dir_defaults_empty(self, tmp_path: Path) -> None:
        genre_root, world_path = _make_world_tree(tmp_path, with_scenario=False)

        world = _load_single_world(world_path, [], genre_root)

        assert world is not None
        assert world.scenarios == {}
