"""Story 102-4 AC3 — dead_premise: a committed action whose premise died.

SWN module design §6.4: when a committed action's target is already dropped
at the actor's initiative slot (a higher-initiative ally got the kill), the
engine emits a typed dead-premise signal and the NARRATOR adjudicates —
redirect in fiction or fizzle. The engine stays deterministic about math;
the narrator is authoritative about fiction. **The player never re-chooses,
and the engine never auto-acts the player** (SOUL: The Test).

Contract pinned here:

- OTEL span ``wwn.dead_premise`` (epic ``{ruleset}.{surface}`` invariant;
  the P4 design's provisional ``encounter.dead_premise`` name yields to the
  epic-context invariant — deviation logged in the session file) with
  ``actor`` and ``target`` attributes naming who swung and who was already
  down.
- NO mechanical resolution of the dead-premised action: no beat application,
  no damage to the corpse, no damage to anyone else (no auto-retarget).
- The narrator surface: ``enc.narrator_hints`` gains a dead-premise line
  naming actor and fallen target — narrator_hints is the existing
  encounter_render -> narrator-prompt pipe, so this IS the narrator call
  reaching prose. (The full ``swn_adjudicate_dead_premise`` tool shape is
  102-5's; this story surfaces the event, per the story scope boundary.)

Kill choreography (deterministic): heavy_metal's ``committed_blow`` carries
``damage_override: 2d6``; with ``random.randint`` pinned to max, A's strike
deals 12 to the 10-HP blade — a one-slot kill, guaranteed.

Skips cleanly when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests.integration._wn_round_102_4 import (
    GENRE_PACKS_DIR,
    dispatch_throw,
    force_initiative,
    load_pack,
    seat_wn_combat,
    spans_named,
)

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)

_OPP = "Hired Blade"
_OPP_2 = "Second Blade"
_PC_A = "Vesska"
_PC_B = "Brakka"

_SPAN_DEAD_PREMISE = "wwn.dead_premise"
_SPAN_BEAT_APPLIED = "encounter.beat_applied"


@pytest.fixture
def kill_order_combat(monkeypatch, otel_capture):
    """A kills the blade at slot 1; B's committed strike on it is premise-dead.

    Forced order: A(9) -> B(7) -> blade(2). rng pinned max so A's 2d6=12
    overkills the 10-HP blade before B's slot arrives.

    Depends on ``otel_capture`` so the in-memory exporter is installed
    BEFORE this fixture dispatches the round — the round spans fire during
    fixture setup, and a test-signature ordering of (kill_order_combat,
    otel_capture) would otherwise instantiate the capture too late to see
    them (Dev green-phase fix, 102-4).
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A, _PC_B], [_OPP])
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2)])

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")
    return pack, snap, enc


def test_dead_premise_span_fires_with_actor_and_target(kill_order_combat, otel_capture):
    """At B's slot the engine must emit the typed dead-premise signal — the
    GM-panel proof that the engine noticed the corpse, not the narrator."""
    dead = spans_named(otel_capture, _SPAN_DEAD_PREMISE)
    assert dead, (
        "a committed action whose target dropped earlier in the order must "
        f"emit {_SPAN_DEAD_PREMISE}; finished spans: "
        f"{[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dead[0].attributes
    assert attrs.get("actor") == _PC_B, f"dead_premise actor must be {_PC_B}; got {attrs}"
    assert attrs.get("target") == _OPP, f"dead_premise target must be {_OPP}; got {attrs}"


def test_no_mechanical_resolution_applies_to_a_corpse(kill_order_combat, otel_capture):
    """B's strike must NOT resolve: exactly one beat application (A's kill),
    and the corpse stays at 0 — dead premises are narrator calls, not hits."""
    _pack, snap, _enc = kill_order_combat
    assert snap.find_creature_core(_OPP).hp.current == 0, (
        "A's pinned 2d6=12 must have dropped the 10-HP blade at slot 1 "
        "(fixture precondition — if this fails the kill choreography broke)"
    )
    applied = spans_named(otel_capture, _SPAN_BEAT_APPLIED)
    assert len(applied) == 1, (
        "exactly ONE beat may apply this round (A's kill); a second "
        "application means the engine mechanically resolved B's dead-premise "
        f"swing; got {len(applied)} beat_applied spans"
    )


def test_dead_premise_reaches_the_narrator_surface(kill_order_combat):
    """The narrator must receive the dead premise to adjudicate: a
    narrator_hints line naming B and the fallen blade (narrator_hints is
    consumed by encounter_render into the narrator prompt — existing pipe)."""
    _pack, _snap, enc = kill_order_combat
    hints = " | ".join(enc.narrator_hints)
    assert _PC_B in hints and _OPP in hints, (
        "the dead-premise narrator call must name the swinging actor and the "
        "already-down target so the narrator can redirect-or-fizzle in "
        f"fiction; narrator_hints: {enc.narrator_hints!r}"
    )


def test_engine_never_auto_retargets_the_swing(otel_capture, monkeypatch):
    """SOUL — The Test: with a SECOND live opponent standing right there,
    the engine must still not redirect B's dead-premise swing. Re-aiming is
    the player's/narrator's call in fiction, never engine improv."""
    monkeypatch.setattr("random.randint", lambda a, b: b)
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A, _PC_B], [_OPP, _OPP_2])
    force_initiative(enc, [(_PC_A, 9), (_PC_B, 7), (_OPP, 2), (_OPP_2, 1)])
    second_hp_before = snap.find_creature_core(_OPP_2).hp.current

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")
    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_B, player_id="p2")

    assert spans_named(otel_capture, _SPAN_DEAD_PREMISE), (
        "B's swing at the dead blade is still a dead premise with bystanders"
    )
    assert snap.find_creature_core(_OPP_2).hp.current == second_hp_before, (
        "the second blade must be untouched — damage appearing on a target "
        "the player never chose is the engine acting the player (The Test)"
    )


def test_actor_dropped_before_its_slot_does_not_act(otel_capture, monkeypatch):
    """§6 rule, mechanically enforced (P4 only stated it in prose): the
    opponent at slot 1 drops 2-HP Vesska; her committed strike never lands.
    """
    monkeypatch.setattr("random.randint", lambda a, b: b)  # opp d20=20 hits, d8=8 dmg
    pack = load_pack("heavy_metal")
    snap, enc = seat_wn_combat(pack, [_PC_A], [_OPP], pc_hp=2)
    force_initiative(enc, [(_OPP, 9), (_PC_A, 3)])
    opp_hp_before = snap.find_creature_core(_OPP).hp.current

    dispatch_throw(pack=pack, snap=snap, enc=enc, character_name=_PC_A, player_id="p1")

    assert snap.characters[0].core.hp.current == 0, (
        "fixture precondition: the pinned d8=8 must drop the 2-HP PC at slot 1"
    )
    assert snap.find_creature_core(_OPP).hp.current == opp_hp_before, (
        "an actor reduced to 0 HP earlier in the order does not act — the "
        "downed PC's committed strike must never apply"
    )
    assert not spans_named(otel_capture, _SPAN_BEAT_APPLIED), (
        "no player beat may apply in a round where the PC dropped before their slot"
    )
