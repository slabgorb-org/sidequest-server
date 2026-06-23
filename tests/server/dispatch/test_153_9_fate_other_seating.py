"""Story 153-9 [FATE-OTHER-SEATING] — the seater names the scene-active
antagonist, not a same-surname roster NPC (ADR-116 / ADR-139 / ADR-144).

Playtest follow-up (epic 153, from the 150-x full-stack /sq-playtest sweep):
under a Fate-bound pack the intent router names the confrontation's Other as a
free string (``materialized_threat`` — the antagonist the narrator just put on
the page, e.g. "Silas Vance"). The 108-2 roster reconciliation
(``_resolve_opponent_from_roster``) then conscripts a *different* co-located,
``creature_id``-statted, hostile NPC who merely shares the surname
("Marguerite Vance") and seats HER as the Other — a player-visible identity
split: the prose fights Silas, the combat panel seats Marguerite.

That reconciliation exists for a WWN/SWN reason (ADR-059 Monster-Manual: keep a
BOUND bestiary creature's already-balanced COMBAT hp stats). **Under Fate that
reason does not exist** — a Fate conflict resolves against the Other's
``FateSheet`` stress (ADR-143/144 "Bind the Ruleset"), never native
hp_depletion, so there is no bound-hp value to preserve and conscripting an
ambient adversary over the narrator's named antagonist is never right. This is
the Fate sibling of the 150-2 non-combat decline
(``test_opponent_roster_resolution.py``): same defect class, the
combat-category gate just didn't cover the Fate combat path.

Contract pinned here (drives the REAL ``instantiate_encounter_from_trigger``
production seam with the live ``pulp_noir`` Fate pack — same altitude as
``tests/integration/test_121_2_pulp_noir_fate_migration.py``; no source-text
assertions):

1. Fate combat — the router-named scene-active antagonist is seated as the
   Other; a same-surname co-located statted adversary is NOT conscripted. (AC-2/AC-5)
2. The decline-to-conscript decision is observable on the GM panel
   (lie-detector span names the declined roster NPC; the 108-2 *resolve* span
   does NOT fire). (AC-2 + CLAUDE.md OTEL principle)
3. Fate combat — when the router names the EXACT co-located roster NPC (the
   player really IS engaging Marguerite), she is seated directly: the Fate
   decline must not over-correct and refuse a legitimately-targeted roster
   adversary. (AC-3)
4. Non-Fate combat — the SAME co-located statted adversary IS still conscripted
   (the 108-2 reconciliation is preserved): the fix keys on the FATE binding,
   it does not blanket-disable roster reconciliation. (AC-4)
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
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
)
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_FATE_PACK = "pulp_noir"
_NON_FATE_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "test_genre"

_LOC = "smoky_back_office"


def _has_fate_content() -> bool:
    try:
        find_pack_path(_FATE_PACK)
        return True
    except PackNotFound:
        return False


pytestmark = pytest.mark.skipif(not _has_fate_content(), reason=f"{_FATE_PACK} pack not on disk")


def _load_fate_pack():
    """Load the real Fate-bound pack through the production loader."""
    try:
        return load_genre_pack(find_pack_path(_FATE_PACK))
    except PackNotFound as exc:  # pragma: no cover — pytestmark guards this
        pytest.skip(str(exc))


def _combat_encounter_type(pack) -> str:
    """The pack's first category=='combat' confrontation type (no hardcoding —
    survives a content rename of the confrontation key)."""
    for cdef in pack.rules.confrontations or []:
        if cdef.category == "combat":
            return cdef.confrontation_type
    raise AssertionError(f"{_FATE_PACK} authors no combat-category confrontation")


def _statted_adversary(
    name: str,
    *,
    creature_id: str = "Thug",
    hp: int = 20,
    location: str | None = _LOC,
) -> Npc:
    """A co-located, ``creature_id``-statted, hostile NPC — exactly the shape
    ``_resolve_opponent_from_roster`` treats as a conscriptable candidate."""
    return Npc(
        core=CreatureCore(
            name=name,
            description="A hard case who shares the antagonist's name.",
            personality="Wary.",
            inventory=Inventory(),
            hp=HpPool(current=hp, max=hp, base_max=hp),
        ),
        creature_id=creature_id,
        threat_level=1,
        disposition=-20,
        last_seen_location=location,
        last_seen_turn=4,
    )


def _snapshot_with(*npcs: Npc, genre_slug: str, player: str = "Sam") -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug=genre_slug,
        world_slug="case_files",
        turn_manager=TurnManager(interaction=5),
    )
    snap.character_locations[player] = _LOC
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


# ---------------------------------------------------------------------------
# 1. Fate combat: the named scene-active antagonist is seated, NOT a
#    same-surname co-located roster adversary.  (AC-2 / AC-5 — the headline bug)
# ---------------------------------------------------------------------------


def test_fate_seats_named_antagonist_over_same_surname_roster_npc():
    """The router names the scene-active antagonist "Silas Vance"; "Marguerite
    Vance" (same surname, statted, in-room, hostile) is ALSO present. Under
    Fate the named antagonist must be seated as the Other — Marguerite must NOT
    be conscripted in his place."""
    pack = _load_fate_pack()
    snap = _snapshot_with(_statted_adversary("Marguerite Vance"), genre_slug=_FATE_PACK)

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_FATE_PACK,
        materialized_threat=NpcMention(name="Silas Vance", role="hostile", side="opponent"),
    )

    assert enc is not None, "Fate combat confrontation failed to instantiate"
    assert _opponents(enc) == ["Silas Vance"], (
        "under Fate the router-named scene-active antagonist must be seated as "
        f"the Other; got {_opponents(enc)!r} (a same-surname roster NPC was "
        "conscripted in his place)"
    )
    assert "Marguerite Vance" not in _opponents(enc), (
        "the same-surname co-located roster NPC must not be seated as the Other"
    )


# ---------------------------------------------------------------------------
# 2. The decline-to-conscript decision is observable (OTEL lie-detector).
#    (AC-2 + CLAUDE.md OTEL Observability Principle)
# ---------------------------------------------------------------------------


def test_fate_decline_to_conscript_emits_decision_span(otel_capture):
    """Declining to conscript the co-located adversary into a Fate confrontation
    is a subsystem decision and MUST be observable on the GM panel — and the
    108-2 *resolve* span (which would mean a conscription happened) must NOT
    fire. Without the span the GM can't tell the seater engaged from the
    narrator improvising (No Silent Fallbacks)."""
    pack = _load_fate_pack()
    snap = _snapshot_with(_statted_adversary("Marguerite Vance"), genre_slug=_FATE_PACK)

    instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_FATE_PACK,
        materialized_threat=NpcMention(name="Silas Vance", role="hostile", side="opponent"),
    )

    spans = {s.name: s for s in otel_capture.get_finished_spans()}
    # The conscription decision must NOT have been taken.
    assert "encounter.opponent_resolved_from_roster" not in spans, (
        "the 108-2 reconciliation fired under Fate — a co-located adversary was "
        "conscripted instead of seating the named antagonist"
    )
    # The decline decision must be recorded, naming the creature it refused.
    assert "encounter.roster_resolution_skipped" in spans, (
        "declined-conscription decision span not emitted; the GM panel cannot "
        f"verify the Fate seater engaged. saw {sorted(spans)}"
    )
    attrs = spans["encounter.roster_resolution_skipped"].attributes or {}
    assert attrs.get("declined_name") == "Marguerite Vance", (
        f"decline span must name the refused roster NPC; got {dict(attrs)!r}"
    )


# ---------------------------------------------------------------------------
# 3. Fate combat: an EXACT roster target is still seated directly — the Fate
#    decline must not refuse a legitimately-named roster adversary.  (AC-3)
# ---------------------------------------------------------------------------


def test_fate_exact_roster_target_is_still_seated():
    """When the player genuinely engages "Marguerite Vance" and the router names
    her exactly, she IS the scene-active antagonist and must be seated. The Fate
    decline only suppresses conscripting a DIFFERENT adversary; it must never
    drop an exactly-named roster Other."""
    pack = _load_fate_pack()
    snap = _snapshot_with(_statted_adversary("Marguerite Vance"), genre_slug=_FATE_PACK)

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type=_combat_encounter_type(pack),
        player_name="Sam",
        npcs_present=[],
        genre_slug=_FATE_PACK,
        materialized_threat=NpcMention(name="Marguerite Vance", role="hostile", side="opponent"),
    )

    assert enc is not None
    assert _opponents(enc) == ["Marguerite Vance"], (
        "an exactly-named roster adversary must be seated as the Other under "
        f"Fate; got {_opponents(enc)!r}"
    )


# ---------------------------------------------------------------------------
# 4. Non-Fate combat: the SAME co-located statted adversary IS still conscripted
#    — the fix keys on the Fate binding, not a blanket disable.  (AC-4)
# ---------------------------------------------------------------------------


def test_non_fate_combat_still_conscripts_colocated_adversary():
    """Regression guard / keys-on-ruleset proof: the 108-2 reconciliation that
    Fate must DECLINE is exactly the behaviour a non-Fate (dial/WN) combat must
    KEEP. Same scene shape — a router-named placeholder + one co-located statted
    adversary — seats the adversary, because preserving a bound creature's
    COMBAT stats is the whole point of 108-2 off the Fate path (ADR-059)."""
    pack = load_genre_pack(_NON_FATE_FIXTURE)
    assert pack.rules.ruleset != "fate", (
        "this guard requires a NON-Fate fixture to prove the fix is Fate-scoped"
    )
    snap = _snapshot_with(_statted_adversary("Marguerite Vance"), genre_slug="caverns_and_claudes")

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name="Sam",
        npcs_present=[],
        genre_slug="caverns_and_claudes",
        materialized_threat=NpcMention(
            name="Hold-Dead Placeholder", role="hostile", side="opponent"
        ),
    )

    assert enc is not None
    assert _opponents(enc) == ["Marguerite Vance"], (
        "non-Fate combat must still reconcile the router placeholder to the "
        f"co-located statted adversary (108-2); got {_opponents(enc)!r}"
    )
