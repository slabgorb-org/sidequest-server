"""Disposition → attitude band mapping (ADR-020 three-tier).

Story 50-10 restored the qualitative attitude layer that was dropped
during the 2026-04 Python port. The Rust-era pattern lived as an
``Attitude`` enum + ``Disposition(i32)`` newtype with ``.attitude()``
derivation; this module mirrors that shape in Python.

The numeric layer (``Disposition.value``) and the qualitative layer
(``Disposition.attitude()`` → ``Attitude``) are deliberately kept
separate. The world-state agent reasons in numbers ("+5 disposition");
the narrator and NPC-presentation agents reason in attitudes
("the bartender is friendly"). Keeping the two layers explicit lets
each agent see only what it needs.

The string values ``"friendly"`` / ``"neutral"`` / ``"hostile"`` are the
stable wire contract — OTEL spans (``SPAN_DISPOSITION_SHIFT``), the GM
panel, the scrapbook, and the narrator's NPC serialization all match on
those exact literals.

Boundaries (strict, defaulting to ADR-020 ±10):

- ``value > friendly_at`` → friendly
- ``value < hostile_at`` → hostile
- otherwise → neutral

With the default thresholds (``friendly_at=10`` / ``hostile_at=-10``):
10 is neutral, 11 is friendly; -10 is neutral, -11 is hostile. The
``Disposition`` constructor clamps any integer to ``-100..+100``.

Story 50-13 makes the numeric cut points genre-pack-configurable. The
qualitative bands stay the locked three-tier ``Attitude`` contract — only
the boundaries move. A pack declares ``disposition_thresholds`` in
rules.yaml; ``load_genre_pack`` applies them process-wide via
``configure_attitude_thresholds`` so the no-argument
``Disposition.attitude()`` callsites (``session.apply_world_patch``,
``opening.py``, the narrator roster) need no rework.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, GetCoreSchemaHandler, model_validator
from pydantic_core import CoreSchema, core_schema

__all__ = [
    "DISPOSITION_LOG_CAP",
    "CHAPTER_BEAT_REASON",
    "PATCH_BEAT_REASON",
    "Attitude",
    "AttitudeThresholds",
    "DEFAULT_ATTITUDE_THRESHOLDS",
    "Disposition",
    "DispositionBeat",
    "configure_attitude_thresholds",
    "reset_attitude_thresholds",
]


# Disposition beat-log (ADR-136). Persists the delta + reason the engine
# already computes at each disposition-shift site — rescuing data that today
# lives only in transient SPAN_DISPOSITION_SHIFT spans — so the relationship
# panel can show the *why* behind each shift (ADR-014 diamonds/coal: a
# relationship story, not a reputation bar).
DISPOSITION_LOG_CAP = 10

# Neutral label for the apply_patch npc_attitudes path (ADR-136). A narrative
# patch carries a delta but no narrator reason — show the shift without
# inventing a specific cause (No Silent Fallbacks: an honest generic label).
PATCH_BEAT_REASON = "shifted by unfolding events"

# Display label for the chapter-upsert path (ADR-136): an existing NPC's
# standing moved as the authored chronicle advanced. Player-facing prose, not
# a machine key — it renders in the relationship panel.
CHAPTER_BEAT_REASON = "shifted as your history together deepened"


class DispositionBeat(BaseModel):
    """One persisted disposition shift: the delta the engine applied and why.

    ``reason`` is narrator-supplied (``update_npc_disposition``), the engagement
    tick label, or a neutral label for opaque patch/world paths. ``location`` is
    the party location at the time, or ``None`` when not in scope.
    """

    model_config = {"extra": "forbid"}

    turn: int
    delta: int
    reason: str
    location: str | None = None


class Attitude(StrEnum):
    """Three-tier attitude band per ADR-020.

    ``StrEnum`` so the enum members compare equal to their string values
    and serialize naturally into OTEL span attributes and JSON. Downstream
    consumers (the GM panel, the narrator) match on the literal strings;
    keeping ``Attitude`` a ``str`` subclass means a ``before_attitude``
    field carrying ``Attitude.FRIENDLY`` reads back from the watcher's
    JSON pipe as ``"friendly"`` without any custom serializer.
    """

    FRIENDLY = "friendly"
    NEUTRAL = "neutral"
    HOSTILE = "hostile"


class AttitudeThresholds(BaseModel):
    """Genre-configurable numeric cut points for attitude derivation.

    ``friendly_at`` / ``hostile_at`` are the strict boundaries:
    ``value > friendly_at`` → friendly, ``value < hostile_at`` → hostile,
    otherwise neutral. Defaults reproduce the pre-50-13 ADR-020 ±10
    contract exactly.

    ``extra="forbid"`` matches the rest of ``rules.py``: a typo'd key
    (``frendly_at``) fails the pack load loudly rather than silently
    falling back to default ±10 while the author believes they set a
    custom band (SOUL: No Silent Fallbacks).
    """

    model_config = {"extra": "forbid"}

    friendly_at: int = 10
    hostile_at: int = -10

    @model_validator(mode="after")
    def _validate_strict_ordering(self) -> AttitudeThresholds:
        if not self.hostile_at < self.friendly_at:
            raise ValueError(
                f"disposition_thresholds: hostile_at ({self.hostile_at}) must be "
                f"strictly less than friendly_at ({self.friendly_at}); an "
                f"inverted or zero-width band is a pack authoring error, not a "
                f"silently-clamped default"
            )
        return self


DEFAULT_ATTITUDE_THRESHOLDS = AttitudeThresholds()
"""The pre-50-13 ADR-020 ±10 contract. The loader passes this when a pack
declares no ``disposition_thresholds`` block, so an opted-out pack is
byte-identical to legacy behavior and a prior pack's custom band is
overwritten rather than inherited (cross-pack / multiplayer no-leak)."""

_active_thresholds: AttitudeThresholds = DEFAULT_ATTITUDE_THRESHOLDS


def configure_attitude_thresholds(thresholds: AttitudeThresholds) -> None:
    """Set the process-wide attitude bands. Called once per pack load by
    ``load_genre_pack``. Overwrites (never merges) the prior value so two
    sessions on different packs in one server process cannot cross-
    contaminate NPC attitudes."""
    global _active_thresholds
    _active_thresholds = thresholds


def reset_attitude_thresholds() -> None:
    """Restore the default ±10 bands. Used by the loader's 'pack opted
    out' path and by test isolation fixtures."""
    global _active_thresholds
    _active_thresholds = DEFAULT_ATTITUDE_THRESHOLDS


class Disposition:
    """NPC disposition score with attitude derivation.

    Wraps a clamped integer in ``-100..+100`` and exposes a single
    derivation method, ``attitude()``, that returns the qualitative band.
    Treated as a value type — distinct ``Disposition`` instances per NPC,
    no shared mutable state across the snapshot.

    Construction accepts a raw int, defaulting to 0. Pydantic models
    that reference ``Disposition`` as a field type get coercion from
    raw int via ``__get_pydantic_core_schema__`` so existing fixtures
    (``Npc(disposition=15)``) continue to work without rewriting every
    integration test.
    """

    __slots__ = ("value",)

    def __init__(self, value: int = 0) -> None:
        self.value = max(-100, min(100, int(value)))

    def attitude(self) -> Attitude:
        if self.value > _active_thresholds.friendly_at:
            return Attitude.FRIENDLY
        if self.value < _active_thresholds.hostile_at:
            return Attitude.HOSTILE
        return Attitude.NEUTRAL

    def __int__(self) -> int:
        return self.value

    def __eq__(self, other: object) -> bool:
        # Value type (docstring): two Dispositions are equal when their
        # clamped scores match. Required so models carrying a Disposition
        # field (``Npc``, ``NpcPoolMember``) compare by value on JSON
        # round-trip rather than by instance identity (story 72-2).
        if isinstance(other, Disposition):
            return self.value == other.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"Disposition({self.value})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Pydantic v2 schema hook.

        Accepts a ``Disposition`` instance (pass-through) or a raw int
        (coerced via ``Disposition(int)`` with clamping). Serializes back
        to a bare ``int`` so save-file JSON stays human-readable and the
        GM panel can read the numeric value without unpacking a wrapper.
        """

        def _from_int(v: int) -> Disposition:
            return cls(v)

        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                core_schema.no_info_after_validator_function(
                    _from_int,
                    core_schema.int_schema(),
                ),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda d: d.value,
                return_schema=core_schema.int_schema(),
            ),
        )
