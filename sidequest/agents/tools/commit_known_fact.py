"""Tool: commit_known_fact — append a fact to the perspective PC's belief state.

Phase C Task 11 — ADR-100 (Journal Pipeline Coherence)
------------------------------------------------------
Replaces the ``journal_entries[]`` sidecar field on
``WorldStatePatch`` with an explicit tool call. The narrator now
*commits* facts one at a time, scoped to the perspective PC, with
real model fidelity for confidence and category.

Plan deviations
~~~~~~~~~~~~~~~
1. **Four-tier capitalised confidence, not three-tier lowercase.** The
   Phase C plan listed ``Literal["suspected", "known", "certain"]``.
   The real model on :class:`sidequest.game.character.KnownFact` is
   ``Literal["Certain", "Suspected", "Rumored", "Discovered"]`` —
   capitalised, four levels. Forwarding the plan's lowercase three-tier
   would silently drop ``Rumored`` and ``Discovered`` from the
   narrator's vocabulary, and the underlying ``KnownFact`` pydantic
   model would refuse the lowercase strings outright. The literal here
   matches the real model.
2. **Default confidence is ``"Discovered"``, not the plan's ``"known"``.**
   The plan's ``"known"`` does not map to a real tier; ``"Discovered"``
   is the tier minted by ``ScenarioClueIntake`` (per the ``KnownFact``
   class docstring), which is the closest analogue to a narrator-led
   commit.
3. **``category: FactCategory`` enum, not ``topic_tags: list[str]``.**
   The plan listed a free-form tag list. The real model carries a
   single :class:`~sidequest.protocol.models.FactCategory` enum
   (``Lore``/``Place``/``Person``/``Quest``/``Ability``). Forwarding
   ``topic_tags`` would either be silently dropped or require minting
   a parallel storage field. v1 swaps to ``category`` and keeps the
   model 1:1.

Scoping rule
~~~~~~~~~~~~
A fact must be attached to *a* PC. ``ctx.perspective_pc is None``
returns a fatal error rather than silently picking one. A perspective
PC that isn't in the snapshot returns ``not_found``.

OTEL
~~~~
The dispatch span (``tool.write.commit_known_fact``) is enriched with:
    * ``tool.belief.fact_id`` — the freshly-minted UUID hex.
    * ``tool.belief.confidence`` — the confidence tier.
    * ``tool.belief.category`` — the FactCategory enum value.
    * ``tool.belief.source`` — provenance label.

The plan's ``tool.belief.topic_tags`` attribute is replaced by
``tool.belief.category`` for the same reason as the args swap.

Concurrency
~~~~~~~~~~~
Sequential-per-session execution is provided by the Registry's
``_write_locks`` map — WRITE handlers don't need their own locking.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sidequest.agents.tool_registry import (
    ToolCategory,
    ToolContext,
    ToolResult,
    tool,
)
from sidequest.game.character import Character, KnownFact
from sidequest.protocol.models import FactCategory


class CommitKnownFactArgs(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        description="Fact content; stored verbatim as KnownFact.content.",
    )
    confidence: Literal["Rumored", "Suspected", "Discovered", "Certain"] = Field(
        default="Discovered",
        description=(
            "ADR-100 confidence ladder (Rumored < Suspected < Discovered < Certain). "
            "Default 'Discovered' matches the scenario-clue intake path. The plan's "
            "three-tier lowercase scale ('suspected'/'known'/'certain') does not "
            "map to the real model and is rejected."
        ),
    )
    source: str = Field(
        default="narrator",
        description="One-line provenance; stored as KnownFact.source.",
    )
    category: Literal["Lore", "Place", "Person", "Quest", "Ability"] = Field(
        default="Lore",
        description=(
            "FactCategory enum (Lore/Place/Person/Quest/Ability). The plan's "
            "'topic_tags: list[str]' was speculative — the real model carries "
            "a single enum, so v1 uses category."
        ),
    )


@tool(
    name="commit_known_fact",
    description=(
        "Commit a fact to the perspective PC's belief state. Use after a "
        "discovery, conversation, or clue resolution. Confidence is one of "
        "Rumored / Suspected / Discovered / Certain (default Discovered, "
        "the scenario-clue intake tier). Category is one of Lore / Place / "
        "Person / Quest / Ability (default Lore). Returns the freshly-minted "
        "fact_id; the fact is persisted to the perspective PC's "
        "known_facts list."
    ),
    category=ToolCategory.WRITE,
)
async def commit_known_fact(args: CommitKnownFactArgs, ctx: ToolContext) -> ToolResult:
    session = ctx.store.load()
    if session is None:
        return ToolResult.error("no active session", recoverable=False)

    if ctx.perspective_pc is None:
        return ToolResult.error(
            "commit_known_fact requires perspective_pc; cannot commit a fact "
            "to an unscoped narrator",
            recoverable=False,
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

    fact = KnownFact(
        content=args.text,
        confidence=args.confidence,
        source=args.source,
        learned_turn=ctx.turn_number,
        category=FactCategory(args.category),
    )
    pc.known_facts.append(fact)
    ctx.store.save(snapshot)

    ctx.otel_span.set_attribute("tool.belief.fact_id", fact.fact_id)
    ctx.otel_span.set_attribute("tool.belief.confidence", args.confidence)
    ctx.otel_span.set_attribute("tool.belief.category", args.category)
    ctx.otel_span.set_attribute("tool.belief.source", args.source)

    return ToolResult.ok(
        {
            "fact_id": fact.fact_id,
            "content": fact.content,
            "confidence": fact.confidence,
            "source": fact.source,
            "category": args.category,
            "learned_turn": fact.learned_turn,
            "perspective_pc": ctx.perspective_pc,
        }
    )
