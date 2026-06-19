"""Defense recording (spec 2026-06-18 §5,§7, story 126-8): FATE_THROW(defend)
resolves from the player's faces (NEVER roll_4df), records defense_total on the
matching pending_defenses entry by request_id, reports when the ledger is full,
and fails loud on an unknown / already-filled request_id (No Silent Fallbacks).

RED: dispatch_fate_defense does not exist yet (plan Task 6).
"""

from __future__ import annotations

import pytest

import sidequest.game.ruleset.fate_resolution as fate_resolution
import sidequest.server.dispatch.fate_conflict as fate_conflict
from sidequest.game.ruleset import get_ruleset_module
from sidequest.server.dispatch.fate_conflict import (
    FateConflictError,
    dispatch_fate_defense,
)
from tests._helpers.fate_fixtures import parked_conflict, parked_conflict_filled


def test_defense_records_from_faces_and_never_rolls(monkeypatch):
    snap, encounter = parked_conflict(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=4, defend_skill_rating=2
    )
    ruleset = get_ruleset_module("fate")

    called = {"roll_4df": 0}
    real = fate_resolution.roll_4df
    monkeypatch.setattr(
        fate_resolution,
        "roll_4df",
        lambda rng: called.__setitem__("roll_4df", called["roll_4df"] + 1) or real(rng),
    )

    res = dispatch_fate_defense(
        encounter=encounter,
        snapshot=snap,
        ruleset=ruleset,
        actor_name="Rux",
        request_id="d1",
        skill="Athletics",
        thrown_faces=(1, 1, 0, 0),
    )

    entry = next(p for p in encounter.pending_defenses if p.request_id == "d1")
    assert entry.defense_total == 1 + 1 + 2  # faces(+2) + Athletics(2), opposition 0
    assert res.ledger_full is True
    assert called["roll_4df"] == 0  # PLAYER defense never server-rolls (ADR-148)


def test_unknown_request_id_fails_loud():
    snap, encounter = parked_conflict(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=4, defend_skill_rating=2
    )
    ruleset = get_ruleset_module("fate")
    with pytest.raises(FateConflictError):
        dispatch_fate_defense(
            encounter=encounter,
            snapshot=snap,
            ruleset=ruleset,
            actor_name="Rux",
            request_id="NOPE",
            skill="Athletics",
            thrown_faces=(0, 0, 0, 0),
        )


def test_already_filled_request_id_fails_loud():
    # One defense per attack — a second throw against a filled entry is an error,
    # not a silent overwrite (No Silent Fallbacks, lang-review #1).
    snap, encounter = parked_conflict_filled(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=5, recorded_defense_total=2
    )
    ruleset = get_ruleset_module("fate")
    with pytest.raises(FateConflictError):
        dispatch_fate_defense(
            encounter=encounter,
            snapshot=snap,
            ruleset=ruleset,
            actor_name="Rux",
            request_id="d1",
            skill="Athletics",
            thrown_faces=(1, 1, 0, 0),
        )


def test_defend_throw_from_non_defender_is_rejected():
    # ADR-119 authorization: a seated player may only answer THEIR OWN defend
    # request. ``request_id`` is client-supplied AND derivable, so without this guard
    # player Mallory could fill (and grief) Rux's defense with Mallory's own
    # dice/skill and lock Rux out ("already recorded"). The throw must fail loud and
    # leave Rux's entry UNFILLED so the real defender can still answer.
    snap, encounter = parked_conflict(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=4, defend_skill_rating=2
    )
    ruleset = get_ruleset_module("fate")
    with pytest.raises(FateConflictError):
        dispatch_fate_defense(
            encounter=encounter,
            snapshot=snap,
            ruleset=ruleset,
            actor_name="Mallory",  # NOT the defender (Rux) — a different seat
            request_id="d1",
            skill="Athletics",
            thrown_faces=(-1, -1, -1, -1),  # a griefing throw
        )

    entry = next(p for p in encounter.pending_defenses if p.request_id == "d1")
    assert entry.defense_total is None  # untouched — Mallory cannot fill Rux's defense
    assert entry.conceded is False


def test_defend_authorization_rejection_emits_watcher_event(monkeypatch):
    # Story 126-13 (WIRING): the defend-path authorization rejection must reach the
    # GM-panel lie-detector at parity with the FATE_THROW player_id spoof-rejection
    # (``fate_throw_player_id_spoof_rejected``). Drive the REAL dispatch through the
    # non-defender path and assert the PUBLISHED WatcherHub event — behavior, not
    # source text (CLAUDE.md "No Source-Text Wiring Tests"). The spy on the module's
    # ``_watcher_publish`` alias is the canonical dispatch-watcher pattern
    # (cf. test_retrieval_reason_watcher.py).
    snap, encounter = parked_conflict(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=4, defend_skill_rating=2
    )
    ruleset = get_ruleset_module("fate")

    captured: list[tuple] = []

    def _spy(event_type, fields, component=None, severity="info", **kwargs):
        captured.append((event_type, fields, component, severity))

    monkeypatch.setattr(fate_conflict, "_watcher_publish", _spy)

    with pytest.raises(FateConflictError):
        dispatch_fate_defense(
            encounter=encounter,
            snapshot=snap,
            ruleset=ruleset,
            actor_name="Mallory",  # NOT the defender (Rux) — the defend-path spoof
            request_id="d1",
            skill="Athletics",
            thrown_faces=(-1, -1, -1, -1),
        )

    rejects = [
        (et, f, comp, sev)
        for (et, f, comp, sev) in captured
        if f.get("op") == "fate_defend_authorization_rejected"
    ]
    assert len(rejects) == 1, (
        f"exactly one defend-authorization-rejected watcher event; got {len(rejects)}"
    )
    event_type, fields, component, severity = rejects[0]
    assert event_type == "state_transition"
    assert component == "encounter"
    assert severity == "warning"  # parity with the player_id spoof-rejection
    # Parity fields: who threw, whose defense it was, and the attack context — so the
    # GM panel can name the griefer and the victim, mirroring inbound/authenticated.
    assert fields["throwing_actor"] == "Mallory"
    assert fields["request_defender"] == "Rux"
    assert fields["request_id"] == "d1"
    assert fields["attacker"] == "Bandit"
    assert fields["source"] == "fate_defense"


def test_concede_marks_entry_and_fills_ledger():
    snap, encounter = parked_conflict(
        defender="Rux", attacker="Bandit", request_id="d1", attack_total=4, defend_skill_rating=2
    )
    ruleset = get_ruleset_module("fate")
    res = dispatch_fate_defense(
        encounter=encounter,
        snapshot=snap,
        ruleset=ruleset,
        actor_name="Rux",
        request_id="d1",
        skill="Athletics",
        thrown_faces=(0, 0, 0, 0),
        conceded=True,
    )
    entry = next(p for p in encounter.pending_defenses if p.request_id == "d1")
    assert entry.conceded is True
    assert res.conceded is True
    assert res.ledger_full is True
