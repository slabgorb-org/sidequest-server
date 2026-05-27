"""scenario_clue subsystem dispatch handler — Intent Router supplementing
engager (Story 59-6, ADR-113).

The router classifies a player action and emits a ``DispatchPackage``
whose ``SubsystemDispatch`` entries may include
``subsystem="scenario_clue"`` with params carrying ``fact_id``,
``summary``, and ``category``. This handler calls
``consume_clue_footnotes`` BEFORE the narrator runs, proactively
advancing prerequisite-eligible clues on investigation-shaped actions.

Unlike the confrontation (59-4) and magic_working (59-5) handlers,
this is a **supplementing** dispatch — the narrator-footnote path in
``scenario_clue_intake.py`` stays alive. Both paths converge on the
same ``ScenarioState.discover_clue`` call, which is idempotent:
re-discovery emits a duplicate-flagged span but does not double-mint
``KnownFact`` entries.
"""

from __future__ import annotations

import logging

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.protocol.models import FactCategory, Footnote
from sidequest.server.dispatch.scenario_clue_intake import consume_clue_footnotes

logger = logging.getLogger(__name__)


def _coerce_fact_category(raw: object) -> FactCategory:
    """Coerce a router-supplied, free-form ``category`` to a valid FactCategory.

    Unlike the ``commit_known_fact`` tool — whose ``category`` is constrained
    to the enum by the ADR-102 tool-use schema — the IntentRouter's
    scenario_clue ``params.category`` is free-form (the router prompt calls it
    "optional category" with no enum). A sloppy LLM value ("Object",
    "evidence", "tech") fed straight into ``Footnote(category=...)`` raises a
    pydantic enum ValidationError and crashes the WHOLE dispatch — the clue
    subsystem then produces nothing and the narrator improvises the finding,
    the exact Illusionism the OTEL lie-detector exists to catch (playtest
    2026-05-27, coyote_star turn 2).

    Clue discovery is keyed by ``fact_id``, not category, so an off-enum
    category must never block it. Match case-insensitively against the enum;
    on no match, fail LOUD via a WARNING log (not a silent swallow, not a
    crash) and default to ``Lore`` so discovery proceeds.
    """
    if isinstance(raw, FactCategory):
        return raw
    text = str(raw or "").strip()
    for member in FactCategory:
        if member.value.lower() == text.lower():
            return member
    logger.warning(
        "scenario_clue.category_coerced raw=%r -> Lore "
        "(router emitted an off-enum FactCategory; clue discovery proceeds)",
        text,
    )
    return FactCategory.Lore


async def run_scenario_clue_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    player_name: str = "",
) -> SubsystemOutput:
    """Advance prerequisite-eligible scenario clues via ``consume_clue_footnotes``.

    Builds a synthetic ``Footnote`` from ``dispatch.params`` and delegates
    to the existing ``consume_clue_footnotes`` seam. That function handles
    clue discovery (Seam A — fires ``SPAN_SCENARIO_ADVANCE``), KnownFact
    minting (Seam B), prerequisite gating, and duplicate protection.

    When ``snapshot.scenario_state is None`` (non-scenario world),
    ``consume_clue_footnotes`` returns silently and the dispatch-engagement
    watcher (59-3) flags the resulting mismatch.
    """
    footnote = Footnote(
        marker=0,
        fact_id=dispatch.params["fact_id"],
        summary=dispatch.params.get("summary", f"Clue {dispatch.params['fact_id']}"),
        category=_coerce_fact_category(dispatch.params.get("category", FactCategory.Lore)),
        is_new=True,
    )
    consume_clue_footnotes(snapshot, [footnote], active_character_name=player_name)
    return SubsystemOutput()


__all__ = ["run_scenario_clue_dispatch"]
