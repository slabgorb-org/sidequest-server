"""reflect_absence — Live-path subsystem (story 59-7).

Wired onto the live turn path via Intent Router dispatch (ADR-113).
The bank executor calls this handler when the router emits a
``reflect_absence`` dispatch; the handler produces ``must_not_narrate``
and ``must_narrate`` directives forcing the narrator to acknowledge
absence honestly rather than inventing an NPC or off-screen responder.
"""

from __future__ import annotations

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch


async def run_reflect_absence(dispatch: SubsystemDispatch) -> SubsystemOutput:
    """Return directives forcing honest-absence narration."""
    tag = dispatch.visibility
    return SubsystemOutput(
        directives=[
            NarratorDirective(
                kind="must_not_narrate",
                payload="inventing an NPC follower or off-screen responder",
                visibility=tag,
            ),
            NarratorDirective(
                kind="must_narrate",
                payload="the empty room answering back — the absence itself is the scene",
                visibility=tag,
            ),
        ],
        data={},
    )


__all__ = ["run_reflect_absence"]
