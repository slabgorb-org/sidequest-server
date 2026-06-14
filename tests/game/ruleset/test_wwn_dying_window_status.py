"""Story 108-6 (RED) — resolve_downed mints a dying-window status that is
incapacitating AND stabilizable, and exposes a structured predicate.

Today the Mortal Injury window is created NON-incapacitating and only when not
superseded. For the input-gate carve to work the window must be:

  - ``incapacitating=True``  — so the PC can't keep taking normal actions, and
  - ``stabilizable=True``    — the structured marker the gate reads to PERMIT a
    stabilize attempt instead of blocking.

``is_dying_window_status`` is the one predicate the gate, the tool, and the
expiry pass all share — keyed on the structured flag, never on status text.

Builder pattern mirrors test_wwn_lethality.py (WwnRulesetModule + WwnConfig +
CreatureCore; no fixtures). ``mortal_injury_rounds`` is read from cfg, never
hardcoded.
"""

from __future__ import annotations

import random

from sidequest.game.creature_core import CreatureCore
from sidequest.game.ruleset.wwn import WwnRulesetModule
from sidequest.genre.models.rules import WwnConfig

_AMAP = {
    "STRENGTH": "Strength",
    "DEXTERITY": "Agility",
    "CONSTITUTION": "Endurance",
    "INTELLIGENCE": "Insight",
    "WISDOM": "Spirit",
    "CHARISMA": "Harmony",
}
_W = WwnRulesetModule()


def _core() -> CreatureCore:
    return CreatureCore(name="Rux", description="caver", personality="dogged")


def test_window_status_is_incapacitating_and_stabilizable():
    from sidequest.game.ruleset.without_number import is_dying_window_status

    cfg = WwnConfig(attribute_map=_AMAP)
    core = _core()
    _W.resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=False,
        cfg=cfg,
        rng=random.Random(1),
        created_turn=3,
        created_in_encounter="combat",
        superseded_by_terminal=False,
    )
    windows = [s for s in core.statuses if is_dying_window_status(s)]
    assert len(windows) == 1, f"expected exactly one dying-window status; got {[s.text for s in core.statuses]}"
    window = windows[0]
    assert window.incapacitating is True, "window must block normal actions"
    assert window.stabilizable is True
    # Provenance is the clock source — the deadline derives from created_turn.
    assert window.created_turn == 3


def test_superseded_by_terminal_mints_no_window():
    from sidequest.game.ruleset.without_number import is_dying_window_status

    cfg = WwnConfig(attribute_map=_AMAP)
    core = _core()
    _W.resolve_downed(
        core=core,
        save_target=15,
        scene_traumatic=False,
        cfg=cfg,
        rng=random.Random(1),
        created_turn=3,
        created_in_encounter="combat",
        superseded_by_terminal=True,
    )
    assert not any(is_dying_window_status(s) for s in core.statuses), (
        "a terminal-dead PC must NOT also carry a stabilizable window "
        "(the #846 single-status coherence)"
    )


def test_is_dying_window_status_keys_on_flag_not_text():
    """The predicate must not be fooled by a same-worded status that lacks the
    structured flag (no text scraping)."""
    from sidequest.game.ruleset.without_number import is_dying_window_status
    from sidequest.game.status import Status, StatusSeverity

    look_alike = Status(
        text="Mortal Injury — dies in 6 rounds unless stabilized",
        severity=StatusSeverity.Scar,
        # stabilizable defaults False — same text, but NOT the live window.
    )
    assert is_dying_window_status(look_alike) is False
