"""Story 108-6 (RED) — Status carries a structured ``stabilizable`` flag.

The WWN dying-window input-gate carve (handlers/player_action.py) must tell a
TERMINAL incapacitating status (block) apart from a STABILIZABLE dying-window
status (permit + route to narrator). Per the server's "no source/text scraping
as a wiring signal" rule (cf. the existing ``incapacitating`` field), that
distinction is a structured boolean on Status — never a substring of ``text``.

These tests pin the field's existence, its safe default, and that it survives
the pydantic save/load round-trip (the status lives in the persisted snapshot).
"""

from __future__ import annotations

from sidequest.game.status import Status, StatusSeverity


def test_stabilizable_defaults_false():
    s = Status(text="Bruised", severity=StatusSeverity.Scratch)
    assert s.stabilizable is False


def test_stabilizable_set_true_is_readable():
    s = Status(
        text="Mortal Injury — dies in 6 rounds unless stabilized",
        severity=StatusSeverity.Scar,
        incapacitating=True,
        stabilizable=True,
    )
    assert s.stabilizable is True
    # The two flags are independent — a stabilizable window is ALSO incapacitating
    # (you can't keep swinging) but is not the terminal-dead status.
    assert s.incapacitating is True


def test_stabilizable_survives_model_roundtrip():
    s = Status(
        text="Mortal Injury — dies in 6 rounds unless stabilized",
        severity=StatusSeverity.Scar,
        incapacitating=True,
        stabilizable=True,
    )
    revived = Status.model_validate(s.model_dump())
    assert revived.stabilizable is True
    assert revived.incapacitating is True
