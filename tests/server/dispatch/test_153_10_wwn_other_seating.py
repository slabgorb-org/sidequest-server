"""Story 153-10 [WWN-OTHER-SEATING] — on a vague target the native/WN seater
seats the scene-active narrative antagonist (a ``npc_pool`` member), NOT a
co-located ambient Monster-Manual bestiary entity (ADR-116 / ADR-059 / 126-32a).

Playtest follow-up (epic 153, from the 150-x full-stack /sq-playtest sweep):
under a Worlds-Without-Number-bound pack a player attacks a vague / non-roster
target ("I attack the Daggereyes" / "I attack the courier"). The intent router
names that threat as a free string. The 108-2 roster reconciliation
(``_resolve_opponent_from_roster``) then reaches PAST the scene-active
antagonist the narrator has been developing — who, having been minted only on a
PRIOR turn, lives in ``snapshot.npc_pool`` and not yet in ``snapshot.npcs`` —
and conscripts the only ``creature_id``-statted candidate it CAN see: a
co-located ambient bestiary monster sitting in ``snapshot.npcs`` from an earlier
Monster-Manual injection. The combat panel then seats the wrong Other
(playtest 150-14: "Mistos Warden 23/23" seated over the in-roster Daggereyes the
player attacked; playtest 150-12: "Restless Battlefield Ghost" over the attacked
courier). "vague target → MM grab; contrast a correct seat on a NAMED target."

Root cause — the WN sibling of 126-32a (the Fate seater fix):
``_resolve_opponent_from_roster`` (and ``_npc_fallback_at_location``) scan only
``snapshot.npcs``; its candidate filter is ``creature_id is not None`` — so a
narrative antagonist that is a *person* (no ``creature_id``) and/or lives in
``snapshot.npc_pool`` is invisible to the native seater, and an ambient bestiary
mob wins by default. The Fate seater (``_seed_fate_opponents``, 126-32a /
153-9) already consults ``snapshot.npc_pool`` for exactly this prior-turn
antagonist; the native/WN combat path does not.

This is the WN/native sibling of 153-9 ([FATE-OTHER-SEATING]); the two share the
same defect class (the seater grabs an ambient adversary over the narrator's
scene-active antagonist) on the two different ruleset paths.

Acceptance criteria (defined by TEA in RED — the sprint YAML carried none):

  AC-1  Under a WWN binding, when the router names a scene-active antagonist that
        lives in ``snapshot.npc_pool`` and a co-located ambient Monster-Manual
        bestiary entity sits in ``snapshot.npcs``, the POOL antagonist is seated
        as the Other — the ambient MM entity is NOT conscripted in its place.
  AC-2  The decline-the-MM-grab decision is observable on the GM panel: the
        lie-detector span names the declined MM entity, and the 108-2 *resolve*
        span (which would mean the MM grab happened) does NOT fire.
  AC-3  When the router names the EXACT ambient MM entity (the player really IS
        attacking the bestiary monster), it is seated directly — the
        pool-preference must not over-correct and refuse a legitimately-targeted
        bestiary adversary.
  AC-4  With NO pool/narrative antagonist present, the co-located ambient MM
        entity IS still conscripted (the 108-2 reconciliation / ADR-059 bound
        stats are preserved) — the fix keys on a pool antagonist EXISTING, it is
        not a blanket disable of roster reconciliation.

Contract pinned here drives the REAL ``instantiate_encounter_from_trigger``
production seam with a live WWN pack (``caverns_and_claudes``) — same altitude
as ``test_153_9_fate_other_seating.py``; no source-text assertions.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_WWN_PACK = "caverns_and_claudes"
_WORLD = "beneath_sunden"
_LOC = "collapsed_gallery"

# The scene-active antagonist the narrator developed last turn (lives in the
# pool, not yet promoted to snapshot.npcs) and the ambient bestiary mob already
# sitting in snapshot.npcs from a prior Monster-Manual injection.
_ANTAGONIST = "Daggereye Skirmisher"
_MM_ENTITY = "Mistos Warden"


def _has_wwn_content() -> bool:
    try:
        find_pack_path(_WWN_PACK)
        return True
    except PackNotFound:
        return False


pytestmark = pytest.mark.skipif(not _has_wwn_content(), reason=f"{_WWN_PACK} pack not on disk")


def _load_wwn_pack():
    """Load the real WWN-bound pack through the production loader."""
    try:
        pack = load_genre_pack(find_pack_path(_WWN_PACK))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))
    assert pack.rules and pack.rules.ruleset == "wwn", (
        f"{_WWN_PACK} must bind the WWN ruleset for this seater test; "
        f"got {pack.rules.ruleset if pack.rules else None!r}"
    )
    return pack


def _combat_encounter_type(pack) -> str:
    """The pack's first category=='combat' confrontation type (no hardcoding —
    survives a content rename of the confrontation key)."""
    for cdef in pack.rules.confrontations or []:
        if cdef.category == "combat":
            return cdef.confrontation_type
    raise AssertionError(f"{_WWN_PACK} authors no combat-category confrontation")


def _ambient_mm_entity(
    name: str = _MM_ENTITY,
    *,
    creature_id: str = "Warden",
    hp: int = 23,
    location: str | None = _LOC,
) -> Npc:
    """A co-located, ``creature_id``-statted, hostile bestiary mob — exactly the
    shape ``_resolve_opponent_from_roster`` treats as a conscriptable candidate
    (the ambient Monster-Manual entity, ADR-059)."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="An ambient warden left over from a prior encounter sweep.",
            personality="Implacable.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
        ),
        creature_id=creature_id,
        threat_level=3,
        disposition=-30,
        last_seen_location=location,
        last_seen_turn=4,
    )


def _pool_antagonist(
    name: str = _ANTAGONIST,
    *,
    location: str | None = _LOC,
) -> NpcPoolMember:
    """The narrator's scene-active antagonist, established on a PRIOR turn and
    sitting in ``snapshot.npc_pool`` (not yet promoted to ``snapshot.npcs``).

    A *person* antagonist (``is_creature=False`` — no ``creature_id``), hostile,
    co-located, ratified (``observation_pending=False``). This is the entity the
    native seater must prefer over the ambient bestiary mob."""
    return NpcPoolMember(
        name=name,
        role="hostile",
        pronouns="they/them",
        appearance="A wiry skirmisher with a notched dagger.",
        disposition=Disposition(-25),
        drawn_from="narrator_invented",
        is_creature=False,
        last_seen_turn=4,
        last_seen_location=location,
    )


def _player_sam(name: str = "Sam") -> Character:
    """A minimal PC carrying the WWN ability block so the native hp_depletion
    seater's initiative roll can resolve the player's DEXTERITY
    (``cfg.attribute_map["DEXTERITY"] == "DEX"``) — without it the seater fails
    loud at ``_roll_and_persist_initiative`` before the seating contract is
    even exercised."""
    return Character(
        core=CreatureCore(
            name=name,
            description="A scrappy delver.",
            personality="Gritty.",
            inventory=Inventory(),
        ),
        char_class="Rogue",
        race="Human",
        backstory="A wandering survivor.",
        stats={"STR": 10, "DEX": 12, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


def _snapshot_with(
    *,
    npcs: list[Npc],
    pool: list[NpcPoolMember],
    player: str = "Sam",
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=_WWN_PACK,
        world_slug=_WORLD,
        turn_manager=TurnManager(interaction=5),
    )
    snap.characters.append(_player_sam(player))
    snap.character_locations[player] = _LOC
    for npc in npcs:
        snap.npcs.append(npc)
    for member in pool:
        snap.npc_pool.append(member)
    return snap


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _opponents(enc) -> list[str]:
    return [a.name for a in enc.actors if a.side == "opponent"]


# ---------------------------------------------------------------------------
# AC-1 — the headline bug: the pool antagonist is seated, NOT the ambient MM mob.
# ---------------------------------------------------------------------------


def test_wwn_seats_pool_antagonist_over_ambient_mm_entity():
    """The router names the scene-active antagonist "Daggereye Skirmisher" (a
    person in ``npc_pool``); an ambient bestiary "Mistos Warden" (``creature_id``,
    in-room, hostile, HP 23) is ALSO present in ``snapshot.npcs``. Under the WWN
    binding the named pool antagonist must be seated as the Other — the ambient
    Monster-Manual mob must NOT be conscripted in his place (the 150-14
    "Mistos Warden over the Daggereyes" repro)."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_ambient_mm_entity()],
        pool=[_pool_antagonist()],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_WWN_PACK,
        materialized_threat=NpcMention(name=_ANTAGONIST, role="hostile", side="opponent"),
    )

    assert enc is not None, "WWN combat confrontation failed to instantiate"
    assert _opponents(enc) == [_ANTAGONIST], (
        "under WWN the router-named scene-active pool antagonist must be seated "
        f"as the Other; got {_opponents(enc)!r} (an ambient Monster-Manual "
        "bestiary entity was conscripted on a vague/non-roster target)"
    )
    assert _MM_ENTITY not in _opponents(enc), (
        f"the ambient co-located bestiary mob {_MM_ENTITY!r} must not be seated "
        "as the Other when a scene-active pool antagonist was named"
    )


# ---------------------------------------------------------------------------
# AC-2 — the decline-the-MM-grab decision is observable (OTEL lie-detector).
# ---------------------------------------------------------------------------


def test_wwn_decline_mm_grab_emits_decision_span(otel_capture):
    """Declining to conscript the ambient bestiary mob in favour of the pool
    antagonist is a subsystem decision and MUST be observable on the GM panel —
    and the 108-2 *resolve* span (which fires only when a roster creature is
    actually conscripted) must NOT fire. Without the span the GM cannot tell the
    seater preferred the pool antagonist from the narrator improvising
    (CLAUDE.md OTEL Observability Principle / No Silent Fallbacks)."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_ambient_mm_entity()],
        pool=[_pool_antagonist()],
    )

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_WWN_PACK,
        materialized_threat=NpcMention(name=_ANTAGONIST, role="hostile", side="opponent"),
    )

    spans = {s.name: s for s in otel_capture.get_finished_spans()}
    # The MM conscription must NOT have been taken.
    assert "encounter.opponent_resolved_from_roster" not in spans, (
        "the 108-2 reconciliation fired — an ambient Monster-Manual bestiary "
        "entity was conscripted instead of seating the named pool antagonist"
    )
    # The decline decision must be recorded, naming the bestiary mob it refused.
    assert "encounter.roster_resolution_skipped" in spans, (
        "declined-conscription decision span not emitted; the GM panel cannot "
        f"verify the WWN seater preferred the pool antagonist. saw {sorted(spans)}"
    )
    attrs = spans["encounter.roster_resolution_skipped"].attributes or {}
    assert attrs.get("declined_name") == _MM_ENTITY, (
        f"decline span must name the refused bestiary mob; got {dict(attrs)!r}"
    )
    # And the antagonist that WAS seated is observable as a joined participant.
    joined = [s for s in otel_capture.get_finished_spans() if s.name == "participant.joined"]
    opponent_names = {
        (s.attributes or {}).get("name")
        for s in joined
        if (s.attributes or {}).get("side") == "opponent"
    }
    assert opponent_names == {_ANTAGONIST}, (
        f"the seated Other on the GM panel must be the pool antagonist; got {opponent_names!r}"
    )


# ---------------------------------------------------------------------------
# AC-3 — an EXACTLY-named ambient MM entity is still seated (no over-correction).
# ---------------------------------------------------------------------------


def test_wwn_exact_mm_target_is_still_seated():
    """When the player genuinely attacks the bestiary monster and the router
    names it exactly ("Mistos Warden"), the Warden IS the target and must be
    seated. The pool-preference only suppresses grabbing an ambient mob OVER a
    named pool antagonist; it must never drop an exactly-named roster
    adversary."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_ambient_mm_entity()],
        pool=[_pool_antagonist()],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_WWN_PACK,
        materialized_threat=NpcMention(name=_MM_ENTITY, role="hostile", side="opponent"),
    )

    assert enc is not None
    assert _opponents(enc) == [_MM_ENTITY], (
        "an exactly-named in-roster bestiary adversary must be seated as the "
        f"Other under WWN; got {_opponents(enc)!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 — with NO pool antagonist, the ambient MM entity is STILL conscripted
#        (108-2 / ADR-059 preserved — the fix keys on a pool antagonist, not a
#        blanket disable).
# ---------------------------------------------------------------------------


def test_wwn_conscripts_mm_entity_when_no_pool_antagonist():
    """Regression guard / keys-on-pool proof: with an empty ``npc_pool`` there is
    no scene-active antagonist to prefer, so the router's vague free-string MUST
    still reconcile to the co-located ``creature_id``-statted bestiary mob —
    preserving the bound creature's WWN COMBAT stats is the whole point of 108-2
    off the pool path (ADR-059). The fix must not refuse a legitimate ambient
    adversary just because the target was vague."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_ambient_mm_entity()],
        pool=[],
    )

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_WWN_PACK,
        materialized_threat=NpcMention(name="the hill bandits", role="hostile", side="opponent"),
    )

    assert enc is not None
    assert _opponents(enc) == [_MM_ENTITY], (
        "with no pool antagonist, a vague target must still reconcile to the "
        f"co-located statted bestiary mob (108-2); got {_opponents(enc)!r}"
    )
