"""#177 defect (a): the narrative_log player-turn entry must record the player's
verbatim words, never the ``[INITIATIVE ORDER]`` scaffold / ``Name: action`` join
that ``dispatch_fired_barrier`` builds for the narrator prompt.

sq-playtest 2026-06-07 barsoom: James's looting/resting actions were overwritten
in the permanent record by the engine's initiative resolution-order preamble.
"""

from __future__ import annotations

from sidequest.handlers.player_action import initiative_preamble
from sidequest.server.session_helpers import player_log_content


def test_solo_logs_raw_text_not_name_prefixed():
    # Solo barrier: dispatch_fired_barrier sets merged to one (name, text)
    # tuple. The log keeps the raw words — no "Ruximus: " prefix, no preamble.
    merged = [("Ruximus", "i loot the body and rest until dawn")]
    assert player_log_content("ignored-scaffold", merged) == "i loot the body and rest until dawn"


def test_mp_keeps_per_speaker_attribution():
    merged = [("Groucho", "I draw my blaster"), ("Chico", "I dive for cover")]
    assert player_log_content("ignored-scaffold", merged) == (
        "Groucho: I draw my blaster\nChico: I dive for cover"
    )


def test_falls_back_to_action_when_no_merged():
    # room-is-None solo path and dice-replay re-entry pass a clean action with
    # no preamble and no merged tuples — record it verbatim.
    assert player_log_content("I open the door", None) == "I open the door"
    assert player_log_content("I open the door", []) == "I open the door"


def test_strips_initiative_preamble_corruption():
    """The exact defect: the scaffold the barrier hands the narrator must NOT
    be what lands in narrative_log."""
    from sidequest.game.encounter import EncounterMetric, StructuredEncounter
    from sidequest.protocol.models import InitiativeEntry

    enc = StructuredEncounter(
        encounter_type="firefight",
        player_metric=EncounterMetric(name="hp", current=1, starting=1, threshold=1),
        opponent_metric=EncounterMetric(name="hp", current=1, starting=1, threshold=1),
        initiative=[InitiativeEntry(token_id="Ruximus", value=7)],
    )
    preamble = initiative_preamble(enc)
    assert preamble is not None
    raw = "i strike the soldier"
    scaffold = f"{preamble}\nRuximus: {raw}"
    merged = [("Ruximus", raw)]
    logged = player_log_content(scaffold, merged)
    assert "INITIATIVE ORDER" not in logged
    assert logged == raw
