"""Prompt-build spans — per-turn token/byte budget of the narrator prompt.

``prompt.game_state.bytes`` fires every narrator turn at the
``<game_state>`` encode site (``session_helpers.py``). Carries the
phase flags and before/after byte counts so the GM panel can verify the
ADR-110 cut is live and within its acceptance gate.

Sebastien's lie-detector contract: the span fires regardless of whether
Phase A or Phase B is applied — silence on the wire would look identical
to the encode site never being reached.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

from ._core import FLAT_ONLY_SPANS
from .span import Span

SPAN_PROMPT_GAME_STATE_BYTES = "prompt.game_state.bytes"

FLAT_ONLY_SPANS.add(SPAN_PROMPT_GAME_STATE_BYTES)


@contextmanager
def prompt_game_state_bytes_span(
    *,
    phase_a_applied: bool,
    phase_b_applied: bool,
    bytes_before: int,
    bytes_after: int,
    _tracer: trace.Tracer | None = None,
    **attrs: Any,
) -> Iterator[trace.Span]:
    """Open the ``prompt.game_state.bytes`` span at the encode site.

    ADR-110 §Observability mandates all four attributes. ``bytes_before``
    is the size of the pre-slimming encoding (``model_dump_json`` +
    ``indent=2``) on the same payload; ``bytes_after`` is the size of
    the actual serialized ``state_summary`` after Phase A + Phase B.
    """
    attributes: dict[str, Any] = {
        "phase_a_applied": phase_a_applied,
        "phase_b_applied": phase_b_applied,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        **attrs,
    }
    with Span.open(
        SPAN_PROMPT_GAME_STATE_BYTES,
        attributes,
        tracer_override=_tracer,
    ) as span:
        yield span
