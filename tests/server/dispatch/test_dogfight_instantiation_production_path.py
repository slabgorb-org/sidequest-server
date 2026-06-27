"""Dogfight seating via the production dispatch path (ADR-153 §6).

These tests drive the *real* production seam — ``run_confrontation_dispatch``,
the router's live engager — with the same empty ``npcs_present`` the router
pass supplies (``intent_router_pass`` line 167).

**ADR-153 §6 supersedes the original Story 59-17 contract (2026-06-27).** The
59-17 fix enabled the sealed-letter *location fallback* so a dogfight could
seat the nearest co-located hostile pilot, and refused (no encounter) when no
adversary was in the scene. ADR-153 §6 replaces that: the dogfight Other is a
**ship/chassis sourced from the def's ``opponent_default_stats`` frame** (or a
router-named contact), **never the nearest co-located creature** — because the
location fallback also conscripted a *ground* creature as the enemy vessel
(finding 158-34). A dogfight still requires an Other (ADR-116), but the def
frame IS that Other, so a framed dogfight **always seats a default-from-frame
enemy ship** rather than refusing.

Consequences for these tests (all updated to the §6 contract):
  - A co-located hostile NPC (pilot OR ground creature) is NO LONGER
    conscripted via the fallback — a frame-default ship is seated instead.
  - "No opponent in scene" no longer refuses — it seats the frame default.
  - The explicit-``npcs_present`` path is unchanged (a router-named opponent
    still seats directly) — ``test_dogfight_instantiates_when_opponent_passed_explicitly``
    stays GREEN.

The router→seater→lifecycle contract that makes the router *name* the opponent
is ADR-153 §7 / Plan 2 (story 158-29) — out of scope here; Plan 1 only changes
the seater so it never grabs a co-located creature.

Skips when ``sidequest-content`` is not checked out alongside the server
(matches the sibling ``test_sealed_letter_dispatch_integration.py`` pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch
from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

CONTENT_ROOT = Path(__file__).resolve().parents[3].parent / "sidequest-content" / "genre_packs"

pytestmark = pytest.mark.skipif(
    not CONTENT_ROOT.is_dir(),
    reason="sidequest-content not on disk alongside sidequest-server",
)

PLAYER = "Maverick"
LOCATION = "asteroid_belt"
DOGFIGHT = "dogfight"


@pytest.fixture(scope="module")
def space_opera_pack() -> GenrePack:
    return load_genre_pack(CONTENT_ROOT / "space_opera")


def _snap_at_location() -> GameSnapshot:
    snap = GameSnapshot(genre="space_opera")
    snap.genre_slug = "space_opera"
    # Per-PC location is the source of truth (Wave 2B / story 45-48); the
    # location fallback resolves the opponent via this perspective.
    snap.character_locations[PLAYER] = LOCATION
    return snap


def _seat_opponent(snap: GameSnapshot, *, name: str = "Vulture") -> None:
    """Put one hostile opponent pilot in the player's scene.

    Mirrors how a prior narration turn records an adversary: an ``Npc`` in
    ``snapshot.npcs`` whose ``last_seen_location`` matches the player's
    current location.
    """
    opp = Npc(
        core=CreatureCore(
            name=name,
            description="An ace enemy pilot in a matte-black interceptor.",
            personality="ruthless, patient",
        )
    )
    opp.last_seen_location = LOCATION
    opp.npc_role_id = "hostile"
    snap.npcs.append(opp)


def _seat_bystander(snap: GameSnapshot, *, name: str = "Deck Crew Chief") -> None:
    """Put one neutral, non-adversarial bystander in the player's scene.

    Same shape as ``_seat_opponent`` but marked ``npc_role_id="bystander"``
    with the default (neutral) disposition — a deck-crew chief who merely
    shares the hangar, not the duel target. The sealed-letter sourcing must
    NOT conscript this NPC into a 1v1 dogfight (Story 45-33 / ADR-116, at the
    live dispatch seam — the regression a Reviewer caught when 59-17's first
    fix seated any single location NPC).
    """
    npc = Npc(
        core=CreatureCore(
            name=name,
            description="A deck hand running pre-flight checks. Not a combatant.",
            personality="harried, neutral",
        )
    )
    npc.last_seen_location = LOCATION
    npc.npc_role_id = "bystander"
    snap.npcs.append(npc)


def _dogfight_dispatch() -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem="confrontation",
        params={"type": DOGFIGHT},
        idempotency_key="dogfight-engage-1",
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


@pytest.fixture
def otel_capture() -> InMemorySpanExporter:
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    # Attach before the dispatch runs so this exporter only sees spans this
    # test emits. The processor rides the module-scoped provider; shutdown
    # happens at provider teardown.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


async def test_dogfight_does_not_conscript_colocated_npc_seats_frame_default(
    space_opera_pack: GenrePack,
    otel_capture: InMemorySpanExporter,
) -> None:
    """ADR-153 §6: a co-located hostile NPC is NOT conscripted as the enemy ship.

    Reproduces the production reality: the router pass dispatches a dogfight
    with ``npcs_present=[]`` while a co-located hostile ("Vulture") is in the
    scene. Per §6 the location fallback no longer fires for a dogfight — the
    Other is sourced from the def frame, so the encounter seats a frame-default
    ship and the scene NPC is never seated as the opponent.

    RED today: the sealed-letter location fallback conscripts "Vulture" as blue,
    so ``blue.name == "Vulture"``.
    """
    snap = _snap_at_location()
    _seat_opponent(snap, name="Vulture")

    out = await run_confrontation_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=space_opera_pack,
        player_name=PLAYER,
        npcs_present=[],  # production reality (intent_router_pass line 167)
    )

    assert out.data == {}, (
        f"dispatch reported an engagement error instead of instantiating: {out.data!r}"
    )
    assert snap.encounter is not None, (
        "dogfight failed to seat via the production router path — a framed "
        "dogfight must seat a default-from-frame ship (ADR-153 §6)"
    )
    assert snap.encounter.encounter_type == DOGFIGHT
    roles = sorted(a.role for a in snap.encounter.actors)
    assert roles == ["blue", "red"], (
        f"expected red (player) + blue (opponent) seating, got {roles!r}"
    )
    # ADR-153 §6: the co-located NPC must NEVER be conscripted as the enemy
    # vessel — the Other is a ship/chassis from the def frame.
    blue = next(a for a in snap.encounter.actors if a.role == "blue")
    assert blue.name != "Vulture", (
        "a co-located scene NPC must not be seated as the dogfight opponent — "
        "the Other comes from the def frame (ADR-153 §6)"
    )
    assert all(a.name != "Vulture" for a in snap.encounter.actors), (
        "the co-located NPC must not be seated in the dogfight at all"
    )

    # OTEL wiring proof (CLAUDE.md OTEL principle): the success path emits
    # encounter.confrontation_initiated exactly once for this engagement.
    initiated = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "encounter.confrontation_initiated"
    ]
    assert len(initiated) == 1, (
        f"expected one encounter.confrontation_initiated span, got {len(initiated)}"
    )


async def test_dogfight_with_no_scene_opponent_seats_frame_default(
    space_opera_pack: GenrePack,
) -> None:
    """ADR-153 §6: no opponent in scene → seat the default-from-frame ship.

    A dogfight requires an Other (ADR-116), and the def frame IS that Other.
    With ``npcs_present=[]`` and no NPC anywhere, the dispatch must seat a
    frame-default enemy ship (a real chassis from ``opponent_default_stats``),
    not refuse — the §6 successor to the old "refuse one-sided" guard.

    RED today: with nothing to seat, the sealed-letter arity validator rejects
    and ``snapshot.encounter`` stays None.
    """
    snap = _snap_at_location()  # no opponent seated

    out = await run_confrontation_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=space_opera_pack,
        player_name=PLAYER,
        npcs_present=[],
    )

    assert out.data == {}, (
        f"a framed dogfight must seat the default-from-frame ship, not error: {out.data!r}"
    )
    assert snap.encounter is not None, (
        "a dogfight whose def carries opponent_default_stats must seat a "
        "frame-default enemy ship (ADR-153 §6), not refuse"
    )
    assert snap.encounter.encounter_type == DOGFIGHT
    roles = sorted(a.role for a in snap.encounter.actors)
    assert roles == ["blue", "red"], (
        f"expected red (player) + blue (frame-default opponent) seating, got {roles!r}"
    )


async def test_dogfight_with_only_a_bystander_seats_frame_default_not_bystander(
    space_opera_pack: GenrePack,
) -> None:
    """ADR-153 §6: a lone bystander is never seated; the frame default is.

    The pre-§6 fallback could conscript a same-location NPC into the duel. Under
    §6 the fallback never fires for a dogfight at all, so a neutral bystander in
    the scene is irrelevant: the dogfight seats the def-frame enemy ship, and the
    bystander is never seated as the opponent.

    RED today: the sealed-letter sourcing filters the bystander out → zero
    candidates → arity rejection, no encounter.
    """
    snap = _snap_at_location()
    _seat_bystander(snap, name="Deck Crew Chief")

    out = await run_confrontation_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=space_opera_pack,
        player_name=PLAYER,
        npcs_present=[],
    )

    assert out.data == {}, (
        f"a framed dogfight must seat the default-from-frame ship, not error: {out.data!r}"
    )
    assert snap.encounter is not None, (
        "a framed dogfight must seat a frame-default enemy ship (ADR-153 §6) "
        "regardless of an unrelated bystander in the scene"
    )
    blue = next(a for a in snap.encounter.actors if a.role == "blue")
    assert blue.name != "Deck Crew Chief", (
        "a neutral bystander must never be seated as the dogfight opponent — "
        "the Other comes from the def frame (ADR-153 §6)"
    )


async def test_dogfight_instantiates_when_opponent_passed_explicitly(
    space_opera_pack: GenrePack,
) -> None:
    """REGRESSION (stay GREEN): explicit npcs_present still seats red/blue.

    When the dispatch DOES carry an explicit opponent mention (the rarer path
    where the router has actor mentions), instantiation already works. The
    59-17 fix must not break it.
    """
    snap = _snap_at_location()

    out = await run_confrontation_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=space_opera_pack,
        player_name=PLAYER,
        npcs_present=[
            NpcMention(name="Vulture", role="hostile", side="opponent"),
        ],
    )

    assert out.data == {}, f"unexpected dispatch error: {out.data!r}"
    assert snap.encounter is not None
    assert snap.encounter.encounter_type == DOGFIGHT
    roles = sorted(a.role for a in snap.encounter.actors)
    assert roles == ["blue", "red"]
