"""Call-type → model id resolver.

Each call site declares its CallType. The default ladder maps Haiku for
cheap classification/scratch, Sonnet for narration, Opus for moments the
caller flags as important. Genre packs may override per-call-type via the
pack_overrides argument (wiring lands in Phase B).
"""

from __future__ import annotations

from enum import StrEnum


class UnknownCallType(ValueError):
    """resolve_model was passed a non-CallType value."""


class CallType(StrEnum):
    NARRATION = "narration"
    NARRATION_IMPORTANT = "narration_important"
    CLASSIFICATION = "classification"
    SCRATCH = "scratch"


_DEFAULT: dict[CallType, str] = {
    CallType.NARRATION: "claude-sonnet-4-6",
    CallType.NARRATION_IMPORTANT: "claude-opus-4-7",
    CallType.CLASSIFICATION: "claude-haiku-4-5-20251001",
    CallType.SCRATCH: "claude-haiku-4-5-20251001",
}


def resolve_model(
    call_type: CallType,
    *,
    pack_overrides: dict[CallType, str] | None = None,
) -> str:
    if not isinstance(call_type, CallType):
        raise UnknownCallType(f"{call_type!r} is not a CallType")
    if pack_overrides is not None and call_type in pack_overrides:
        return pack_overrides[call_type]
    return _DEFAULT[call_type]
