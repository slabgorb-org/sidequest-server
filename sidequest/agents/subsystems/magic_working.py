"""magic_working subsystem dispatch handler — Intent Router live engager
(Story 59-5, ADR-113).

The router classifies a player action and emits a ``DispatchPackage``
whose ``SubsystemDispatch`` entries may include
``subsystem="magic_working"`` with params carrying a full
``MagicWorking``-shaped dict (plugin, mechanism, actor, costs, domain,
narrator_basis). This handler is what the dispatch bank invokes for
that key — it engages the magic engine on the canonical snapshot BEFORE
the narrator runs, so the narrator sees already-applied magical state
instead of self-reporting it via the (retired) ``result.magic_working``
sidecar field.

No silent fallbacks: missing ``magic_state`` on the snapshot or an
unknown actor propagate as ``MagicWorkingParseError`` so the dispatch
bank records the error span and the watcher observes the engagement gap.
"""

from __future__ import annotations

import logging
from typing import Any

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.protocol.dispatch import SubsystemDispatch
from sidequest.server.narration_apply import (
    _apply_magic_status_promotions,
    apply_magic_working,
)

logger = logging.getLogger(__name__)


async def run_magic_working_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: Any = None,
    player_name: str = "",
) -> SubsystemOutput:
    """Engage magic on the canonical snapshot via ``apply_magic_working``.

    Reads ``dispatch.params`` as a ``MagicWorking``-shaped dict and
    delegates to the existing ``apply_magic_working`` parse-validate-apply
    seam. That function emits the ``magic.working`` OTEL span and publishes
    a watcher event, so the GM panel sees engagement on the new live path.

    After applying the working, any threshold crossings are promoted to
    character statuses via ``_apply_magic_status_promotions`` — the same
    chain the retired sidecar consumer ran.

    Raises ``MagicWorkingParseError`` on:
    - ``snapshot.magic_state is None`` (no magic config loaded)
    - ``dispatch.params`` failing ``MagicWorking`` pydantic validation
    - unknown actor (no instantiated character bars)
    """
    magic_result = apply_magic_working(snapshot=snapshot, patch_field=dict(dispatch.params))
    _apply_magic_status_promotions(
        snapshot=snapshot,
        magic_result=magic_result,
        player_name=player_name,
    )
    return SubsystemOutput()


__all__ = ["run_magic_working_dispatch"]
