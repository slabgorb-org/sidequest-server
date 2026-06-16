"""Scenario-binding integration — Story 2.3 Slice D.

Two layers:

- Unit-level: :func:`bind_scenario` against a handcrafted
  :class:`GenrePack` + :class:`GameSnapshot`, asserting belief seeding
  and OTEL emission independently of the chargen pipeline.
- Dispatch-level: full chargen walk through caverns_and_claudes with
  a ScenarioPack injected into ``sd.genre_pack.scenarios`` before
  confirmation, asserting the bind wires into
  ``_chargen_confirmation`` and populates both ``snapshot.scenario_state``
  and ``sd.active_scenario``.
- No-scenarios path: the default caverns pack (no scenarios) leaves
  both holders at their defaults after confirmation.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
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


# ---------------------------------------------------------------------------
# Unit: bind_scenario against handcrafted pack + snapshot
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def caverns_pack() -> GenrePack:
    """Load caverns as a fully-assembled GenrePack — used by the unit
    tests that only care about ``bind_scenario``'s behavior, not the
    rest of the pack content. We mutate ``pack.scenarios`` per test."""
    path = CONTENT_ROOT / "caverns_and_claudes"
    if not path.is_dir():
        pytest.skip(f"content pack not found at {path}")
    return load_genre_pack(path)


class TestBindScenarioUnit:
    def test_returns_none_when_pack_has_no_scenarios(self, caverns_pack: GenrePack) -> None:
        # caverns ships without scenarios — copy and ensure it stays empty.
        import copy as _copy

        pack = _copy.deepcopy(caverns_pack)
        pack.scenarios = {}
        snap = GameSnapshot(genre_slug="caverns_and_claudes", world_slug="flickering_reach")
        result = bind_scenario(
            pack, snap, genre_slug="caverns_and_claudes", world_slug="flickering_reach"
        )
        assert result is None
        assert snap.scenario_state is None

    def test_seeds_matching_npc_beliefs_and_emits_event(self, caverns_pack: GenrePack) -> None:
        import copy as _copy

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
        pack = _copy.deepcopy(caverns_pack)
        pack.scenarios = {"whodunit": scenario}

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

            # Inject a scenario into the loaded pack before confirmation.
            sd.genre_pack.scenarios["test_whodunit"] = _scenario_pack(
                npcs=[
                    _scenario_npc(
                        "suspect",
                        "A Person Not In The World",
                    )
                ],
                suspects=[Suspect(id="suspect", archetype_ref="r", can_be_guilty=True)],
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
            # Default caverns has no scenarios; confirm that directly
            # so the test stays honest if content later adds one.
            assert sd.genre_pack.scenarios == {}

            out = await _walk_and_confirm(handler)
            assert isinstance(out[0], CharacterCreationMessage)
            assert out[0].payload.phase == "complete"

            assert sd.snapshot.scenario_state is None
            assert sd.active_scenario is None

        asyncio.run(body())
