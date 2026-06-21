"""Story 153-3 [WRY-WHIMSY-NO-FATE-CONTEST-DEFS] — content port verification
(RED phase), driven through the real `load_genre_pack` boundary.

These tests double as the wiring proof for the loud guard: once the guard ships,
a Fate pack that still carries a native `beat_selection` confrontation FAILS to
load — so a green here means the guard fired through the production loader AND the
content was ported to the Fate Contest/Conflict schema.

Mapping (Architect design, `sprint/context/context-story-153-3.md`):
- wry_whimsy: 5 social/competition -> contest, `violence` -> conflict.
- pulp_noir: 2 social -> contest, `combat` -> conflict.
- spaghetti_western: standoff/negotiation/chase -> contest, `combat` -> conflict
  (was a no-harm contest; a gunfight is lethal), poker stays table_resolution.
- tea_and_murder: already clean — must keep loading (no over-rejection).

RED today: wry_whimsy / pulp_noir are all `beat_selection`; spaghetti_western's
`combat` is `contest` and standoff/negotiation/chase are `beat_selection`.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from tests._helpers.genre_paths import PackNotFound, find_pack_path

_FATE_MODES = {"contest", "conflict", "table_resolution", "sealed_letter_lookup"}
_DISPLAY_ONLY_MODES = {"contest", "conflict"}

_WRY = {
    "audience": "contest",
    "wit_duel": "contest",
    "escape": "contest",
    "wonder_shock": "contest",
    "persuasion": "contest",
    "violence": "conflict",
}
_PULP = {"standoff": "contest", "combat": "conflict", "negotiation": "contest"}
_SPAG = {
    "standoff": "contest",
    "negotiation": "contest",
    "chase": "contest",
    "combat": "conflict",
    "poker": "table_resolution",
}


def _load(slug: str):
    try:
        path = find_pack_path(slug)
    except PackNotFound:
        pytest.skip(f"content pack {slug!r} not available")
    return load_genre_pack(path)


def _modes(pack) -> dict[str, str]:
    return {c.confrontation_type: str(c.resolution_mode) for c in pack.rules.confrontations}


def _assert_display_only(pack, expected: dict[str, str]) -> None:
    """Every contest/conflict confrontation carries display-only beats (no armed
    dial fields). table_resolution defs (e.g. poker) keep armed beats — excluded."""
    targets = {t for t, m in expected.items() if m in _DISPLAY_ONLY_MODES}
    for c in pack.rules.confrontations:
        if c.confrontation_type in targets:
            for b in c.beats:
                assert b.kind is None and b.stat_check is None, (
                    f"{c.confrontation_type} beat {b.id!r} still armed "
                    f"(kind={b.kind!r}, stat_check={b.stat_check!r}) — strip to display-only"
                )


def _assert_ported(slug: str, expected: dict[str, str]) -> None:
    pack = _load(slug)
    modes = _modes(pack)
    for ctype, mode in expected.items():
        assert modes.get(ctype) == mode, (
            f"{slug} {ctype!r}: expected resolution_mode {mode!r}, got {modes.get(ctype)!r}"
        )
    _assert_display_only(pack, expected)


def test_wry_whimsy_ported_to_contest_conflict():
    _assert_ported("wry_whimsy", _WRY)


def test_pulp_noir_ported_to_contest_conflict():
    _assert_ported("pulp_noir", _PULP)


def test_spaghetti_western_ported_to_contest_conflict():
    _assert_ported("spaghetti_western", _SPAG)


def test_tea_and_murder_still_loads_with_fate_modes():
    """Regression: the already-clean Fate pack must keep loading — the guard must
    not over-reject. Every confrontation already uses a Fate resolution mode."""
    pack = _load("tea_and_murder")
    modes = _modes(pack)
    assert modes, "tea_and_murder declares no confrontations"
    for ctype, mode in modes.items():
        assert mode in _FATE_MODES, f"tea_and_murder {ctype!r} has non-Fate mode {mode!r}"
