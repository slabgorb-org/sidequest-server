from __future__ import annotations

from sidequest.mutation.state import (
    CharacterMutationState,
    MutationState,
    StigmaRecord,
    UsageCounter,
)


def test_round_trip_serialization() -> None:
    state = MutationState()
    state.characters["Rux"] = CharacterMutationState(
        mp_remaining=3,
        negative_ids=["negative/withered_arm"],
        positive_ids=["structure/crushing_jaws"],
        stigma=[
            StigmaRecord(body_part="eyes", nature="luminous", flavor="amber", concealable=True)
        ],
        usage={"structure/crushing_jaws": UsageCounter(period="per_scene", used=1)},
        acquisition_log=["negative/withered_arm", "structure/crushing_jaws"],
    )
    state.roll_sequence = 4
    reloaded = MutationState.model_validate(state.model_dump())
    assert reloaded == state


def test_reset_scene_clears_only_scene_counters() -> None:
    cs = CharacterMutationState(
        mp_remaining=0,
        usage={
            "sense/echo_location": UsageCounter(period="per_scene", used=1),
            "exotic/second_wind": UsageCounter(period="per_day", used=1),
        },
    )
    state = MutationState(characters={"Rux": cs})
    state.reset_scene()
    assert state.characters["Rux"].usage["sense/echo_location"].used == 0
    assert state.characters["Rux"].usage["exotic/second_wind"].used == 1


def test_reset_day_clears_both() -> None:
    cs = CharacterMutationState(
        mp_remaining=0,
        usage={
            "sense/echo_location": UsageCounter(period="per_scene", used=2),
            "exotic/second_wind": UsageCounter(period="per_day", used=1),
        },
    )
    state = MutationState(characters={"Rux": cs})
    state.reset_day()
    assert all(c.used == 0 for c in state.characters["Rux"].usage.values())
