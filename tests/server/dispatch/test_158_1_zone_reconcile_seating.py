"""Story 158-1 (RED) — WWN-COMBAT-NEVER-SEATS-ON-FRESH-DESCENT.

Playtest forensics (2026-06-22, beneath_sunden / caverns_and_claudes, WWN, fresh
solo Chico/Warrior descent, save ``8b54610d``, turn 7): the narrator surfaced a
live hostile — a beetle/gnaw swarm "pooling at your feet, mandibles testing the
air" — in the PC's current scene ("The Winding Catacomb", region ``exp002.r3``).
The player struck with the bluntest possible attack ("I attack the beetle swarm
with my short sword"). **No confrontation seated.** ``encounter_type=None``,
``total_beats_fired=0``, ``in_combat=False`` — and the narrator then improvised
the entire fight AND fabricated a player HP value ("You have four hit points
remaining") while the engine + character panel showed HP 10/10, unchanged. The
invented "blade can't disperse it" rule was even persisted as a Lore footnote.

FIXER root-cause (forensics on the shared DB, NOT inference): the attacked
creature was in a DIFFERENT ZONE than the PC. The authored Gnaw-Swarm IS seeded
in ``snapshot.npcs`` (``creature_id=gnaw_swarm``, disposition −20) but its
``location`` / ``last_seen_location`` is the entrance room ("Under the Rope"),
NOT the PC's current scene — the swarm is the *entrance's* authored opponent and
the PC had descended past it, while the narrator dragged it forward in prose.
Per ADR-116 ("a confrontation requires a co-located Other") the seater's
co-location predicate (``_resolve_opponent_from_roster`` /
``_npc_fallback_at_location``) finds NO candidate at the PC's scene, so the
router-named vague threat ("the beetle swarm") is seated as a fabricated HP-stub
— or nothing seats — and the bound, WWN-statted creature never reaches the fight.
The contrast save ``697cbc14`` DID seat: there the opponent's ``location`` MATCHED
the PC's scene. The only material difference between seat and no-seat is target
co-location.

The product decision (story title, SOUL "Yes, And" + "Diamonds and Coal"): when
the narrator *surfaces* a creature on-stage in the PC's current scene, reconcile
that creature's engine location to the PC's current scene so the seater finds a
co-located Other and seats the BOUND creature — instead of leaving the mechanics
ungrounded for the narrator to improvise. The reconciliation is **turn-scoped**:
it applies ONLY to a creature the narrator engaged THIS turn (the turn's combat
target / a this-turn surfacing), NEVER a blanket region-wide widening of the
co-location filter (ADR-116 deliberately forbids region-wide sourcing —
``_resolve_opponent_from_roster`` docstring: "Region-wide sourcing is deliberately
NOT done … the exact over-reach ADR-116 guards against").

Acceptance criteria (from ``context-story-158-1.md``):

  AC-1/AC-2  A hostile, ``creature_id``-statted Monster-Manual creature SURFACED
             THIS TURN (``manual_origin``, ``last_seen_turn == current``) but
             carrying a STALE ``last_seen_location`` / ``location`` is reconciled
             to the PC's current scene, so a blunt vague-target combat trigger
             ("I attack the beetle swarm") SEATS the BOUND creature as the Other
             (not a fabricated stub, not nothing).
  AC-3       The zone-reconcile decision is observable on the GM panel: a
             ``encounter.creature_zone_reconciled`` span fires naming the creature
             and the from/to zones; the stub-mint lie-detector
             (``encounter.opponent_minted_stub``) does NOT fire; the bound creature
             resolves (``encounter.opponent_resolved_from_roster`` fires).
  AC-4       No over-reach: a hostile statted creature NOT surfaced this turn
             (``last_seen_turn`` on a PRIOR turn, stale location) is NOT
             reconciled and NOT seated — its location is left untouched and no
             reconcile span fires. The signal is "engaged this turn", not "any
             stale roster creature".
  AC-5       Reconciliation is a location fix, not a respawn: the reconciled
             creature keeps its bound WWN HP (``hp=6/6``), not a fabricated
             HP-stub value.

Contract drives the REAL ``instantiate_encounter_from_trigger`` production seam
with a live WWN pack (``caverns_and_claudes``) — same altitude as
``test_153_10_wwn_other_seating.py`` / ``test_153_9_fate_other_seating.py``; no
source-text assertions (CLAUDE.md "No Source-Text Wiring Tests").

RED on the feature branch: there is no zone reconciliation today, so the
stale-located swarm is invisible to the co-location predicate — the vague threat
seats a stub, the bound Gnaw-Swarm is NOT the Other, and
``encounter.creature_zone_reconciled`` never fires. GREEN once a surfaced
creature's zone is reconciled to the PC's scene before the seater projects
co-located Others.
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
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_WWN_PACK = "caverns_and_claudes"
_WORLD = "beneath_sunden"

# The PC's CURRENT scene after a fresh descent (the narrator surfaced the swarm
# here); the swarm's stale stored zone is the entrance room it was authored into.
_PC_SCENE = "The Winding Catacomb"
_STALE_ZONE = "Under the Rope"

# The authored bestiary swarm and the bluntly-vague way the player named it (the
# exact "attack X with Y" form the playtest used — a free string the router
# carries that does NOT equal the creature's authored name).
_SWARM_NAME = "Gnaw-Swarm"
_SWARM_HP = 6
_VAGUE_TARGET = "the beetle swarm"

# The interaction turn under test; "surfaced this turn" == last_seen_turn == this.
_TURN = 7


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
        f"{_WWN_PACK} must bind the WWN ruleset for this seating test; "
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


def _swarm(
    *,
    last_seen_location: str | None,
    last_seen_turn: int,
    location: str | None = None,
    hp: int = _SWARM_HP,
) -> Npc:
    """The authored Gnaw-Swarm as it sits in ``snapshot.npcs``: a hostile,
    ``creature_id``-statted, Monster-Manual bestiary mob. ``last_seen_location`` /
    ``last_seen_turn`` are parameterised so a test can pose it either as
    SURFACED-THIS-TURN-but-stale (the bug) or genuinely off-stage (AC-4)."""
    return Npc(
        core=CreatureCore(
            name=_SWARM_NAME,
            description="A roiling carpet of gnawing beetles.",
            personality="Mindless hunger.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
        ),
        creature_id="gnaw_swarm",
        manual_origin=True,
        threat_level=1,
        disposition=-20,
        location=location if location is not None else last_seen_location,
        last_seen_location=last_seen_location,
        last_seen_turn=last_seen_turn,
    )


def _player_chico(name: str = "Chico") -> Character:
    """A minimal WWN PC carrying the ability block so the native hp_depletion
    seater's initiative roll resolves DEXTERITY — without it the seater fails
    loud at ``_roll_and_persist_initiative`` before the seating contract is even
    exercised (same scaffolding as test_153_10's ``_player_sam``)."""
    return Character(
        core=CreatureCore(
            name=name,
            description="A scrappy delver with a short sword.",
            personality="Gritty.",
            inventory=Inventory(),
        ),
        char_class="Warrior",
        race="Human",
        backstory="A wandering survivor.",
        stats={"STR": 12, "DEX": 12, "CON": 10, "INT": 10, "WIS": 10, "CHA": 10},
    )


def _snapshot_with(
    *, npcs: list[Npc], player: str = "Chico", interaction: int = _TURN
) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=_WWN_PACK,
        world_slug=_WORLD,
        turn_manager=TurnManager(interaction=interaction),
    )
    snap.characters.append(_player_chico(player))
    snap.character_locations[player] = _PC_SCENE
    for npc in npcs:
        snap.npcs.append(npc)
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


def _seat_swarm_attack(pack, snap):
    """Drive the production seater exactly as the live router does on a blunt
    attack: the router names the threat as a FREE STRING ("the beetle swarm")
    that does not equal the creature's authored name, handed to the seater as the
    ``materialized_threat``. ``npcs_present=[]`` mirrors the pre-narrator
    dispatch (intent_router_pass hands the subsystem no explicit mentions)."""
    return instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Chico",
        npcs_present=[],
        genre_slug=_WWN_PACK,
        materialized_threat=NpcMention(name=_VAGUE_TARGET, role="hostile", side="opponent"),
    )


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — the headline bug: a swarm surfaced THIS turn at a stale zone is
# reconciled to the PC's scene and the BOUND creature is seated as the Other.
# ---------------------------------------------------------------------------


def test_surfaced_stale_swarm_is_reconciled_and_seated():
    """The Gnaw-Swarm is surfaced THIS turn (``last_seen_turn == 7``,
    ``manual_origin``) but its stored zone is stale ("Under the Rope" ≠ the PC's
    "The Winding Catacomb"). A blunt vague-target attack must reconcile the
    surfaced creature's zone to the PC's scene and seat the BOUND Gnaw-Swarm as
    the Other — the forensic ``8b54610d`` "never seats" repro.

    RED: with no zone reconciliation the stale swarm is invisible to the
    co-location predicate, so the vague threat seats a fabricated stub and the
    bound Gnaw-Swarm is NOT the Other (or nothing seats at all)."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_swarm(last_seen_location=_STALE_ZONE, last_seen_turn=_TURN)]
    )

    enc = _seat_swarm_attack(pack, snap)

    assert enc is not None, (
        "WWN combat never seated against a creature the narrator surfaced at the "
        "PC's feet — the stale-zone swarm was invisible to the co-location "
        "projection (the forensic 8b54610d 'never seats' repro)"
    )
    assert _opponents(enc) == [_SWARM_NAME], (
        "the bound Monster-Manual Gnaw-Swarm the narrator surfaced this turn must "
        f"be seated as the Other after zone reconciliation; got {_opponents(enc)!r} "
        "(a fabricated stub for the router's vague free-string was seated instead)"
    )


def test_reconciled_swarm_location_matches_pc_scene():
    """After reconciliation the surfaced creature's engine location must equal the
    PC's current scene — the durable state fix so the projection (and every later
    co-location check this turn) treats it as a present Other.

    RED: the swarm's location is left at the stale "Under the Rope"."""
    pack = _load_wwn_pack()
    swarm = _swarm(last_seen_location=_STALE_ZONE, last_seen_turn=_TURN)
    snap = _snapshot_with(npcs=[swarm])

    _seat_swarm_attack(pack, snap)

    reconciled = next(n for n in snap.npcs if n.core.name == _SWARM_NAME)
    assert reconciled.last_seen_location == _PC_SCENE, (
        "a creature surfaced at the PC's scene must have its last_seen_location "
        f"reconciled to {_PC_SCENE!r}; still {reconciled.last_seen_location!r} "
        "(stale) — the router's projection has no co-located Other and declines"
    )


# ---------------------------------------------------------------------------
# AC-5 — reconciliation is a location fix, NOT a respawn: bound WWN HP survives.
# ---------------------------------------------------------------------------


def test_reconciled_swarm_keeps_bound_hp_not_a_stub():
    """The whole point of seating the bound creature (ADR-059) is its WWN-balanced
    stats reach the fight. The reconciled Gnaw-Swarm must keep its bestiary HP
    (6/6), never reset to a fabricated stub value.

    RED: today the bound swarm is never seated, so its stats never reach the
    encounter — a HP-stub stands in for it."""
    pack = _load_wwn_pack()
    swarm = _swarm(last_seen_location=_STALE_ZONE, last_seen_turn=_TURN, hp=_SWARM_HP)
    snap = _snapshot_with(npcs=[swarm])

    enc = _seat_swarm_attack(pack, snap)

    assert enc is not None and _opponents(enc) == [_SWARM_NAME], (
        "precondition: the bound Gnaw-Swarm must be the seated Other"
    )
    bound = next(n for n in snap.npcs if n.core.name == _SWARM_NAME)
    assert bound.core.hp.current == _SWARM_HP and bound.core.hp.max == _SWARM_HP, (
        f"reconciliation is a location fix, not a respawn — the bound swarm must "
        f"keep its WWN HP {_SWARM_HP}/{_SWARM_HP}; got "
        f"{bound.core.hp.current}/{bound.core.hp.max}"
    )


# ---------------------------------------------------------------------------
# AC-3 — the zone-reconcile decision is observable on the GM panel (OTEL).
# ---------------------------------------------------------------------------


def test_zone_reconcile_emits_decision_span(otel_capture):
    """Reconciling a surfaced creature's zone is a subsystem decision and MUST be
    observable on the GM panel — the lie-detector that the engine MADE the Other
    present rather than the narrator improvising. The span names the creature and
    the from→to zones (CLAUDE.md OTEL Observability Principle / No Silent
    Fallbacks). The stub-mint lie-detector must NOT fire (no stub was minted), and
    the bound creature must resolve from the roster.

    RED: no reconciliation happens, so ``encounter.creature_zone_reconciled``
    never fires; instead the seater mints a stub
    (``encounter.opponent_minted_stub``)."""
    pack = _load_wwn_pack()
    snap = _snapshot_with(
        npcs=[_swarm(last_seen_location=_STALE_ZONE, last_seen_turn=_TURN)]
    )

    _seat_swarm_attack(pack, snap)

    spans = {s.name: s for s in otel_capture.get_finished_spans()}

    assert "encounter.creature_zone_reconciled" in spans, (
        "the zone-reconcile decision span did not fire; the GM panel cannot verify "
        "the engine reconciled the surfaced creature to the PC's scene. saw "
        f"{sorted(spans)}"
    )
    attrs = spans["encounter.creature_zone_reconciled"].attributes or {}
    assert attrs.get("from_location") == _STALE_ZONE, (
        f"reconcile span must record the stale from-zone; got {dict(attrs)!r}"
    )
    assert attrs.get("to_location") == _PC_SCENE, (
        f"reconcile span must record the PC's scene as the to-zone; got {dict(attrs)!r}"
    )
    assert (attrs.get("creature_id") == "gnaw_swarm") or (
        attrs.get("creature_name") == _SWARM_NAME
    ), f"reconcile span must name the reconciled creature; got {dict(attrs)!r}"

    # No stub was minted — the bound creature reached the fight instead.
    assert "encounter.opponent_minted_stub" not in spans, (
        "the seater minted a fabricated stub — the surfaced bound creature was not "
        "reconciled into the scene (the forensic HP-stub / ungrounded-fight shape)"
    )
    # The bound creature resolved from the roster (108-2 / ADR-059).
    assert "encounter.opponent_resolved_from_roster" in spans, (
        "the bound Gnaw-Swarm was not resolved from the roster after reconciliation; "
        f"saw {sorted(spans)}"
    )


# ---------------------------------------------------------------------------
# AC-4 — no over-reach: a creature NOT surfaced this turn is left untouched.
# ADR-116 forbids region-wide sourcing; the signal is "engaged THIS turn".
# ---------------------------------------------------------------------------


def test_offstage_stale_swarm_is_not_reconciled(otel_capture):
    """A hostile statted creature last seen on a PRIOR turn at a different zone is
    genuinely off-stage — the narrator did NOT surface it this turn. The fix must
    NOT teleport it to the PC's feet: its location stays put and no reconcile span
    fires. This pins that the signal is "engaged this turn", not "any stale roster
    creature" (ADR-116: region-wide sourcing is the over-reach the seater guards
    against).

    Guard: passes today (no reconciliation exists) and MUST keep passing — a fix
    that blanket-reconciles every stale creature breaks it."""
    pack = _load_wwn_pack()
    # last_seen_turn well before the current turn → not surfaced this turn.
    offstage = _swarm(last_seen_location=_STALE_ZONE, last_seen_turn=_TURN - 4)
    snap = _snapshot_with(npcs=[offstage])

    _seat_swarm_attack(pack, snap)

    untouched = next(n for n in snap.npcs if n.core.name == _SWARM_NAME)
    assert untouched.last_seen_location == _STALE_ZONE, (
        "an off-stage creature (last seen on a prior turn, elsewhere) must NOT be "
        f"reconciled to the PC's scene; its location moved to "
        f"{untouched.last_seen_location!r} — the fix over-reached (ADR-116 forbids "
        "region-wide sourcing)"
    )
    assert untouched.last_seen_turn == _TURN - 4, (
        "an off-stage creature's last_seen_turn must not be advanced by a turn it "
        "did not appear in"
    )

    spans = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.creature_zone_reconciled" not in spans, (
        "a zone-reconcile span fired for an off-stage creature — reconciliation "
        "must be scoped to creatures the narrator engaged THIS turn"
    )
    assert _SWARM_NAME not in _opponents_or_empty(snap), (
        "an off-stage creature must not be conscripted as the Other on a vague "
        "target"
    )


# ---------------------------------------------------------------------------
# AC-4 (boundary guards added in rework — Reviewer flagged two over-reach holes
# the gap-4 guard above missed). ADR-116 no-region-wide-sourcing, sharpened.
# ---------------------------------------------------------------------------


def test_never_surfaced_creature_not_reconciled_on_turn_one(otel_capture):
    """A manual_origin adversary with ``last_seen_turn == 0`` was NEVER surfaced:
    0 is the ``Npc`` model's documented "never mentioned in this session" default
    (the turn counter starts at 1). At ``interaction == 1`` the recency window
    ``0 <= 1 - 0 <= 1`` is True, so a naive filter reconciles+seats a creature the
    narrator never put on-stage — the exact region-wide over-reach ADR-116 and AC-4
    forbid, and a direct contradiction of the helper's own docstring ("surfaced
    THIS turn or the immediately-preceding one"; ``last_seen_turn == 0`` is
    neither). It must NOT be reconciled.

    RED before the rework fix (``n.last_seen_turn > 0`` guard): the never-seen
    creature passes the window at turn 1, is reconciled, and is seated as the
    Other."""
    pack = _load_wwn_pack()
    never_seen = _swarm(last_seen_location=_STALE_ZONE, last_seen_turn=0)
    snap = _snapshot_with(npcs=[never_seen], interaction=1)

    _seat_swarm_attack(pack, snap)

    untouched = next(n for n in snap.npcs if n.core.name == _SWARM_NAME)
    assert untouched.last_seen_location == _STALE_ZONE, (
        "a never-surfaced creature (last_seen_turn==0, the model's 'never mentioned' "
        "default) must NOT be reconciled to the PC's scene at turn 1; its location "
        f"moved to {untouched.last_seen_location!r} — the recency window admitted a "
        "creature the narrator never put on-stage (ADR-116 over-reach / AC-4)"
    )
    spans = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.creature_zone_reconciled" not in spans, (
        "a zone-reconcile span fired for a never-surfaced creature (last_seen_turn==0)"
    )
    assert _SWARM_NAME not in _opponents_or_empty(snap), (
        "a never-surfaced creature must not be conscripted as the Other"
    )


def test_unlocated_creature_not_reconciled(otel_capture):
    """A manual_origin adversary with NO location at all — both ``last_seen_location``
    and ``location`` are None — is not a zone-DRIFT case: there is no stale zone to
    reconcile away from, only absence. The candidate filter checks only
    ``!= location``, so two None fields both pass (``None != "<scene>"``), and the
    reconcile span would fire with ``from_location == ""`` — a phantom "drift" on
    the GM-panel lie-detector (the very telemetry the project trusts to catch
    improvisation). An unlocated creature must NOT be reconciled.

    RED before the rework fix (require a non-None location field): the None-located
    creature passes the filter, is reconciled, and the span fires with an empty
    from_location."""
    pack = _load_wwn_pack()
    # last_seen_location=None → _swarm leaves location None too → both fields absent,
    # surfaced this turn (last_seen_turn == interaction) so only the missing-location
    # guard — not the recency window — can exclude it.
    unlocated = _swarm(last_seen_location=None, last_seen_turn=_TURN)
    snap = _snapshot_with(npcs=[unlocated])
    assert unlocated.location is None and unlocated.last_seen_location is None, (
        "fixture precondition: the unlocated creature carries no location at all"
    )

    _seat_swarm_attack(pack, snap)

    spans = {s.name for s in otel_capture.get_finished_spans()}
    assert "encounter.creature_zone_reconciled" not in spans, (
        "a zone-reconcile span fired for a creature with no location (both fields "
        "None) — it has no stale zone to drift from; a from_location='' span is a "
        "phantom drift on the GM panel"
    )
    assert _SWARM_NAME not in _opponents_or_empty(snap), (
        "an unlocated creature must not be conscripted as the Other"
    )


def _opponents_or_empty(snap) -> list[str]:
    enc = snap.encounter
    if enc is None:
        return []
    return [a.name for a in enc.actors if a.side == "opponent"]
