"""Story 108-6 (RED) — the downed seam branches on live-hostile presence.

Under a WWN binding, an ordinary 0-HP down resolves one of two ways, and they
must stay mutually exclusive (the #846 single-status coherence):

  - A live hostile can still act on the PC  → terminal-dead immediately, as
    today. NO stabilizable window (no last stand at sword-point).
  - No live hostile (the field is cleared — the scoped solo case) → the WWN
    dying window opens as the single status, and the seam emits
    ``wwn.dying_window.opened`` with reason=no_live_hostile (the GM-panel proof
    the window path fired rather than the terminal path).

Doctrine (ADR-143): branching on whether a hostile is alive is a seating
decision, NOT a native beat/dial tune. Nothing native is converted or gated.

Reuses the production downed-seam harness from test_reprisal_wn_downed_seam.py
(MagicMock pack + REAL RulesConfig + REAL encounter). The no-live-hostile case
is produced by ablating the seated opponent to 0 HP before running the seam.
``otel_capture`` (tests/server/conftest.py) captures the global-tracer span the
seam emits.
"""

from __future__ import annotations

from sidequest.game.ruleset import get_ruleset_module
from sidequest.server.dispatch.downed_seam import run_cwn_wwn_downed_seam
from tests.server.test_reprisal_wn_downed_seam import (
    OPPONENT,
    PLAYER,
    _make_reprisal_pack,
    _make_snapshot_and_encounter,
)


def _run_seam(*, hostile_alive: bool):
    """Drive the WWN downed seam with the PC at 0 HP. ``hostile_alive`` toggles
    whether the seated opponent still has HP (live hostile) or is ablated to 0
    (field cleared — the solo case)."""
    pack = _make_reprisal_pack("wwn", pc_verdict="dying")
    snap, enc = _make_snapshot_and_encounter(player_hp=0)
    if not hostile_alive:
        opp_core = snap.find_creature_core(OPPONENT)
        assert opp_core is not None
        opp_core.hp.current = 0
    cdef = pack.rules.confrontations[0]
    run_cwn_wwn_downed_seam(
        ruleset=get_ruleset_module("wwn"),
        snapshot=snap,
        encounter=enc,
        cdef=cdef,
        pack=pack,
        actor_side="opponent",
    )
    return snap.find_creature_core(PLAYER)


def test_no_live_hostile_opens_the_dying_window():
    from sidequest.game.ruleset.without_number import is_dying_window_status

    core = _run_seam(hostile_alive=False)
    assert core is not None
    windows = [s for s in core.statuses if is_dying_window_status(s)]
    assert len(windows) == 1, (
        f"a solo down (no live hostile) must open exactly one stabilizable window; "
        f"got {[s.text for s in core.statuses]}"
    )


def test_no_live_hostile_emits_opened_span(otel_capture):
    core = _run_seam(hostile_alive=False)
    assert core is not None
    opened = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.dying_window.opened"]
    assert len(opened) == 1, (
        f"the window-open decision must emit wwn.dying_window.opened; "
        f"got {[s.name for s in otel_capture.get_finished_spans()]}"
    )
    attrs = dict(opened[0].attributes or {})
    assert attrs["actor"] == PLAYER
    assert attrs["reason"] == "no_live_hostile"


def test_live_hostile_goes_terminal_no_window(otel_capture):
    from sidequest.game.ruleset.without_number import is_dying_window_status

    core = _run_seam(hostile_alive=True)
    assert core is not None
    assert not any(is_dying_window_status(s) for s in core.statuses), (
        "a PC downed while an enemy still stands must NOT get a stabilizable window "
        "(terminal path — #846 coherence regression guard)"
    )
    # And the window-open span must NOT fire on the terminal branch.
    opened = [s for s in otel_capture.get_finished_spans() if s.name == "wwn.dying_window.opened"]
    assert not opened, "the terminal branch must not emit wwn.dying_window.opened"
