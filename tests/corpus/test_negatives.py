from __future__ import annotations

from sidequest.corpus.negatives import detect_retarget
from sidequest.corpus.schema import MineProvenance, TrainingPair


def _p(round_: int, inp: str) -> TrainingPair:
    return TrainingPair(
        schema_version=1,
        genre="g",
        world="w",
        round_number=round_,
        input_text=inp,
        output_text="…",
        provenance=MineProvenance(source_save="x.db", event_seq=None),
    )


def test_detect_retarget_flags_no_i_meant() -> None:
    pairs = [_p(1, "I swing at the bandit"), _p(2, "No, I meant the fortune teller")]
    suspects = list(detect_retarget(pairs))
    assert [s.round_number for s in suspects] == [1]


def test_detect_retarget_does_not_flag_unrelated_correction() -> None:
    pairs = [_p(1, "I enter the tavern"), _p(2, "I order a drink")]
    assert list(detect_retarget(pairs)) == []


def test_detect_retarget_handles_single_pair() -> None:
    assert list(detect_retarget([_p(1, "hi")])) == []


def test_detect_retarget_handles_empty_list() -> None:
    assert list(detect_retarget([])) == []


def test_detect_retarget_is_case_insensitive() -> None:
    pairs = [_p(1, "attack the bandit"), _p(2, "WAIT — I meant the rogue")]
    suspects = list(detect_retarget(pairs))
    assert [s.round_number for s in suspects] == [1]


def test_detect_retarget_matches_actually_token() -> None:
    pairs = [_p(1, "I buy the sword"), _p(2, "Actually, make it the dagger")]
    suspects = list(detect_retarget(pairs))
    assert [s.round_number for s in suspects] == [1]
