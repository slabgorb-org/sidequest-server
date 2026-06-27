"""ADR-153 §7 / 158-30 (Plan 2, lifecycle) — a husk-reaped duel stays reaped.

RED suite for the dogfight-resurrection soft-lock (coyote_star playtest
2026-06-25). After a dogfight RESOLVED (``active=False``), a normal non-combat
action in the SAME region re-summoned it: ``reap_resolved_encounter_husk``
correctly cleared the resolved encounter at turn start, but later that same turn
the seater re-seated a fresh dogfight (the ``continued_same_region_drift``
keep-alive, or any re-dispatch), resetting ``structured_phase`` Resolution→Setup.
The player was soft-locked into ship-maneuver beats while on foot in a derelict,
Enter disabled. Two lifecycle rules disagreed (husk_reaped clear vs. re-seat) and
the re-seat won.

The fix: husk-reap stamps a transient ``(encounter_type, turn)`` marker, and
``instantiate_encounter_from_trigger`` — the universal seating chokepoint — refuses
to seat a type reaped THIS same turn. The refusal is observable
(``reseat_refused_husk_reaped`` watcher event, CLAUDE.md OTEL = lie detector),
never silent. The marker is keyed by turn, so a stale marker from a PRIOR turn is
inert and a genuinely-fresh dogfight on a later turn still seats (the created_turn
exemption).

Every test here drives the REAL production seater
(``instantiate_encounter_from_trigger``), so the suite is its own wiring test: the
guard is exercised on the path the running server actually takes, not a mock.

RED today (the marker + guard do not exist yet):
  * ``test_reaped_dogfight_does_not_reseat_same_turn`` — the re-seat currently
    succeeds (a fresh dogfight in Setup), so ``result is None`` fails.
  * ``test_reseat_refusal_emits_observable_watcher_event`` — no
    ``reseat_refused_husk_reaped`` event fires today.
GREEN today, guarding the fix against regressions / over-blocking:
  * ``test_fresh_dogfight_still_seats_when_not_reaped_this_turn`` — no marker, seats.
  * ``test_reaped_marker_from_prior_turn_does_not_block_later_seat`` — turn-keyed
    exemption: a marker from a prior turn must NOT block a later-turn seat.
"""

from __future__ import annotations

from sidequest.server.dispatch import encounter_lifecycle
from sidequest.server.dispatch.encounter_lifecycle import (
    instantiate_encounter_from_trigger,
    reap_resolved_encounter_husk,
)
from tests.fixtures.dogfight_playtest_encounter import (
    GENRE_SLUG,
    make_dogfight_pack,
    make_empty_snapshot,
)

DOGFIGHT = "dogfight"


def _seat_and_resolve_dogfight(snapshot, pack):
    """Seat a dogfight through the production seater, then resolve it.

    Mirrors the playtest sequence: a duel was seated and fought on a prior
    (dice-replay) turn and is now sitting on ``snapshot.encounter`` as a
    resolved husk at the start of the next normal turn.
    """
    seated = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT,
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert seated is not None, (
        "precondition: Plan 1 frame-default seating must seat a dogfight with no "
        "named opponent — without it the lifecycle repro cannot be set up"
    )
    assert snapshot.encounter is not None
    snapshot.encounter.resolved = True
    snapshot.encounter.outcome = "player_victory"


def test_reaped_dogfight_does_not_reseat_same_turn() -> None:
    """AC4 — the bug repro. A dogfight husk-reaped at turn start must NOT re-seat
    the same turn; a reaped duel stays reaped (no Resolution→Setup resurrection,
    no soft-lock into ship maneuvers on foot)."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name="Pilot")
    turn = snapshot.turn_manager.interaction

    _seat_and_resolve_dogfight(snapshot, pack)

    # Turn start: husk-reap clears the resolved dogfight (and stamps the marker).
    reaped = reap_resolved_encounter_husk(snapshot, is_dice_replay=False, turn=turn)
    assert reaped is True
    assert snapshot.encounter is None

    # SAME turn: a re-seat of the just-reaped type must be refused.
    result = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT,
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert result is None, (
        "a dogfight reaped THIS turn must not re-seat — the husk_reaped clear must "
        "win over a same-turn re-dispatch / drift keep-alive (soft-lock repro)"
    )
    assert snapshot.encounter is None, (
        "the snapshot must stay clear — a resurrected encounter in Setup is the "
        "exact soft-lock (ship-maneuver beats on foot, Enter disabled)"
    )


def test_fresh_dogfight_still_seats_when_not_reaped_this_turn() -> None:
    """AC5 — no prior encounter, no reaped marker: a dogfight seats normally. The
    guard must not block a genuinely-fresh duel."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name="Pilot")
    assert snapshot.husk_reaped_this_turn is None, (
        "a fresh snapshot carries no reaped marker"
    )

    result = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT,
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert result is not None, "an un-reaped dogfight must seat (no over-blocking)"
    assert snapshot.encounter is not None
    assert snapshot.encounter.encounter_type == DOGFIGHT


def test_reaped_marker_from_prior_turn_does_not_block_later_seat() -> None:
    """Created_turn exemption — the marker is keyed by turn, so a reap on a PRIOR
    turn must NOT block a fresh seat on a LATER turn. This locks the fix to
    turn-keying: a guard that keyed on type alone would wrongly block here."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name="Pilot")
    turn = snapshot.turn_manager.interaction

    _seat_and_resolve_dogfight(snapshot, pack)
    reap_resolved_encounter_husk(snapshot, is_dice_replay=False, turn=turn)
    assert snapshot.encounter is None

    # A genuinely new turn elapses.
    snapshot.turn_manager.record_interaction()
    assert snapshot.turn_manager.interaction == turn + 1, (
        "precondition: the interaction counter advanced to a later turn"
    )

    # A fresh dogfight on the later turn still seats — the prior-turn marker is inert.
    result = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT,
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert result is not None, (
        "a reaped marker from a PRIOR turn must not block a later-turn seat "
        "(the created_turn exemption — guard must key on (type, turn), not type alone)"
    )
    assert snapshot.encounter is not None


def test_reseat_refusal_emits_observable_watcher_event(monkeypatch) -> None:
    """AC7 — the refusal is observable, never silent. When the seater refuses a
    same-turn re-seat it must publish a ``reseat_refused_husk_reaped`` state
    transition the GM panel can see (CLAUDE.md: OTEL is the lie detector)."""
    pack = make_dogfight_pack()
    snapshot = make_empty_snapshot(pc_name="Pilot")
    turn = snapshot.turn_manager.interaction

    _seat_and_resolve_dogfight(snapshot, pack)
    reap_resolved_encounter_husk(snapshot, is_dice_replay=False, turn=turn)
    assert snapshot.encounter is None

    # Spy on the module's watcher alias AFTER the reap, so we only observe events
    # emitted by the re-seat attempt itself (mirrors the universal-retrieval
    # dispatch test pattern: monkeypatch the module-local ``_watcher_publish``).
    captured: list[tuple] = []

    def _spy(event_type, fields, component=None, severity="info", **kwargs):
        captured.append((event_type, fields, component))

    monkeypatch.setattr(encounter_lifecycle, "_watcher_publish", _spy)

    result = instantiate_encounter_from_trigger(
        snapshot=snapshot,
        pack=pack,
        encounter_type=DOGFIGHT,
        player_name="Pilot",
        npcs_present=[],
        genre_slug=GENRE_SLUG,
    )
    assert result is None

    refusals = [
        (event_type, fields, component)
        for (event_type, fields, component) in captured
        if fields.get("op") == "reseat_refused_husk_reaped"
    ]
    assert len(refusals) == 1, (
        "exactly one reseat_refused_husk_reaped event must fire — the refusal "
        f"cannot be silent (CLAUDE.md No Silent Fallbacks); got {len(refusals)}"
    )
    event_type, fields, component = refusals[0]
    assert event_type == "state_transition"
    assert component == "encounter"
    assert fields.get("field") == "encounter"
    assert fields.get("encounter_type") == DOGFIGHT
    assert fields.get("turn") == str(turn), (
        "the event must carry the turn the duel was reaped on so the GM panel can "
        f"correlate the refusal to the reap; expected {turn!r}, got {fields.get('turn')!r}"
    )
