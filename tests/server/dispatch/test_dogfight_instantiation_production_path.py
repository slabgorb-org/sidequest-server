"""Story 59-17 — dogfight confrontation must instantiate via the production path.

The 2026-05-27 coyote_star playtest surfaced a dogfight that never engaged.
Root cause (measured, not asserted — see the story session file):

  Production confrontation engagement is router-driven (Story 59-4 / ADR-113).
  The pre-narrator pass ``intent_router_pass.execute_intent_router_pre_narrator_pass``
  invokes the dispatch bank with a HARDCODED ``npcs_present=[]`` (it has no
  explicit actor mentions to hand the confrontation subsystem). For an
  ordinary adversarial encounter that is fine — ``instantiate_encounter_from_trigger``
  sources the opponent from ``snapshot.npcs`` at the player's location
  (``_npc_fallback_at_location``, story 45-18/45-52).

  But a dogfight is ``ResolutionMode.sealed_letter_lookup``, and the
  instantiator deliberately SKIPS the location fallback for sealed-letter
  encounters. So with an empty ``npcs_present`` the sealed-letter arity
  validator sees zero opponents and raises ``SealedLetterArityError`` —
  ``run_confrontation_dispatch`` catches it and returns an error, leaving
  ``snapshot.encounter`` unset. The duel can never start in real play even
  though a hostile pilot is right there in the scene.

  This is the ADR-116 family ("a confrontation requires an Other") — the
  Other exists in the scene but is never SEATED at instantiation.

These tests drive the *real* production seam — ``run_confrontation_dispatch``,
the router's live engager — with the same empty ``npcs_present`` the router
pass supplies. They are the wiring tests for 59-17.

RED today:
  - ``test_dogfight_instantiates_via_router_dispatch_with_scene_opponent``
    FAILS — encounter is never seated.

Guards that must stay GREEN (so the fix doesn't overcorrect):
  - ``test_dogfight_dispatch_without_any_opponent_refuses_one_sided`` — with
    no opponent anywhere, the dispatch must NOT silently seat a one-sided
    duel (ADR-116). It refuses; no encounter.
  - ``test_dogfight_instantiates_when_opponent_passed_explicitly`` — the
    pre-existing explicit-``npcs_present`` path keeps working.

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


async def test_dogfight_instantiates_via_router_dispatch_with_scene_opponent(
    space_opera_pack: GenrePack,
    otel_capture: InMemorySpanExporter,
) -> None:
    """RED (59-17): the live router seat path must seat the scene opponent.

    Reproduces the production reality: the router pass dispatches a dogfight
    with ``npcs_present=[]`` while exactly one hostile opponent is present in
    the scene (``snapshot.npcs`` at the player's location). The encounter MUST
    instantiate with the red (player) + blue (opponent) seating the
    sealed-letter resolver requires.

    Fails today: sealed-letter skips the location fallback, so the opponent
    is never seated and ``run_confrontation_dispatch`` returns
    ``sealed_letter_arity_rejected`` with no encounter.
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
        "dogfight failed to instantiate via the production router path — "
        "snapshot.encounter is still None even though a hostile opponent is "
        "in the scene"
    )
    assert snap.encounter.encounter_type == DOGFIGHT
    roles = sorted(a.role for a in snap.encounter.actors)
    assert roles == ["blue", "red"], (
        f"expected red (player) + blue (opponent) seating, got {roles!r}"
    )
    # The opponent seated as blue must be the hostile pilot from the scene,
    # not a phantom — ADR-116 "requires an Other" means a real Other.
    blue = next(a for a in snap.encounter.actors if a.role == "blue")
    assert blue.name == "Vulture"

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


async def test_dogfight_dispatch_without_any_opponent_refuses_one_sided(
    space_opera_pack: GenrePack,
) -> None:
    """GUARD (stay GREEN): no opponent in scene → no one-sided duel.

    ADR-116 — a confrontation requires an Other. With ``npcs_present=[]`` AND
    no NPC anywhere in the scene, the dispatch must refuse rather than seat a
    phantom blue. This keeps the 59-17 fix from overcorrecting into seating an
    imaginary opponent just to satisfy the arity check.
    """
    snap = _snap_at_location()  # no opponent seated

    out = await run_confrontation_dispatch(
        _dogfight_dispatch(),
        snapshot=snap,
        pack=space_opera_pack,
        player_name=PLAYER,
        npcs_present=[],
    )

    assert snap.encounter is None, (
        "a dogfight with zero available opponents must NOT instantiate a "
        "one-sided duel (ADR-116 requires an Other)"
    )
    assert out.data.get("error") is not None, (
        "expected an engagement-gap error in the dispatch output so the "
        "watcher/GM panel sees the refusal, not a silent no-op"
    )


async def test_dogfight_with_only_a_bystander_present_refuses(
    space_opera_pack: GenrePack,
) -> None:
    """GUARD (Story 45-33 at the dispatch seam): a lone bystander is not the Other.

    The first 59-17 fix relaxed the sealed-letter fallback so it seated ANY
    single location NPC — which silently conscripted a neutral bystander into
    the duel (count == 1 passes the arity check). ADR-116 requires "an Other,"
    not "any warm body." With ``npcs_present=[]`` and only a neutral
    ``npc_role_id="bystander"`` NPC at the location, the sealed-letter
    sourcing must filter it out → zero candidates → loud arity refusal, no
    encounter. (The module-level twin is
    ``test_encounter_lifecycle.py::test_sealed_letter_empty_npcs_present_raises_without_consuming_fallback``;
    this is its live-dispatch counterpart.)
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

    assert snap.encounter is None, (
        "a lone bystander must NOT be seated as the duel opponent — the "
        "sealed-letter sourcing must require an adversary, not any "
        "same-location NPC (Story 45-33 / ADR-116)"
    )
    assert out.data.get("error") == "sealed_letter_arity_rejected", (
        "expected a loud sealed_letter_arity_rejected refusal (got 0 "
        f"adversaries), not a silent bystander seat; got {out.data!r}"
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
