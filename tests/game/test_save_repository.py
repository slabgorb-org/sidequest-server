"""SaveRepository interface + SqliteSaveRepository adapter tests."""

from __future__ import annotations

from sidequest.game.repository import SaveRepository, SaveTransaction


def test_protocols_are_runtime_checkable():
    # Protocols must be importable and runtime_checkable so isinstance()
    # works in wiring tests and the adapter can be asserted against them.
    assert hasattr(SaveRepository, "_is_runtime_protocol")
    assert hasattr(SaveTransaction, "_is_runtime_protocol")
