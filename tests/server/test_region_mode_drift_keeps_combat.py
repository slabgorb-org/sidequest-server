"""Story 90-6 (RED): region-mode location-drift abandons live combat the turn
it instantiates.

Measured root cause (Perseus MP playtest 2026-06-06, server log). In a
region-mode world the narrator drifts the *scene title* every turn — a
cosmetic re-title within the SAME cartography region
(``narrator.location_drift_repaired`` fires each turn). The encounter-abandon
block in ``_apply_narration_result_to_snapshot`` keys off the raw
``result.location`` STRING (``if old_loc and old_loc != result.location:``),
not the cartography region. A combat confrontation (category="combat" -> not
``_encounter_is_mobile``) therefore falls to the ``else`` branch and is
resolved ``abandoned_on_location_change`` the same turn it instantiated. Net:
combat instantiates -> dies same turn -> next turn there is no active
encounter -> no beat menu -> ``beat_selections=0 confrontation=None
active=False`` forever, and the narrator free-hands hit/miss with zero
mechanical backing (the SOUL.md "Illusionism" failure the GM panel exists to
catch).

The signal to distinguish the two cases already exists upstream in the same
function: the region-resolution block (L3142-3292) ALREADY detects a
same-region sub-location POI in a region-mode world
(``region.entry_skipped_sub_location``, current_region unchanged) versus a
REAL region change (``region.current_region_advanced``, current_region moves).
The abandon block must consult that signal: a same-region scene-title drift
CONTINUES an anchored confrontation; only a genuine region change abandons it.

These tests pin the five acceptance criteria. They are RED until the abandon
block stops keying purely off the location string in region-mode worlds.

Scope note (AC5): the full live proof — Perseus SWN combat where
``beat_selections>0`` and an ``hp_depletion``/strike damage patch lands on
tracked enemy HP — is re-run by the playtest DRIVER post-merge (see
``~/Projects/sq-playtest-pingpong.md``). The closest deterministic unit-level
proxy is that the combat encounter SURVIVES across multiple same-region drifts
so beats CAN be offered on the following turns; that proxy is
``test_combat_survives_multiple_same_region_drifts`` below.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.genre.models.world import CartographyConfig, NavigationMode, Region
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for

# Watcher event_type strings the GM panel keys on (the OTEL lie-detector for
# this subsystem decision). The abandon span name is fixed by the existing
# code; the continue span name is matched by PREFIX so the fix may reuse the
# existing ``confrontation_continued_across_location_change`` or mint a
# distinct same-region variant (AC2 wildcard: ``confrontation_continued_*``).
_DEACTIVATED_EVENT = "confrontation_deactivated_on_location_change"
_CONTINUED_PREFIX = "confrontation_continued"

# A region-mode world with two authored cartography regions. The region set is
# AUTHORED/closed, so a narrator heading that does NOT name one of these is a
# sub-location/POI WITHIN the current region — never a new region.
_PERSEUS_REGION = "perseus_cloud"
_VANCE_REGION = "vance_reach"


def _make_region_mode_world():
    """A duck-typed world stand-in exposing ``.cartography`` only.

    ``_resolve_heading_to_cartography`` and the region-mode detector read
    ``getattr(world_obj, "cartography", None)`` plus ``cartography.regions``
    / ``cartography.navigation_mode`` — nothing else — so a SimpleNamespace
    over a real ``CartographyConfig`` is sufficient and avoids assembling a
    full ``World`` (which requires config/lore/theme).
    """
    cartography = CartographyConfig(
        navigation_mode=NavigationMode.region,
        starting_region=_PERSEUS_REGION,
        regions={
            _PERSEUS_REGION: Region(
                name="Perseus Cloud",
                summary="A drifting nebular sector under Governance writ.",
                description="Ash-lit docks and enforcer checkpoints.",
            ),
            _VANCE_REGION: Region(
                name="Vance Reach",
                summary="The next system out along the transit lane.",
                description="A customs station orbiting a dim star.",
            ),
        },
    )
    return SimpleNamespace(cartography=cartography)


def _bind_region_mode(pack, snap, *, world_slug: str = _PERSEUS_REGION) -> str:
    """Make ``pack`` resolve ``world_slug`` to a region-mode world and seat the
    party in ``perseus_cloud``. Returns the world slug to pass as ``world=``.
    """
    pack.worlds = {world_slug: _make_region_mode_world()}
    snap.current_region = _PERSEUS_REGION
    snap.pc_regions["Linus"] = _PERSEUS_REGION
    return world_slug


def _attach_active_combat(snap) -> StructuredEncounter:
    """Mount a live, anchored combat confrontation — what the intent router /
    apply step would have instantiated on the turn the player drew and fired.

    category="combat" (NOT "movement") so ``_encounter_is_mobile`` is False —
    this is an ANCHORED confrontation, the exact class the bug abandons.
    Threshold unmet (current=0) so neither the dial-win nor the opponent-yield
    resolving branch fires; only the same-region-drift logic is under test.
    """
    encounter = StructuredEncounter(
        encounter_type="combat",
        category="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Linus", role="combatant", side="player"),
            EncounterActor(name="Governance Enforcer", role="opposition", side="opponent"),
        ],
    )
    snap.encounter = encounter
    return encounter


def _attach_active_social(snap) -> StructuredEncounter:
    """Mount a live, anchored SOCIAL negotiation — the Cowardly-Lion / Inspector-
    Karenina class of confrontation.

    category="social" (NOT "movement") so ``_encounter_is_mobile`` is False — like
    combat it is ANCHORED, but it is OUT of story 90-6's measured scope (combat).
    The 2026-04-30 negotiation-walk-out fix established that a social negotiation
    walked out of must ABANDON (clicking "Threaten" two rooms away puppets an NPC
    who isn't there). The same-region-continue path is scoped to combat, so a
    social negotiation drifted within the same region must keep abandoning.
    Threshold unmet (current=0) so neither dial-win nor opponent-yield fires.
    """
    encounter = StructuredEncounter(
        encounter_type="negotiation",
        category="social",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="rapport", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="resolve", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Linus", role="negotiator", side="player"),
            EncounterActor(name="Governance Adjutant", role="opposition", side="opponent"),
        ],
    )
    snap.encounter = encounter
    return encounter


@pytest.fixture
def watcher_events(monkeypatch):
    """Capture every ``_watcher_publish`` call the apply step makes.

    ``narration_apply`` binds ``publish_event`` at import as the module-level
    name ``_watcher_publish``; patching that name records the subsystem's OTEL
    lie-detector emits synchronously (no event loop / hub needed). Returns the
    captured list of ``{"event_type", "fields", "component"}`` dicts.
    """
    captured: list[dict] = []

    def _recorder(event_type, fields, *, component="sidequest-server", **kwargs):
        captured.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _recorder)
    return captured


def _event_types(captured: list[dict]) -> list[str]:
    return [e["event_type"] for e in captured]


# ---------------------------------------------------------------------------
# AC1 — a same-region scene-title drift does NOT abandon the confrontation.
# ---------------------------------------------------------------------------


def test_same_region_scene_drift_does_not_abandon_combat(
    snapshot_with_pack,
    character_named_sam,
):
    """The Perseus repro. Combat is live in ``perseus_cloud``; the narrator
    re-titles the scene to a different POI WITHIN the same region (neither
    heading names a cartography region, so current_region is unchanged). The
    confrontation must STAY active — a cosmetic re-title is not a scene exit.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)
    assert encounter.resolved is False  # baseline: live combat

    # Narrator drifts the scene title to another POI in the SAME region.
    result = NarrationTurnResult(
        narration="Linus fires; the enforcers scatter behind the cargo spar.",
        location="The Cargo Spar",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a same-region scene-title drift must NOT abandon an anchored combat "
        "confrontation — the location string changed but current_region did "
        f"not (got resolved={snap.encounter.resolved}, "
        f"outcome={snap.encounter.outcome!r})"
    )
    assert snap.encounter.outcome is None
    assert snap.current_region == _PERSEUS_REGION, "region must not have moved"


def test_same_region_drift_does_not_emit_deactivation_span(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """AC1 OTEL: ``confrontation_deactivated_on_location_change`` must NOT fire
    on a same-region drift — the GM panel must not record an abandonment the
    engine should never have made (CLAUDE.md OTEL principle).
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    _attach_active_combat(snap)

    result = NarrationTurnResult(
        narration="The firefight spills toward the loading cranes.",
        location="The Loading Cranes",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert _DEACTIVATED_EVENT not in _event_types(watcher_events), (
        "a same-region drift must not emit the deactivation span — fired "
        f"events were: {_event_types(watcher_events)}"
    )


# ---------------------------------------------------------------------------
# AC2 — a continued confrontation emits a confrontation_continued_* span so
# the GM panel sees the engine CHOSE to continue (lie-detector).
# ---------------------------------------------------------------------------


def test_same_region_drift_emits_confrontation_continued_span(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """AC2: continuing across a same-region drift is a DECISION the engine
    made, so it must emit a ``confrontation_continued_*`` watcher/OTEL span —
    otherwise the GM panel cannot tell "the engine kept the fight live" from
    "the narrator is improvising over a dead encounter".
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    _attach_active_combat(snap)

    result = NarrationTurnResult(
        narration="Linus presses the attack down the gantry.",
        location="The Lower Gantry",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    continued = [e for e in watcher_events if e["event_type"].startswith(_CONTINUED_PREFIX)]
    assert continued, (
        "a same-region continue must emit a confrontation_continued_* span "
        f"(component=confrontation); fired events were: "
        f"{_event_types(watcher_events)}"
    )
    span = continued[0]
    assert span["component"] == "confrontation"
    assert span["fields"].get("encounter_type") == "combat", (
        f"the continued span must identify the combat encounter it kept live: {span['fields']!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — a REAL region change still abandons an anchored confrontation
# (regression guard: the 2026-04-30 negotiation-walk-out semantics survive).
# ---------------------------------------------------------------------------


def test_real_region_change_still_abandons_anchored_combat(
    snapshot_with_pack,
    character_named_sam,
):
    """The party leaves ``perseus_cloud`` entirely for ``vance_reach`` — a
    genuine region change (the heading names a known cartography region). An
    anchored combat that is left behind must still abandon. The narrowed
    trigger must not become a blanket "never abandon".
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)
    assert encounter.resolved is False

    # A heading whose leading place resolves to the DIFFERENT cartography
    # region vance_reach — a real region change, not a sub-location drift.
    result = NarrationTurnResult(
        narration="They burn for the transit lane and dock at the next system.",
        location="Vance Reach — The Customs Deck",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == _VANCE_REGION, "baseline: region actually changed"
    assert snap.encounter is encounter
    assert snap.encounter.resolved is True, (
        "an anchored combat left behind by a REAL region change must still "
        f"abandon (got resolved={snap.encounter.resolved})"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"


def test_real_region_change_emits_deactivation_span(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """AC3 OTEL: a real region change keeps emitting the deactivation span so
    the GM panel still sees genuine abandonments.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    _attach_active_combat(snap)

    result = NarrationTurnResult(
        narration="They abandon the firefight and jump to the next system.",
        location="Vance Reach — The Customs Deck",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert _DEACTIVATED_EVENT in _event_types(watcher_events), (
        "a real region change must still emit the deactivation span; fired "
        f"events were: {_event_types(watcher_events)}"
    )


# ---------------------------------------------------------------------------
# AC4 — mobile (movement) confrontations keep the existing _encounter_is_mobile
# continue behavior (no regression from the new same-region logic).
# ---------------------------------------------------------------------------


def _attach_active_chase(snap) -> StructuredEncounter:
    encounter = StructuredEncounter(
        encounter_type="chase",
        category="movement",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="separation", current=3, starting=0, threshold=8),
        opponent_metric=EncounterMetric(name="pursuit", current=1, starting=0, threshold=8),
        actors=[
            EncounterActor(name="Linus", role="driver", side="player"),
            EncounterActor(name="Enforcer Wing", role="pursuer", side="opponent"),
        ],
    )
    snap.encounter = encounter
    return encounter


def test_mobile_chase_continues_on_same_region_drift_in_region_mode(
    snapshot_with_pack,
    character_named_sam,
):
    """A movement-category chase below threshold continues across a same-region
    drift exactly as before — the mobile path must not be perturbed by the new
    anchored-continue logic.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_chase(snap)

    result = NarrationTurnResult(
        narration="The skiff threads the docking spars at speed.",
        location="The Outer Spars",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a movement chase below threshold must continue across a same-region "
        f"drift (got resolved={snap.encounter.resolved})"
    )
    assert snap.encounter.player_metric.current == 3, "dial preserved across the move"


def test_mobile_chase_continues_on_real_region_change_in_region_mode(
    snapshot_with_pack,
    character_named_sam,
):
    """The mobile exemption is checked BEFORE the abandon branch, so a chase
    continues even across a REAL region change (it moves WITH the party). The
    new anchored same-region logic must not flip this into an abandon.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_chase(snap)

    result = NarrationTurnResult(
        narration="The chase spills across the transit lane into the next system.",
        location="Vance Reach — The Approach Lane",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a movement chase continues across a real region change too — the "
        f"mobile exemption precedes the abandon branch (got "
        f"resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r})"
    )


# ---------------------------------------------------------------------------
# AC5 — live-proof proxy: combat survives across turns so beats CAN keep
# firing. (Full Perseus SWN proof — beat_selections>0 + hp patch on tracked
# enemy HP — is re-run by the DRIVER post-merge.)
# ---------------------------------------------------------------------------


def test_combat_survives_multiple_same_region_drifts(
    snapshot_with_pack,
    character_named_sam,
):
    """The core regression: pre-fix, combat instantiated then died the same
    turn, so the NEXT turn had no active encounter, no beat menu, forever. A
    confrontation that survives two consecutive same-region drifts proves the
    engine keeps it live across turns so beats can be offered again.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)
    room = room_for(snapshot=snap)

    # Turn N: narrator drifts the scene title within perseus_cloud.
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="Linus ducks behind the cargo spar and returns fire.",
            location="The Cargo Spar",
        ),
        pack=pack,
        world=world,
        player_name="Linus",
        room=room,
    )
    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, "must survive the first drift"

    # Turn N+1: another cosmetic re-title within the same region.
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="The fight grinds on across the gantry.",
            location="The Lower Gantry",
        ),
        pack=pack,
        world=world,
        player_name="Linus",
        room=room,
    )
    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "combat must remain live across multiple same-region drifts so the "
        "narrator is offered its beats again next turn — this is the exact "
        "instantiate-then-die-forever loop the story fixes "
        f"(got resolved={snap.encounter.resolved}, "
        f"outcome={snap.encounter.outcome!r})"
    )
    assert snap.encounter.outcome is None


# ---------------------------------------------------------------------------
# Scope guard — the fix is gated to region-mode worlds. A non-region-mode
# world (or no world) keeps the existing abandon-on-location-string-change
# behavior (the original 2026-04-30 negotiation-walk-out fix).
# ---------------------------------------------------------------------------


def test_non_region_mode_world_still_abandons_combat_on_location_change(
    snapshot_with_pack,
    character_named_sam,
):
    """Gating guard: with no region-mode world bound (``world=None`` →
    ``_is_region_mode_world`` False), a location-string change still abandons
    an anchored combat. The same-region-continue logic must NOT leak into
    room-graph / non-region-mode worlds.
    """
    snap, pack = snapshot_with_pack  # pack.worlds == {} from the fixture
    snap.character_locations["Linus"] = "Karenina's Office"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)

    result = NarrationTurnResult(
        narration="The crew breaks contact and slips into the corridor.",
        location="Third-Floor Corridor",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=None,  # no region-mode world — the gated path must not engage
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is True, (
        "with no region-mode world the existing abandon-on-location-change "
        f"behavior must be unchanged (got resolved={snap.encounter.resolved})"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"


# ---------------------------------------------------------------------------
# Category scope — the same-region CONTINUE is combat-only. A SOCIAL negotiation
# walked out of within the same region must still ABANDON (the 2026-04-30
# negotiation-walk-out semantics). The continue path must not silently widen to
# all anchored categories — that reintroduces the puppet-NPC bug for every
# region-mode world (oz Cowardly Lion, perseus Governance adjutant, …).
# ---------------------------------------------------------------------------


def test_same_region_drift_abandons_social_negotiation_in_region_mode(
    snapshot_with_pack,
    character_named_sam,
):
    """Blocking scope guard. A region-mode world hosts a live SOCIAL negotiation;
    the narrator re-titles the scene to another POI within the SAME region
    (current_region unchanged — exactly the input that CONTINUES a combat). A
    social negotiation is anchored, NOT combat, so it must still ABANDON: the
    player walked away from the table, and the beat buttons must not stay
    clickable two locations from the NPC. The same-region-continue branch is
    scoped to combat; if it fires for ``category="social"`` this fails.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_social(snap)
    assert encounter.resolved is False  # baseline: live negotiation

    # Same-region drift: another POI in perseus_cloud, current_region unchanged.
    result = NarrationTurnResult(
        narration="Linus turns on his heel and walks out toward the cargo spar.",
        location="The Cargo Spar",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == _PERSEUS_REGION, "baseline: region did not move"
    assert snap.encounter is encounter
    assert snap.encounter.resolved is True, (
        "a SOCIAL negotiation walked out of within the same region must still "
        "ABANDON — only combat continues across a same-region drift. The "
        "continue branch leaked to all anchored categories (got "
        f"resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r})"
    )
    assert snap.encounter.outcome == "abandoned_on_location_change"


def test_same_region_drift_social_emits_deactivation_not_continue_span(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """OTEL companion to the social scope guard. The GM panel must see the
    negotiation genuinely deactivated — ``confrontation_deactivated_on_location_change``
    fires and NO ``confrontation_continued_*`` span fires. If the continue branch
    wrongly engages for social, the deactivation span is missing and a continue
    span appears (the lie-detector inversion the OTEL principle exists to catch).
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    _attach_active_social(snap)

    result = NarrationTurnResult(
        narration="The negotiation collapses; Linus stalks off down the gantry.",
        location="The Lower Gantry",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    events = _event_types(watcher_events)
    assert _DEACTIVATED_EVENT in events, (
        "an abandoned social negotiation must emit the deactivation span so the "
        f"GM panel records the walk-out; fired events were: {events}"
    )
    continued = [e for e in events if e.startswith(_CONTINUED_PREFIX)]
    assert not continued, (
        "a social negotiation must NOT emit a confrontation_continued_* span on "
        f"a same-region drift — the continue path is combat-only; fired: {events}"
    )


# ---------------------------------------------------------------------------
# Invalid-heading edge ([SILENT]). A garbage narrator heading
# (``validate_region_name`` False — bracketed/multiline/too_long) is NOT a region
# exit. In a region-mode world a live combat must survive it: the region-mode
# determination depends only on pack+world, not the (rejected) location string,
# so it must be computed BEFORE the validity gate. Pre-fix the flag is set only
# inside the valid-region branch, so a garbage re-title silently abandons combat
# with no causal span linking the rejected heading to the lost encounter.
# ---------------------------------------------------------------------------


def test_invalid_heading_does_not_abandon_combat_in_region_mode(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """A bracketed narrator aside ("(aside — narrator brief)") fails
    ``validate_region_name`` and never resolves to a region, so current_region
    cannot have changed — it is not a scene exit. A live combat in a region-mode
    world must SURVIVE it (and emit the continue span), not abandon. RED until the
    region-mode flag is computed before the validity gate.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)
    assert encounter.resolved is False

    # A bracketed heading — validate_region_name -> (False, "bracketed").
    result = NarrationTurnResult(
        narration="(aside — narrator brief) The enforcers reload.",
        location="(aside — narrator brief)",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.current_region == _PERSEUS_REGION, (
        "a rejected heading never resolves to a region — current_region must not move"
    )
    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a live combat must survive a garbage re-title in a region-mode world — "
        "an invalid heading is not a region exit. The region-mode flag must be "
        "computed before the validity gate (got "
        f"resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r})"
    )
    assert _DEACTIVATED_EVENT not in _event_types(watcher_events), (
        "no deactivation span may fire for a combat that survived a garbage "
        f"re-title; fired events were: {_event_types(watcher_events)}"
    )


# ---------------------------------------------------------------------------
# Wiring test (CLAUDE.md: "Every Test Suite Needs a Wiring Test").
# The websocket_session_handler dispatch branch (`elif prior_live and not
# now_live:`) builds a CONFRONTATION { active: false } clear payload off the
# encounter's resolved-flip, which co-fires with the
# ``confrontation_deactivated_on_location_change`` watcher span. So the real,
# refactor-stable signal that the panel STAYS OPEN is: after a same-region
# combat drift the deactivation span did NOT fire AND a
# ``confrontation_continued_*`` span DID — i.e. the engine emitted the
# keep-it-live decision the handler keys on, not the tear-down one. (Per
# server CLAUDE.md "No Source-Text Wiring Tests": assert on the OTEL span the
# subsystem emits, not on handler internals re-derived in the test body.)
# ---------------------------------------------------------------------------


def test_same_region_continue_does_not_emit_panel_clear_signal(
    snapshot_with_pack,
    character_named_sam,
    watcher_events,
):
    """Wiring: the dispatch branch tears the confrontation panel down only when
    the encounter resolves, which co-emits
    ``confrontation_deactivated_on_location_change``. A same-region combat
    continue must (1) leave the encounter live and (2) emit the
    ``confrontation_continued_*`` decision span while NOT emitting the
    deactivation span — the exact OTEL signature that keeps the panel open.
    """
    snap, pack = snapshot_with_pack
    world = _bind_region_mode(pack, snap)
    snap.character_locations["Linus"] = "The Governance Checkpoint"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_combat(snap)
    assert encounter.resolved is False, "baseline: combat live before the drift"

    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=NarrationTurnResult(
            narration="The exchange of fire moves down the gantry.",
            location="The Lower Gantry",
        ),
        pack=pack,
        world=world,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    # (1) Encounter still live — the handler's now_live read stays True.
    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "after a same-region continue the encounter must still be live so the "
        f"dispatch branch's now_live read is True (got resolved={snap.encounter.resolved})"
    )

    # (2) The OTEL signature the dispatch branch keys on: the deactivation span
    # (which co-fires with the resolved-flip that builds the clear payload) must
    # be ABSENT, and the continue-decision span must be PRESENT.
    events = _event_types(watcher_events)
    assert _DEACTIVATED_EVENT not in events, (
        "the deactivation span co-fires with the resolved-flip that builds the "
        "CONFRONTATION{active:false} clear payload — it must NOT fire on a "
        f"same-region continue, or the panel is torn down. Fired: {events}"
    )
    assert any(e.startswith(_CONTINUED_PREFIX) for e in events), (
        "the engine must emit a confrontation_continued_* span so the GM panel "
        f"sees the keep-it-live decision (not the narrator improvising). Fired: {events}"
    )
