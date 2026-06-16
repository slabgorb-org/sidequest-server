from __future__ import annotations

import pytest

from sidequest.game.lethality import MAJOR_INJURY_TABLE, major_injury_entry


def test_table_has_twelve_entries():
    assert len(MAJOR_INJURY_TABLE) == 12
    assert sorted(MAJOR_INJURY_TABLE.keys()) == list(range(1, 13))


def test_lookup_returns_text():
    assert isinstance(major_injury_entry(1), str)
    assert major_injury_entry(12).strip() != ""


def test_roll_12_is_instant_death():
    assert "death" in major_injury_entry(12).lower()


@pytest.mark.parametrize("bad", [0, 13, -1, 100])
def test_out_of_range_fails_loud(bad):
    with pytest.raises(ValueError, match="1..12"):
        major_injury_entry(bad)
