"""Story 153-23 (RED) — DUNGEON-ROOM-POPULATION-INERT.

Playtest forensics (beneath_sunden, WWN): the party descended into generated
rooms and the narrator improvised "three pale, long-limbed, wet things" with
zero mechanical backing — ``npc_pool=[None]``, no bestiary placement, no
encounter. The authored entrance room
(``worlds/beneath_sunden/rooms/entrance.yaml``) binds
``encounter_creatures: [gnaw_swarm]`` — the documented "easy first fight, on
purpose" — and **it never fired.**

The per-room placement infrastructure already ships (Story 107-2 / ADR-059):

* ``room_creature_binding.resolve_room_creatures(pack, world, room_id)`` reads
  the room YAML's ``encounter_creatures`` and emits ``monster_manual.room_bound``.
* ``monster_manual_inject.inject(..., room_id=<id>)`` materializes the bound
  bestiary creature under its AUTHORED name when a room id is supplied.

The break is purely a **wiring gap**: the sole production caller —
``websocket_session_handler._execute_narration_turn`` (the ``inject(...)`` call
at ~line 843) — never passed ``room_id`` (this diff is the fix that threads it).
It defaulted to ``None``, the binding branch was skipped, and the authored
Gnaw-Swarm was dead code in production. The
107-2 tests all drove ``inject`` with an EXPLICIT ``room_id`` and left the
handler→inject ``region_for()`` plumbing as a documented, blocking delivery
finding — *this story is that finding.*

So every test here drives the **real production turn** (``_execute_narration_turn``,
the function ``websocket_session_handler`` runs each turn) and asserts BEHAVIOUR
and OTEL spans — never source text (CLAUDE.md "No Source-Text Wiring Tests").
The fix under test: resolve the entered room id from ``snapshot.region_for()``
(per-PC ``pc_regions`` graph truth, 107-1's key) and thread it into ``inject``.

RED on develop: the handler ignores ``region_for()``, ``inject`` is called with
``room_id=None``, the binding never fires — so the authored Gnaw-Swarm is absent
and ``monster_manual.room_bound`` never emits. GREEN once the handler threads the
resolved room id.

Scope (matches context-story-153-23 "Story Scope"):
- AC1/AC2/AC6 — authored binding placed on entry via the production inject call,
  room id sourced from ``region_for()``.
- AC3 — the placed creature is a real, combat-capable Other (not an inert pool
  entry the narrator describes with no mechanical state). This story's testable
  guarantee is Other-readiness; full StructuredEncounter arming against a bound
  ruleset is out of scope (SOUL "Bind the Ruleset, Don't Balance It") — see the
  TEA delivery finding.
- AC5 — ``monster_manual.room_bound`` fires from the production path.
- AC4 (loot honesty / authored treasure) — entrance.yaml declares no treasure
  binding and the resolver only handles ``encounter_creatures``, so there is no
  authored-loot surface to place at this scope; the narrator-flavor honesty tag
  (``id="narrator:..."``) already exists. Logged as a delivery finding rather
  than covered with a vacuous test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from sidequest.game.monster_manual import MonsterManual
from sidequest.telemetry.spans.monster_manual import SPAN_MONSTER_MANUAL_ROOM_BOUND
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# session_fixture + otel_capture are re-exported into tests/integration via
# this directory's conftest; the two plain helpers live in tests/server/conftest.
from tests.server.conftest import (  # noqa: F401
    _build_turn_context_for_test,
    _make_minimal_narration_turn_result,
)

_GNAW_SWARM_AUTHORED_NAME = "Gnaw-Swarm"


# ---------------------------------------------------------------------------
# Turn-driver scaffolding — run the REAL production narration turn.
#
# Same shape as tests/server/dispatch/test_monster_manual_inject.py::
# test_execute_narration_turn_refreshes_stale_monster_manual: stub the
# orchestrator/local_dm/validator so the turn runs to the snapshot-mutation
# stage without a live narrator, and assert on the post-turn snapshot + spans.
# ---------------------------------------------------------------------------


def _fake_local_dm() -> MagicMock:
    """Dormant LocalDM stub — returns an empty DispatchPackage so the turn
    stays off the decompose path and the test isolates the inject seam."""
    from sidequest.protocol.dispatch import DispatchPackage

    fake = MagicMock()
    fake.decompose = AsyncMock(
        return_value=DispatchPackage(
            turn_id="t-153-23",
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
        )
    )
    return fake


def _arm_handler_for_turn(sd, handler) -> None:
    """Stub the narrator/validator collaborators so ``_execute_narration_turn``
    completes without a live LLM. Leaves the monster-manual + region state to
    the caller."""

    async def _capture(action: str, turn_context: object, *, room: object = None) -> object:
        return _make_minimal_narration_turn_result()

    sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_capture)
    sd.local_dm = _fake_local_dm()
    handler._validator = MagicMock()
    handler._validator.submit = AsyncMock()
    handler._validator.is_running = MagicMock(return_value=True)


def _synthetic_bestiary():
    from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry

    return Bestiary(
        entries=[
            BestiaryEntry(
                id="gnaw_swarm",
                name=_GNAW_SWARM_AUTHORED_NAME,
                level=1,
                hp=6,
                armor_class=12,
                attack_bonus=1,
                role="swarm",
                abilities=["Gnaw — overwhelms by numbers"],
            )
        ]
    )


def _write_synthetic_world(tmp_path: Path, *, room_id: str, creatures: list[str]) -> None:
    rooms_dir = tmp_path / "worlds" / "beneath_sunden" / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    (rooms_dir / f"{room_id}.yaml").write_text(
        yaml.safe_dump(
            {"room_type": "settlement", "name": "Under the Rope", "encounter_creatures": creatures}
        ),
        encoding="utf-8",
    )


def _bind_world(sd, *, world_slug: str = "beneath_sunden") -> None:
    """Point the session at the bound world on both sd and the snapshot."""
    sd.world_slug = world_slug
    sd.snapshot.world_slug = world_slug
    # Empty real Manual so ensure_loaded short-circuits (its first line returns
    # when sd.monster_manual is not None) — the opponent must come purely from
    # the per-room binding, not a seeded pool.
    sd.monster_manual = MonsterManual(genre=sd.genre_slug, world=world_slug)


def _graft_synthetic_pack(sd, tmp_path: Path) -> None:
    """Give the fixture's pack a synthetic source_dir + bestiary so the resolver
    reads a tmp ``rooms/entrance.yaml`` and resolves ``gnaw_swarm``. Keeps the
    fixture's RulesConfig()/ProgressionConfig so the turn machinery stays light."""
    bestiary = _synthetic_bestiary()
    sd.genre_pack.source_dir = tmp_path
    sd.genre_pack.effective_bestiary = lambda world: (bestiary, world)


def _graft_real_caverns_pack(sd):
    """Graft the REAL caverns_and_claudes source_dir + effective_bestiary onto the
    fixture pack so the resolver reads the shipped entrance.yaml and real
    bestiary, while the turn keeps the fixture's light dial rules."""
    from sidequest.genre.loader import load_genre_pack

    pack_dir = find_pack_path("caverns_and_claudes")
    real = load_genre_pack(pack_dir)
    sd.genre_pack.source_dir = real.source_dir
    sd.genre_pack.effective_bestiary = real.effective_bestiary


async def _drive_turn(sd, handler, action: str = "I disturb the bone-drifts.") -> None:
    _arm_handler_for_turn(sd, handler)
    turn_context = _build_turn_context_for_test(sd)
    await handler._execute_narration_turn(sd, action, turn_context)


def _placed(sd, name: str):
    return next((n for n in sd.snapshot.npcs if n.core.name == name), None)


def _room_bound_spans(otel_capture) -> list[dict]:
    return [
        dict(s.attributes or {})
        for s in otel_capture.get_finished_spans()
        if s.name == SPAN_MONSTER_MANUAL_ROOM_BOUND
    ]


_content_required = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


# ---------------------------------------------------------------------------
# AC1 / AC2 / AC6 — the authored creature reaches state through the production
# turn, with the room id sourced from region_for() (real shipped content).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_production_turn_places_authored_gnaw_swarm_on_entry(session_fixture, otel_capture):
    """AC1/AC2/AC6: a PC standing in the authored ``entrance`` (region truth in
    ``pc_regions``) drives the real ``_execute_narration_turn``; the handler must
    resolve room_id from ``region_for()`` and thread it into ``inject``, so the
    authored Gnaw-Swarm materializes under its real name — not left for the
    narrator to label.

    RED on develop: inject is called with room_id=None, the binding is skipped,
    and no Gnaw-Swarm appears."""
    sd, handler = session_fixture
    try:
        _graft_real_caverns_pack(sd)
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    _bind_world(sd)
    sd.snapshot.pc_regions["TestHero"] = "entrance"

    await _drive_turn(sd, handler)

    gnaw = _placed(sd, _GNAW_SWARM_AUTHORED_NAME)
    assert gnaw is not None, (
        "the authored Gnaw-Swarm was not placed on entry — the production turn "
        "called inject() without the entered room id (region_for not threaded), "
        "so the entrance binding never fired and the narrator is left to improvise"
    )
    assert gnaw.manual_origin is True, (
        "placed creature is not attributed to the Monster Manual binding "
        "(manual_origin=False) — the GM panel cannot tell engine placement from improv"
    )


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_production_turn_emits_room_bound_span(session_fixture, otel_capture):
    """AC5: entering the bound room through the production turn fires
    ``monster_manual.room_bound`` naming the room + bound creature — the
    GM-panel lie-detector that the engine placed the authored opponent.

    RED on develop: no room id is threaded, resolve_room_creatures never runs,
    the span never emits."""
    sd, handler = session_fixture
    try:
        _graft_real_caverns_pack(sd)
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    _bind_world(sd)
    sd.snapshot.pc_regions["TestHero"] = "entrance"

    await _drive_turn(sd, handler)

    spans = _room_bound_spans(otel_capture)
    assert spans, (
        "monster_manual.room_bound never fired from the production turn — the "
        "handler did not resolve+thread the entered room id, so the binding "
        "resolver was never reached (no OTEL proof of engine placement)"
    )
    attrs = spans[-1]
    assert attrs.get("room_id") == "entrance", attrs
    assert "gnaw_swarm" in (attrs.get("bound_creatures") or []), attrs


@pytest.mark.integration
@pytest.mark.asyncio
@_content_required
async def test_placed_room_creature_is_a_combat_ready_other(session_fixture, otel_capture):
    """AC3: when a room places a hostile creature, it must be a real Other to
    resolve against — hostile disposition, real HP, threat level, and a
    creature_id — not an inert ``npc_pool`` entry with no mechanical state
    behind a narrator's fight description.

    (Full StructuredEncounter arming against a bound ruleset is out of scope —
    see the TEA delivery finding. This pins the "real Other" guarantee.)

    RED on develop: the creature is never placed, so there is no Other at all."""
    sd, handler = session_fixture
    try:
        _graft_real_caverns_pack(sd)
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    _bind_world(sd)
    sd.snapshot.pc_regions["TestHero"] = "entrance"

    await _drive_turn(sd, handler)

    gnaw = _placed(sd, _GNAW_SWARM_AUTHORED_NAME)
    assert gnaw is not None, (
        "no Other placed — the room binding did not fire (room_id not threaded)"
    )
    assert int(gnaw.disposition) < 0, (
        f"a placed dungeon opponent must be hostile (disposition<0) to be a real "
        f"combat Other; got disposition={int(gnaw.disposition)}"
    )
    assert gnaw.core.hp.current > 0 and gnaw.core.hp.max > 0, (
        f"the Other must carry real HP to resolve combat against; "
        f"got {gnaw.core.hp.current}/{gnaw.core.hp.max}"
    )
    assert gnaw.threat_level is not None and gnaw.threat_level >= 1, (
        f"the Other must carry a threat level; got {gnaw.threat_level!r}"
    )
    assert gnaw.creature_id == "gnaw_swarm", (
        f"the Other must carry its bestiary creature_id for downstream resolution; "
        f"got {gnaw.creature_id!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — the room id comes from region_for() (per-PC pc_regions), NOT the
# free-text scene string or the spawn-anchor current_region. Synthetic world so
# the assertion is isolated from shipped content.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_room_id_resolves_from_pc_regions_not_scene_string(
    session_fixture, otel_capture, tmp_path
):
    """AC2: the entered room id must come from ``region_for()`` / ``pc_regions``
    (107-1's graph key), not the free-text ``current_location`` scene string nor
    the ``current_region`` spawn anchor.

    Decoys: ``current_region`` and the PC's scene location are set to a node with
    NO room file; only ``pc_regions`` points at the bound ``entrance``. If the
    fix wrongly keyed off the scene string / current_region, the resolver would
    read a missing room file and place nothing.

    RED on develop: nothing is threaded at all, so the binding never fires."""
    sd, handler = session_fixture
    _graft_synthetic_pack(sd, tmp_path)
    _write_synthetic_world(tmp_path, room_id="entrance", creatures=["gnaw_swarm"])
    _bind_world(sd)

    # Decoys that must NOT be used as the room key.
    sd.snapshot.current_region = "exp009.r7"  # no rooms/exp009.r7.yaml exists
    sd.snapshot.character_locations["TestHero"] = "A misty cavern with no room file"
    # The only true per-PC graph region:
    sd.snapshot.pc_regions["TestHero"] = "entrance"

    await _drive_turn(sd, handler)

    assert _placed(sd, _GNAW_SWARM_AUTHORED_NAME) is not None, (
        "the entrance binding did not fire — the handler either failed to thread a "
        "room id at all (develop) or keyed off the scene string/current_region "
        "instead of region_for()/pc_regions"
    )
    assert _room_bound_spans(otel_capture), (
        "monster_manual.room_bound did not fire for the pc_regions room"
    )


# ---------------------------------------------------------------------------
# AC2 (additive / No-Silent-Fallback guard) — an unresolved region must place
# NO binding and must not fabricate a room id. Guards that the fix is strictly
# additive: room_id=None behaviour is preserved when region_for() is None.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unresolved_region_places_no_binding(session_fixture, otel_capture, tmp_path):
    """A split / unseeded party makes ``region_for()`` return None (No Silent
    Fallbacks — it never degrades to current_region). The turn must then place
    NO room binding and emit NO room_bound span — the handler must not invent a
    room id. This proves the fix is gated on a genuinely-resolved region and
    preserves the legacy room_id=None path."""
    sd, handler = session_fixture
    _graft_synthetic_pack(sd, tmp_path)
    _write_synthetic_world(tmp_path, room_id="entrance", creatures=["gnaw_swarm"])
    _bind_world(sd)

    # Two seated PCs in DIFFERENT regions → region_for() (no perspective) → None.
    sd.snapshot.player_seats["player:Rux"] = "Rux"
    sd.snapshot.pc_regions["TestHero"] = "entrance"
    sd.snapshot.pc_regions["Rux"] = "exp001.r4"

    assert sd.snapshot.region_for() is None, "precondition: split party → region_for() None"

    await _drive_turn(sd, handler)

    assert _placed(sd, _GNAW_SWARM_AUTHORED_NAME) is None, (
        "a creature was placed despite an unresolved region — the handler "
        "fabricated a room id instead of honouring region_for()=None"
    )
    assert not _room_bound_spans(otel_capture), (
        "monster_manual.room_bound fired with no resolved region — binding must "
        "not fire on a split/unseeded party"
    )
