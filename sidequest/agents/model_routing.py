"""Call-type → model id resolver.

Each call site declares its CallType. The default ladder maps Haiku for
cheap classification/scratch, Sonnet for narration, Opus for moments the
caller flags as important. Genre packs may override per-call-type via the
pack_overrides argument (wiring lands in Phase B).

Story 92-2 (epic 92 "Local Classification Routing"): the ladder gains a
LOCAL rung. Behind EXPLICIT config (``SIDEQUEST_CLASSIFICATION_BACKEND=
ollama``), ``CLASSIFICATION`` and ``SCRATCH`` resolve to the A/B-validated
local model (:data:`LOCAL_CLASSIFIER_MODEL`) instead of Haiku. The default
is unchanged — the epic's hard gate forbids any routing flip by default.
An unknown env value fails loud here (a typo silently meaning "haiku"
would recreate dark spend by configuration error — No Silent Fallbacks).
"""

from __future__ import annotations

import os
from enum import StrEnum

ENV_CLASSIFICATION_BACKEND = "SIDEQUEST_CLASSIFICATION_BACKEND"

# The 92-1 A/B gate evidence (router corpus, dispatch-selection agreement,
# latency budget) was collected against EXACTLY this model id. The production
# rung must serve the evaluated model — routing a different one would ship an
# unevaluated classifier (a SOUL/agency problem per the epic, not just cost).
# ``ab_eval_harness.OLLAMA_MODEL`` imports this constant so the instrument
# and the rung cannot drift apart.
LOCAL_CLASSIFIER_MODEL = "qwen2.5:7b-instruct"

_VALID_CLASSIFICATION_BACKENDS = frozenset({"anthropic", "ollama"})

# The call types the local rung covers. Narration tiers NEVER consult the
# classification seam — flipping the narrator is emphatically not this story.
_LOCAL_RUNG_CALL_TYPES = frozenset({"classification", "scratch"})


class UnknownCallType(ValueError):
    """resolve_model was passed a non-CallType value."""


class UnknownClassificationBackend(ValueError):
    """``SIDEQUEST_CLASSIFICATION_BACKEND`` value is not a supported backend.

    Story 92-2: raised by :func:`classification_backend` so a typo
    (``olama``, ``local``...) fails loud instead of silently meaning Haiku.
    """


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


def classification_backend() -> str:
    """Resolve the explicit classification-backend config (story 92-2).

    Returns ``"anthropic"`` (the default — current Haiku behavior) or
    ``"ollama"`` (the local rung). Normalizes strip+lower like
    ``SIDEQUEST_LLM_BACKEND`` handling in ``llm_factory``, then gates:
    an unrecognized value raises :class:`UnknownClassificationBackend`
    naming the env var (No Silent Fallbacks).
    """
    raw = os.environ.get(ENV_CLASSIFICATION_BACKEND, "anthropic")
    key = raw.strip().lower()
    if key not in _VALID_CLASSIFICATION_BACKENDS:
        raise UnknownClassificationBackend(
            f"{ENV_CLASSIFICATION_BACKEND}={raw!r} not supported; pick one of "
            f"{sorted(_VALID_CLASSIFICATION_BACKENDS)}"
        )
    return key


def resolve_model(
    call_type: CallType,
    *,
    pack_overrides: dict[CallType, str] | None = None,
) -> str:
    if not isinstance(call_type, CallType):
        raise UnknownCallType(f"{call_type!r} is not a CallType")
    if pack_overrides is not None and call_type in pack_overrides:
        return pack_overrides[call_type]
    # Story 92-2 local rung: only CLASSIFICATION/SCRATCH consult the seam,
    # and only after pack overrides (the existing highest-precedence rung).
    if call_type.value in _LOCAL_RUNG_CALL_TYPES and classification_backend() == "ollama":
        return LOCAL_CLASSIFIER_MODEL
    return _DEFAULT[call_type]
