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
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sidequest.agents.tooling_protocol import (
        CacheableBlock,
        ToolDefinition,
        ToolingLlmClient,
    )

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
        return _parse_resolution(raw)


def _parse_resolution(raw: str) -> AsideResolution:
    """Parse + validate the resolver's compact-JSON contract.

    Shared by both resolver paths (thin read-view Haiku and the
    narrator-cache path). Malformed output is the loud-but-degraded
    ``resolver_error`` outcome — No Silent Fallbacks: honest, never
    invents lore.
    """
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


# ---------------------------------------------------------------------------
# Narrator-cache path (playtest 2026-06-07 re-scope of ADR-107 grounding)
#
# The thin read-view above gives the resolver ~500 tokens of state — it
# cannot answer "what day is it", "who have I met", "what do I know so far".
# Instead of assembling a parallel game-state slice, the aside re-presents
# the NARRATOR's exact cached prompt (system blocks + tools + model, stashed
# by the orchestrator each SDK turn) with the OOC question as the user turn
# and ``tool_choice={"type":"none"}`` so it structurally cannot mutate
# anything. Cost ≈ one cache READ of the prefix (play itself keeps the 5m/1h
# blocks warm) + a short completion. The aside then knows EVERYTHING the
# narrator knows, with zero new state-assembly code.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsidePromptStash:
    """The narrator's exact SDK prompt artifacts, stashed per turn.

    Byte-identity matters: the cache key is exact bytes per model, so the
    stash holds REFERENCES to the same objects the narrator turn shipped —
    never a rebuild. ``None`` on the orchestrator until the first SDK turn.
    """

    system_blocks: list[CacheableBlock]
    tools: list[ToolDefinition]
    model: str
    # DRIVER verification failure 2026-06-07: the narrator's per-turn game
    # state rides the USER bucket (ADR-110 placement), NOT the system blocks
    # — riding the cache alone gave the aside the rulebook and zero state.
    # The stash therefore also carries the narrator turn's exact user message
    # (game-state sections + that turn's action) for re-presentation in the
    # aside's user turn. User-turn bytes are NOT part of the cached prefix,
    # so this costs plain input tokens and cannot bust the cache.
    user_state_text: str = ""
    # The calendar reaches the narrator only via the get_world_grounding
    # TOOL (conversation-side), so it is in NO block either way — and tools
    # are structurally forbidden on the aside. Carried explicitly (compact
    # JSON of the authored calendar; "" when the world authored none).
    calendar_summary: str = ""


_NARRATOR_CACHE_STATE_SECTION = """\
[NARRATOR TURN STATE — the game state exactly as the most recent narrated \
turn saw it; the trailing player action belongs to that PAST turn, not to \
this aside]
{user_state_text}

"""

_NARRATOR_CACHE_CALENDAR_SECTION = """\
[WORLD CALENDAR — authored calendar for this world]
{calendar_summary}

"""

_NARRATOR_CACHE_ASIDE_USER_TEMPLATE = """\
[OUT-OF-CHARACTER ASIDE — NOT A TURN]

The player is asking an out-of-character table-talk question. The fiction is \
FROZEN: do not narrate, do not advance the world, do not call tools. Answer \
as the GM at the table, grounding ONLY in the game state you already have in \
your context (the system prompt above, plus the NARRATOR TURN STATE and \
WORLD CALENDAR sections when present: world state, NPCs, calendar, journal, \
rules, recent narration).

ANSWER (outcome "answered") — 1-3 plain sentences, second-person GM voice — \
for capability/perception the character would already know, rules/genre \
mechanics, or recap of established events.

REFUSE with "You'd have to check — that's an action, not a question." \
(outcome "refused_hidden_state") for hidden world state the character has \
not earned (traps, unseen stats, behind unopened doors).

If answering honestly would require the world to change, outcome \
"refused_would_advance" and point back to the action box.

If your context genuinely does not pin the answer down, outcome \
"ungrounded_declined" and say so — never invent.

grounded_on MUST list the context areas you used (e.g. calendar, npcs, \
journal, rulebook, inventory, recent_narration). Empty only on a \
refusal/decline.

Respond ONLY as compact JSON: \
{{"answer": str, "outcome": str, "grounded_on": [str, ...]}}

PLAYER ASIDE: {question}"""


async def resolve_aside_on_narrator_cache(
    *,
    client: ToolingLlmClient,
    stash: AsidePromptStash,
    question: str,
    session_id: str,
) -> tuple[AsideResolution, int]:
    """Resolve an aside against the narrator's cached prompt prefix.

    Returns ``(resolution, cache_read_tokens)`` — the caller stamps
    ``cache_hit`` on the aside span from the token count (the lie-detector
    that the prefix actually came from cache; 0 on a warm-window miss means
    byte drift between the stash and the live narrator prompt).

    ``tool_choice={"type":"none"}`` presents the narrator's exact tools
    array (cache-prefix preservation) while forbidding tool calls — the
    aside structurally cannot mutate state (ADR-107 "no hands"). The
    ``session_id`` keys the spend into the ADR-134 per-session cumulative
    ceiling exactly like the narrator's own calls.
    """
    from sidequest.agents.tooling_protocol import Message

    # State grounding rides the USER turn (never the system blocks — those
    # are the byte-exact cache key). Empty sections are omitted entirely: an
    # empty [WORLD CALENDAR] header invites confabulation.
    parts: list[str] = []
    if stash.user_state_text.strip():
        parts.append(_NARRATOR_CACHE_STATE_SECTION.format(user_state_text=stash.user_state_text))
    if stash.calendar_summary.strip():
        parts.append(
            _NARRATOR_CACHE_CALENDAR_SECTION.format(calendar_summary=stash.calendar_summary)
        )
    parts.append(_NARRATOR_CACHE_ASIDE_USER_TEMPLATE.format(question=question))
    user = "".join(parts)
    try:
        result = await client.complete_with_tools(
            stash.system_blocks,
            [Message(role="user", content=user)],
            stash.tools,
            None,  # tool_dispatch — unreachable under tool_choice=none; loud if not
            model=stash.model,
            max_iterations=1,
            session_id=session_id,
            tool_choice={"type": "none"},
            caller="aside",
        )
    except Exception:  # noqa: BLE001 — LLM call-failure boundary (spec §6 parity)
        logger.error(
            "aside.resolver_error reason=llm_call_failed path=narrator_cache", exc_info=True
        )
        return (
            AsideResolution(
                answer=_RESOLVER_ERROR_ANSWER,
                outcome="resolver_error",
                grounded_on=(),
            ),
            0,
        )
    return _parse_resolution(result.text), result.cached_input_read_tokens
