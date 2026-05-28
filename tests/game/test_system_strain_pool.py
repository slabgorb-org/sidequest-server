from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.game.system_strain import StrainResult, SystemStrainPool


def test_pool_defaults_current_and_permanent_to_zero():
    pool = SystemStrainPool(max=12)
    assert pool.current == 0
    assert pool.permanent == 0
    assert pool.max == 12


def test_pool_requires_max():
    with pytest.raises(ValidationError):
        SystemStrainPool()  # type: ignore[call-arg]


def test_pool_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SystemStrainPool(max=10, bogus=1)  # type: ignore[call-arg]


def test_strain_result_carries_outcome():
    r = StrainResult(
        applied=False, current=12, max=12, permanent=2, delta=0, reason="would exceed max"
    )
    assert r.applied is False
    assert r.delta == 0
    assert r.reason == "would exceed max"
