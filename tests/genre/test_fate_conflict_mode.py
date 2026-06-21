"""Story 153-3 [WRY-WHIMSY-NO-FATE-CONTEST-DEFS] — the `conflict` resolution_mode
and the loud Fate-mode loader guard (RED phase).

Design: `sprint/context/context-story-153-3.md` (Architect, ADR-144). A Fate
**Contest** is a no-harm first-to-N competition; a Fate **Conflict** is a lethal
fight (4dF + ladder, ablative stress -> consequences -> Taken Out, resolved
against the Other's FateSheet). The native dial mode `beat_selection` is REMOVED
from the Fate path — a Fate pack must declare a Fate resolution mode
(contest / conflict / table_resolution / sealed_letter_lookup), never the native
default. wry_whimsy / pulp_noir / spaghetti_western currently smuggle lethal and
social confrontations in as `beat_selection` with armed-but-INERT native beats;
this story adds the explicit `conflict` mode + the loud guard so they fail loud
until ported.

These tests drive the pydantic model layer (the same `RulesConfig.model_validate`
that `load_genre_pack` calls), mirroring `tests/genre/test_fate_no_opposed_check.py`.
The real-content port is verified at the `load_genre_pack` boundary in
`tests/integration/test_153_3_fate_pack_port.py`.

RED today: `ResolutionMode` has no `conflict` value, and no guard rejects a Fate
`beat_selection` def.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, FateConfig, RulesConfig

_WN_ATTRS = ("STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA")
_FLAVOR = ("Strength", "Constitution", "Dexterity", "Intelligence", "Wisdom", "Charisma")
_CWN_AMAP = dict(zip(_WN_ATTRS, _FLAVOR, strict=True))


def _fate_rules(**kwargs) -> RulesConfig:
    """Minimal valid Fate RulesConfig (empty FateConfig; all fields default)."""
    return RulesConfig(ruleset="fate", fate=FateConfig(), **kwargs)


def _cwn_rules(**kwargs) -> RulesConfig:
    """Minimal valid CWN RulesConfig — all six WN attributes wired."""
    return RulesConfig(
        ruleset="cwn",
        ability_score_names=list(_FLAVOR),
        cwn=CwnConfig(attribute_map=_CWN_AMAP),
        **kwargs,
    )


def _conflict_conf(ctype: str = "brawl", category: str = "combat") -> dict:
    """A lethal Fate Conflict def: display-only beats, NO player/opponent metric
    (the win track is the opponent's FateSheet stress, synthesized inert at
    seating)."""
    return {
        "type": ctype,
        "label": "Brawl",
        "category": category,
        "resolution_mode": "conflict",
        "beats": [{"id": "press", "label": "Press the Attack"}],
    }


def _contest_conf(ctype: str = "duel") -> dict:
    return {
        "type": ctype,
        "label": "Duel of Wits",
        "category": "social",
        "resolution_mode": "contest",
        "player_metric": {"name": "upper_hand", "starting": 0, "threshold": 3},
        "beats": [{"id": "riposte", "label": "Riposte"}],
    }


def _native_conf(ctype: str = "thug_fight", category: str = "social") -> dict:
    """A fully-VALID native beat_selection def (armed beats + both dial metrics).
    It loads on a non-Fate pack today; the Fate guard must REJECT it on a Fate
    pack. Beats are armed (kind+stat_check) and both metrics present so the ONLY
    possible rejection reason is the new Fate-mode guard — not the pre-existing
    'beat missing dial field' or 'missing player/opponent metric' guards."""
    return {
        "type": ctype,
        "label": "Thug Fight",
        "category": category,
        "resolution_mode": "beat_selection",
        "player_metric": {"name": "momentum", "starting": 0, "threshold": 10},
        "opponent_metric": {"name": "momentum", "starting": 0, "threshold": 10},
        "beats": [
            {"id": "swing", "label": "Swing", "kind": "strike", "stat_check": "Resolve", "base": 2}
        ],
    }


# --- AC-1: the new lethal Fate Conflict authoring mode --------------------------


def test_conflict_mode_loads_with_display_only_beats_and_no_metric():
    cfg = _fate_rules(confrontations=[_conflict_conf()])
    cdef = cfg.confrontations[0]
    assert cdef.resolution_mode == "conflict"
    assert cdef.player_metric is None and cdef.opponent_metric is None
    assert all(b.kind is None and b.stat_check is None for b in cdef.beats)


def test_conflict_mode_rejects_armed_beats():
    """A conflict beat is display-only, same contract as contest — an armed dial
    field is rejected so the dial engine can't run alongside the Fate engine."""
    bad = _conflict_conf()
    bad["beats"] = [{"id": "swing", "label": "Swing", "kind": "strike", "stat_check": "Fight"}]
    with pytest.raises(ValidationError):
        _fate_rules(confrontations=[bad])


# --- AC-2: contest behaviour is unchanged (no regression from the conflict add) -


def test_contest_still_rejects_armed_beats():
    bad = _contest_conf()
    bad["beats"] = [{"id": "riposte", "label": "Riposte", "kind": "strike", "stat_check": "Will"}]
    with pytest.raises(ValidationError):
        _fate_rules(confrontations=[bad])


def test_contest_still_requires_player_metric():
    """The conflict metric-exemption must NOT leak into contest — contest still
    seeds its victory target from player_metric.threshold."""
    bad = _contest_conf()
    del bad["player_metric"]
    with pytest.raises(ValidationError, match="player_metric"):
        _fate_rules(confrontations=[bad])


# --- AC-3: the loud Fate-mode guard --------------------------------------------


def test_fate_pack_rejects_native_beat_selection():
    with pytest.raises(ValidationError) as excinfo:
        _fate_rules(confrontations=[_native_conf("thug_fight")])
    # The guard must name the offending confrontation (No Silent Fallbacks).
    assert "thug_fight" in str(excinfo.value)


def test_fate_pack_still_rejects_opposed_check():
    """The existing opposed_check ban (the bleed tripwire) survives the guard
    generalization."""
    oc = _native_conf("wits_duel")
    oc["resolution_mode"] = "opposed_check"
    with pytest.raises(ValidationError, match="opposed_check"):
        _fate_rules(confrontations=[oc])


def test_fate_pack_allows_contest_and_conflict():
    cfg = _fate_rules(confrontations=[_contest_conf("duel"), _conflict_conf("brawl")])
    modes = {c.confrontation_type: c.resolution_mode for c in cfg.confrontations}
    assert modes["duel"] == "contest"
    assert modes["brawl"] == "conflict"


# --- AC-4: the guard is Fate-gated — native packs keep the native dial ----------


def test_non_fate_pack_keeps_native_beat_selection():
    cfg = _cwn_rules(confrontations=[_native_conf("brawl", category="combat")])
    assert cfg.confrontations[0].resolution_mode == "beat_selection"
