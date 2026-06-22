"""intent_router — Production live-path producer for the Intent Router
engagement spine (ADR-113).

The Intent Router is the pre-narrator pass that reads each submitted player
action plus a state summary, asks Haiku (via the Anthropic SDK) to infer
intent as confidence-scored advisory dispatches, and emits a
``DispatchPackage``. Downstream, ``run_dispatch_bank`` engages the matching
mechanical engines on the canonical snapshot BEFORE the narrator runs — so
the narrator narrates already-real state rather than self-reporting
engagement via unreliable sidecar fields.

Originally shipped 2026-04-23 as ``local_dm.py`` (the LocalDM decomposer,
``claude -p`` subprocess backed). Shelved 2026-04-28 because the subprocess
doubled per-turn latency. ADR-113 (2026-05-23) reverses the shelving on the
SDK path: Haiku via the Anthropic SDK is a cheap single API call sharing the
narrator's transport, so the producer is back on the live turn path. The
``claude -p`` legacy backend stays under ADR-013.

Failure policy: no silent fallbacks (memory rule
``feedback_no_fallbacks_hard`` + CLAUDE.md "No Silent Fallbacks"). On
timeout / transport error / unparseable / schema-invalid output, this
module emits an ERROR-level ``intent_router.failed`` OTEL span, attempts
ONE bounded retry, and raises :class:`IntentRouterFailure` if the retry
also fails. There is no degraded-shape ``DispatchPackage`` return path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from pydantic import ValidationError

from sidequest.agents.llm_factory import (
    _INTENT_ROUTER_MODEL,
    IntentRouterEmptyResponse,
)
from sidequest.agents.narrator_guardrails import CONFRONTATION_TRIGGER_CORE
from sidequest.protocol.dispatch import DispatchPackage
from sidequest.telemetry.spans.intent_router import (
    intent_router_action_rewrite_span,
    intent_router_decompose_span,
    intent_router_failed_span,
)

logger = logging.getLogger(__name__)

_RAW_PREVIEW_LIMIT = 160
_MAX_TOTAL_ATTEMPTS = 2  # First attempt + one bounded retry.


class IntentRouterFailure(Exception):
    """The Intent Router producer failed after the bounded retry.

    Raised explicitly so the orchestrator (wired in 59-4) can surface the
    failure rather than silently degrading to narrator-only continuation.
    Carries the human-readable reason of the LAST failed attempt.
    """


class IntentRouterLLM(Protocol):
    """Single-shot tool-use LLM contract the router consumes.

    Per ADR-102, structured output is produced through native tool-use:
    the adapter issues a forced ``tool_choice`` call and returns the
    ``tool_use`` block's already-structured ``input`` dict. There is no
    free-text JSON to parse — the ``unparseable`` failure mode is gone.
    Tests inject an ``AsyncMock`` returning a dict; the SDK-Haiku adapter
    in ``llm_factory.py`` is the live implementation.
    """

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


# ADR-102 tool-use contract: the router forces a single call to this tool
# whose input_schema IS the DispatchPackage schema, so Haiku returns
# structured input instead of fenced JSON.
_TOOL_NAME = "emit_dispatch_package"
_TOOL_DESCRIPTION = (
    "Emit the structured DispatchPackage for this single player action. "
    "Call this tool exactly once. Do not write prose."
)


def _dispatch_tool_schema() -> dict[str, Any]:
    """Build the tool input_schema from the DispatchPackage model.

    Reuses the same ``model_json_schema()`` path the tool registry uses
    (``tool_registry.py``) so the schema stays in lockstep with the model.
    """
    return DispatchPackage.model_json_schema()


FATE_ROUTING_RULES = """
## Fate action classification (only when <game_state> contains a "fate" block)

When — and ONLY when — the game state includes a top-level "fate" object, this is
a Fate Core pack. Classify a player's freeform action that engages the four-action
core into a single dispatch with subsystem "fate_action" and these params:
  - action: one of "overcome", "create_advantage", "attack", "concede"
      * attack — they try to harm/defeat an opponent (deals stress).
      * create_advantage — they set up a situation aspect (pin, distract, scout).
      * overcome — they push past an obstacle that isn't an opponent's defense.
      * concede — they choose to lose on their own terms (pre-roll, voluntary).
  - skill: the Fate skill used, chosen from the acting PC's skills in
    fate.skills[<character>]. Pick the single best-fitting skill name verbatim.
  - target: the opposed actor's name for an attack/create_advantage against a
    foe; null otherwise.
  - difficulty: the passive opposition rung (integer) when there is no opposed
    target; 0 when target is set.
  - invoke_aspect: the exact text of an aspect from fate.scene_aspects or
    fate.character_aspects[<character>] the player explicitly leverages for +2;
    "" if none.
  - aspect_text: for create_advantage, the short name of the situation aspect
    being placed (e.g. "Pinned Down"); "" otherwise.

Only emit a fate_action dispatch when fate.active_conflict is true. Do NOT emit
it for narration, movement, or table-talk. Do NOT emit a confrontation dispatch
for the same action — fate_action operates inside an already-active conflict.
"""


_SYSTEM_PROMPT = (
    """You are the Intent Router — an impartial structured-output reader.

Your job: read a player's action + the game state summary, then emit the
DispatchPackage by calling the ``emit_dispatch_package`` tool exactly once.
Never write prose. Put every field into the tool input.

For each player action:
  1. Emit subsystem dispatches keyed on the action's mechanical intent.
     Each dispatch carries a free-form ``params`` object. ``params`` is NOT a
     place to describe the action — it is the typed input the subsystem's
     handler reads. Emit exactly the keys listed; do not invent extra keys.
     Available subsystem keys and their required params:
       - confrontation: structured encounter (combat, negotiation, chase, etc.).
         params={"type": "<one of game_state.confrontation_types[].type>",
                 "opponent": {"name": "<the adversary>", "description": "<one clause>"}}.
         Choose the single type whose category fits the action (a physical
         contest → a combat-category type; a parley → a social-category type;
         a flee/pursue → a movement-category type). The type MUST be one of the
         values listed in game_state.confrontation_types — never invent a type
         and never describe the action here instead of naming the type.
         ALWAYS name the Other in params["opponent"] — the single adversary the
         contest targets (the person grabbed, the NPC threatened, the pilot
         pursued). A confrontation REQUIRES an Other (ADR-116): the engine seats
         this opponent, and when they are named only in the fiction and are not
         yet a tracked NPC it materializes them FROM this field. Omitting it
         when an adversary exists is the failure mode that collapses a real
         contest into prose — the encounter never starts and no dice roll. Use
         the resolved referent's name; if the action is genuinely one-sided
         (no adversary — forcing a locked door, steadying a fall) omit opponent.
         Recognise a stake-binding engagement and emit the confrontation
         dispatch on the SAME turn its trigger appears in the fiction. The
         recognition rules below (shared verbatim with the narrator's
         game_patch steering — story 61-18 / ADR-111) name which fictional
         beats are real triggers; the type names they cite are illustrative,
         so always pick from game_state.confrontation_types:
"""
    + CONFRONTATION_TRIGGER_CORE
    + """       - magic_working: spell or magical ability usage. When the player
         NAMES a specific spell ("I cast foundation_of_flame", "I work the
         Foundation of Flame"), params MUST carry
         {"actor": "<the casting character's name>",
          "spell": "<the spell exactly as the player named it>"} —
         the engine resolves the name against the world's spell catalog and
         the caster's prepared list; never resolve, rename, or invent the
         spell yourself. An explicit named cast is an unambiguous mechanical
         intent: score its confidence HIGH. For an unnamed/ambient working
         (pact-working worlds), params is a MagicWorking-shaped object
         (the spell/effect fields) and MUST still include "actor".
       - scenario_clue: clue/evidence discovery. params={"fact_id": "<id>"}
         (optional "summary", "category").
       - npc_agency: NPC reacts per role and disposition.
         params={"npc_name": "<name>"} (optional "situation").
       - distinctive_detail_hint: name a referent by its distinctive detail.
         params={"target": "<entity id>", "hint": "<detail>"}.
       - movement: the party physically relocates between dungeon regions
         (descend, ascend, go through an exit, retreat). params={
           "direction": "<one of: deeper | back | toward_exit>",
           "exit_descriptor": "<the way the player named, IN THEIR OWN
                               WORDS, e.g. 'the iron stair', 'the crack
                               in the east wall', 'south'>"
         }.
         Emit movement ONLY for genuine region relocation, not look-around /
         search / examine. NEVER emit a region id — you do not know the
         graph. Describe WHICH exit by exit_descriptor only; the engine
         resolves it.
         Confidence scores WHETHER the player intends to relocate — NOT
         whether you can map their words onto a listed exit. "I go
         south", "I head through the archway", "I press on" are
         unambiguous relocation: score them HIGH and pass the player's
         own words (even a compass direction) through exit_descriptor
         verbatim. The engine matches the descriptor against the real
         exits and refuses honestly when nothing matches — that loud
         refusal is the correct outcome for an unmappable way; a
         low-confidence dispatch is not, because it degrades to prose
         and the move silently becomes fiction.
         When game_state.current_region_exits is present it lists the
         REAL exits from where the party stands; an action that takes,
         descends, or follows one of them IS movement — name it in
         exit_descriptor. A "seam" exit is the threshold between the surface
         and the underworld. From the surface, crossing it goes DOWN —
         direction "deeper". From the dungeon entrance, the seam exit named in
         current_region_exits leads back UP to the surface — climbing or
         heading back out it is direction "back" (or "toward_exit").
         Exits of kind "corridor", "stairs", "shaft", or "chute" are
         passages WITHIN the underworld: pressing on, descending, or
         heading through one IS movement (direction "deeper" to push on
         down, "back" to retreat the way the party came).
       - reflect_absence: player addresses someone/something not present.
       - witnessed_act: the player commits an EARNED, PUBLIC act that
         contradicts a belief-powered authority or shows a cowed population
         that defiance survives (pull the curtain on a humbug, name the trick
         to the authority's face, break a forbidden rule and walk away unharmed,
         help a population take a first collective refusal). params={
           "act_id": "<one of game_state.witnessed_act_vocabulary[].id>",
           "witnesses": ["<names drawn ONLY from game_state.present_npcs>"]
         }.
         Emit this ONLY when game_state.witnessed_act_vocabulary is present.
         The act_id MUST be one of the listed vocabulary ids — never invent an
         act. witnesses are the people who PERCEIVE the act; populate it only
         from game_state.present_npcs. An act with NO witness moves nothing
         (exposing a humbug in an empty room changes nothing): if no one present
         perceives it, emit an empty witnesses list and a LOW confidence. Do not
         invent a witness who is not in present_npcs. Reshaping a society is
         earned — score confidence honestly; a low score degrades to a narrator
         hint instead of moving the political dials.
       - equip: the player puts on, wears, dons, laces on, straps on, draws, or
         wields a carried item, OR takes off / removes / sheathes one. params={
           "item": "<the item as the player named it — a name or id of an item
                    the character already carries>",
           "action": "<one of: equip | unequip>"
         }.
         Emit equip ONLY for genuinely wearing/wielding or removing a carried
         item (lace on the shoes, draw the sword, don the cloak, take off the
         armor) — NOT for using/consuming an item, picking one up, or dropping
         it. action defaults to equip; use unequip for take-off/remove/sheathe.
         The item MUST be one the character already carries; name it as the
         player named it and the engine resolves it against the inventory.
       - environment_clock: the player deliberately LIGHTS a fresh torch (or
         lantern) to push back the dark — "I light a torch", "I spark a fresh
         torch", "I relight the lantern". params={
           "mode": "relight",
           "character_name": "<the acting PC's name>"
         }.
         Emit environment_clock with {"mode": "relight"} ONLY for a deliberate
         relight action (lighting/sparking a torch or lantern to restore light).
         The engine consumes one carried light-source charge, refills the light
         pool, and clears the darkness penalty — so name the acting PC in
         character_name. Do NOT emit this for moving through the dark, searching,
         or merely holding a lit torch (the per-turn light burn is handled
         automatically by the engine, not by you). A relight is a deliberate
         intent: score its confidence on how clearly the player chose to light a
         torch — do not force it.
       - quest_offer: the player ACCEPTS or DECLINES a job/offer that the world
         has put on the table. params={
           "quest_id": "<one of game_state.pending_quest_offers[].quest_id>",
           "decision": "<one of: accept | decline>"
         }.
         Emit quest_offer ONLY when game_state.pending_quest_offers is present
         and non-empty — it lists the named offers currently on the table, each
         with its quest_id, title, and the giver who made it. Read the player's
         words against the SPECIFIC offer they answer: "yeah, alright, I'll look
         into it" / "I take the job" / "where do I start" / "I'm in" all ACCEPT;
         "not interested" / "I'll pass" DECLINE. Name the quest_id of the offer
         the answer addresses — do NOT invent a quest_id that is not in
         pending_quest_offers. This is intent classification, not a keyword
         match: a clear yes scores HIGH and mints the quest; an ambiguous turn
         scores LOW and degrades to a narrator hint, leaving the offer live for a
         clearer re-accept. When several offers are pending, use the title/giver
         context to pick which one the player's "yes" answers.
       - course: the party plots a course / burns / sets out to travel to a
         specific celestial body (a planet, moon, station, or gate). params={
           "destination": "<the body the player named — a body id surfaced in the
                           game_state <courses> block / orbital chart>"
         }.
         Emit course ONLY when the world has an orbital tier (a <courses> block is
         present in game_state) AND the player declares travel to a NAMED body
         ("we burn for the Red Prospect", "set a course for Tethys Watch", "jump
         to the Gate", "head for the inner moon"). The engine computes the ETA and
         Δv, plots the course, and advances the story clock by the travel time —
         so this is travel, NOT looking at the chart, drilling the map, or asking
         where something is. Name the destination as the body the player chose;
         the engine resolves it against the world's bodies and refuses an unknown
         body LOUDLY. A clear declaration of travel to a named body scores HIGH; a
         vague "let's get moving" with no named destination scores LOW.
       - dogfight: SHIP-vs-SHIP / fighter combat — the player's ship or fighter
         engages a hostile contact, vessel, or enemy pilot (paint/lock a contact
         with targeting radar, bring the ship to combat attitude with weapons hot,
         intercept/pursue/engage another vessel, open fire on a ship). params={
           "opponent": {"name": "<the hostile ship/pilot/contact>",
                        "description": "<one clause>"}
         }.
         Emit dogfight (NOT confrontation) for VEHICLE/ship combat in space — the
         ADR-077 sealed-letter dogfight engine seats the duel. Use confrontation
         for PERSONAL combat (a corridor shoot-out, a brawl). A dogfight REQUIRES
         an Other (ADR-116): ALWAYS name the hostile vessel/pilot in
         params["opponent"] — the contact painted, the ship pursued — even when it
         was only just named in the fiction; the engine materializes it as the
         opponent. Omitting it collapses a real ship fight into prose (the engine
         never starts, no dice). An unambiguous ship-combat action ("full lock,
         weapons hot, engage the contact") scores HIGH; a vague "keep an eye on
         that contact" with no engagement scores LOW.
     Every dispatch carries a per-dispatch confidence (0.0-1.0): how certain you
     are that THIS specific mechanical engagement is what the player intended.
     Score the confidence for each dispatch honestly — a high score fires the
     engine, a low score degrades the dispatch to a narrator hint instead of
     engaging. Do not inflate confidence to force engagement.
  2. Emit narrator_instructions — advisory directives to the narrator. Each
     item is EXACTLY {kind, payload, visibility} and NOTHING else. Do NOT add
     any other key (e.g. "target"); the schema forbids unknown fields and one
     stray key REJECTS THE ENTIRE package, dropping every dispatch this turn.
     kind is one of: must_narrate / must_not_narrate /
     distinctive_detail_for_referent / canonical_only_do_not_reveal_to_others.
     For distinctive_detail_for_referent, put BOTH the referent and its detail
     inside ``payload`` (e.g. "the goblin: broken tooth") — there is no separate
     referent/target field here. (The ``target`` key belongs ONLY to the
     distinctive_detail_hint DISPATCH in step 1 — a different mechanism; do not
     carry it into narrator_instructions.)
  3. Set confidence_global to your overall confidence across the turn.
  4. Emit action_rewrite — rewrite the player's raw action into two
     perspectives: {"named": "<third-person with the acting character's name>",
     "intent": "<neutral distilled intent, no pronouns>"}. "I draw my sword" →
     {"named": "Kael draws their sword", "intent": "draw sword"}. Derive it from
     the raw action ALONE — no prose is needed. Emit it on every turn a character
     acts; omit only for pure atmosphere with no actor.

Visibility is server-defaulted to "all". Emit a visibility tag on a dispatch or
directive ONLY when an action is genuinely secret (seen by some PCs and not
others); otherwise omit it entirely.

Pydantic rejects unknown fields. Stay inside the schema. Emit everything
through the tool input — no preamble, no commentary, no extra text blocks."""
    + FATE_ROUTING_RULES
)


def _serialize_state_summary(state_summary: Any) -> str:
    """Serialize ``state_summary`` exactly as it lands in the user prompt.

    A string summary passes through verbatim; anything else is JSON-encoded
    (sorted keys, ``default=str``). This is the single source of truth for both
    the prompt body and the ``state_summary_bytes`` attribution attribute
    (Story 71-40) so the recorded size matches the bytes actually sent.
    """
    if isinstance(state_summary, str):
        return state_summary
    return json.dumps(state_summary, default=str, sort_keys=True)


def _build_user_prompt(action: str, state_summary: Any) -> str:
    state_text = _serialize_state_summary(state_summary)
    return (
        f"<game_state>\n{state_text}\n</game_state>\n"
        f"<raw_action>\n{action}\n</raw_action>\n"
        f"Call emit_dispatch_package once for this single action."
    )


def _schema_correction_suffix(validation_error: str) -> str:
    """Build the retry correction block fed back after a schema rejection.

    The bare retry (re-sending the identical prompt) cannot fix a *deterministic*
    schema confusion — the model reproduces the same malformed shape and the
    whole DispatchPackage is lost a second time. Feeding the pydantic error back
    lets the producer self-correct (drop the offending key / fix the field) on
    the bounded retry, so a single hallucinated advisory key no longer sinks the
    turn's mechanical dispatch. Still fail-loud: if the informed retry also
    fails, ``decompose`` raises (No Silent Fallbacks)."""
    return (
        "\n\n<schema_correction>\n"
        "Your previous emit_dispatch_package input was REJECTED by schema "
        "validation:\n"
        f"{validation_error}\n"
        "Emit the SAME intent again, strictly inside the DispatchPackage schema. "
        "Remove every field the schema does not define — Pydantic forbids unknown "
        "keys, and one stray key drops the entire package. In particular, a "
        "narrator_instructions item has ONLY {kind, payload, visibility}; never "
        "add a 'target' key there — fold any referent id into 'payload'.\n"
        "</schema_correction>"
    )


def _count_dispatches(pkg: DispatchPackage) -> int:
    count = sum(len(pd.dispatch) for pd in pkg.per_player)
    count += sum(len(ca.dispatch) for ca in pkg.cross_player)
    return count


class IntentRouter:
    """Live-path Intent Router producer.

    Constructor takes an injected :class:`IntentRouterLLM` so the SDK
    backend can be swapped (ADR-073 future: local fine-tuned model)
    without rewriting the router. Tests inject mocks; the live path uses
    :func:`sidequest.agents.llm_factory.build_intent_router_llm`.

    The class is stateless — every ``decompose`` call sends the full
    system prompt and a fresh user prompt. There is no session id, no
    persistent context across turns.
    """

    def __init__(self, *, llm: IntentRouterLLM) -> None:
        self._llm = llm
        # Story 91-2 (epic 91 "Dark Spend"): SDK round-trips spent by the most
        # recent ``decompose`` call — first attempt plus any bounded retry,
        # counting attempts that raised (a timed-out call is still a spent
        # round-trip). The pre-narrator pass reads this after ``decompose``
        # returns to enforce ``INTENT_ROUTER_CALL_BUDGET_PER_TURN``; a counter
        # that only saw decompose invocations would be structurally blind to a
        # retry storm (the [COST-1] "steady 2x floor" suspect).
        self.sdk_round_trips_last_decompose: int = 0

    async def decompose(
        self,
        *,
        action: str,
        state_summary: Any,
    ) -> DispatchPackage:
        """Decompose a player action into a :class:`DispatchPackage`.

        Raises :class:`IntentRouterFailure` if both the initial attempt
        and the bounded retry fail. There is no degraded-shape return
        path: failure surfaces as an exception (per ADR-113 and memory
        rule ``feedback_no_fallbacks_hard``).
        """
        base_user_prompt = _build_user_prompt(action, state_summary)
        tool_schema = _dispatch_tool_schema()
        action_length = len(action)
        # AC2 (code suspect): the serialized state-summary size — the prime
        # candidate for the 4-12s decompose blowup is an oversized prompt
        # inflating input tokens every call. Recorded as the sibling of
        # ``action_length``. Measured from the exact serialization the prompt
        # uses so the byte count matches what is actually sent.
        state_summary_bytes = len(_serialize_state_summary(state_summary).encode("utf-8"))
        start_ns = time.perf_counter_ns()
        # AC2 (env cost): the raw SDK round-trip of the SUCCESSFUL ``emit_tool``
        # call, timed independently of the surrounding retry/validation
        # bookkeeping. A component of the total ``latency_ms``, so the
        # ``sdk_latency_ms <= latency_ms`` invariant holds by construction.
        sdk_latency_ms = 0
        last_failure: tuple[str, str] | None = None
        # When the prior attempt was rejected by DispatchPackage validation,
        # carry the pydantic error into the retry prompt so the producer can
        # self-correct instead of reproducing the same malformed shape.
        last_schema_error: str | None = None

        self.sdk_round_trips_last_decompose = 0
        for attempt_index in range(_MAX_TOTAL_ATTEMPTS):
            retry_count = attempt_index  # 0 on first try, 1 on retry.
            user_prompt = base_user_prompt
            if last_schema_error is not None:
                user_prompt += _schema_correction_suffix(last_schema_error)
            try:
                # Counted BEFORE the call so an attempt that raises (timeout,
                # transport) is still a spent round-trip (Story 91-2 budget).
                self.sdk_round_trips_last_decompose += 1
                sdk_start_ns = time.perf_counter_ns()
                tool_input = await self._llm.emit_tool(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    tool_name=_TOOL_NAME,
                    tool_description=_TOOL_DESCRIPTION,
                    tool_schema=tool_schema,
                )
                sdk_latency_ms = max(0, (time.perf_counter_ns() - sdk_start_ns) // 1_000_000)
            except TimeoutError as exc:
                last_failure = ("timeout", str(exc))
                _emit_failed_span(
                    reason="timeout",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=timeout attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue
            except IntentRouterEmptyResponse as exc:
                # SDK call succeeded but Haiku emitted no tool_use block —
                # distinct from transport failure and from a schema-invalid
                # tool input. Preserve the diagnostic message (stop_reason,
                # content blocks, usage) in raw_preview so the GM panel can
                # see why.
                last_failure = ("empty_response", str(exc))
                _emit_failed_span(
                    reason="empty_response",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=empty_response attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue
            except Exception as exc:  # noqa: BLE001 — transport boundary, see ADR-113 §5
                last_failure = ("transport", str(exc))
                _emit_failed_span(
                    reason="transport",
                    raw_preview=str(exc)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=transport attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            try:
                pkg = DispatchPackage.model_validate(tool_input)
            except ValidationError as exc:
                last_failure = ("schema_invalid", type(exc).__name__)
                # Feed the concrete error into the next attempt's prompt.
                last_schema_error = str(exc)
                _emit_failed_span(
                    reason="schema_invalid",
                    raw_preview=str(tool_input)[:_RAW_PREVIEW_LIMIT],
                    retry_count=retry_count,
                )
                logger.warning(
                    "intent_router.failed reason=schema_invalid attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            latency_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
            with intent_router_decompose_span(
                action_length=action_length,
                model=_INTENT_ROUTER_MODEL,
            ) as span:
                span.set_attribute("dispatch_count", _count_dispatches(pkg))
                span.set_attribute("latency_ms", int(latency_ms))
                # AC2 env-vs-code attribution split (Story 71-40): the GM panel
                # can now tell whether the over-budget decompose is the raw SDK
                # round-trip (env) or an oversized state-summary prompt (code).
                span.set_attribute("sdk_latency_ms", int(sdk_latency_ms))
                span.set_attribute("state_summary_bytes", int(state_summary_bytes))
                span.set_attribute("retry_count", retry_count)
                span.set_attribute("confidence_global", float(pkg.confidence_global))
                # True when this success came from a schema-error-informed
                # retry — the GM panel can see the self-heal that saved the
                # turn's dispatch from a malformed first attempt.
                span.set_attribute("schema_corrected", last_schema_error is not None)
                # Happy-path decompose spans are never degraded (Story 71-29):
                # the degrade path emits its own decompose span with degraded=True
                # from the caller. Setting it explicitly here keeps the attribute
                # present on every decompose span so the GM panel can filter.
                span.set_attribute("degraded", False)

            # Story 151-3 (ADR-150 step 3): the pre-pass now PRODUCES the
            # player-action rewrite (named/intent). Emit the GM-panel
            # lie-detector span on every successful decompose — emitted=False is
            # the loud net when the producer omitted it (the omitted→default
            # fallback is the transition safety, never a silent skip).
            ar = pkg.action_rewrite
            ar_emitted = bool(ar and (ar.named or ar.intent))
            with intent_router_action_rewrite_span(
                emitted=ar_emitted,
                intent=(ar.intent if ar is not None else ""),
            ):
                pass
            return pkg

        assert last_failure is not None  # _MAX_TOTAL_ATTEMPTS >= 1
        reason, detail = last_failure
        raise IntentRouterFailure(f"intent_router producer failed after retry: {reason} ({detail})")


def _emit_failed_span(*, reason: str, raw_preview: str, retry_count: int) -> None:
    """Open and immediately close the ERROR-level failed span.

    The span captures one attempt's failure. Using the context manager
    inline keeps the call site terse — there is no in-attempt work to
    perform inside the span body.
    """
    with intent_router_failed_span(
        reason=reason,
        raw_preview=raw_preview,
        retry_count=retry_count,
    ):
        pass


__all__ = [
    "IntentRouter",
    "IntentRouterFailure",
    "IntentRouterLLM",
]
