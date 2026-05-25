"""OTEL span: narration.turn — rollup parent for one narrator turn.

Children: llm.request spans (Task 6) + tool.{read,write,gen}.* spans (Phase B).

Seeded attributes (entry):
    world_id, session_id, turn_number, acting_pc

Rollup attributes (set by caller before exit):
    narration.turn.model_chosen
    narration.turn.total_input_tokens
    narration.turn.total_output_tokens
    narration.turn.cache_read_tokens
    narration.turn.cache_write_tokens
    narration.turn.cache_ttl
    narration.turn.cache_write_5m_tokens
    narration.turn.cache_write_1h_tokens
    narration.turn.system_block_sizes_json
    narration.turn.total_cost_usd
    narration.turn.cost_band          # all_systems_go / needs_work / stop_everything
    narration.turn.tool_call_count
    narration.turn.llm_request_count
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .span import Span


@contextmanager
def narration_turn_cost_span(
    *,
    world_id: str,
    session_id: str,
    turn_number: int,
    acting_pc: str,
    _tracer: trace.Tracer | None = None,
) -> Iterator[trace.Span]:
    """Open a narration.turn span and seed entry attributes.

    Args:
        world_id: Identifier of the world/scenario being run.
        session_id: Unique session identifier for the current game session.
        turn_number: Sequential turn counter within the session.
        acting_pc: Name of the player character taking the turn.
        _tracer: Optional tracer override for test isolation.
    """
    with Span.open(
        "narration.turn",
        {
            "world_id": world_id,
            "session_id": session_id,
            "turn_number": turn_number,
            "acting_pc": acting_pc,
        },
        tracer_override=_tracer,
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
