"""ADR-107 — read-only AsideResolver answer policy (RED, story 50-25).

Plan: docs/superpowers/plans/2026-05-17-aside-channel.md Task 3.
Fails until Dev creates sidequest/agents/aside_resolver.py (GREEN).

The resolver is a GM ruling, not a story beat: it takes a read view +
question, asks the LLM behind a Protocol, and returns a structured
resolution. It has NO write path — "no turn consumed" is enforced by
the object having no hands (test_resolver_has_no_write_surface).
"""

import pytest

from sidequest.agents.aside_resolver import (
    AsideReadView,
    AsideResolution,
    AsideResolver,
)


def _view() -> AsideReadView:
    return AsideReadView(
        character_summary="Hiken, halfling, Small, unencumbered.",
        region_summary="Flooded chamber, standing water ankle-deep for a human.",
        inventory=["torch", "sling"],
        rulebook_summary="Small creatures wade in water rated knee-deep-or-less.",
        recent_narration="The door bursts; black water ankle-deep across the floor.",
    )


class _FakeLLM:
    """Returns whatever JSON the test wires, capturing the system prompt."""

    def __init__(self, payload: str):
        self.payload = payload
        self.seen_system = ""

    async def complete(self, *, system: str, user: str) -> str:
        self.seen_system = system
        return self.payload


@pytest.mark.asyncio
async def test_capability_question_is_answered_and_grounded():
    llm = _FakeLLM(
        '{"answer":"Knee-deep on you, Hiken — wading is slow but fine, no carry.",'
        '"outcome":"answered","grounded_on":["character.size","region.water_depth"]}'
    )
    res = await AsideResolver(llm=llm).resolve(
        question="can I wade or must I be carried?", read_view=_view()
    )
    assert isinstance(res, AsideResolution)
    assert res.outcome == "answered"
    assert res.grounded_on == ("character.size", "region.water_depth")
    assert "wading" in res.answer.lower()


@pytest.mark.asyncio
async def test_hidden_state_question_is_refused():
    llm = _FakeLLM(
        '{"answer":"You\\u0027d have to check — that\\u0027s an action, not a question.",'
        '"outcome":"refused_hidden_state","grounded_on":[]}'
    )
    res = await AsideResolver(llm=llm).resolve(
        question="is the far door trapped?", read_view=_view()
    )
    assert res.outcome == "refused_hidden_state"
    assert res.grounded_on == ()


@pytest.mark.asyncio
async def test_policy_is_in_system_prompt():
    llm = _FakeLLM('{"answer":"x","outcome":"answered","grounded_on":["a"]}')
    await AsideResolver(llm=llm).resolve(question="how does Edge work?", read_view=_view())
    sys = llm.seen_system.lower()
    assert "out-of-character" in sys or "ooc" in sys
    assert "you'd have to check" in sys or "action, not a question" in sys


@pytest.mark.asyncio
async def test_unparseable_llm_output_declines_loudly_not_improvises():
    res = await AsideResolver(llm=_FakeLLM("not json at all")).resolve(
        question="anything", read_view=_view()
    )
    assert res.outcome == "resolver_error"
    assert res.grounded_on == ()
    assert res.answer  # a non-empty loud "ask again" message, never invented lore


@pytest.mark.asyncio
async def test_resolver_has_no_write_surface():
    # Structural guarantee: the resolver exposes only `resolve`. No method
    # name hints at mutation.
    public = [m for m in dir(AsideResolver) if not m.startswith("_")]
    assert public == ["resolve"]


# --------------------------------------------------------------------------- #
# RED rework (review round-trip 1): spec §6 — "Resolver LLM call fails/times
# out → outcome=resolver_error + ERROR-level log. No turn is lost."
# Reviewer HIGH: the current `except (json.JSONDecodeError, ValueError,
# KeyError, TypeError)` catches malformed *output* only; a raising
# `complete()` (timeout / connection / API error) escapes and crashes the
# PLAYER_ACTION handler. These tests pin the call-failure contract. They
# must NOT pass with a bare `except Exception` mindset — they assert the
# *spec-named* failure modes (timeout, connection) decline gracefully;
# they do not assert that arbitrary programming bugs are swallowed.
# --------------------------------------------------------------------------- #

import logging  # noqa: E402 — grouped with the rework block by intent


class _RaisingLLM:
    """An ``AsideLLM`` whose ``complete()`` raises — simulates an LLM
    call failure/timeout (the single most likely production failure)."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def complete(self, *, system: str, user: str) -> str:
        raise self._exc


@pytest.mark.asyncio
async def test_llm_timeout_declines_loudly_does_not_propagate():
    # Spec §6: "Resolver LLM call ... times out → ... outcome=resolver_error."
    res = await AsideResolver(llm=_RaisingLLM(TimeoutError("upstream timed out"))).resolve(
        question="can I wade?", read_view=_view()
    )
    assert isinstance(res, AsideResolution)
    assert res.outcome == "resolver_error"
    assert res.grounded_on == ()
    assert res.answer  # non-empty loud "ask again" — never invents lore


@pytest.mark.asyncio
async def test_llm_connection_error_declines_loudly():
    # Spec §6: "Resolver LLM call fails ... → outcome=resolver_error."
    res = await AsideResolver(llm=_RaisingLLM(ConnectionError("connection reset by peer"))).resolve(
        question="how does Edge work?", read_view=_view()
    )
    assert res.outcome == "resolver_error"
    assert res.grounded_on == ()
    assert res.answer


@pytest.mark.asyncio
async def test_resolver_failure_emits_error_log(caplog):
    # Spec §6 mandates "+ ERROR-level log" on resolver failure so the GM
    # panel / ops can see it (CLAUDE.md OTEL principle). The resolver_error
    # path is currently silent — this fails RED until a logger.error lands.
    with caplog.at_level(logging.ERROR):
        res = await AsideResolver(llm=_RaisingLLM(TimeoutError("boom"))).resolve(
            question="anything", read_view=_view()
        )
    assert res.outcome == "resolver_error"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "spec §6 requires an ERROR-level log on resolver failure"
    assert any(
        "aside" in r.name.lower() or "aside" in r.getMessage().lower() for r in error_records
    ), "the ERROR log must be attributable to the aside resolver"


@pytest.mark.asyncio
async def test_malformed_output_still_logs_error(caplog):
    # The pre-existing parse-failure path must ALSO emit the spec §6
    # ERROR log (it returned resolver_error but silently before).
    with caplog.at_level(logging.ERROR):
        res = await AsideResolver(llm=_FakeLLM("not json at all")).resolve(
            question="anything", read_view=_view()
        )
    assert res.outcome == "resolver_error"
    assert [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "parse-failure resolver_error must also be logged at ERROR"
    )
