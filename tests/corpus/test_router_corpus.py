"""Failing tests for story 92-1 (corpus layer): the router A/B corpus.

RED phase (TEA / Radar O'Reilly). These tests fail until Dev creates
``sidequest/corpus/router_corpus.py`` →
  - ``ROUTER_CORPUS_SCHEMA_VERSION``
  - ``RouterCapture``   — one captured (action, state_summary) → Haiku-baseline
                          ``DispatchPackage`` row (sibling of ``TrainingPair``)
  - ``write_captures``  — atomic JSONL writer (mirror ``corpus/writer.py``,
                          incl. the refuse-to-overwrite-saves guard)
  - ``read_captures``   — fail-loud JSONL reader (rule #8: a malformed line
                          raises, it is never silently skipped)

Spec: sprint/context/context-story-92-1.md AC1 — "A corpus of N real
DispatchPackage prompts is captured ... Each row carries (action,
state_summary) plus the Haiku-baseline DispatchPackage."

The existing ``TrainingPair`` (action → narrator output) is the WRONG corpus
for a router A/B: it has no ``state_summary`` and no ``DispatchPackage``
ground truth. These tests pin the new row shape.

Rule-enforcement (.pennyfarthing/gates/lang-review/python.md):
  #5  path handling      — test_write_captures_refuses_save_paths,
                           explicit encoding asserted via roundtrip
  #8  unsafe deserialization — test_read_captures_rejects_malformed_line,
                           test_read_captures_rejects_wrong_shape_line
  #11 input validation   — test_router_capture_rejects_empty_action,
                           test_router_capture_rejects_unknown_fields
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.corpus.router_corpus import (  # noqa: E402 — RED until Dev lands it
    ROUTER_CORPUS_SCHEMA_VERSION,
    RouterCapture,
    read_captures,
    write_captures,
)
from sidequest.corpus.schema import MineProvenance
from sidequest.protocol.dispatch import DispatchPackage

# --------------------------------------------------------------------------- #
# Fixture helpers — REAL DispatchPackage instances, no mocks.
# --------------------------------------------------------------------------- #


def _package(
    *subsystems: str,
    turn_id: str = "turn-1",
) -> DispatchPackage:
    """Build a minimal, schema-valid DispatchPackage with the given
    subsystem dispatches."""
    return DispatchPackage.model_validate(
        {
            "turn_id": turn_id,
            "confidence_global": 0.9,
            "per_player": [
                {
                    "player_id": "p1",
                    "raw_action": "the captured action",
                    "dispatch": [
                        {
                            "subsystem": name,
                            "params": {},
                            "idempotency_key": f"{turn_id}-k{i}",
                            "visibility": {"visible_to": "all"},
                            "confidence": 0.9,
                        }
                        for i, name in enumerate(subsystems)
                    ],
                }
            ],
            "cross_player": [],
        }
    )


def _capture(idx: int = 0, *subsystems: str) -> RouterCapture:
    return RouterCapture(
        schema_version=ROUTER_CORPUS_SCHEMA_VERSION,
        genre="caverns_and_claudes",
        world="beneath_sunden",
        round_number=idx,
        action=f"I draw my sword and attack the goblin chief ({idx}).",
        state_summary='{"present_npcs": ["goblin chief"], "region": "ropefoot"}',
        baseline_package=_package(*(subsystems or ("confrontation",)), turn_id=f"t{idx}"),
        provenance=MineProvenance(source_save="test.db", event_seq=idx),
    )


# --------------------------------------------------------------------------- #
# Schema shape — the row carries the full (action, state_summary) → baseline
# DispatchPackage triple. This is the contract 92-1's harness and 92-2's gate
# both consume.
# --------------------------------------------------------------------------- #


def test_router_capture_carries_the_full_triple() -> None:
    cap = _capture(3, "confrontation", "npc_agency")
    assert cap.action.startswith("I draw my sword")
    assert "present_npcs" in cap.state_summary
    assert isinstance(cap.baseline_package, DispatchPackage)
    subsystems = {d.subsystem for pd in cap.baseline_package.per_player for d in pd.dispatch}
    assert subsystems == {"confrontation", "npc_agency"}


def test_router_capture_rejects_empty_action() -> None:
    """Rule #11: an empty action is not a capturable router prompt."""
    with pytest.raises(ValidationError):
        RouterCapture(
            schema_version=ROUTER_CORPUS_SCHEMA_VERSION,
            genre="g",
            world="w",
            round_number=0,
            action="",
            state_summary="{}",
            baseline_package=_package("movement"),
            provenance=MineProvenance(source_save="test.db", event_seq=0),
        )


def test_router_capture_rejects_unknown_fields() -> None:
    """extra='forbid' — a stray key in a corpus row is a schema error, not
    silently dropped data (mirror TrainingPair / DispatchPackage doctrine)."""
    with pytest.raises(ValidationError):
        RouterCapture(
            schema_version=ROUTER_CORPUS_SCHEMA_VERSION,
            genre="g",
            world="w",
            round_number=0,
            action="probe",
            state_summary="{}",
            baseline_package=_package("movement"),
            provenance=MineProvenance(source_save="test.db", event_seq=0),
            hallucinated_field="nope",  # type: ignore[call-arg]
        )


def test_router_capture_state_summary_is_a_string() -> None:
    """The corpus stores ``state_summary`` exactly as it lands in the prompt
    (the router's ``_serialize_state_summary`` output) — a string, so replay
    is byte-identical to what production sent. A dict here would re-serialize
    differently across runs and break AC2 determinism."""
    cap = _capture(0)
    assert isinstance(cap.state_summary, str)


def test_router_capture_json_roundtrip_preserves_baseline() -> None:
    cap = _capture(7, "movement")
    wire = cap.model_dump_json()
    back = RouterCapture.model_validate_json(wire)
    assert back.action == cap.action
    assert back.state_summary == cap.state_summary
    assert back.baseline_package.turn_id == cap.baseline_package.turn_id
    back_subsystems = {d.subsystem for pd in back.baseline_package.per_player for d in pd.dispatch}
    assert back_subsystems == {"movement"}


# --------------------------------------------------------------------------- #
# JSONL I/O — atomic write, fail-loud read.
# --------------------------------------------------------------------------- #


def test_write_and_read_captures_roundtrip(tmp_path: Path) -> None:
    rows = [_capture(i) for i in range(3)]
    out = tmp_path / "router_corpus.jsonl"

    write_captures(out, rows)
    back = list(read_captures(out))

    assert len(back) == 3
    assert [c.round_number for c in back] == [0, 1, 2]
    assert all(isinstance(c, RouterCapture) for c in back)
    # JSONL: exactly one row per line.
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_read_captures_rejects_malformed_line(tmp_path: Path) -> None:
    """Rule #8: a corrupt JSONL line is an untrusted boundary input. The
    reader must raise loudly — silently skipping a line would quietly shrink
    the corpus and inflate the agreement metric."""
    bad = tmp_path / "corpus.jsonl"
    bad.write_text("{ broken json\n", encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        list(read_captures(bad))
    assert not isinstance(excinfo.value, StopIteration)


def test_read_captures_rejects_wrong_shape_line(tmp_path: Path) -> None:
    """A syntactically-valid line that is not a RouterCapture (e.g. an old
    TrainingPair row fed to the wrong tool) must fail validation loudly."""
    bad = tmp_path / "corpus.jsonl"
    bad.write_text('{"this": "is not a RouterCapture"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid RouterCapture"):
        list(read_captures(bad))


def test_write_captures_refuses_save_paths(tmp_path: Path) -> None:
    """Mirror corpus/writer.py's guard: a .db output path is a real save
    waiting to be destroyed by the atomic rename. Refuse loudly."""
    with pytest.raises(ValueError):
        write_captures(tmp_path / "oops.db", [_capture(0)])
