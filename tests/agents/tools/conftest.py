"""Per-tool test fixtures."""

from __future__ import annotations

import pytest

from sidequest.agents import narrator_perception_filter as _npf


@pytest.fixture(autouse=True)
def _isolate_perception_rules():
    """Snapshot and restore the perception _RULES table across tests.

    Tool modules call ``register_rule`` at import time; without isolation,
    test order would couple rule presence across files.
    """
    snapshot = dict(_npf._RULES)
    try:
        yield
    finally:
        _npf._RULES.clear()
        _npf._RULES.update(snapshot)
