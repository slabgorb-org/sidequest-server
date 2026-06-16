"""Dice wire payloads (DiceRequest/DiceThrow/DiceResult).

Port of the dice types from sidequest-protocol/src/message.rs:

- DieSides — enum serialized as u32 face count (4, 6, 8, 10, 12, 20, 100; 0 = Unknown)
- DieSpec — {sides, count} one group in a pool
- ThrowParams — physics gesture params (animation only, not outcome)
- RollOutcome — CritSuccess / Success / Tie / Fail / CritFail (+Unknown for forward-compat)
- DieGroupResult — per-group rolled faces paired with originating spec
- DiceRequestPayload — server -> all clients: "roll for me"
- DiceThrowPayload — rolling client -> server: physics-is-the-roll result
- DiceResultPayload — server -> all clients: resolved outcome

Wire format matches the Rust crate exactly — the React UI already consumes
this shape via sidequest-ui/src/types/payloads.ts.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any

from pydantic import Field, model_validator

from sidequest.protocol.base import ProtocolBase
from sidequest.protocol.types import Stat

_SUPPORTED_SIDES: frozenset[int] = frozenset({4, 6, 8, 10, 12, 20, 100})


class DieSides(IntEnum):
    """Supported die face counts. ``Unknown=0`` is the forward-compat sentinel.

    Serializes as a plain integer face count on the wire — Rust uses
    ``#[serde(from = "u32", into = "u32")]``. IntEnum preserves that
    serialization (pydantic emits the underlying int).
    """

    Unknown = 0
    D4 = 4
    D6 = 6
    D8 = 8
    D10 = 10
    D12 = 12
    D20 = 20
    D100 = 100

    @classmethod
    def from_wire(cls, value: int) -> DieSides:
        """Map an arbitrary integer to a variant; unknown values → ``Unknown``."""
        if value in _SUPPORTED_SIDES:
            return cls(value)
        return cls.Unknown

    def faces(self) -> int | None:
        """Return the face count, or ``None`` for ``Unknown``."""
        return None if self is DieSides.Unknown else int(self)


class RollOutcome(str, Enum):  # noqa: UP042 — matches project convention (see protocol/enums.py)
    """Outcome classification — feeds narrator tone.

    Serializes as the variant name (``"CritSuccess"`` etc.). Unknown wire
    values map to ``Unknown`` via ``_missing_`` so a newer variant from a
    future wire version doesn't hard-error at parse time.

    Tie is the 5th tier added for dual-track momentum (spec
    2026-04-25-dual-track-momentum-design.md): fired when total == difficulty.
    """

    CritSuccess = "CritSuccess"
    Success = "Success"
    Tie = "Tie"
    Fail = "Fail"
    CritFail = "CritFail"
    Unknown = "Unknown"

    @classmethod
    def _missing_(cls, value: object) -> RollOutcome:
        return cls.Unknown


class DieSpec(ProtocolBase):
    """One group in a dice pool — e.g. ``{sides: 20, count: 1}``.

    Count is 1..=255 (Rust NonZeroU8). Sides accepts either a raw integer or
    the enum, and legacy ``"d20"`` strings are coerced for a defensive
    boundary — the React client builds a fresh payload each roll so this is
    mostly about catching drift, not relying on it.
    """

    sides: DieSides
    count: int = Field(ge=1, le=255)

    @model_validator(mode="before")
    @classmethod
    def _coerce_sides(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        raw = v.get("sides")
        if isinstance(raw, int) and not isinstance(raw, DieSides):
            return {**v, "sides": DieSides.from_wire(raw)}
        if isinstance(raw, str):
            trimmed = raw.strip().lower().removeprefix("d")
            try:
                return {**v, "sides": DieSides.from_wire(int(trimmed))}
            except ValueError as exc:
                raise ValueError(f"unknown die sides {raw!r}") from exc
        return v


class ThrowParams(ProtocolBase):
    """Drag-and-flick gesture parameters.

    Animation aesthetics only — the outcome is determined by the seed
    (server-side RNG path) or the client-reported face values (physics-is-
    the-roll path). Spectators replay deterministically from the same
    params + seed.
    """

    velocity: tuple[float, float, float]
    angular: tuple[float, float, float]
    position: tuple[float, float]


class DieGroupResult(ProtocolBase):
    """Per-group rolled faces paired with the originating ``DieSpec``.

    Invariant: ``len(faces) == spec.count``. Enforced in
    ``DiceResultPayload`` at the wire boundary.
    """

    spec: DieSpec
    faces: list[int]


class DiceRequestPayload(ProtocolBase):
    """Server -> all clients: roll this dice pool against this DC.

    ``rolling_player_id`` identifies who must throw; other clients spectate.
    Broadcast to the whole room so every dice overlay renders in sync.
    """

    request_id: str
    rolling_player_id: str
    character_name: str
    dice: list[DieSpec]
    modifier: int
    stat: Stat
    difficulty: int = Field(ge=1)
    context: str = ""

    @model_validator(mode="after")
    def _require_non_empty_pool(self) -> DiceRequestPayload:
        if not self.dice:
            raise ValueError("dice pool must be non-empty")
        return self


class DiceThrowPayload(ProtocolBase):
    """Rolling client -> server: physics settled, here are the faces.

    Physics-is-the-roll (ADR-074, story 34-12). ``beat_id`` — when present,
    server applies the beat to the active encounter before resolving the
    dice, then runs the narrator in the same tick. This is the UI's primary
    path.

    ``player_action`` — the freeform text the player typed into the InputBar
    at the moment they clicked a beat tile. Beats are an alternate submit
    verb for whatever the player typed (D2 confrontation panel, 2026-05-13):
    "I swing from the chandelier" + click Attack carries the chandelier
    swing as the player's stated action. When present, the dispatch
    prepends it to ``replay_action_text`` so the narrator runs with both
    the mechanical beat outcome AND the player's invention — Zork Problem
    avoidance (SOUL.md). When absent/empty the synthetic ``[BEAT_RESOLVED]``
    line is used alone, matching the pre-2026-05-13 wire shape.
    """

    request_id: str
    throw_params: ThrowParams
    face: list[int]
    beat_id: str | None = None
    player_action: str | None = None


class DiceResultPayload(ProtocolBase):
    """Server -> all clients: resolved dice outcome.

    ``rolls`` carries per-group faces so consumers can attribute rolls back
    to their die type. ``seed`` drives spectator replay animation only —
    the face values are already authoritative from the rolling player.
    """

    request_id: str
    rolling_player_id: str
    character_name: str
    rolls: list[DieGroupResult]
    modifier: int
    total: int
    difficulty: int = Field(ge=1)
    outcome: RollOutcome
    seed: int
    throw_params: ThrowParams

    @model_validator(mode="after")
    def _require_face_count_matches_pool(self) -> DiceResultPayload:
        for i, group in enumerate(self.rolls):
            if len(group.faces) != group.spec.count:
                raise ValueError(
                    f"roll group {i}: faces.len={len(group.faces)} does not "
                    f"match spec.count={group.spec.count}"
                )
        return self


class OpposedRollResult(ProtocolBase):
    """Engine-derived outcome of an opposed-check resolution.

    Surfaced in turn results so the UI can render both rolls — the dice
    tray shows the player's d20, the result pane shows the opponent's
    roll, and the shift drives tier display. Visible to all participants
    (no fog-of-war for now).

    ``tier`` is the ``RollOutcome`` derived from ``shift`` via the bands
    in ``sidequest.game.opposed_check`` and is the same value fed to
    ``apply_beat`` for both sides. See ``OpposedRollResult`` in
    ``sidequest.game.opposed_check`` for the engine-side dataclass; this
    class is the wire-shape mirror.
    """

    player_roll: int = Field(ge=1, le=20)
    player_mod: int
    opponent_roll: int = Field(ge=1, le=20)
    opponent_mod: int
    shift: int
    tier: RollOutcome
