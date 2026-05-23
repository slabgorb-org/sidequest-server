"""RigComposurePool — vessel-attached composure pool (Epic 53 / Road Warrior).

Story 53-1 foundation. Extends the EdgePool framework (ADR-014, ADR-078)
with rig vessel binding: a pool tracks the structural composure of a
:class:`~sidequest.game.chassis.ChassisInstance` bound to a character.

Story scope (deliberately tight):
  - Holds ``current``/``max``/``base_max`` clamped to ``[0, max]``.
  - Detects downward zero-crossings (``old_current > 0 and new_current == 0``)
    on :meth:`apply_delta`. Upward crossings, no-ops, and re-zeros do NOT
    fire the crossing signal.
  - Emits OTEL spans at construction, every delta, and every zero-crossing
    so the GM panel can audit rig damage (CLAUDE.md OTEL principle).
  - **Detects, does not act.** This module never applies injury tags,
    Edge loss, or dismount logic. Story 53-3's crash handler subscribes
    to ``rig_pool.zero_crossing`` to fire those consequences.

Pydantic strict (``extra='forbid'``) per the project save-surface rule
(CLAUDE.md "No Silent Fallbacks"): malformed pool dicts fail loud rather
than silently default. ``character_id`` and ``chassis_id`` are required
non-blank strings so a born-unbound pool cannot reach the save file.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationInfo, field_validator, model_validator

from sidequest.telemetry.spans import (
    SPAN_RIG_POOL_CREATED,
    SPAN_RIG_POOL_DELTA,
    SPAN_RIG_POOL_ZERO_CROSSING,
    Span,
)


class RigComposureDeltaResult(BaseModel):
    """Result of a single :meth:`RigComposurePool.apply_delta` call.

    Carries the realized old/new values (after clamping) and a
    ``zero_crossed`` flag that fires iff this delta brought the pool
    from positive to zero. Not a save-file surface — no
    ``extra='forbid'`` required (matches the ``ResourcePatchResult``
    precedent in :mod:`sidequest.game.resource_pool`).
    """

    old_current: int
    new_current: int
    zero_crossed: bool


class RigComposurePool(BaseModel):
    """Vessel-attached composure pool.

    ``current`` ∈ ``[0, max]``. ``max > 0`` is required at construction
    so a born-dead rig cannot enter game state silently. The pool is
    bound to a single character + chassis pair for its lifetime; the
    binding is preserved across zero-crossings so the crash handler
    (story 53-3) can find the right target.
    """

    model_config = {"extra": "forbid"}

    current: int
    max: int
    base_max: int
    character_id: str
    chassis_id: str

    @field_validator("character_id", "chassis_id")
    @classmethod
    def _binding_id_non_blank(cls, v: str, info: ValidationInfo) -> str:
        if not v.strip():
            raise ValueError(f"{info.field_name} cannot be blank")
        return v

    @model_validator(mode="after")
    def _check_bounds(self) -> RigComposurePool:
        if self.max <= 0:
            raise ValueError(f"max must be > 0, got {self.max}")
        if self.current < 0:
            raise ValueError(f"current must be >= 0, got {self.current}")
        if self.current > self.max:
            raise ValueError(f"current ({self.current}) cannot exceed max ({self.max})")
        return self

    def model_post_init(self, __context: Any) -> None:
        """Emit ``rig_pool.created`` after pydantic finishes validation.

        Round-trip loads (``model_validate`` / ``model_validate_json``)
        also fire this span so the GM panel sees every pool instance
        that enters the live snapshot. Fires after ``_check_bounds`` so
        a malformed pool never emits a phantom span.
        """
        with Span.open(
            SPAN_RIG_POOL_CREATED,
            attrs={
                "character_id": self.character_id,
                "chassis_id": self.chassis_id,
                "current": self.current,
                "max": self.max,
            },
        ):
            pass

    def apply_delta(self, delta: int) -> RigComposureDeltaResult:
        """Apply a composure delta and return the realized result.

        Positive ``delta`` heals (capped at ``max``); negative ``delta``
        damages (floored at 0). ``zero_crossed`` is True iff the call
        brought the pool from a positive value to exactly 0 — repeated
        damage on a wrecked rig does NOT re-fire crossing, and healing
        from 0 back to positive is repair, not crash.

        Emits ``rig_pool.delta`` on every call. Emits
        ``rig_pool.zero_crossing`` only when ``zero_crossed`` is True
        — story 53-3's crash handler subscribes to the dedicated
        channel rather than filtering every delta.
        """
        old_current = self.current
        raw = self.current + delta
        new_current = max(0, min(self.max, raw))
        self.current = new_current
        zero_crossed = old_current > 0 and new_current == 0

        with Span.open(
            SPAN_RIG_POOL_DELTA,
            attrs={
                "character_id": self.character_id,
                "chassis_id": self.chassis_id,
                "delta": delta,
                "old_current": old_current,
                "new_current": new_current,
            },
        ):
            pass

        if zero_crossed:
            with Span.open(
                SPAN_RIG_POOL_ZERO_CROSSING,
                attrs={
                    "character_id": self.character_id,
                    "chassis_id": self.chassis_id,
                    "old_current": old_current,
                    "new_current": new_current,
                },
            ):
                pass

        return RigComposureDeltaResult(
            old_current=old_current,
            new_current=new_current,
            zero_crossed=zero_crossed,
        )

    def is_destroyed(self) -> bool:
        """Snapshot: True iff ``current == 0``.

        Distinct from ``RigComposureDeltaResult.zero_crossed`` —
        ``is_destroyed`` is a post-hoc query, ``zero_crossed`` is the
        edge-trigger from a specific delta call.
        """
        return self.current == 0


__all__ = [
    "RigComposureDeltaResult",
    "RigComposurePool",
]
