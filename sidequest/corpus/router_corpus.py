"""Router A/B corpus — captured Intent Router prompts with Haiku baselines.

Story 92-1 (epic 92, Local Classification Routing). The existing corpus
pipeline (``schema.py``/``miner.py``) emits ``TrainingPair`` = (player action →
narrator output) — the WRONG corpus for a router A/B: it carries no
``state_summary`` and no ``DispatchPackage`` ground truth. This module is the
sibling schema for the router workload:

    ``RouterCapture`` = (action, state_summary) → Haiku-baseline DispatchPackage

``state_summary`` is stored as the **string** the production prompt actually
sent (``intent_router._serialize_state_summary`` output), so a replay is
byte-identical to the captured turn — re-serializing a dict could reorder keys
across runs and break the story's deterministic-re-run AC.

I/O mirrors ``writer.py``: atomic JSONL writes (complete or raise — never
half-write), a loud refusal to overwrite anything under the real saves root,
and a fail-loud reader — a malformed line raises instead of being silently
skipped, because a silently shrunk corpus would inflate the agreement metric
the 92-2 routing flip gates on.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sidequest.corpus.schema import MineProvenance
from sidequest.protocol.dispatch import DispatchPackage

ROUTER_CORPUS_SCHEMA_VERSION: Literal[1] = 1

_SAVE_ROOT = Path.home() / ".sidequest" / "saves"


class RouterCapture(BaseModel):
    """One captured router turn: the (action, state_summary) prompt pair plus
    the Haiku-baseline ``DispatchPackage`` the production router emitted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    genre: str
    world: str
    round_number: int = Field(ge=0)
    action: str = Field(min_length=1)
    state_summary: str
    baseline_package: DispatchPackage
    provenance: MineProvenance


def _refuse_save_overwrite(path: Path) -> None:
    """Reject output paths that would overwrite a real SQLite save (mirror of
    ``writer._refuse_save_overwrite`` — real saves are reference data)."""
    if path.suffix == ".db":
        raise ValueError(f"refusing to write JSONL to a .db path: {path}")
    try:
        resolved = path.resolve()
        save_root = _SAVE_ROOT.resolve()
        resolved.relative_to(save_root)
    except (ValueError, OSError):
        return  # not under saves root, or path doesn't exist yet — safe
    raise ValueError(f"refusing to write to a path under {_SAVE_ROOT}: {path}")


def write_captures(path: Path, captures: Iterable[RouterCapture]) -> None:
    """Write captures as JSONL atomically. Completes or raises — never
    half-writes (same guards as ``writer.write_pairs``)."""
    _refuse_save_overwrite(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for capture in captures:
                fh.write(capture.model_dump_json())
                fh.write("\n")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_captures(path: Path) -> Iterator[RouterCapture]:
    """Read a router-corpus JSONL file, validating every line.

    Fail-loud (rule #8): a malformed or wrong-shape line raises
    ``pydantic.ValidationError`` with the offending line number in context —
    it is never silently skipped. The corpus is the evidence the 92-2 gate
    rests on; a quietly shrunk corpus is a lie about coverage.
    """
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield RouterCapture.model_validate_json(stripped)
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid RouterCapture row: {exc}") from exc


__all__ = [
    "ROUTER_CORPUS_SCHEMA_VERSION",
    "RouterCapture",
    "read_captures",
    "write_captures",
]
