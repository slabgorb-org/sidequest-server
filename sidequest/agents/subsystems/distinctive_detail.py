"""distinctive_detail — Live-path subsystem (story 59-7).

Wired onto the live turn path via Intent Router dispatch (ADR-113).
The bank executor calls this handler when the router emits a
``distinctive_detail_hint`` dispatch; the handler produces a narrator
directive instructing the narrator to name a referent by a distinctive
physical or environmental detail rather than by generic description.
"""

from __future__ import annotations

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch


async def run_distinctive_detail(dispatch: SubsystemDispatch) -> SubsystemOutput:
    """Emit a narrator directive naming the target referent by a distinctive detail.

    The decomposer prompt instructs the LLM to provide both ``target`` and
    ``hint``, but LLM compliance is best-effort. When either is missing we
    degrade to a no-op (empty directives + ``data["error"]``) instead of
    raising — the bank surfaces ``error`` as a span attribute, so OTEL
    still flags the bad dispatch without spewing TypeError into the
    orchestrator log every turn.
    """
    target = dispatch.params.get("target")
    hint = dispatch.params.get("hint")
    if not target:
        return SubsystemOutput(
            directives=[],
            data={"error": "missing_params.target"},
        )
    if not hint:
        return SubsystemOutput(
            directives=[],
            data={"error": "missing_params.hint", "target": target},
        )

    return SubsystemOutput(
        directives=[
            NarratorDirective(
                kind="distinctive_detail_for_referent",
                payload=f"name {target} by its distinctive detail: {hint}",
                visibility=dispatch.visibility,
            ),
        ],
        data={},
    )


__all__ = ["run_distinctive_detail"]
