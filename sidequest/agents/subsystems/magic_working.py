"""magic_working subsystem dispatch handler — Intent Router live engager
(Story 59-5, ADR-113; WN free-play cast spine added by Story 102-3).

The router classifies a player action and emits a ``DispatchPackage``
whose ``SubsystemDispatch`` entries may include
``subsystem="magic_working"``. Two engines can service the dispatch:

1. **ADR-126 pact-working plugin** (``snapshot.magic_state`` populated —
   worlds that ship a ``magic.yaml``, e.g. coyote_star): params carry a
   full ``MagicWorking``-shaped dict and the handler engages
   ``apply_magic_working`` on the canonical snapshot, exactly as before.
2. **WN cast spine** (Story 102-3 — ``magic_state`` is None but a PC
   carries WWN ``core.spellcasting``): params carry
   ``{"actor": <caster>, "spell": <the spell as the player typed it>}``.
   The handler resolves the typed name against the world/genre WWN spell
   catalog and the existing ``WwnRulesetModule.resolve_spellcast`` —
   the SAME spine the apply_beat path drives via
   ``_resolve_wwn_cast_for_beat`` (reuse-first; no second cast
   implementation). Free play seats no defender: the cast spends and
   rolls with no target, per the spine's no-target contract.

Every ``resolve_spellcast`` invocation (cast AND refused) appends a
turn-stamped ``WwnCastLogEntry`` to ``snapshot.wwn_spell_cast_log`` — the
receipt the post-turn ``magic_working`` engagement witness reads (the
59-30 turn-scoped-ledger pattern). A refusal IS engagement.

No silent fallbacks: an unresolvable spell name is a FAILED PREMISE —
no spend, an explicit failed-premise narrator directive, an
``error``-coded output the bank span records, and no cast receipt (so
the post-turn watcher emits the mismatch). On the pact-working path,
missing ``magic_state`` or an unknown actor still propagate as
``MagicWorkingParseError`` so the bank records the error span.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from sidequest.agents.subsystems import SubsystemOutput
from sidequest.game.session import GameSnapshot
from sidequest.game.wwn_magic import WwnCastLogEntry
from sidequest.protocol.dispatch import NarratorDirective, SubsystemDispatch
from sidequest.server.narration_apply import (
    _apply_magic_status_promotions,
    apply_magic_working,
)

logger = logging.getLogger(__name__)


def _norm_spell_name(value: str) -> str:
    """Fold a typed spell reference for catalog matching: casefold and
    collapse whitespace/underscores, so "Foundation of Flame",
    "foundation_of_flame", and "foundation of flame" all key identically."""
    return re.sub(r"[\s_]+", " ", value.strip().casefold())


def _wwn_cast_module(pack: Any):
    """Return the pack's ruleset module when it exposes the WWN cast spine,
    else None. Keyed on module capability (isinstance), never the genre slug
    — the ADR-117 one-seam rule."""
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.ruleset.wwn import WwnRulesetModule

    rules = getattr(pack, "rules", None)
    slug = getattr(rules, "ruleset", None) if rules is not None else None
    if not slug:
        return None
    module = get_ruleset_module(slug)
    return module if isinstance(module, WwnRulesetModule) else None


def _failed_premise(
    dispatch: SubsystemDispatch, *, error: str, payload: str, **data: Any
) -> SubsystemOutput:
    """An error-coded output with an explicit failed-premise directive — the
    bank span records ``error`` and the narrator narrates the miss honestly
    (never improv-with-no-spend)."""
    return SubsystemOutput(
        directives=[
            NarratorDirective(
                kind="must_narrate",
                payload=payload,
                visibility=dispatch.visibility,
            )
        ],
        data={"error": error, **data},
    )


async def _run_wn_freeplay_cast(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: Any,
    module: Any,
) -> SubsystemOutput:
    """Story 102-3: route a named free-play cast through resolve_spellcast."""
    actor = dispatch.params.get("actor")
    if not isinstance(actor, str) or not actor:
        return _failed_premise(
            dispatch,
            error="missing_actor",
            payload=(
                "A magic working was classified but no caster was identified; "
                "narrate the attempt fizzling without mechanical effect."
            ),
        )

    spell_ref = dispatch.params.get("spell")
    if not isinstance(spell_ref, str) or not spell_ref:
        return _failed_premise(
            dispatch,
            error="missing_spell",
            payload=(
                f"{actor} attempts a working but named no spell; narrate the "
                "gesture failing to take shape — no spell was cast and nothing "
                "was spent."
            ),
            actor=actor,
        )

    caster_core = snapshot.find_creature_core(actor)
    if caster_core is None or caster_core.spellcasting is None:
        return _failed_premise(
            dispatch,
            error="no_spellcasting",
            payload=(
                f"{actor} has no spellcasting ability — the attempted working "
                "has no mechanical backing; narrate the failure honestly."
            ),
            actor=actor,
        )

    from sidequest.server.dispatch.wwn_spell_catalog_resolve import resolve_wwn_spell_catalog

    catalog = resolve_wwn_spell_catalog(pack, snapshot.world_slug)
    if catalog is None:
        return _failed_premise(
            dispatch,
            error="no_spell_catalog",
            payload=(
                f"{actor} reaches for a spell but this world ships no spell "
                "catalog — the working cannot resolve mechanically; narrate a "
                "failed premise, not a success."
            ),
            actor=actor,
        )

    needle = _norm_spell_name(spell_ref)
    spell = next(
        (
            s
            for s in catalog.spells
            if _norm_spell_name(s.id) == needle or _norm_spell_name(s.name) == needle
        ),
        None,
    )
    if spell is None:
        return _failed_premise(
            dispatch,
            error="unknown_spell",
            payload=(
                f"{actor} invokes {spell_ref!r}, but no such spell exists in "
                "this world's catalog — a failed premise. Nothing was cast and "
                "nothing was spent; narrate the miscast honestly."
            ),
            actor=actor,
            spell=spell_ref,
            available_ids=[s.id for s in catalog.spells],
        )

    cfg = pack.rules.ruleset_config()
    result = module.resolve_spellcast(
        caster_core=caster_core,
        spell=spell.to_cast_input(),
        target_core=None,
        target_stats=None,
        cfg=cfg,
        rng=random,
    )

    # The engagement receipt the post-turn witness reads (102-3). Stamped on
    # cast AND refusal — the engine answered either way.
    snapshot.wwn_spell_cast_log.append(
        WwnCastLogEntry(
            turn=snapshot.turn_manager.interaction,
            actor=actor,
            spell_id=spell.id,
            cast=result.cast,
        )
    )

    if result.cast:
        payload = (
            f"{actor} successfully cast {spell.name} ({spell.id}); "
            f"{result.casts_remaining} cast(s) remain today."
        )
        if result.damage > 0:
            payload += f" The spell rolled {result.damage} damage."
        payload += " Narrate THIS mechanical outcome — the cast is real and spent."
    else:
        payload = (
            f"{actor} attempted to cast {spell.name} ({spell.id}) but the cast "
            f"was REFUSED: {result.reason}. Nothing was spent "
            f"({result.casts_remaining} cast(s) remain). Narrate the refusal as "
            "a mechanical fact — the magic does not come."
        )

    return SubsystemOutput(
        directives=[
            NarratorDirective(
                kind="must_narrate",
                payload=payload,
                visibility=dispatch.visibility,
            )
        ],
        data={
            "cast": result.cast,
            "spell_id": result.spell_id,
            "casts_remaining": result.casts_remaining,
            "damage": result.damage,
            "reason": result.reason,
        },
    )


async def run_magic_working_dispatch(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: Any = None,
    player_name: str = "",
) -> SubsystemOutput:
    """Engage magic on the canonical snapshot.

    Pact-working worlds (``magic_state`` populated) take the ADR-126 path:
    ``dispatch.params`` is a ``MagicWorking``-shaped dict delegated to the
    existing ``apply_magic_working`` parse-validate-apply seam (which emits
    the ``magic.working`` OTEL span), followed by status promotions — the
    same chain the retired sidecar consumer ran.

    WN worlds (``magic_state`` is None, a PC carries ``core.spellcasting``,
    and the pack's ruleset module exposes the WWN cast spine) take the
    Story 102-3 free-play cast route — see :func:`_run_wn_freeplay_cast`.

    Raises ``MagicWorkingParseError`` on the pact-working path when:
    - ``snapshot.magic_state is None`` and no WN cast spine is available
    - ``dispatch.params`` failing ``MagicWorking`` pydantic validation
    - unknown actor (no instantiated character bars)
    """
    if snapshot.magic_state is None:
        module = _wwn_cast_module(pack)
        if module is not None and any(c.core.spellcasting is not None for c in snapshot.characters):
            return await _run_wn_freeplay_cast(
                dispatch, snapshot=snapshot, pack=pack, module=module
            )

    magic_result = apply_magic_working(snapshot=snapshot, patch_field=dict(dispatch.params))
    _apply_magic_status_promotions(
        snapshot=snapshot,
        magic_result=magic_result,
        player_name=player_name,
    )
    return SubsystemOutput()


__all__ = ["run_magic_working_dispatch"]
