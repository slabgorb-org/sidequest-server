"""Hermetic ``claude-agent-sdk`` transport fakes — Story 119-3 RED.

``claude-agent-sdk`` is NOT installed during the RED phase (declaring it in
``pyproject.toml`` plus an import-wiring test is Dev's first GREEN step — spec
§10). These fakes therefore **never import ``claude_agent_sdk``**: they are
duck-typed stand-ins for the message/block stream that ``query()`` yields, so
the whole RED suite runs without the package and without a live subscription
(spec §9: "All tests must run without a live subscription — they drive a fake
``query``/transport injected via the seam in §6.2/OQ-9").

The transport seam (OQ-9). Each module that talks to the Agent SDK exposes a
module-level ``query`` symbol — the analog of ``build_async_anthropic``, looked
up late-bound so a monkeypatched fake is what the port calls. Tests do::

    monkeypatch.setattr(mod, "query", FakeQuery(...), raising=False)

Message shapes mirror the documented Agent SDK surface (spec §3.2):

* ``AssistantMessage(content=[TextBlock | ToolUseBlock, ...])``
* ``ResultMessage(is_error, subtype, result, num_turns, total_cost_usd,
  usage, structured_output)``

The port must read this stream **structurally** (attribute presence /
``getattr``) — the codebase's existing convention for SDK objects
(``getattr(block, "type", None)``) — not ``isinstance`` against
``claude_agent_sdk`` classes, which would couple the loop to the transport and
make this hermetic fake-``query`` seam impossible to exercise in RED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

# ---------------------------------------------------------------------------
# Duck-typed message / block stand-ins
# ---------------------------------------------------------------------------


@dataclass
class FakeTextBlock:
    """A ``TextBlock`` of an ``AssistantMessage`` (final or intermediate prose)."""

    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    """A ``ToolUseBlock`` — a tool invocation the model made."""

    name: str
    input: dict[str, Any]
    id: str = "toolu_fake"
    type: str = "tool_use"


@dataclass
class FakeAssistantMessage:
    """An assistant turn carrying a list of text / tool-use blocks."""

    content: list[Any]


@dataclass
class FakeResultMessage:
    """The terminal ``ResultMessage`` (spec §3.2).

    ``structured_output`` carries the ``output_format`` JSON-schema result for
    the Haiku single-shot path (where the forced tool's ``.input`` dict went).
    """

    result: str = ""
    is_error: bool = False
    subtype: str = "success"
    num_turns: int = 2
    total_cost_usd: float | None = 0.0
    usage: Any = None
    structured_output: Any = None


def fake_usage(
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
) -> dict[str, int]:
    """``ResultMessage.usage`` is documented ``dict | None`` (spec §3.2)."""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
    }


# ---------------------------------------------------------------------------
# Isolation predicate (AC1 landmine)
# ---------------------------------------------------------------------------

# Sentinel distinct from ``[]`` / ``None`` so a missing attribute never looks
# like an intentional "load nothing".
_MISSING = object()


def options_pin_isolation(options: Any) -> bool:
    """True iff ``options`` carries the AC1 context-isolation pins (spec §6.3).

    The narrator/Haiku path MUST pin ``setting_sources=[]`` + ``add_dirs=[]``
    and pass a **plain-string** ``system_prompt`` (no ``claude_code`` preset).
    A ``cwd`` check is the test's job (it needs the repo root to compare).
    """
    return (
        getattr(options, "setting_sources", _MISSING) == []
        and getattr(options, "add_dirs", _MISSING) == []
        and isinstance(getattr(options, "system_prompt", None), str)
    )


# ---------------------------------------------------------------------------
# Fake query callables
# ---------------------------------------------------------------------------


class FakeQuery:
    """Records every ``(prompt, options)`` and replays a scripted message stream.

    Monkeypatched over a module's late-bound ``query`` symbol. The real SDK
    signature is ``query(*, prompt, options)`` returning an async iterator of
    messages; this mirrors it exactly.
    """

    def __init__(self, messages: list[Any]) -> None:
        self._messages = list(messages)
        self.calls: list[SimpleNamespace] = []

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        self.calls.append(SimpleNamespace(prompt=prompt, options=options))
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        for msg in self._messages:
            yield msg

    @property
    def last_options(self) -> Any:
        assert self.calls, "query() was never called"
        return self.calls[-1].options


class RaisingFakeQuery:
    """A ``query`` that raises on call — simulates the SDK failing when no
    subscription credential is present (OQ-5: absence surfaces as a failed
    query). The port must map this to a loud raise, never a degraded success.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[SimpleNamespace] = []

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        self.calls.append(SimpleNamespace(prompt=prompt, options=options))
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        raise self._exc
        yield  # pragma: no cover - makes this an async generator


class ContaminatingFakeQuery:
    """Stands in for the real SDK's project-context absorption (AC1 landmine).

    The 119-3 spike replied **in an SM persona** because the Agent SDK absorbed
    the repo ``CLAUDE.md`` from ``cwd`` / ``setting_sources``. This fake
    reproduces that failure mode behaviorally: if the options it receives do
    NOT pin isolation (spec §6.3), it yields the *contaminated* persona text;
    if they DO, it yields the clean narration. A turn whose converged prose is
    clean therefore proves the pins are in place — the spike-regression gate.
    """

    def __init__(self, *, clean_text: str, persona_text: str) -> None:
        self._clean = clean_text
        self._persona = persona_text
        self.calls: list[SimpleNamespace] = []

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        self.calls.append(SimpleNamespace(prompt=prompt, options=options))
        isolated = options_pin_isolation(options)
        text = self._clean if isolated else self._persona
        return self._aiter(text)

    async def _aiter(self, text: str) -> AsyncIterator[Any]:
        yield FakeAssistantMessage(content=[FakeTextBlock(text=text)])
        yield FakeResultMessage(
            result=text,
            is_error=False,
            subtype="success",
            num_turns=2,
            usage=fake_usage(),
        )


# ---------------------------------------------------------------------------
# Convenience builders for scripted streams
# ---------------------------------------------------------------------------


def converged_text_stream(
    *,
    text: str,
    num_turns: int = 2,
    usage: dict[str, int] | None = None,
) -> list[Any]:
    """A no-tool turn: one assistant text block + a successful ResultMessage."""
    return [
        FakeAssistantMessage(content=[FakeTextBlock(text=text)]),
        FakeResultMessage(
            result=text,
            is_error=False,
            subtype="success",
            num_turns=num_turns,
            usage=usage if usage is not None else fake_usage(),
        ),
    ]


def structured_output_stream(
    payload: dict[str, Any] | None,
    *,
    is_error: bool = False,
    subtype: str = "success",
    usage: dict[str, int] | None = None,
) -> list[Any]:
    """A Haiku single-shot ``output_format`` stream: just a ResultMessage whose
    ``structured_output`` carries the schema-valid dict (or ``None``)."""
    return [
        FakeResultMessage(
            result="",
            is_error=is_error,
            subtype=subtype,
            num_turns=2,
            usage=usage if usage is not None else fake_usage(),
            structured_output=payload,
        )
    ]


def max_turns_one_stream(*, usage: dict[str, int] | None = None) -> list[Any]:
    """The empirically-verified ``max_turns=1`` fail-closed shape (spec §3.6 /
    OQ-16): ``is_error=True``, ``subtype='error_max_turns'``,
    ``structured_output=None``."""
    return [
        FakeResultMessage(
            result="",
            is_error=True,
            subtype="error_max_turns",
            num_turns=1,
            usage=usage if usage is not None else fake_usage(),
            structured_output=None,
        )
    ]


def error_result_stream(
    *,
    subtype: str = "error",
    usage: dict[str, int] | None = None,
) -> list[Any]:
    """A generic terminal failure: ``is_error=True``. The port must raise, not
    return a degraded-success ToolingResult / empty payload."""
    return [
        FakeResultMessage(
            result="",
            is_error=True,
            subtype=subtype,
            num_turns=2,
            usage=usage if usage is not None else fake_usage(),
            structured_output=None,
        )
    ]


__all__ = [
    "ContaminatingFakeQuery",
    "FakeAssistantMessage",
    "FakeQuery",
    "FakeResultMessage",
    "FakeTextBlock",
    "FakeToolUseBlock",
    "RaisingFakeQuery",
    "converged_text_stream",
    "error_result_stream",
    "fake_usage",
    "max_turns_one_stream",
    "options_pin_isolation",
    "structured_output_stream",
]
