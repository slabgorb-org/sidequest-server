"""Post-resolution PC-down handler (EH-2 burning_peace playtest 2026-06-05).

PLAYTEST BUG: after a combat DEFEAT (``opponent_victory``), the PC is left parked
at HP 0/10 with full free-input agency — no downed flag, no recovery, no death
state, and no OTEL span recording any decision. The opponent-reprisal path
resolves the encounter against the player but applies NO mechanical consequence
to the player (the player-strike / player-cast paths run the CWN/WWN downed seam;
the reprisal path does not).

``apply_post_resolution_lethality`` is the single policy-driven seam. It reads the
mandatory genre ``lethality_policy.verdicts_on_zero_hp.pc`` and applies one
mechanical consequence per resolution:

  - NON-LETHAL verdict (defeated/captured/humiliated/maimed/unscathed): recover the
    PC to a 1-HP floor + a "Recovering" Wound status (the recoverable setback).
  - LETHAL verdict (dead/dying): leave HP at 0 + a "Downed" Scar status.

Always emits ``encounter.post_resolution_lethality`` — the lie-detector for "did
anything handle the 0-HP exit". Idempotent across the two production call sites.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.lethality import LethalityPolicy, VerdictsOnZeroHp
from sidequest.server.post_resolution_lethality import (
    SPAN_POST_RESOLUTION_LETHALITY,
    apply_post_resolution_lethality,
)

PLAYER = "Lucy"
OPPONENT = "Padre Ferreira"


def _policy(pc_verdict: str, reversibility: str = "reversible_with_cost") -> LethalityPolicy:
    return LethalityPolicy(
        genre_key="test_genre",
        default_reversibility=reversibility,  # type: ignore[arg-type]
        verdicts_on_zero_hp=VerdictsOnZeroHp(pc=pc_verdict, npc="defeated"),  # type: ignore[arg-type]
        soul_md_constraint="genre_truth:test",
        must_narrate="A recoverable setback with meaningful cost.",
        must_not_narrate="permanent death of a PC",
    )


class _Pack:
    """Minimal stand-in carrying only the lethality_policy the seam reads."""

    def __init__(self, policy: LethalityPolicy | None) -> None:
        self.lethality_policy = policy


def _snapshot(player_hp: int) -> GameSnapshot:
    player = Character(
        core=CreatureCore(
            name=PLAYER,
            description="Ember Isles channeler",
            personality="steady",
            hp=HpPool(current=player_hp, max=10, base_max=10),
        ),
        char_class="Channeler",
        race="Islander",
        backstory="Temple-trained.",
    )
    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    return snap


def _resolved_encounter(outcome: str | None) -> StructuredEncounter:
    enc = StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(
            name="momentum", current=0, starting=0, threshold=1_000_000
        ),
        actors=[
            EncounterActor(name=PLAYER, role="combatant", side="player"),
            EncounterActor(name=OPPONENT, role="combatant", side="opponent"),
        ],
    )
    enc.resolved = outcome is not None
    enc.outcome = outcome
    return enc


def _spans(otel_capture, name: str = SPAN_POST_RESOLUTION_LETHALITY):
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# Non-lethal verdict — the PC recovers to a floor and is no longer at 0/10
# ---------------------------------------------------------------------------


def test_non_lethal_verdict_recovers_pc_to_floor(otel_capture):
    """EH ships pc=`defeated` (non-lethal). After opponent_victory at 0 HP the
    PC must recover to a 1-HP floor (alive, barely) so play does not continue at
    0/10 — and pick up a "Recovering" status carrying the cost."""
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=6
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert core.hp.current == 1, (
        f"a non-lethal defeat must recover the PC off 0 HP to the floor; got {core.hp.current}"
    )
    assert any("Recovering" in s.text for s in core.statuses), (
        f"a non-lethal defeat must attach a Recovering status; got {[s.text for s in core.statuses]}"
    )


def test_non_lethal_verdict_emits_decision_span(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=6
    )

    spans = _spans(otel_capture)
    assert len(spans) == 1, f"exactly one decision span; got {len(spans)}"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("decision") == "non_lethal_recover"
    assert attrs.get("verdict") == "defeated"
    assert attrs.get("actor") == PLAYER
    assert attrs.get("hp_before") == 0
    assert attrs.get("hp_after") == 1


# ---------------------------------------------------------------------------
# Lethal verdict — the PC stays down and is flagged Downed (not silently at 0)
# ---------------------------------------------------------------------------


def test_lethal_verdict_holds_pc_down_and_flags(otel_capture):
    """A lethal genre (pc=`dying`/`dead`) must NOT auto-heal the PC. The PC stays
    at 0 HP but takes a Downed status so the 0-HP state is mechanically real, not
    a silent parked-at-0 with full agency."""
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap,
        encounter=snap.encounter,
        pack=_Pack(_policy("dying", reversibility="permanent")),
        turn=6,
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert core.hp.current == 0, "a lethal verdict must NOT recover the PC's HP"
    assert any("Downed" in s.text for s in core.statuses), (
        f"a lethal verdict must flag the PC Downed; got {[s.text for s in core.statuses]}"
    )
    spans = _spans(otel_capture)
    assert len(spans) == 1
    assert (spans[0].attributes or {}).get("decision") == "lethal_down"


# ---------------------------------------------------------------------------
# Gates — only fires for a PC-down resolution
# ---------------------------------------------------------------------------


def test_unresolved_encounter_is_noop(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter(None)  # not resolved

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=1
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None and core.hp.current == 0
    assert not _spans(otel_capture)


def test_player_victory_outcome_is_noop(otel_capture):
    """The PC won — even if (somehow) at 0 HP, a player_victory is not a PC-down
    resolution; the seam must not fire."""
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("player_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=1
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None and core.hp.current == 0
    assert not _spans(otel_capture)


def test_pc_above_zero_is_noop(otel_capture):
    """A dial-threshold opponent_victory where the PC still has HP needs no HP
    recovery — gate on PC-at-0."""
    snap = _snapshot(player_hp=4)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=1
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None and core.hp.current == 4
    assert not any("Recovering" in s.text for s in core.statuses)
    assert not _spans(otel_capture)


# ---------------------------------------------------------------------------
# Idempotency — the two production call sites can both call it safely
# ---------------------------------------------------------------------------


def test_idempotent_non_lethal(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")
    pack = _Pack(_policy("defeated"))

    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=6)
    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=6)

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert core.hp.current == 1, "second call must not heal further"
    recovering = [s for s in core.statuses if "Recovering" in s.text]
    assert len(recovering) == 1, f"second call must not stack a status; got {len(recovering)}"


def test_idempotent_lethal(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")
    pack = _Pack(_policy("dying", reversibility="permanent"))

    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=6)
    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=6)

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    downed = [s for s in core.statuses if "Downed" in s.text]
    assert len(downed) == 1, f"lethal flag must not stack; got {len(downed)}"


# ---------------------------------------------------------------------------
# sq-playtest 2026-06-07 SILENT death-spiral — the decision must SURFACE:
# narrator directive, status_added watcher event, INFO log. (Groucho sat
# "Downed — dying" in state while the prose had him crewing a boarding action.)
# ---------------------------------------------------------------------------


def _capture_lethality_watcher(monkeypatch) -> list[dict]:
    import sidequest.server.post_resolution_lethality as prl_mod

    captured: list[dict] = []

    def _capture(event_type, fields, **kwargs):
        captured.append({"event_type": event_type, **fields})

    monkeypatch.setattr(prl_mod, "_watcher_publish", _capture)
    return captured


def test_lethal_down_appends_narrator_directive(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("dying")), turn=6
    )

    downed = [d for d in snap.next_turn_directives if "DOWN" in d and PLAYER in d]
    assert downed, (
        f"a lethal down must append a narrator directive — the narration is the "
        f"only channel telling the table the PC is dying; got "
        f"{snap.next_turn_directives!r}"
    )


def test_non_lethal_recover_appends_narrator_directive(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=6
    )

    assert any("brink" in d and PLAYER in d for d in snap.next_turn_directives), (
        f"a non-lethal recovery must append a cost directive; got {snap.next_turn_directives!r}"
    )


def test_decision_publishes_status_added_watcher_event(otel_capture, monkeypatch):
    """op="status_added" → _maybe_persist_encounter_row persists an
    ENCOUNTER_STATUS_ADDED row: the forensic timeline gains the authoring event
    for the Downed/Recovering status."""
    captured = _capture_lethality_watcher(monkeypatch)
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("dying")), turn=6
    )

    rows = [e for e in captured if e.get("op") == "status_added"]
    assert len(rows) == 1, f"exactly one status_added event; got {captured!r}"
    assert rows[0]["actor"] == PLAYER
    assert rows[0]["decision"] == "lethal_down"
    assert rows[0]["source"] == "post_resolution_lethality"
    assert rows[0]["field"] == "encounter"


def test_decision_logs_info_line(otel_capture, caplog):
    import logging

    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    with caplog.at_level(logging.INFO, logger="sidequest.server.post_resolution_lethality"):
        apply_post_resolution_lethality(
            snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("dying")), turn=6
        )

    assert any("post_resolution_lethality.applied" in r.message for r in caplog.records), (
        "the decision must INFO-log for text-log forensics"
    )


def test_idempotent_skip_adds_no_second_directive(otel_capture):
    """The idempotency gate must also cover the surfacing channels — a re-run
    must not stack duplicate narrator directives."""
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")
    pack = _Pack(_policy("dying"))

    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=6)
    first = list(snap.next_turn_directives)
    apply_post_resolution_lethality(snapshot=snap, encounter=snap.encounter, pack=pack, turn=7)

    assert snap.next_turn_directives == first, (
        f"idempotent re-run must not append a second directive; got {snap.next_turn_directives!r}"
    )


# ---------------------------------------------------------------------------
# sq-playtest 2026-06-07 (barsoom-3, BLOCKING) — a dead PC kept full agency for
# four rounds. The lethal verdict must (1) stamp a DURABLE incapacitating marker
# the turn-intake gate reads, and (2) RETURN an event so the kill-turn dispatch
# can broadcast the player-facing death surface.
# ---------------------------------------------------------------------------


def test_lethal_down_sets_incapacitating_marker(otel_capture):
    """The Downed status must carry the structured ``incapacitating`` flag — the
    durable signal the turn-intake gate keys on (NOT a text match)."""
    from sidequest.game.incapacitation import find_incapacitating_status, is_incapacitated

    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("dead")), turn=6
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert is_incapacitated(core), "a lethal down must mark the PC incapacitated"
    status = find_incapacitating_status(core)
    assert status is not None and status.incapacitating is True


def test_non_lethal_recover_does_NOT_set_incapacitating_marker(otel_capture):
    """A recoverable defeat leaves the PC with agency — the marker stays False so
    the intake gate never locks a still-playing PC."""
    from sidequest.game.incapacitation import is_incapacitated

    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=6
    )

    core = snap.find_creature_core(PLAYER)
    assert core is not None
    assert not is_incapacitated(core), (
        "a non-lethal recovery must NOT incapacitate — the PC keeps acting"
    )


def test_lethal_down_returns_incapacitation_event(otel_capture):
    """The kill-turn dispatch surfaces the death from this return value."""
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")

    events = apply_post_resolution_lethality(
        snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("dead")), turn=6
    )

    assert len(events) == 1, f"one PC down → one event; got {events!r}"
    assert events[0].actor == PLAYER
    assert events[0].verdict == "dead"
    assert "Downed" in events[0].status_text


def test_non_lethal_and_noop_return_no_events(otel_capture):
    snap = _snapshot(player_hp=0)
    snap.encounter = _resolved_encounter("opponent_victory")
    assert (
        apply_post_resolution_lethality(
            snapshot=snap, encounter=snap.encounter, pack=_Pack(_policy("defeated")), turn=6
        )
        == []
    ), "a non-lethal recover incapacitates no one"

    snap2 = _snapshot(player_hp=0)
    snap2.encounter = _resolved_encounter("player_victory")
    assert (
        apply_post_resolution_lethality(
            snapshot=snap2, encounter=snap2.encounter, pack=_Pack(_policy("dead")), turn=6
        )
        == []
    ), "a non-PC-down outcome returns no events"


def test_build_incapacitated_message_and_headline():
    from sidequest.server.post_resolution_lethality import (
        build_incapacitated_message,
        incapacitation_headline,
        verdict_from_status_text,
    )

    assert incapacitation_headline("Abinthe", "dead") == "Abinthe has fallen."
    assert "bleeding out" in incapacitation_headline("Abinthe", "dying")
    # unknown verdict degrades to the generic line, never crashes
    assert incapacitation_headline("Abinthe", "") == "Abinthe is out of the fight."

    assert verdict_from_status_text("Downed — dead (mortally wounded)") == "dead"
    assert verdict_from_status_text("Downed — dying (mortally wounded)") == "dying"
    assert verdict_from_status_text("Recovering — defeated; reversible") == ""

    msg = build_incapacitated_message(
        character_name="Abinthe",
        verdict="dead",
        status_text="Downed — dead (mortally wounded)",
        player_id="p1",
    )
    assert msg.type.value == "CHARACTER_INCAPACITATED"
    assert msg.payload.character_name == "Abinthe"
    assert msg.payload.headline == "Abinthe has fallen."
    assert msg.payload.can_reroll is True
    assert msg.player_id == "p1"
