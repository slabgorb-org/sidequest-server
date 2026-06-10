from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot
from sidequest.mutation.models import (
    MpEconomy,
    MutationCatalog,
    NegativeMutationDef,
    PositiveMutationDef,
    StigmaTables,
)
from sidequest.server.mutation_init import init_mutation_state_for_session


@pytest.fixture
def captured_mutation_init_events(monkeypatch):
    """Capture _watcher_publish calls inside mutation_init.

    Mirror of tests/server/test_magic_init.py::captured_magic_init_events —
    same monkeypatch-the-module-symbol mechanism.
    """
    captured: list[dict] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.server import mutation_init as mutation_init_module

    monkeypatch.setattr(mutation_init_module, "_watcher_publish", _capture, raising=False)
    return captured


def _catalog() -> MutationCatalog:
    return MutationCatalog(
        mp_economy=MpEconomy(mutant_classes=["Mutant"]),
        stigma=StigmaTables(body_part=["a"] * 6, nature=["b"] * 6, flavor=["c"] * 12),
        negatives=[
            NegativeMutationDef(id="negative/frail", name="F", roll_range=(1, 100), effect="y")
        ],
        positives=[
            PositiveMutationDef(
                id="structure/crushing_jaws", name="C", category="structure", effect="bite"
            )
        ],
    )


def _snapshot() -> GameSnapshot:
    # Mirror the minimal-GameSnapshot construction used by existing tests:
    # grep "GameSnapshot(" tests/ for the lightest fixture and reuse its shape.
    return GameSnapshot.model_construct(mutation_state=None)


def test_no_catalog_skips_silently(captured_mutation_init_events) -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap,
        catalog=None,
        character_name="Rux",
        character_class="Mutant",
        session_id="s1",
    )
    assert snap.mutation_state is None
    skipped = [
        e for e in captured_mutation_init_events if e["event_type"] == "mutation.init_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["fields"]["reason"] == "no_catalog"


def test_mutant_seeds_snapshot_state(captured_mutation_init_events) -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap,
        catalog=_catalog(),
        character_name="Rux",
        character_class="Mutant",
        session_id="s1",
    )
    assert snap.mutation_state is not None
    assert "Rux" in snap.mutation_state.characters
    inits = [e for e in captured_mutation_init_events if e["event_type"] == "mutation.init"]
    assert len(inits) == 1
    assert inits[0]["fields"]["actor"] == "Rux"


def test_non_mutant_leaves_no_character_entry(captured_mutation_init_events) -> None:
    snap = _snapshot()
    init_mutation_state_for_session(
        snap,
        catalog=_catalog(),
        character_name="Rux",
        character_class="Scavenger",
        session_id="s1",
    )
    # container may exist (created on first init), but no entry for a non-mutant
    if snap.mutation_state is not None:
        assert "Rux" not in snap.mutation_state.characters
    skipped = [
        e for e in captured_mutation_init_events if e["event_type"] == "mutation.init_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["fields"]["reason"] == "non_mutant_class"
