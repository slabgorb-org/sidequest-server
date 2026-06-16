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
from sidequest.protocol.models import Footnote
from sidequest.server.dispatch.scenario_clue_intake import consume_clue_footnotes

logger = logging.getLogger(__name__)


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
        category=dispatch.params.get("category", "Lore"),
        is_new=True,
    )
    consume_clue_footnotes(snapshot, [footnote], active_character_name=player_name)
    return SubsystemOutput()


__all__ = ["run_scenario_clue_dispatch"]
