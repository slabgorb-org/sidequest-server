"""Read-only out-of-band aside resolver (ADR-107).

A GM ruling, not a story beat. Receives a *read* view of state and returns
a short OOC answer. It holds no write path — it structurally cannot advance
the world, mutate inventory, tick tropes, or touch the dungeon. "No turn
consumed" is enforced by this object having no hands.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

_RESOLVER_ERROR_ANSWER = "(The GM didn't catch that — ask again.)"

_VALID_OUTCOMES = {
    "answered",
    "refused_hidden_state",
    "refused_would_advance",
    "ungrounded_declined",
    "resolver_error",
}

_SYSTEM_PROMPT = """You are the GM answering a player's OUT-OF-CHARACTER aside \
during a tabletop session. This is table-talk, not narration. The fiction is \
FROZEN — nothing you say moves the world.

ANSWER (outcome answered) — 1-3 plain sentences, second-person GM voice. \
When you answer the question, set outcome to "answered":
- Capability/perception the character would already know (size, encumbrance, \
stated depth, what they can see/reach).
- Rules/genre mechanics from the rulebook summary.
- Recap from the recent narration / inventory.

REFUSE by saying "You'd have to check — that's an action, not a question." \
(outcome refused_hidden_state) for hidden world state: traps, unseen creature \
stats, what's behind an unopened door, anything the character has not earned.

If answering honestly would require the world to change, outcome \
refused_would_advance and point back to the action box.

If the provided state does not contain the answer, outcome \
ungrounded_declined and say the game doesn't pin it down — never invent.

grounded_on MUST list the state keys you used (e.g. character.size, \
region.water_depth, rulebook, inventory, recent_narration). Empty only on a \
refusal/decline.

Respond ONLY as compact JSON: \
{"answer": str, "outcome": str, "grounded_on": [str, ...]}"""


@dataclass(frozen=True)
class AsideReadView:
    """Immutable read slice handed to the resolver. No setters, no handles."""

    character_summary: str
    region_summary: str
    inventory: list[str]
    rulebook_summary: str
    recent_narration: str


@dataclass(frozen=True)
class AsideResolution:
    answer: str
    outcome: str
    grounded_on: tuple[str, ...]


_FENCE_RE = re.compile(r"```(?:json)?\s*\n([\s\S]*?)\n```")


def _extract_json(raw: str) -> str:
    """Strip markdown code fences if present, returning the inner JSON."""
    m = _FENCE_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.strip()


class AsideLLM(Protocol):
    async def complete(self, *, system: str, user: str) -> str: ...


class AsideResolver:
    def __init__(self, llm: AsideLLM) -> None:
        self._llm = llm

    async def resolve(self, *, question: str, read_view: AsideReadView) -> AsideResolution:
        user = (
            f"CHARACTER: {read_view.character_summary}\n"
            f"REGION: {read_view.region_summary}\n"
            f"INVENTORY: {', '.join(read_view.inventory) or '(none)'}\n"
            f"RULEBOOK: {read_view.rulebook_summary}\n"
            f"RECENT: {read_view.recent_narration}\n\n"
            f"PLAYER ASIDE: {question}"
        )
        # I/O boundary (spec §6: "Resolver LLM call fails/times out →
        # outcome=resolver_error + ERROR-level log. No turn is lost.").
        # The resolver is decoupled from the concrete LLM behind
        # ``AsideLLM`` (Protocol) — it deliberately does NOT import
        # anthropic, so it cannot enumerate ``anthropic.APITimeoutError``
        # etc. The correct decoupled realization is a broad catch scoped
        # to the *single external call only* (timeout / connection / API
        # error / any backend failure). This is NOT the "bare except over
        # the whole method" anti-pattern: resolver-logic errors live in
        # the second block below, which keeps its precise typed except so
        # a genuine programming bug still surfaces loudly.
        try:
            raw = await self._llm.complete(system=_SYSTEM_PROMPT, user=user)
        except Exception:  # noqa: BLE001 — LLM call-failure boundary (spec §6)
            logger.error("aside.resolver_error reason=llm_call_failed", exc_info=True)
            return AsideResolution(
                answer=_RESOLVER_ERROR_ANSWER,
                outcome="resolver_error",
                grounded_on=(),
            )
        try:
            data = json.loads(_extract_json(raw))
            outcome = str(data.get("outcome", ""))
            if outcome not in _VALID_OUTCOMES:
                raise ValueError(f"invalid outcome {outcome!r}")
            grounded = tuple(str(g) for g in data.get("grounded_on", []))
            answer = str(data.get("answer", "")).strip()
            if not answer:
                raise ValueError("empty answer")
            return AsideResolution(answer=answer, outcome=outcome, grounded_on=grounded)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            # No Silent Fallbacks: loud, honest, never invents lore.
            # Narrow + precise so a real programming bug (e.g.
            # AttributeError) still propagates instead of masquerading
            # as a resolver_error (Reviewer RT1 guidance).
            logger.error(
                "aside.resolver_error reason=malformed_output raw=%r",
                (raw or "")[:200],
                exc_info=True,
            )
            return AsideResolution(
                answer=_RESOLVER_ERROR_ANSWER,
                outcome="resolver_error",
                grounded_on=(),
            )
