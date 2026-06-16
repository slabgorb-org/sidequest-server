"""Tool: query_known_facts — fetch the perspective PC's belief state.

Phase C Task 10 — ADR-100 (Journal Pipeline Coherence)
------------------------------------------------------
Replaces ad-hoc ``known_facts`` blocks dumped into the narrator prompt
with an on-demand tool call. The narrator can ask for *only the facts
that matter to this turn* — filtered by content substring and/or by a
confidence floor — instead of receiving the full belief log every
turn.

Scoping rule
~~~~~~~~~~~~
**Only the perspective PC's facts are ever returned.** Cross-PC belief
leakage is impossible at this layer — the handler reads
``ctx.perspective_pc`` directly, finds that ``Character`` in the
session snapshot, and walks only their ``known_facts`` list. No
perception rule is registered for this tool because there is nothing
to coarsen post-hoc: the wrong-PC payload never gets constructed.

Plan deviations
~~~~~~~~~~~~~~~
1. **Four-tier confidence, not three.** The Phase C plan listed
   ``Literal["suspected", "known", "certain"]`` (lowercase, three
   levels). The real model on
   :class:`sidequest.game.character.KnownFact` is
   ``Literal["Certain", "Suspected", "Rumored", "Discovered"]`` —
   capitalised, four levels. Forwarding the plan's three-tier scale
   would silently exclude every ``Rumored`` and ``Discovered`` fact in
   every save file. Final literal here matches the real model.
2. **``confidence_min`` defaults to ``"Rumored"``.** The floor is the
   *minimum* tier returned, so the default returns everything. The
   ordering ``Rumored < Suspected < Discovered < Certain`` matches
   ADR-100's intent (clue-graph evidence climbs that ladder).
3. **Added a ``limit`` arg (default 20, max 100).** ``known_facts``
   accumulates monotonically — long sessions can register hundreds of
   facts. An unbounded dump would re-bloat the prompt window we just
   removed by going on-demand. The narrator can call again with a
   tighter ``topic`` if the truncation matters.

Perspective handling
~~~~~~~~~~~~~~~~~~~~
* ``perspective_pc is None`` — return empty list (no error). Pre-
  chargen / GM-mode contexts have no PC belief state; the narrator
  shouldn't fabricate one.
* ``perspective_pc`` set but not in ``session.snapshot.characters`` —
  ``ToolResult.not_found``. The caller passed a PC name that doesn't
  exist; that's a wiring bug, not a "play through it" condition.

OTEL
~~~~
The dispatch span (``tool.read.query_known_facts``) is enriched with:
    * ``tool.belief.fact_count`` — number of facts in the response
      (post-filter, post-limit).
    * ``tool.belief.topic`` — the substring filter (empty string if
      none).

The GM panel uses these to detect "narrator quoted a fact but tool
returned zero matches" — a sign the narrator is improvising belief.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.character import Character

# Order matches ADR-100 belief ladder. Higher = more solid evidence.
_CONFIDENCE_ORDER: dict[str, int] = {
    "Rumored": 0,
    "Suspected": 1,
    "Discovered": 2,
    "Certain": 3,
}

_ConfidenceTier = Literal["Rumored", "Suspected", "Discovered", "Certain"]


class QueryKnownFactsArgs(BaseModel):
    topic: str | None = Field(
        default=None,
        description=("Case-insensitive substring matched against fact content. None = no filter."),
    )
    confidence_min: _ConfidenceTier = Field(
        default="Rumored",
        description=(
            "Confidence floor; only facts at or above this tier are returned. "
            "Order: Rumored < Suspected < Discovered < Certain. Default "
            "'Rumored' returns everything."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Maximum facts to return — known_facts accumulates monotonically "
            "and an unbounded dump would bloat the prompt."
        ),
    )


def _serialize_fact(fact: Any) -> dict[str, Any]:
    """Project a KnownFact into the narrow JSON shape the narrator sees."""
    category = fact.category
    # FactCategory is a StrEnum; .value is the str, but be defensive in case
    # the model ever swaps to a plain str.
    category_str = category.value if hasattr(category, "value") else str(category)
    return {
        "fact_id": fact.fact_id,
        "content": fact.content,
        "confidence": fact.confidence,
        "source": fact.source,
        "learned_turn": fact.learned_turn,
        "category": category_str,
    }


@tool(
    name="query_known_facts",
    description=(
        "Return facts the perspective PC has registered as known, suspected, "
        "rumored, or discovered. Filter by `topic` (case-insensitive substring) "
        "and/or `confidence_min` (one of Rumored, Suspected, Discovered, "
        "Certain — default Rumored returns everything). `limit` caps result "
        "size (default 20, max 100). Only the perspective PC's facts are ever "
        "returned; another PC's beliefs are not accessible through this tool."
    ),
    category=ToolCategory.READ,
)
async def query_known_facts(args: QueryKnownFactsArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    # Always set the topic attribute — empty string if no filter, so the
    # GM panel sees a consistent shape.
    ctx.otel_span.set_attribute("tool.belief.topic", args.topic or "")

    if ctx.perspective_pc is None:
        # Plan: "ignore the model's request if perspective_pc is None".
        # Translated to: empty list, count=0. The narrator gets a
        # well-formed empty response instead of an error — pre-chargen
        # and GM-mode contexts have no PC belief state to ship.
        ctx.otel_span.set_attribute("tool.belief.fact_count", 0)
        return ToolResult.ok(
            {
                "facts": [],
                "perspective_pc": None,
                "confidence_min": args.confidence_min,
            }
        )

    snapshot = session.snapshot
    pc: Character | None = next(
        (c for c in snapshot.characters if c.core.name == ctx.perspective_pc),
        None,
    )
    if pc is None:
        return ToolResult.not_found(
            f"perspective_pc not in session.characters: {ctx.perspective_pc!r}"
        )

    floor = _CONFIDENCE_ORDER[args.confidence_min]
    topic_lc = args.topic.lower() if args.topic else None

    matched: list[dict[str, Any]] = []
    for fact in pc.known_facts:
        if _CONFIDENCE_ORDER[fact.confidence] < floor:
            continue
        if topic_lc is not None and topic_lc not in fact.content.lower():
            continue
        matched.append(_serialize_fact(fact))
        if len(matched) >= args.limit:
            break

    ctx.otel_span.set_attribute("tool.belief.fact_count", len(matched))

    return ToolResult.ok(
        {
            "facts": matched,
            "perspective_pc": ctx.perspective_pc,
            "confidence_min": args.confidence_min,
        }
    )
