from sidequest.game.disposition import Disposition, DispositionBeat
from sidequest.game.projection.relationships import (
    band_for,
    build_relationship_entries,
    personality_read,
    trend_for,
)
from tests.game.test_disposition_beat import _npc


def test_band_thresholds():
    assert band_for(80) == "Devoted"
    assert band_for(50) == "Devoted"
    assert band_for(49) == "Warm"
    assert band_for(10) == "Warm"
    assert band_for(9) == "Neutral"
    assert band_for(0) == "Neutral"
    assert band_for(-9) == "Neutral"
    assert band_for(-10) == "Cool"
    assert band_for(-49) == "Cool"
    assert band_for(-50) == "Hostile"
    assert band_for(-100) == "Hostile"


def test_trend_up_flat_down():
    up = [
        DispositionBeat(turn=1, delta=2, reason="a"),
        DispositionBeat(turn=2, delta=1, reason="b"),
    ]
    down = [DispositionBeat(turn=1, delta=-3, reason="a")]
    assert trend_for(up) == "up"
    assert trend_for(down) == "down"
    assert trend_for([]) == "flat"


def test_trend_windows_last_k():
    beats = [
        DispositionBeat(turn=1, delta=-10, reason="old"),  # outside window
        DispositionBeat(turn=2, delta=1, reason="x"),
        DispositionBeat(turn=3, delta=1, reason="y"),
        DispositionBeat(turn=4, delta=1, reason="z"),
    ]
    assert trend_for(beats, k=3) == "up"  # last 3 sum +3, ignores the -10


def _snapshot_with(npcs):
    class _Snap:
        pass

    s = _Snap()
    s.npcs = npcs
    return s


def test_build_entries_phase_a_fields():
    npc = _npc("Tabitha")
    npc.disposition = Disposition(24)
    npc.last_seen_turn = 6
    npc.last_seen_location = "parlor"
    npc.record_disposition_beat(turn=6, delta=3, reason="candor", location="parlor")

    entries = build_relationship_entries(_snapshot_with([npc]))
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "Tabitha"
    assert e.band == "Warm"
    assert e.disposition == 24
    assert e.trend == "up"
    assert e.last_seen_turn == 6
    assert e.last_seen_location == "parlor"
    assert len(e.beats) == 1 and e.beats[0].reason == "candor"
    # Phase A: OCEAN + claims empty
    assert e.ocean is None
    assert e.personality_read is None
    assert e.claims == []


def test_build_entries_never_seen_npc():
    # A never-seen NPC keeps the Npc defaults (last_seen_turn=0,
    # last_seen_location=None). The builder must construct a valid payload
    # without raising a pydantic ValidationError.
    npc = _npc("Ghost")
    entries = build_relationship_entries(_snapshot_with([npc]))
    assert len(entries) == 1
    assert entries[0].last_seen_turn == 0
    assert entries[0].last_seen_location is None


def test_build_entries_empty_roster():
    assert build_relationship_entries(_snapshot_with([])) == []


def test_personality_read_none_for_no_profile():
    assert personality_read(None) is None


def test_personality_read_picks_salient_traits():
    # extraversion very high, agreeableness very low → outgoing + abrasive
    read = personality_read(
        {
            "openness": 5.0,
            "conscientiousness": 5.0,
            "extraversion": 9.0,
            "agreeableness": 1.0,
            "neuroticism": 5.0,
        }
    )
    assert read is not None
    assert "outgoing" in read.lower()
    assert "abrasive" in read.lower() or "blunt" in read.lower()


def test_personality_read_flat_profile_is_balanced():
    read = personality_read(
        {
            "openness": 5.0,
            "conscientiousness": 5.0,
            "extraversion": 5.0,
            "agreeableness": 5.0,
            "neuroticism": 5.0,
        }
    )
    assert read is not None and "even-keeled" in read.lower()


def test_build_entries_populates_ocean_and_read():
    npc = _npc("Tabitha")
    npc.disposition = Disposition(24)
    npc.ocean = {
        "openness": 5.0,
        "conscientiousness": 7.0,
        "extraversion": 9.0,
        "agreeableness": 1.0,
        "neuroticism": 4.0,
    }
    e = build_relationship_entries(_snapshot_with([npc]))[0]
    assert e.ocean == npc.ocean
    assert e.personality_read is not None and "outgoing" in e.personality_read.lower()


def test_build_entries_no_ocean_keeps_none():
    npc = _npc("Stranger")
    assert npc.ocean is None
    e = build_relationship_entries(_snapshot_with([npc]))[0]
    assert e.ocean is None
    assert e.personality_read is None


def test_build_entries_populates_claims():
    from sidequest.game.belief_state import BeliefClaim, BeliefSourceToldBy

    npc = _npc("Tabitha")
    npc.belief_state.beliefs.append(
        BeliefClaim(
            subject="alibi",
            content="I was in the garden",
            source=BeliefSourceToldBy(by="Tabitha"),
            believed=True,
        )
    )
    e = build_relationship_entries(_snapshot_with([npc]))[0]
    assert len(e.claims) == 1
    assert e.claims[0].text == "I was in the garden"
    assert e.claims[0].credibility_hint  # non-empty


def test_build_entries_claims_firewall_excludes_facts_and_suspicions():
    # Security boundary: the production builder runs belief_state through
    # claims_to_party, which drops Fact/Suspicion (the mystery's solution).
    from sidequest.game.belief_state import (
        BeliefClaim,
        BeliefFact,
        BeliefSourceToldBy,
        BeliefSourceWitnessed,
        BeliefSuspicion,
    )

    npc = _npc("Tabitha")
    npc.belief_state.beliefs.append(
        BeliefFact(
            subject="killer",
            content="The butler did it",
            source=BeliefSourceWitnessed(),
        )
    )
    npc.belief_state.beliefs.append(
        BeliefSuspicion(
            subject="motive",
            content="It was about the will",
            source=BeliefSourceWitnessed(),
            confidence=0.5,
        )
    )
    npc.belief_state.beliefs.append(
        BeliefClaim(
            subject="alibi",
            content="I was in the garden",
            source=BeliefSourceToldBy(by="Tabitha"),
            believed=True,
        )
    )
    e = build_relationship_entries(_snapshot_with([npc]))[0]
    texts = [c.text for c in e.claims]
    assert texts == ["I was in the garden"]
    assert "The butler did it" not in texts
    assert "It was about the will" not in texts
