"""Tool registry — Phase B foundation.

@tool decorator + ToolContext + ToolResult + Registry + dispatch.
Phase C populates the v1 catalog by importing each adapter from
sidequest.agents.tools.<name>.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from sidequest.agents.tooling_protocol import (
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Span

    from sidequest.agents.perception_filter import (  # type: ignore[import-not-found]
        PerceptionFilter,
    )
    from sidequest.game.lore_store import LoreStore
    from sidequest.game.monster_manual import MonsterManual
    from sidequest.game.weather import WeatherState
    from sidequest.genre.names.generator import NameGenerator


class ToolCategory(StrEnum):
    READ = "read"
    WRITE = "write"
    GENERATE = "generate"


class ToolResultStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ERROR_RECOVERABLE = "error_recoverable"
    ERROR_FATAL = "error_fatal"


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: ToolResultStatus
    payload: Any | None = None
    message: str | None = None

    @classmethod
    def ok(cls, payload: Any) -> ToolResult:
        return cls(status=ToolResultStatus.OK, payload=payload)

    @classmethod
    def not_found(cls, message: str) -> ToolResult:
        return cls(status=ToolResultStatus.NOT_FOUND, message=message)

    @classmethod
    def error(cls, message: str, *, recoverable: bool = True) -> ToolResult:
        status = ToolResultStatus.ERROR_RECOVERABLE if recoverable else ToolResultStatus.ERROR_FATAL
        return cls(status=status, message=message)

    def to_anthropic_payload(self) -> tuple[str, bool]:
        """Render as (content_str, is_error) for the SDK tool_result message.

        Non-JSON-serializable payload values are coerced to str via json.dumps(default=str).
        """
        if self.status is ToolResultStatus.OK:
            return (json.dumps(self.payload, default=str), False)
        if self.status is ToolResultStatus.NOT_FOUND:
            return (f"NOT_FOUND: {self.message}", False)
        # error_recoverable / error_fatal
        return (f"ERROR: {self.message}", True)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Runtime state injected into each tool handler.

    The model never sees ToolContext — it is the server-side companion to
    the JSON-Schema-validated args the model does see.
    """

    world_id: str
    session_id: str
    perspective_pc: str | None
    turn_number: int
    repository: Any  # SaveRepository — kept Any to avoid Phase B coupling
    otel_span: Span
    perception_filter: PerceptionFilter
    # Phase C Task 13 amendment: narrator-private LoreStore reference for
    # the query_lore tool. LoreStore lives on SessionHandler, not on the
    # save repository, so it cannot be reached via ``repository``. Phase E
    # wires this at the production call site; Phase C tools tolerate ``None``
    # (query_lore returns an empty result with an OTEL marker).
    lore_store: LoreStore | None = None
    # Phase C Task 14 amendment: MonsterManual reference for the
    # lookup_monster tool. The MonsterManual is per-genre/world and lives on
    # SessionHandler (loaded via ``MonsterManual.load(genre, world)``), not on
    # the save repository — same shape as the lore_store amendment
    # above. Phase E wires this at the production call site; Phase C tools
    # tolerate ``None`` (lookup_monster returns ``found=False`` with an OTEL
    # marker).
    monster_manual: MonsterManual | None = None
    # Phase C Task 20 amendment: GenrePack reference for the tick_tropes
    # tool. The trope engine (``sidequest.game.trope_tick.tick_tropes``)
    # duck-types on ``pack.tropes`` — a list of ``TropeDefinition`` — so
    # this slot is typed ``Any`` to avoid pulling the entire
    # ``sidequest.genre`` machinery into the Phase B foundation. The
    # production wire site holds the loaded ``GenrePack`` on the session
    # handler; Phase E plumbs it through. Phase C tools tolerate ``None``
    # (tick_tropes records an OTEL marker and no-ops).
    genre_pack: Any | None = None
    # Phase C Task 24 amendment: per-culture NameGenerator dict for the
    # generate_name tool. ADR-091 culture corpora are loaded at session
    # bootstrap (the corpus directory lives in sidequest-content, outside
    # the server repo) via
    # ``sidequest.genre.names.generator.build_from_culture`` — keyed by
    # ``Culture.name``. Holding the pre-built generators here avoids
    # per-tool disk I/O and corpus-dir guessing. Phase E wires this at the
    # production call site; Phase C tools tolerate ``None`` (generate_name
    # returns an empty list with ``name_generators_wired=False``).
    name_generators: dict[str, NameGenerator] | None = None
    # Story 24-6 amendment: world-grounding data for the get_world_grounding
    # tool. Three optional plain-data fields — current WeatherState (from the
    # 24-5 generator), demographics dict (24-3 YAML), calendar dict (24-4
    # YAML, backlog at story-creation time). The session handler loads these
    # at session bootstrap and stamps them on every ToolContext. Tools
    # tolerate ``None`` (get_world_grounding returns ``section=None`` and
    # stamps ``tool.grounding.<section>_present=False`` on the dispatch span
    # — explicit absence, no silent fallback per CLAUDE.md).
    weather_state: WeatherState | None = None
    world_demographics: dict[str, Any] | None = None
    world_calendar: dict[str, Any] | None = None


_ArgsT = TypeVar("_ArgsT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class _RegisteredTool:
    name: str
    description: str
    category: ToolCategory
    args_model: type[BaseModel]
    handler: Callable[..., Awaitable[ToolResult]]


class Registry:
    """Holds the @tool-decorated handlers and dispatches tool_use blocks."""

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        category: ToolCategory,
        args_model: type[BaseModel],
        handler: Callable[..., Awaitable[ToolResult]],
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool {name!r} already registered")
        self._tools[name] = _RegisteredTool(
            name=name,
            description=description,
            category=category,
            args_model=args_model,
            handler=handler,
        )

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def tool_definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=t.name,
                description=t.description,
                input_schema=t.args_model.model_json_schema(),
            )
            for t in self._tools.values()
        ]

    async def dispatch(
        self,
        block: ToolUseBlock,
        ctx: ToolContext,
    ) -> ToolResultBlock:
        # Lazy import breaks the tool_registry → tool_dispatch → tool_registry
        # circular dependency. tool_dispatch.py imports ToolCategory from this
        # module at its top level; if we imported it here at module level,
        # ToolCategory would not yet be bound when tool_dispatch.py executed.
        # By deferring to method-call time, tool_registry is fully initialised.
        from sidequest.telemetry.spans.tool_dispatch import tool_dispatch_span

        registered = self._tools.get(block.name)
        if registered is None:
            from sidequest.telemetry.spans.span import Span as _SpanHelper

            err = ToolResult.error(
                f"unknown tool {block.name!r}",
                recoverable=True,
            )
            body, is_err = err.to_anthropic_payload()
            with _SpanHelper.open(
                f"tool.unknown.{block.name}",
                {
                    "tool.name": block.name,
                    "tool.category": "unknown",
                    "tool.result_status": err.status.value,
                    "tool.result_size_bytes": len(body),
                },
            ):
                pass
            return ToolResultBlock(tool_use_id=block.id, content=body, is_error=is_err)

        with tool_dispatch_span(
            name=registered.name,
            category=registered.category,
            perspective_pc=ctx.perspective_pc,
        ) as span:
            # Swap in the dispatch span so handlers' per-tool attribute writes
            # (ctx.otel_span.set_attribute("tool.<short>.*", ...)) land on the
            # span the GM panel actually watches via tool.{read,write,gen}.{name}.
            # ToolContext is frozen+slots, so dataclasses.replace is canonical.
            handler_ctx = replace(ctx, otel_span=span)
            try:
                args = registered.args_model.model_validate(block.arguments)
            except ValidationError as exc:
                err = ToolResult.error(
                    f"argument validation failed: {json.dumps(exc.errors(), default=str)}",
                    recoverable=True,
                )
                body, is_err = err.to_anthropic_payload()
                span.set_attribute("tool.result_status", err.status.value)
                span.set_attribute("tool.result_size_bytes", len(body))
                return ToolResultBlock(tool_use_id=block.id, content=body, is_error=is_err)

            try:
                if registered.category is ToolCategory.WRITE:
                    lock = self._write_locks.setdefault(handler_ctx.session_id, asyncio.Lock())
                    async with lock:
                        result = await registered.handler(args, handler_ctx)
                else:
                    result = await registered.handler(args, handler_ctx)
            except Exception as exc:
                # Handler raised — record on the span (tool_dispatch_span's except clause
                # also records at the outer level when we re-raise via this path,
                # so we explicitly do it here without re-raising).
                span.record_exception(exc)
                err = ToolResult.error(
                    f"handler raised {type(exc).__name__}: {exc}",
                    recoverable=False,
                )
                body, is_err = err.to_anthropic_payload()
                span.set_attribute("tool.result_status", err.status.value)
                span.set_attribute("tool.result_size_bytes", len(body))
                return ToolResultBlock(tool_use_id=block.id, content=body, is_error=is_err)

            filtered = handler_ctx.perception_filter.filter_result(
                tool_name=registered.name,
                category=registered.category,
                result=result,
                perspective_pc=handler_ctx.perspective_pc,
            )

            body, is_err = filtered.to_anthropic_payload()
            span.set_attribute("tool.result_status", filtered.status.value)
            span.set_attribute("tool.result_size_bytes", len(body))
            return ToolResultBlock(tool_use_id=block.id, content=body, is_error=is_err)


# ---------------------------------------------------------------------------
# Module-level default registry + decorator
# ---------------------------------------------------------------------------

default_registry = Registry()


def tool(
    *,
    name: str,
    description: str,
    category: ToolCategory,
    registry: Registry | None = None,
) -> Callable[[Callable[..., Awaitable[ToolResult]]], Callable[..., Awaitable[ToolResult]]]:
    """Decorator: register an async handler with a Pydantic-args model.

    Deviation from plan: ``from __future__ import annotations`` turns all
    annotations into strings at definition time. The plan's ``params[0].annotation``
    check would always see a string, never a type. We use ``typing.get_type_hints(fn)``
    to resolve the forward references back to real types before validating the
    args-model annotation.
    """
    chosen = registry if registry is not None else default_registry

    def decorate(
        fn: Callable[..., Awaitable[ToolResult]],
    ) -> Callable[..., Awaitable[ToolResult]]:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params:
            raise TypeError(f"@tool {name!r} handler must take (args_model, ctx) — got no params")
        # Resolve annotations to real types: needed because `from __future__ import
        # annotations` makes all annotations strings. inspect.Parameter.annotation
        # would return a str, not a type, so isinstance(ann, type) would always fail.
        hints = typing.get_type_hints(fn)
        first_param_name = params[0].name
        args_annotation = hints.get(first_param_name)
        if (
            args_annotation is None
            or not isinstance(args_annotation, type)
            or not issubclass(args_annotation, BaseModel)
        ):
            raise TypeError(
                f"@tool {name!r}: first parameter must be annotated with a "
                f"pydantic.BaseModel subclass (got {args_annotation!r})"
            )
        chosen.register(
            name=name,
            description=description,
            category=category,
            args_model=args_annotation,
            handler=fn,
        )
        return fn

    return decorate
