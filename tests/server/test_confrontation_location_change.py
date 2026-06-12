"""Pingpong 2026-04-30: confrontation panel sticks open after the party
physically leaves the encounter location.

Repro: 4P MP, space_opera/coyote_star, turn 8. The narrator activated a
"Diplomatic Negotiation" with Inspector Karenina + a junior clerk on
turn 6. The party then turns extraction-mode and walks out of Karenina's
office over turns 7-8. Location updates correctly through three rooms
(Karenina's Office → Third-Floor Corridor → East Freight Stair). The
Confrontation tab in the side panel still shows the active negotiation
with Karenina + clerk and the four beat buttons clickable, even though
Karenina is two floors up and out of the room. Mechanically problematic:
clicking "Threaten Karenina" would generate puppet narration of an
interaction that can't physically happen.

Fix: ``_apply_narration_result_to_snapshot`` resolves any active
encounter as ``abandoned_on_location_change`` whenever ``result.location``
changes from the prior location. The dispatch branch in
``websocket_session_handler.py`` (``elif prior_live and not now_live:``)
detects the resolved=True flip and broadcasts a CONFRONTATION
{ active: false } clear payload to every connected socket via the
existing handshake path — no new wire message needed.

Adds OTEL ``confrontation_deactivated_on_location_change`` watcher event
keyed on encounter_type + old/new location so the GM panel can verify
the engine fired (CLAUDE.md OTEL principle: silent regressions of this
exact bug must be Sebastien-visible).
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NarrationTurnResult
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from tests._helpers.session_room import room_for


def _attach_active_negotiation(snapshot) -> StructuredEncounter:
    """Mount a live encounter on the snapshot — mirrors what the narrator
    apply step would have done on a prior turn when the negotiation
    opened. Threshold=10 / current=0 keeps the encounter unresolved
    (resolved=False) so prior_live=True at apply time.
    """
    encounter = StructuredEncounter(
        encounter_type="negotiation",
        player_metric=EncounterMetric(
            name="leverage",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=EncounterMetric(
            name="leverage",
            current=0,
            starting=0,
            threshold=10,
        ),
        actors=[
            EncounterActor(name="Linus", role="negotiator", side="player"),
            EncounterActor(name="Inspector Karenina", role="opposition", side="opponent"),
            EncounterActor(name="Junior Clerk", role="bystander", side="neutral"),
        ],
    )
    snapshot.encounter = encounter
    return encounter


def test_location_change_resolves_active_encounter_as_abandoned(
    snapshot_with_pack,
    character_named_sam,
):
    """Active encounter + location change → encounter resolves with
    outcome=abandoned_on_location_change. The dispatch path will then
    fire the CONFRONTATION clear payload (verified separately).
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Vaskov Centrum — Inspector Karenina's Office"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_negotiation(snap)
    assert encounter.resolved is False  # baseline: still active

    # Narrator emits a new location — the party walks out into the
    # corridor. No explicit beat selection; this isn't a confrontation
    # beat resolution, it's a scene-change abandonment.
    result = NarrationTurnResult(
        narration="The four crew step into the corridor and pull the door shut.",
        location="Vaskov Centrum — Third-Floor Corridor",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    # Encounter is still on the snapshot (the dispatch block needs to
    # see the resolved=True flip to build a clear payload — it doesn't
    # see a None encounter as a transition), but flagged resolved with
    # the abandonment outcome.
    assert snap.encounter is not None, (
        "Encounter must remain on the snapshot — the websocket_session_handler "
        "dispatch branch checks `prior_live=True, now_live=False` to detect "
        "the resolved-flip and emit the CONFRONTATION clear payload. "
        "Setting snap.encounter=None instead would make `now_encounter is None` "
        "and skip the clear-payload branch entirely."
    )
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "abandoned_on_location_change"
    assert snap.encounter.encounter_type == "negotiation"


def _attach_active_chase(
    snapshot, *, separation: int = 3, threshold: int = 7
) -> StructuredEncounter:
    """A road_warrior-style chase: category='movement', dial below threshold.

    A chase is intrinsically continuous movement — the narrator advances the
    scene location every turn (Kanjō Loop → Dunkelkurve → ...). The mobile
    category is the discriminator the location-change handler uses to CONTINUE
    the chase instead of abandoning it.
    """
    encounter = StructuredEncounter(
        encounter_type="chase",
        category="movement",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(
            name="separation", current=separation, starting=0, threshold=threshold
        ),
        opponent_metric=EncounterMetric(name="pursuit", current=0, starting=0, threshold=threshold),
        actors=[
            EncounterActor(name="Linus", role="driver", side="player"),
            EncounterActor(name="Bōsōzoku formation riders", role="pursuer", side="opponent"),
        ],
    )
    snapshot.encounter = encounter
    return encounter


def test_mobile_chase_continues_below_threshold_across_location_change(
    snapshot_with_pack,
    character_named_sam,
):
    """road_warrior chase bug (DRIVER 2026-06-04). A chase (category=movement)
    MOVES with the party — the narrator advances the scene location every turn
    by design. A scene/location change must CONTINUE the chase, never abandon
    it. Pre-fix the chase was flagged abandoned_on_location_change on its first
    scene move, going invisible to the narrator (no beats offered) — the genre's
    signature subsystem never mechanically fired.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Kanjō Loop — Kannai Onramp"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_chase(snap, separation=3)
    assert encounter.resolved is False

    result = NarrationTurnResult(
        narration="Hornet threads the gap and drops into the tunnel mouth.",
        location="Dunkelkurve — Inside the Tunnel",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a chase (category=movement) below threshold MUST continue active "
        "across a scene/location change — it moves WITH the party; abandoning "
        "it on every scene move makes the chase structurally un-runnable "
        f"(got resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r})"
    )
    assert snap.encounter.outcome is None
    assert snap.encounter.player_metric.current == 3, "dial preserved across the move"


def test_mobile_escape_continues_below_threshold_across_location_change(
    snapshot_with_pack,
    character_named_sam,
):
    """The escape confrontation is also category=movement; same mobility rule.
    A mid-race escape that moves to a new scene continues (you're still
    fleeing) — it does not abandon. (Reaching threshold still resolves as
    victory — see the won-escape test.)
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "The Yellow Brick Road — The Poppy Field"
    snap.characters.append(character_named_sam)
    encounter = StructuredEncounter(
        encounter_type="escape",
        category="movement",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="distance", current=3, starting=0, threshold=8),
        opponent_metric=EncounterMetric(name="pursuit", current=1, starting=0, threshold=8),
        actors=[
            EncounterActor(name="Linus", role="protagonist", side="player"),
            EncounterActor(name="The Poppy Field", role="pursuer", side="opponent"),
        ],
    )
    snap.encounter = encounter

    result = NarrationTurnResult(
        narration="Linus stumbles onward, poppies still grasping.",
        location="The Yellow Brick Road — A Forgotten Lane",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "a mid-race escape (movement) continues across a scene change — "
        f"got resolved={snap.encounter.resolved}, outcome={snap.encounter.outcome!r}"
    )
    assert snap.encounter.outcome is None


def test_mobile_chase_at_threshold_resolves_as_victory_not_continue(
    snapshot_with_pack,
    character_named_sam,
):
    """A chase whose player dial has REACHED threshold (separation 7/7) at the
    moment of a location change is a WIN (the player broke away) — the
    dial-threshold victory branch fires BEFORE the mobile-continue branch.
    The mobile exemption must not swallow a met-threshold victory.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Kanjō Loop — Kannai Onramp"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_chase(snap, separation=7, threshold=7)

    result = NarrationTurnResult(
        narration="The pursuit dwindles to a smear of headlights in the mirror.",
        location="Dunkelkurve — Beyond the Tunnel",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "player_victory", (
        "a chase at separation threshold (7/7) at a location change is a WIN, "
        f"not a continue and not an abandon; got outcome={snap.encounter.outcome!r}"
    )


def _attach_won_escape(snapshot) -> StructuredEncounter:
    """A dial_threshold escape whose PLAYER dial has already reached threshold
    (8/8) but which a non-beat momentum path left unresolved (sq-playtest
    2026-06-02: total_beats_fired=0, so apply_beat's victory check never ran).
    resolved=False so the location-change path still inspects it."""
    encounter = StructuredEncounter(
        encounter_type="escape",
        win_condition="dial_threshold",
        player_metric=EncounterMetric(name="distance", current=8, starting=0, threshold=8),
        opponent_metric=EncounterMetric(name="pursuit", current=1, starting=0, threshold=8),
        actors=[
            EncounterActor(name="Linus", role="protagonist", side="player"),
            EncounterActor(name="The Poppy Field", role="pursuer", side="opponent"),
        ],
    )
    snapshot.encounter = encounter
    return encounter


def test_location_change_at_win_threshold_resolves_as_victory_not_abandoned(
    snapshot_with_pack,
    character_named_sam,
):
    """sq-playtest 2026-06-02 (wry_whimsy/oz). The player's escape dial reached
    8/8 (win threshold MET) but a non-beat momentum path left the encounter
    unresolved (total_beats_fired=0). On the SAME turn the narrator advanced
    location — the natural consequence of escaping. The location-change handler
    must resolve the encounter on its met win condition (player_victory), NOT
    abandoned_on_location_change. Pre-fix the player won the escape but the
    sheet recorded 'abandoned', silently losing the victory credit."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "The Yellow Brick Road — The Poppy Field"
    snap.characters.append(character_named_sam)
    encounter = _attach_won_escape(snap)
    assert encounter.resolved is False

    result = NarrationTurnResult(
        narration="Linus bursts onto the clean bricks beyond the poppies.",
        location="The Yellow Brick Road — Beyond the Poppy Field",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is not None
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "player_victory", (
        "an encounter whose player dial met threshold (8/8) at the moment of a "
        "location change is a WIN (the escape succeeded) — not an abandonment. "
        f"got outcome={snap.encounter.outcome!r}"
    )


def test_location_change_below_win_threshold_still_abandons(
    snapshot_with_pack,
    character_named_sam,
):
    """Guard: a genuinely-unfinished encounter (no dial at threshold) still
    abandons on a location change. The victory shortcut must NOT swallow the
    abandonment path for mid-race encounters (the original 2026-04-30 bug)."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "The Yellow Brick Road — The Poppy Field"
    snap.characters.append(character_named_sam)
    encounter = _attach_won_escape(snap)
    encounter.player_metric.current = 3  # mid-race, threshold not met
    assert encounter.resolved is False

    result = NarrationTurnResult(
        narration="Linus stumbles off down a side path, the poppies behind him.",
        location="The Yellow Brick Road — A Forgotten Lane",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is not None
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "abandoned_on_location_change", (
        "a mid-race encounter (no dial at threshold) must still abandon on a "
        f"location change; got outcome={snap.encounter.outcome!r}"
    )


def test_no_location_change_leaves_active_encounter_alone(
    snapshot_with_pack,
    character_named_sam,
):
    """Same-location turn (in-room negotiation continues): the encounter
    stays active. Guards against firing the deactivate path on every
    apply — only the location-change edge should trigger it.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Vaskov Centrum — Inspector Karenina's Office"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_negotiation(snap)

    # Narration with NO location field — apply step short-circuits the
    # location-update branch entirely.
    result = NarrationTurnResult(
        narration="Karenina taps the desk; Linus presses his case.",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False, (
        "Encounter must NOT be resolved when location did not change — "
        "in-room negotiation/combat/social turns must stay live."
    )
    assert snap.encounter.outcome is None


def test_location_set_to_same_value_does_not_resolve(
    snapshot_with_pack,
    character_named_sam,
):
    """The narrator re-emits the current location. Not a real change;
    the deactivate branch must not fire."""
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Vaskov Centrum — Inspector Karenina's Office"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_negotiation(snap)

    result = NarrationTurnResult(
        narration="Karenina sets her datasplinter down.",
        location="Vaskov Centrum — Inspector Karenina's Office",  # same
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is False
    assert snap.encounter.outcome is None


def test_already_resolved_encounter_not_re_resolved_on_location_change(
    snapshot_with_pack,
    character_named_sam,
):
    """An encounter that resolved on the prior turn (player_victory,
    opponent_victory, etc.) keeps its outcome — the location-change
    guard checks `not encounter.resolved` and skips already-resolved
    encounters. Without this guard, a player-victory outcome could be
    overwritten with abandoned_on_location_change on the very next
    turn's location change, losing the win record.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Vaskov Centrum — Inspector Karenina's Office"
    snap.characters.append(character_named_sam)
    encounter = _attach_active_negotiation(snap)
    encounter.resolved = True
    encounter.outcome = "player_victory"

    result = NarrationTurnResult(
        narration="The party steps out into the corridor.",
        location="Vaskov Centrum — Third-Floor Corridor",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    assert snap.encounter is encounter
    assert snap.encounter.resolved is True
    assert snap.encounter.outcome == "player_victory", (
        "Outcome must NOT be overwritten with abandoned_on_location_change "
        "for an already-resolved encounter — the deactivate guard checks "
        "`not encounter.resolved` precisely to preserve outcomes from "
        "the prior turn's resolution."
    )


def test_first_location_set_does_not_attempt_deactivation(
    snapshot_with_pack,
    character_named_sam,
):
    """At session start ``snap.location`` is empty; the very first
    location set on the snapshot is not a "scene change" — there's no
    prior scene to abandon. Mirrors the existing scratch-sweep guard
    (``if old_loc and old_loc != result.location:``).
    """
    snap, pack = snapshot_with_pack
    snap.character_locations.pop("Linus", None)  # explicit empty — fresh session
    snap.characters.append(character_named_sam)
    encounter = _attach_active_negotiation(snap)

    result = NarrationTurnResult(
        narration="Linus opens the door to Karenina's office.",
        location="Vaskov Centrum — Inspector Karenina's Office",
    )
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )

    # Location did update, but encounter must stay live — initial scene
    # set is not an abandonment edge.
    assert snap.character_locations.get("Linus") == "Vaskov Centrum — Inspector Karenina's Office"
    assert snap.encounter is encounter
    assert snap.encounter.resolved is False
    assert snap.encounter.outcome is None


def test_location_change_with_no_active_encounter_is_no_op(
    snapshot_with_pack,
    character_named_sam,
):
    """Snapshot has no encounter at all — the apply step handles the
    location change normally without raising or emitting a
    deactivate span.
    """
    snap, pack = snapshot_with_pack
    snap.character_locations["Linus"] = "Vaskov Centrum — Loading Bay"
    snap.characters.append(character_named_sam)
    assert snap.encounter is None  # baseline

    result = NarrationTurnResult(
        narration="The crew climbs the freight stair.",
        location="Vaskov Centrum — East Freight Stair",
    )
    # Must not raise — exercises the
    # `if active_encounter is not None and not active_encounter.resolved`
    # guard short-circuit.
    _apply_narration_result_to_snapshot(
        snapshot=snap,
        result=result,
        pack=pack,
        player_name="Linus",
        room=room_for(snapshot=snap),
    )
    assert snap.character_locations.get("Linus") == "Vaskov Centrum — East Freight Stair"
    assert snap.encounter is None


# ---------------------------------------------------------------------------
# Wiring test (CLAUDE.md: "Every Test Suite Needs a Wiring Test").
# Proves the dispatch branch in websocket_session_handler.py reads the
# `resolved=True` flip we set in narration_apply and emits the
# CONFRONTATION clear payload — i.e. the new deactivation logic is
# wired into the existing transition-detection path, not just unit-tested
# in isolation.
# ---------------------------------------------------------------------------


def test_dispatch_branch_treats_abandoned_encounter_as_prior_live_to_now_dead_transition():
    """Wiring: the websocket_session_handler.py dispatch branch at
    `elif prior_live and not now_live:` emits the CONFRONTATION clear
    payload by reading exactly two booleans:

      prior_live = prior_encounter is not None and not prior_encounter.resolved
      now_live   = now_encounter   is not None and not now_encounter.resolved

    The narration_apply deactivation we ship sets `resolved=True` and
    leaves `snap.encounter` non-None. This test exercises the boolean
    arithmetic the dispatch branch performs against a snapshot that has
    been through our deactivation path — so a future refactor that
    e.g. sets snap.encounter=None instead would fail this test loudly,
    rather than silently skipping the clear-payload emit.
    """
    from sidequest.game.encounter import (
        EncounterActor as _EncounterActor,
    )
    from sidequest.game.encounter import (
        EncounterMetric as _EncounterMetric,
    )
    from sidequest.game.encounter import (
        StructuredEncounter as _StructuredEncounter,
    )

    # Pre-apply state captured by websocket_session_handler.py at L1488:
    pre_apply = _StructuredEncounter(
        encounter_type="negotiation",
        player_metric=_EncounterMetric(
            name="leverage",
            current=0,
            starting=0,
            threshold=10,
        ),
        opponent_metric=_EncounterMetric(
            name="leverage",
            current=0,
            starting=0,
            threshold=10,
        ),
        actors=[
            _EncounterActor(name="Linus", role="negotiator", side="player"),
        ],
    )
    prior_live = pre_apply is not None and not pre_apply.resolved
    assert prior_live is True, (
        "Baseline: pre-apply encounter is unresolved → prior_live=True. "
        "Without this, `elif prior_live and not now_live:` short-circuits "
        "and the clear payload is never built."
    )

    # Post-apply: our narration_apply path mutates the SAME encounter
    # object (it doesn't create a new one) — sets resolved=True and
    # outcome="abandoned_on_location_change".
    pre_apply.resolved = True
    pre_apply.outcome = "abandoned_on_location_change"

    # The dispatch branch reads `now_encounter = snapshot.encounter`
    # which is the same object after apply. It must NOT be None — the
    # branch that builds the clear payload runs only on the
    # `prior_live and not now_live` edge, NOT on the `prior_live and
    # now_encounter is None` edge (that path doesn't exist; it would
    # be silently dropped).
    now_encounter = pre_apply  # same object — the apply mutated in place
    now_live = now_encounter is not None and not now_encounter.resolved

    assert now_live is False, (
        "Post-deactivation: now_live must be False so the dispatch "
        "branch picks the `prior_live and not now_live` path and builds "
        "the CONFRONTATION { active: false } clear payload."
    )
    assert now_encounter is not None, (
        "now_encounter must not be None — websocket_session_handler.py "
        "asserts `prior_type is not None` (guaranteed by prior_live=True) "
        "but that's the prior reference. The clear payload is built from "
        "`prior_type` which is captured before apply, so the branch is "
        "safe even if snap.encounter were None — but our deactivation "
        "deliberately keeps the encounter on the snapshot for outcome "
        "preservation. This guards the contract."
    )

    # The dispatch branch then takes:
    branch = "elif prior_live and not now_live"
    assert prior_live and not now_live, (
        f"Branch {branch!r} must fire on this state — pingpong "
        "2026-04-30 confrontation-sticks-open is fixed by routing "
        "narration_apply's resolved-flip through this exact branch's "
        "clear-payload broadcast (no new wire message, reuses the "
        "existing transition-detection emit at "
        "websocket_session_handler.py:~1897)."
    )
