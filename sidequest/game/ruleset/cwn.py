"""CwnRulesetModule — Cities Without Number resolution behind the seam.

CWN (Sine Nomine, CC0) shares the "Without Number" resolution engine, so this
reparents directly onto ``WithoutNumberRulesetModule`` (ADR-142) and inherits
attack/skill/save/initiative/damage AND the lethality layer (Luck save, Shock,
Trauma, System Strain, Mortal/Major Injury) from the shared core. Those core
methods emit ``cwn.*`` spans via ``self.slug``. What remains here is the only
CWN-specific surface: cyberspace hacking. NOT a fallback — selected explicitly
by `ruleset: cwn`.
"""

from __future__ import annotations

from opentelemetry import trace

from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
from sidequest.telemetry.spans.cwn import cwn_hacking_security_check_span


class CwnRulesetModule(WithoutNumberRulesetModule):
    slug = "cwn"

    # ------------------------------------------------------------------
    # Inherited from ``WithoutNumberRulesetModule`` (ADR-142): the d20/2d6
    # resolution engine, the Luck + attribute ``save_params``, the Effort engine,
    # and the full lethality stack (``resolve_shock`` / ``resolve_trauma`` /
    # ``apply_system_strain`` / ``resolve_downed``) — all emitting ``cwn.*`` spans
    # via ``self.slug``. What remains here is the CWN-specific hacking surface.
    # ------------------------------------------------------------------

    def resolve_hacking(
        self,
        *,
        verb: str,
        tier: str,
        base_dc: int,
        alert_modifier: int,
        outcome: str,
        actor: str = "",
        _tracer: trace.Tracer | None = None,
    ) -> int:
        """Record a CWN cyberspace security check; return the effective DC.

        effective_dc = base_dc + alert_modifier (the CWN situational modifier:
        each network-alert escalation adds +1). Emits cwn.hacking.security_check
        — the GM lie-detector for the hacking subsystem; fires on EVERY net_run
        verb so the panel sees engaged + unengaged rolls alike. Does NOT mutate
        metrics or roll dice — the net_run dispatch seam builds the 2d6 check
        whose difficulty is this returned DC, the dice lib resolves the throw,
        and the confrontation engine applies the beat's tier deltas. Thin
        record-and-compute, consistent with resolve_shock/resolve_trauma."""
        effective_dc = int(base_dc) + int(alert_modifier)
        cwn_hacking_security_check_span(
            actor=actor,
            verb=verb,
            tier=tier,
            base_dc=int(base_dc),
            alert_modifier=int(alert_modifier),
            effective_dc=effective_dc,
            result=str(outcome),
            _tracer=_tracer,
        )
        return effective_dc
