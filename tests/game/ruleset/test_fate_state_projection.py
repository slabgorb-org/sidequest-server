"""RED tests for Story 118-1 (F3a) — ``build_fate_state_payload`` (the rich projection).

F2b shipped ``build_fate_projection`` (a COMPACT dict: skills/fate_points/
character_aspects/scene_aspects/active_conflict) consumed by the router and the
narrator prompt — that contract must NOT change (one source of truth, tested in
``test_fate_projection.py``). F3a ADDS a sibling, ``build_fate_state_payload``, that
promotes the SAME snapshot reads into the full client ``FateStatePayload``:

  * per-PC fate_points + refresh,
  * skills -> ladder (rating + adjective from ``fate_resolution.ladder_name``),
  * the named character aspects with kind + free-invoke counts,
  * the two stress tracks as checkable boxes,
  * the four consequence slots (open vs filled),
  * scene situation aspects + boosts, and
  * the active conflict's participants by side (seating order).

All the new-symbol assertions FAIL today (RED). New symbols are imported inside each
test; ``build_fate_projection`` is imported at module scope so the regression guard
runs even before the new symbol lands.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset.fate_projection import build_fate_projection
from sidequest.game.ruleset.fate_resolution import ladder_name
from sidequest.game.session import GameSnapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pc(name: str = "Vance", *, fate_points: int = 3, refresh: int = 3) -> Character:
    sheet = FateSheet(skills={"Fight": 3, "Notice": -1}, fate_points=fate_points, refresh=refresh)
    sheet.aspects.append(Aspect(text="Last Honest Cop in Vega", kind="high_concept"))
    sheet.aspects.append(Aspect(text="Quick on the Draw", kind="character", free_invokes=1))
    return Character(
        core=CreatureCore(name=name, description="d", personality="p", fate_sheet=sheet),
        char_class="Agent",
        race="Human",
        backstory="b",
    )


def _conflict_snapshot(*, fate_points: int = 3, resolved: bool = False) -> GameSnapshot:
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name="Vance", role="lead", side="player"),
            EncounterActor(name="Mr. Big", role="lead", side="opponent"),
        ],
    )
    enc.situation_aspects.append(Aspect(text="Overturned Table", kind="situation", free_invokes=1))
    enc.situation_aspects.append(Aspect(text="Off Balance", kind="boost", free_invokes=1))
    enc.resolved = resolved
    return GameSnapshot(
        genre_slug="pulp_noir",
        characters=[_pc(fate_points=fate_points)],
        encounter=enc,
    )


def _pc_entry(payload, name: str = "Vance"):
    matches = [c for c in payload.characters if c.name == name]
    assert matches, f"no character entry for {name!r} in payload"
    return matches[0]


# ---------------------------------------------------------------------------
# AC-1 — the builder exists and returns a typed FateStatePayload.
# ---------------------------------------------------------------------------


def test_build_fate_state_payload_returns_payload():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload
    from sidequest.protocol.models import FateStatePayload

    payload = build_fate_state_payload(_conflict_snapshot())
    assert isinstance(payload, FateStatePayload)


# ---------------------------------------------------------------------------
# AC-2 — per-PC fate points + refresh.
# ---------------------------------------------------------------------------


def test_character_carries_fate_points_and_refresh():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    pc = _pc_entry(build_fate_state_payload(_conflict_snapshot(fate_points=2)))
    assert pc.fate_points == 2
    assert pc.refresh == 3


def test_reflects_live_fate_points_not_a_frozen_copy():
    """A different fate-point value shows through — guards a stamped constant."""
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    low = _pc_entry(build_fate_state_payload(_conflict_snapshot(fate_points=0)))
    high = _pc_entry(build_fate_state_payload(_conflict_snapshot(fate_points=5)))
    assert low.fate_points == 0
    assert high.fate_points == 5


# ---------------------------------------------------------------------------
# AC-3 — skills carry their ladder rating AND adjective (mechanics legibility).
# ---------------------------------------------------------------------------


def test_skills_carry_rating_and_ladder_label():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    pc = _pc_entry(build_fate_state_payload(_conflict_snapshot()))
    by_name = {s.name: s for s in pc.skills}
    assert by_name["Fight"].rating == 3
    assert by_name["Fight"].ladder == ladder_name(3)  # "Good"
    # A NEGATIVE rung must survive — the Fate ladder has sub-zero rungs (no Field(ge=0)).
    assert by_name["Notice"].rating == -1
    assert by_name["Notice"].ladder == ladder_name(-1)  # "Poor"
    # Vacuity guard: the two labels are genuinely different strings.
    assert by_name["Fight"].ladder != by_name["Notice"].ladder


# ---------------------------------------------------------------------------
# AC-4 — aspects carry kind + free-invoke counts (and ONLY the named aspects;
#         filled consequences surface in the consequences list, not here).
# ---------------------------------------------------------------------------


def test_aspects_carry_kind_and_free_invokes():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    pc = _pc_entry(build_fate_state_payload(_conflict_snapshot()))
    by_text = {a.text: a for a in pc.aspects}
    assert by_text["Last Honest Cop in Vega"].kind == "high_concept"
    assert by_text["Last Honest Cop in Vega"].free_invokes == 0
    assert by_text["Quick on the Draw"].kind == "character"
    assert by_text["Quick on the Draw"].free_invokes == 1


def test_aspects_are_the_named_sheet_aspects_only():
    """The character's ``aspects`` are exactly the named sheet aspects (high concept /
    trouble / character) — a FILLED consequence is invokable but surfaces in the
    consequences list, not duplicated into ``aspects`` (no double-listing)."""
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = _conflict_snapshot()
    sheet = snap.characters[0].core.fate_sheet
    sheet.consequences[0].aspect = Aspect(text="Twisted Ankle", kind="consequence")

    pc = _pc_entry(build_fate_state_payload(snap))
    texts = [a.text for a in pc.aspects]
    assert "Twisted Ankle" not in texts, "filled consequence leaked into aspects (double-listed)"
    assert len(pc.aspects) == len(sheet.aspects)


# ---------------------------------------------------------------------------
# AC-5 — stress tracks projected as checkable boxes.
# ---------------------------------------------------------------------------


def test_stress_boxes_projected_with_values_and_checked_state():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = _conflict_snapshot()
    snap.characters[0].core.fate_sheet.stress["physical"].boxes[0].checked = True

    pc = _pc_entry(build_fate_state_payload(snap))
    physical = pc.stress["physical"]
    assert [b.value for b in physical] == [1, 2]
    assert physical[0].checked is True
    assert physical[1].checked is False
    # both default tracks are present
    assert set(pc.stress) == {"physical", "mental"}


# ---------------------------------------------------------------------------
# AC-6 — consequence slots: open vs filled.
# ---------------------------------------------------------------------------


def test_consequences_open_vs_filled():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = _conflict_snapshot()
    snap.characters[0].core.fate_sheet.consequences[0].aspect = Aspect(
        text="Twisted Ankle", kind="consequence"
    )

    pc = _pc_entry(build_fate_state_payload(snap))
    by_level = {c.level: c for c in pc.consequences}
    # All four SRD slots are present with their absorption values.
    assert {c.level for c in pc.consequences} == {"mild", "moderate", "severe", "extreme"}
    assert by_level["mild"].value == 2
    assert by_level["mild"].filled is True
    assert by_level["mild"].text == "Twisted Ankle"
    # An untouched slot reads open with no text.
    assert by_level["severe"].filled is False
    assert by_level["severe"].text == ""


# ---------------------------------------------------------------------------
# AC-7 — scene situation aspects + boosts (gated on an unresolved encounter).
# ---------------------------------------------------------------------------


def test_scene_aspects_include_situation_and_boost():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    payload = build_fate_state_payload(_conflict_snapshot())
    by_text = {a.text: a for a in payload.scene_aspects}
    assert by_text["Overturned Table"].kind == "situation"
    assert by_text["Off Balance"].kind == "boost"
    # free-invoke counts ride through (the invoke control in F3d reads these).
    assert by_text["Off Balance"].free_invokes == 1


# ---------------------------------------------------------------------------
# AC-8 — conflict: participants by side, in seating order.
# ---------------------------------------------------------------------------


def test_conflict_carries_participants_with_sides_in_seating_order():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    payload = build_fate_state_payload(_conflict_snapshot())
    assert payload.conflict is not None
    assert payload.conflict.active is True
    # Seating order is the engine's tiebreak order (fate_opponent._live_player_actors),
    # so the projection preserves encounter.actors order.
    assert [(p.name, p.side) for p in payload.conflict.participants] == [
        ("Vance", "player"),
        ("Mr. Big", "opponent"),
    ]


def test_no_conflict_when_no_encounter():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = GameSnapshot(genre_slug="pulp_noir", characters=[_pc()])
    payload = build_fate_state_payload(snap)
    assert payload.conflict is None
    assert payload.scene_aspects == []


def test_resolved_encounter_yields_no_conflict_and_no_scene_aspects():
    """A resolved confrontation's situation aspects are stale fiction (mirrors the
    compact projection's ``not enc.resolved`` gate) and the conflict is over."""
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    payload = build_fate_state_payload(_conflict_snapshot(resolved=True))
    assert payload.conflict is None
    assert payload.scene_aspects == []


# ---------------------------------------------------------------------------
# AC-9 — only PCs with a Fate sheet contribute.
# ---------------------------------------------------------------------------


def test_pc_without_fate_sheet_is_omitted():
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = GameSnapshot(
        genre_slug="pulp_noir",
        characters=[
            _pc(),
            Character(
                core=CreatureCore(name="NoSheet", description="d", personality="p"),
                char_class="Agent",
                race="Human",
                backstory="b",
            ),
        ],
    )
    payload = build_fate_state_payload(snap)
    names = [c.name for c in payload.characters]
    assert "Vance" in names
    assert "NoSheet" not in names


# ---------------------------------------------------------------------------
# AC-10 — one source of truth: the COMPACT projection is unchanged (regression).
# ---------------------------------------------------------------------------


def test_compact_build_fate_projection_contract_is_unchanged():
    """The router/narrator vocabulary projection must keep its F2b dict shape — F3a
    ADDS the rich payload, it does not replace or break the compact one (the
    'one source of truth' guard from test_fate_projection.py)."""
    compact = build_fate_projection(_conflict_snapshot())
    assert set(compact) == {
        "skills",
        "fate_points",
        "character_aspects",
        "scene_aspects",
        "active_conflict",
    }
    assert compact["skills"]["Vance"] == {"Fight": 3, "Notice": -1}
    assert compact["fate_points"]["Vance"] == 3
    assert compact["active_conflict"] is True


# ---------------------------------------------------------------------------
# Playtest 150-2 — a PC's chosen stunts project onto FATE_STATE (under Fate the
# player's special abilities ARE their stunts; the Character/Fate panel renders
# these in place of the native class-move surface). Before this, FateCharacterEntry
# had no stunts field, so the persisted sheet.stunts never reached the client.
# ---------------------------------------------------------------------------


def test_stunts_project_onto_fate_state():
    from sidequest.game.fate_sheet import Stunt
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    snap = _conflict_snapshot()
    sheet = snap.characters[0].core.fate_sheet
    sheet.stunts.append(Stunt(name="The Quick Draw", description="Iron clears leather first."))
    sheet.stunts.append(
        Stunt(name="Nerves of Cold Iron", description="+2 to Will under a leveled gun.")
    )

    pc = _pc_entry(build_fate_state_payload(snap))
    assert [s.name for s in pc.stunts] == ["The Quick Draw", "Nerves of Cold Iron"]
    assert pc.stunts[0].description == "Iron clears leather first."


def test_stunts_empty_when_sheet_has_none():
    """A PC who picked no stunts projects an empty list (the wire default), never a
    missing field — the UI renders no Stunts section rather than crashing."""
    from sidequest.game.ruleset.fate_projection import build_fate_state_payload

    pc = _pc_entry(build_fate_state_payload(_conflict_snapshot()))
    assert pc.stunts == []
