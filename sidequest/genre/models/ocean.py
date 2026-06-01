"""OCEAN personality profile types.

Port of sidequest-genre/src/models/ocean.rs (data shape only — no random/jitter/shift methods).
Also includes DramaThresholds which co-locates in ocean.rs in Rust.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# Authored OCEAN content uses short keys (O/C/E/A/N) on a 0..1 scale (ADR-042 /
# #318); runtime OceanProfile uses full keys on 0..10. Module-level so pydantic v2
# doesn't reinterpret an underscore-prefixed class attr as a ModelPrivateAttr.
_AUTHORED_KEY_MAP = {
    "O": "openness",
    "C": "conscientiousness",
    "E": "extraversion",
    "A": "agreeableness",
    "N": "neuroticism",
}


class DramaThresholds(BaseModel):
    """Genre-tunable breakpoints for pacing decisions.

    Loaded from an optional pacing.yaml. Missing fields fall back to defaults.
    Uses extra="ignore" to match Rust's #[serde(default)] behavior — unknown
    fields in content YAML (like world-level pacing.yaml with extra sections)
    are silently dropped.
    """

    model_config = {"extra": "ignore"}

    sentence_delivery_min: float = 0.30
    streaming_delivery_min: float = 0.70
    render_threshold: float = 0.40
    escalation_streak: int = 5
    ramp_length: int = 8


class OceanProfile(BaseModel):
    """Big Five (OCEAN) personality profile. Each dimension is 0.0–10.0."""

    model_config = {"extra": "forbid"}

    openness: float = 5.0
    conscientiousness: float = 5.0
    extraversion: float = 5.0
    agreeableness: float = 5.0
    neuroticism: float = 5.0

    @field_validator(
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
        mode="before",
    )
    @classmethod
    def clamp_dimension(cls, v: float) -> float:
        return max(0.0, min(10.0, float(v)))

    @classmethod
    def from_authored(cls, authored: dict[str, float]) -> OceanProfile:
        """Normalize an authored OCEAN dict (short keys, 0..1) to a runtime profile.

        Authored content uses short keys (``O/C/E/A/N``) on a 0..1 scale (ADR-042
        / #318). Runtime ``OceanProfile`` uses full keys on 0..10. Unknown keys
        raise — a typo in authored content is an authoring error, not a silent
        default (No Silent Fallbacks). Out-of-range values (outside 0..1) also
        raise: an author who writes ``5`` thinking 0..10 must be told, not have
        it silently clamped by the runtime ``clamp_dimension`` validator. Missing
        dimensions keep the 5.0 center.
        """
        kwargs: dict[str, float] = {}
        for key, raw in authored.items():
            if key not in _AUTHORED_KEY_MAP:
                raise ValueError(
                    f"ocean: unknown authored key {key!r}; expected one of "
                    f"{sorted(_AUTHORED_KEY_MAP)}"
                )
            value = float(raw)
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"ocean: authored value for key {key!r} is {value}; "
                    f"expected 0..1 (authored OCEAN scale)"
                )
            kwargs[_AUTHORED_KEY_MAP[key]] = value * 10.0
        return cls(**kwargs)


class OceanDimension(StrEnum):
    """One of the Big Five personality dimensions."""

    openness = "Openness"
    conscientiousness = "Conscientiousness"
    extraversion = "Extraversion"
    agreeableness = "Agreeableness"
    neuroticism = "Neuroticism"


class OceanShift(BaseModel):
    """A single recorded personality shift."""

    model_config = {"extra": "forbid"}

    dimension: OceanDimension
    old_value: float
    new_value: float
    cause: str
    turn: int


class OceanShiftLog(BaseModel):
    """Append-only log of personality shifts."""

    model_config = {"extra": "forbid"}

    shifts: list[OceanShift] = Field(default_factory=list)
