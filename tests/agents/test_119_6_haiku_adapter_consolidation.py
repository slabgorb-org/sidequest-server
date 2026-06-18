"""Story 119-6 — characterization net for the Haiku-adapter consolidation.

119-6 extracts a shared ``_call_haiku_sdk(...)`` + ``_extract_structured_output_or_raise(...)``
from the four single-shot Haiku call sites in ``llm_factory.py`` (``_AsideLlm.complete``,
``_IntentRouterLlm.emit_tool``, ``_UnseededObjectiveClassifierLlm.emit_tool``,
``infer_archetype_from_freeform``). It is **STRUCTURAL-ONLY** — no behavior change. The
existing 119-3 / 93-1 suites already pin most of each site's behavior (structured-output
dict-or-raise, the ``max_turns=2`` + ``output_format`` options surface, thinking-disabled,
the per-site caller-tagged span + cost-safety for router/aside/classifier, and the whole
archetype enum/empty/truncation taxonomy).

This file closes the THREE coverage holes that a "extract a shared helper" refactor could
slip through and that nothing currently pins — so the green phase has a complete safety net:

* **Gap A — archetype caller tag.** The per-site caller-tag parametrize in
  ``test_119_3_haiku_port.py`` covers router/aside/classifier but NOT ``archetype_inference``.
  The shared helper takes ``caller`` as a parameter; a crossed tag would silently break the
  [COST-1] attribution axis with no test failing.
* **Gap B — archetype options surface.** ``test_forced_extraction_sites_use_output_format_*``
  and ``*_disable_thinking`` parametrize only router/classifier. The archetype site's
  ``max_turns=2`` / ``allowed_tools=[]`` / ``output_format`` schema round-trip / thinking-disabled
  is never pinned, yet the helper centralizes options-building.
* **Gap C — the ``session_id=None`` ceiling BYPASS.** No test asserts that a sessionless call
  does NOT touch the ledger. The refactor centralizes the ``if session_id is not None:`` guard
  into the helper; dropping it would make every sessionless call silently mis-record under a
  ``None`` session (a No-Silent-Fallbacks violation in cost accounting).

These are characterization tests: they are GREEN against the current (pre-refactor) code —
that is the precondition. They go RED only if the consolidation crosses a per-site parameter
or drops the session guard. All tests drive the hermetic fake ``query`` seam (OQ-9) — no live
subscription.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.agents.fakes.fake_agent_sdk import (
    FakeQuery,
    converged_text_stream,
    structured_output_stream,
)

_HAIKU = "claude-haiku-4-5-20251001"
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["intent"],
    "additionalProperties": False,
}

_CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
_VALID_JUNGIAN = "hero"
_VALID_RPG_ROLE = "tank"
_FREEFORM = (
    "I was born in the slag-quarters under the foundry stacks. I protect what "
    "is mine, and I stand in front when it counts."
)
_NO_HINTS: dict[str, str | None] = {"jungian_hint": None, "rpg_role_hint": None}


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient PAYG key — the SDK path must run on subscription auth alone."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def _patch_query(monkeypatch: pytest.MonkeyPatch, stream: list[Any]) -> FakeQuery:
    from sidequest.agents import llm_factory

    fake = FakeQuery(stream)
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)
    return fake


async def _drive_emit_tool(adapter: Any, *, tool_schema: dict[str, Any] | None = None) -> Any:
    return await adapter.emit_tool(
        system="ROUTER-SYS",
        user="attack the bandit captain",
        tool_name="emit_dispatch_package",
        tool_description="Decompose the action into a DispatchPackage.",
        tool_schema=tool_schema if tool_schema is not None else _TOOL_SCHEMA,
    )


def _load_heavy_metal_axes():
    """Real pack axes so the archetype enum-validation path is exercised, not stubbed."""
    from sidequest.genre.loader import GenreLoader

    if not (_CONTENT_ROOT / "heavy_metal").is_dir():
        pytest.skip("heavy_metal content not found")
    pack = GenreLoader(search_paths=[_CONTENT_ROOT]).load("heavy_metal")
    assert pack.base_archetypes is not None and pack.archetype_constraints is not None
    return pack.base_archetypes, pack.archetype_constraints


class _SpyLedger:
    """Records every ``check_ceiling`` / ``record_call`` so the cost-safety wiring is
    asserted independent of the real ledger's accounting (mirrors the 119-3 spy)."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.recorded: list[SimpleNamespace] = []

    def check_ceiling(self, session_id: str, *, ceiling_usd: float) -> None:
        self.checked.append(session_id)

    def record_call(
        self,
        *,
        session_id: str,
        caller: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        ceiling_usd: float,
    ) -> None:
        self.recorded.append(SimpleNamespace(session_id=session_id, caller=caller, model=model))


def _install_spy_ledger(monkeypatch: pytest.MonkeyPatch) -> _SpyLedger:
    from sidequest.agents import cost_safety

    spy = _SpyLedger()
    monkeypatch.setattr(cost_safety, "ledger", lambda: spy)
    return spy


def _llm_request_spans(exporter: Any, *, caller: str) -> list[Any]:
    return [
        s
        for s in exporter.get_finished_spans()
        if s.name == "llm.request" and (s.attributes or {}).get("llm.caller") == caller
    ]


# ===========================================================================
# Gap A — the archetype site's caller tag (the 4th, un-parametrized site)
# ===========================================================================


async def test_archetype_inference_emits_caller_tagged_span_and_records_cost(
    monkeypatch: pytest.MonkeyPatch, otel_capture: Any
) -> None:
    """``infer_archetype_from_freeform`` must open an ``llm.request`` span carrying
    ``llm.caller='archetype_inference'`` AND record to the per-session ledger under
    that same caller tag.

    This is the [COST-1] attribution axis for the 4th Haiku site — the one the
    119-3 caller-tag parametrize omits. The consolidated helper takes ``caller`` as a
    parameter, so a crossed tag (e.g. recording the chargen call under
    ``intent_router``) would silently corrupt cost attribution with nothing failing.
    """
    from sidequest.agents.llm_factory import infer_archetype_from_freeform

    base, constraints = _load_heavy_metal_axes()
    spy = _install_spy_ledger(monkeypatch)
    _patch_query(
        monkeypatch,
        structured_output_stream(
            {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}
        ),
    )

    result = await infer_archetype_from_freeform(
        freeform_text=_FREEFORM,
        base=base,
        constraints=constraints,
        existing_hints=dict(_NO_HINTS),
        session_id="119-6-archetype",
    )
    assert result == {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}

    spans = _llm_request_spans(otel_capture, caller="archetype_inference")
    assert spans, (
        "the archetype site must open an llm.request span carrying "
        "llm.caller='archetype_inference' — span-based cost attribution depends on it"
    )
    assert spy.checked == ["119-6-archetype"], (
        "the pre-flight ceiling check must fire for a session-bearing inference call"
    )
    assert [r.caller for r in spy.recorded] == ["archetype_inference"], (
        "the call must record to the session ledger under caller='archetype_inference' "
        f"(the [COST-1] attribution); recorded {[r.caller for r in spy.recorded]!r}"
    )


# ===========================================================================
# Gap B — the archetype site's forced-extraction options surface
# ===========================================================================


async def test_archetype_inference_uses_output_format_at_max_turns_two_no_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archetype site is a forced-extraction call: it must build
    ``max_turns=2`` + ``allowed_tools=[]`` + ``output_format={'type':'json_schema', ...}``
    with thinking disabled — the same Path A surface the router/classifier sites pin
    (but which the 119-3 options parametrize omits for archetype).
    """
    from sidequest.agents.llm_factory import infer_archetype_from_freeform

    base, constraints = _load_heavy_metal_axes()
    _install_spy_ledger(monkeypatch)
    fake = _patch_query(
        monkeypatch,
        structured_output_stream(
            {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}
        ),
    )

    await infer_archetype_from_freeform(
        freeform_text=_FREEFORM,
        base=base,
        constraints=constraints,
        existing_hints=dict(_NO_HINTS),
        session_id=None,
    )

    opts = fake.last_options
    assert getattr(opts, "max_turns", None) == 2, (
        "max_turns MUST be 2 — the SDK spends an internal finalize turn (OQ-16); "
        f"got {getattr(opts, 'max_turns', None)!r}"
    )
    assert getattr(opts, "allowed_tools", "MISSING") == [], (
        "the structured-output path advertises NO tools (allowed_tools=[])"
    )
    output_format = getattr(opts, "output_format", None)
    assert isinstance(output_format, dict), (
        f"output_format must be set (Path A structured output); got {output_format!r}"
    )
    assert output_format.get("type") == "json_schema"
    schema = output_format.get("schema")
    assert isinstance(schema, dict), f"output_format.schema must be the tool schema; got {schema!r}"
    # The archetype schema is built from the missing axes, each constrained to a
    # per-axis enum — the security/validity boundary that must survive the refactor.
    props = schema.get("properties", {})
    assert set(props) == {"jungian_hint", "rpg_role_hint"}, (
        f"the archetype schema must carry one property per MISSING axis; got {sorted(props)!r}"
    )
    assert _VALID_JUNGIAN in props["jungian_hint"]["enum"], (
        "the jungian_hint property must constrain to the pack's valid jungian ids"
    )
    assert _VALID_RPG_ROLE in props["rpg_role_hint"]["enum"], (
        "the rpg_role_hint property must constrain to the pack's valid rpg-role ids"
    )
    assert getattr(opts, "thinking", None) == {"type": "disabled"}, (
        "output_format calls MUST disable thinking — a thinking pass blows the mt=2 "
        f"budget (error_max_turns); got thinking={getattr(opts, 'thinking', None)!r}"
    )


# ===========================================================================
# Gap C — the session_id=None ceiling BYPASS (the centralizing-refactor's
# single biggest risk: dropping the `if session_id is not None:` guard)
# ===========================================================================


@pytest.mark.parametrize("site", ["aside", "router", "classifier"])
async def test_sessionless_adapter_call_never_touches_the_ledger(
    monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """With ``session_id=None`` the adapter sites must NOT call ``check_ceiling`` or
    ``record_call`` — the ADR-134 hard bypass. The shared helper centralizes the
    ``if session_id is not None:`` guard; if it drops it, a sessionless call would
    mis-record under a ``None`` session (No Silent Fallbacks in cost accounting).
    """
    from sidequest.agents import llm_factory

    spy = _install_spy_ledger(monkeypatch)

    if site == "aside":
        _patch_query(monkeypatch, converged_text_stream(text="ok"))
        adapter = llm_factory.build_aside_llm(session_id=None)
        out = await adapter.complete(system="S", user="U")
        assert out == "ok", "the aside must still return its completion text"
    else:
        _patch_query(monkeypatch, structured_output_stream({"intent": "attack"}))
        builders = {
            "router": llm_factory.build_intent_router_llm,
            "classifier": llm_factory.build_unseeded_objective_classifier_llm,
        }
        adapter = builders[site](session_id=None)
        out = await _drive_emit_tool(adapter)
        assert out == {"intent": "attack"}, "the forced-extraction dict must still return"

    assert spy.checked == [], (
        f"{site} with session_id=None must NOT pre-flight the ceiling; "
        f"checked={spy.checked!r} (the ADR-134 bypass was dropped)"
    )
    assert spy.recorded == [], (
        f"{site} with session_id=None must NOT record to the ledger; "
        f"recorded under {[r.session_id for r in spy.recorded]!r} (the bypass was dropped)"
    )


async def test_sessionless_archetype_inference_never_touches_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ADR-134 bypass for the archetype site: ``session_id=None`` reaches the SDK
    (axes are missing, freeform is non-empty) but records nothing to the ledger.
    """
    from sidequest.agents.llm_factory import infer_archetype_from_freeform

    base, constraints = _load_heavy_metal_axes()
    spy = _install_spy_ledger(monkeypatch)
    fake = _patch_query(
        monkeypatch,
        structured_output_stream(
            {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}
        ),
    )

    result = await infer_archetype_from_freeform(
        freeform_text=_FREEFORM,
        base=base,
        constraints=constraints,
        existing_hints=dict(_NO_HINTS),
        session_id=None,
    )
    assert result == {"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE}
    assert len(fake.calls) == 1, "the inference must actually reach the SDK seam"
    assert spy.checked == [], (
        f"sessionless archetype inference must NOT pre-flight the ceiling; checked={spy.checked!r}"
    )
    assert spy.recorded == [], (
        f"sessionless archetype inference must NOT record to the ledger; "
        f"recorded under {[r.session_id for r in spy.recorded]!r}"
    )
