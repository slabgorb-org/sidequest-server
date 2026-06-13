"""magic_working subsystem dispatch handler — Intent Router live engager
(Story 59-5, ADR-113; WN free-play cast spine added by Story 102-3).

The router classifies a player action and emits a ``DispatchPackage``
whose ``SubsystemDispatch`` entries may include
``subsystem="magic_working"``. Three engines can service the dispatch:

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
3. **AWN mutation engine** (Story 102-7 — ``magic_state`` is None, no WN
   cast surface, but the pack ships a mutations.yaml catalog and the
   snapshot carries seeded ``mutation_state``): the same param shape, the
   working resolved against the MUTATION catalog and routed through
   ``sidequest.mutation.use_ops.use_mutation`` — mutations ARE an AWN
   pack's magic. Receipts land in ``snapshot.mutation_use_log``.

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

# Review S1: upper bound on the LLM-copied spell reference before it touches
# the normalizer/catalog scan. Generous — the longest live spell display name
# is well under 64 chars.
_MAX_SPELL_REF_CHARS = 256


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


def _psionic_module(pack: Any):
    """Return the pack's ruleset module when it can activate psionic disciplines
    (any WN module — the Effort engine and ``activate_discipline`` live on
    ``WithoutNumberRulesetModule`` after ADR-142), else None. Capability
    (isinstance), never the genre slug — the ADR-117 one-seam rule. The
    catalog-presence + psychic-effort checks at the call site are what narrow
    this to packs that actually ship psionics (e.g. swn space_opera AND wwn
    heavy_metal — Story 102-6's "WWN has both psionics and a strain seam"). The
    gate is WithoutNumberRulesetModule, not SwnRulesetModule, because the WN
    siblings were flattened — WWN no longer inherits SWN, so an SwnRulesetModule
    gate would silently drop a wwn psychic."""
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule

    rules = getattr(pack, "rules", None)
    slug = getattr(rules, "ruleset", None) if rules is not None else None
    if not slug:
        return None
    module = get_ruleset_module(slug)
    return module if isinstance(module, WithoutNumberRulesetModule) else None


def _has_psionic_catalog(pack: Any, world_slug: str | None) -> bool:
    """True when the pack ships a psionic discipline catalog at either tier
    (world-over-genre, mirroring ``resolve_psionic_discipline_catalog``)."""
    if getattr(pack, "psionic_discipline_catalog", None) is not None:
        return True
    worlds = getattr(pack, "worlds", None) or {}
    world = worlds.get(world_slug) if world_slug else None
    return world is not None and getattr(world, "psionic_discipline_catalog", None) is not None


def _awn_mutation_module(pack: Any):
    """Return the pack's ruleset module when the pack carries the AWN
    mutation surface (an AWN module + a loaded mutations.yaml catalog),
    else None. Capability + catalog presence, never the genre slug.

    Mutations are AWN-specific (ADR-142): they previously matched any CWN-family
    module only because AWN was an ``Awn(Cwn)`` subclass; the flattened WN
    hierarchy narrows this gate to ``AwnRulesetModule``."""
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.ruleset.awn import AwnRulesetModule

    if getattr(pack, "mutations", None) is None:
        return None
    rules = getattr(pack, "rules", None)
    slug = getattr(rules, "ruleset", None) if rules is not None else None
    if not slug:
        return None
    module = get_ruleset_module(slug)
    return module if isinstance(module, AwnRulesetModule) else None


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
    # Review S1 (CWE-400 hardening): the spell reference is LLM-copied from
    # player free text — cap it before the regex normalize + catalog scan so
    # an adversarially long value cannot tax the turn. No real spell id or
    # display name approaches this length.
    if len(spell_ref) > _MAX_SPELL_REF_CHARS:
        return _failed_premise(
            dispatch,
            error="spell_ref_too_long",
            payload=(
                f"{actor} attempts a working, but the invocation is not a spell "
                "this world recognizes — a failed premise. Nothing was cast and "
                "nothing was spent."
            ),
            actor=actor,
            spell_ref_chars=len(spell_ref),
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
        # Review S3: do NOT echo the player-typed spell reference into the
        # narrator directive — the payload lands verbatim in the narrator
        # prompt (orchestrator narrator_directives section) and would bypass
        # the ADR-047 sanitization lane. The identity rides in ``data`` only,
        # which never reaches the prompt (orchestrator forwards directives,
        # not data) — kept there for forensics/OTEL alongside available_ids
        # (same server-side audience as the beat path's
        # ``wwn.cast_spell_unknown`` watcher payload).
        return _failed_premise(
            dispatch,
            error="unknown_spell",
            payload=(
                f"{actor} invokes a spell that does not exist in this world's "
                "catalog — a failed premise. Nothing was cast and nothing was "
                "spent; narrate the miscast honestly, without inventing a "
                "working."
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


async def _run_psionic_freeplay_activation(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: Any,
    module: Any,
) -> SubsystemOutput:
    """Story 102-6: route a named free-play psionic discipline activation through
    ``WithoutNumberRulesetModule.activate_discipline`` — the 102-3 cast-spine
    mirror for psionics (any WN sibling that ships a catalog, swn or wwn). The
    router's param contract is reused verbatim: the discipline AS THE PLAYER
    TYPED IT rides the ``spell`` key; the HANDLER resolves it against the pack's
    discipline catalog (by id or display name)."""
    from sidequest.game.ruleset.swn import PSIONIC_EFFORT_SOURCE
    from sidequest.server.dispatch.psionic_discipline_resolve import (
        resolve_psionic_discipline_catalog,
    )

    actor = dispatch.params.get("actor")
    if not isinstance(actor, str) or not actor:
        return _failed_premise(
            dispatch,
            error="missing_actor",
            payload=(
                "A psionic working was classified but no psychic was identified; "
                "narrate the attempt fizzling without mechanical effect."
            ),
        )

    discipline_ref = dispatch.params.get("spell")
    if not isinstance(discipline_ref, str) or not discipline_ref:
        return _failed_premise(
            dispatch,
            error="missing_discipline",
            payload=(
                f"{actor} reaches inward but names no discipline; narrate the "
                "focus failing to take shape — nothing was activated and no "
                "Effort was spent."
            ),
            actor=actor,
        )
    # Same CWE-400 hardening as the cast spine: the reference is LLM-copied from
    # player free text — cap it before the normalize + catalog scan.
    if len(discipline_ref) > _MAX_SPELL_REF_CHARS:
        return _failed_premise(
            dispatch,
            error="discipline_ref_too_long",
            payload=(
                f"{actor} reaches for a discipline this mind does not hold — a "
                "failed premise. Nothing was activated and no Effort was spent."
            ),
            actor=actor,
            discipline_ref_chars=len(discipline_ref),
        )

    core = snapshot.find_creature_core(actor)
    if core is None or not core.effort:
        return _failed_premise(
            dispatch,
            error="no_psionics",
            payload=(
                f"{actor} has no psionic training — the attempted discipline has "
                "no mechanical backing; narrate the failure honestly."
            ),
            actor=actor,
        )

    catalog = resolve_psionic_discipline_catalog(pack, snapshot.world_slug)
    if catalog is None:
        return _failed_premise(
            dispatch,
            error="no_discipline_catalog",
            payload=(
                f"{actor} reaches for a discipline but this world ships no "
                "discipline catalog — the working cannot resolve mechanically; "
                "narrate a failed premise, not a success."
            ),
            actor=actor,
        )

    needle = _norm_spell_name(discipline_ref)
    discipline = next(
        (
            d
            for d in catalog.disciplines
            if _norm_spell_name(d.id) == needle or _norm_spell_name(d.name) == needle
        ),
        None,
    )
    if discipline is None:
        # Same S3 discipline as the cast spine: the typed reference rides
        # ``data`` (forensics/OTEL), never the narrator directive payload.
        return _failed_premise(
            dispatch,
            error="unknown_discipline",
            payload=(
                f"{actor} reaches for a discipline this catalog does not know — a "
                "failed premise. Nothing was activated and no Effort was spent; "
                "narrate the miss honestly, without inventing a power."
            ),
            actor=actor,
            discipline=discipline_ref,
            available_ids=[d.id for d in catalog.disciplines],
        )

    cfg = pack.rules.ruleset_config()
    result = module.activate_discipline(
        core=core,
        discipline=discipline,
        source=PSIONIC_EFFORT_SOURCE,
        cfg=cfg,
    )

    if result.applied:
        payload = (
            f"{actor} activated {discipline.name} ({discipline.id}); "
            f"{discipline.effort_cost} Effort committed, {result.available} free."
        )
        if result.strained > 0:
            payload += f" The push cost {result.strained} System Strain."
        if discipline.save:
            payload += f" It forces a {discipline.save} save on its target."
        payload += " Narrate THIS mechanical outcome — the discipline is real and spent."
    else:
        payload = (
            f"{actor} reached for {discipline.name} ({discipline.id}) but the "
            f"activation was REFUSED: {result.reason}. Nothing was spent. Narrate "
            "the refusal as a mechanical fact — the power does not come."
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
            "applied": result.applied,
            "discipline_id": result.discipline_id,
            "available": result.available,
            "strained": result.strained,
            "reason": result.reason,
        },
    )


async def _run_awn_freeplay_mutation(
    dispatch: SubsystemDispatch,
    *,
    snapshot: GameSnapshot,
    pack: Any,
    module: Any,
) -> SubsystemOutput:
    """Story 102-7: route a named free-play mutation use through use_ops.

    The 102-3 cast-spine mirror for AWN packs, where mutations ARE the
    pack's magic. The router's param contract is reused verbatim — the
    working AS THE PLAYER TYPED IT rides the ``spell`` key; the HANDLER
    resolves it against the pack's mutation catalog (by id or display
    name) because the router does not know spell from mutation.
    """
    from sidequest.mutation.state import MutationUseLogEntry
    from sidequest.mutation.use_ops import use_mutation
    from sidequest.telemetry.spans.awn import awn_mutation_refused_span

    actor = dispatch.params.get("actor")
    if not isinstance(actor, str) or not actor:
        return _failed_premise(
            dispatch,
            error="missing_actor",
            payload=(
                "A mutation use was classified but no actor was identified; "
                "narrate the attempt fizzling without mechanical effect."
            ),
        )

    working_ref = dispatch.params.get("spell")
    if not isinstance(working_ref, str) or not working_ref:
        return _failed_premise(
            dispatch,
            error="missing_mutation",
            payload=(
                f"{actor} reaches for the change but named no mutation; "
                "narrate the body failing to answer — nothing was used and "
                "nothing was spent."
            ),
            actor=actor,
        )
    # Same CWE-400 hardening as the cast spine: the reference is LLM-copied
    # from player free text — cap it before the normalize + catalog scan.
    if len(working_ref) > _MAX_SPELL_REF_CHARS:
        return _failed_premise(
            dispatch,
            error="mutation_ref_too_long",
            payload=(
                f"{actor} reaches for a power this body does not carry — a "
                "failed premise. Nothing was used and nothing was spent."
            ),
            actor=actor,
            mutation_ref_chars=len(working_ref),
        )

    core = snapshot.find_creature_core(actor)
    state = snapshot.mutation_state
    if core is None or state is None or actor not in state.characters:
        # No mutation surface on THIS actor. Loud span (GM-panel evidence) +
        # failed premise — the narrator must not improvise a power.
        awn_mutation_refused_span(
            actor=actor if isinstance(actor, str) else "",
            mutation_id="",
            reason="no_mutation_state",
        )
        return _failed_premise(
            dispatch,
            error="no_mutation_state",
            payload=(
                f"{actor} carries no mutation — the attempted use has no "
                "mechanical backing; narrate the failure honestly."
            ),
            actor=actor,
        )

    catalog = pack.mutations
    needle = _norm_spell_name(working_ref)
    mutation = next(
        (
            m
            for m in catalog.positives
            if _norm_spell_name(m.id) == needle
            or _norm_spell_name(m.id.split("/", 1)[1]) == needle
            or _norm_spell_name(m.name) == needle
        ),
        None,
    )
    if mutation is None:
        # Unknown working → failed premise. Same S3 discipline as the cast
        # spine: the typed reference rides ``data`` (forensics/OTEL), never
        # the narrator directive payload (ADR-047 sanitization lane).
        awn_mutation_refused_span(actor=actor, mutation_id="", reason="unknown_mutation")
        return _failed_premise(
            dispatch,
            error="unknown_mutation",
            payload=(
                f"{actor} reaches for a power this catalog does not know — a "
                "failed premise. Nothing was used and nothing was spent; "
                "narrate the miss honestly, without inventing a mutation."
            ),
            actor=actor,
            mutation=working_ref,
            available_ids=[m.id for m in catalog.positives],
        )

    # v1 save handling matches the use_mutation tool: the narrator narrates
    # the target's save from the returned save_stat; opposed-save dice wiring
    # rides the dice protocol in a later plan.
    result = use_mutation(
        state=state,
        catalog=catalog,
        module=module,
        cfg=pack.rules.ruleset_config(),
        core=core,
        actor=actor,
        mutation_id=mutation.id,
        target_id=str(dispatch.params.get("target") or ""),
        save_resolver=lambda stat, target: "fail",
    )

    # The engagement receipt the post-turn witness reads (the
    # wwn_spell_cast_log mirror). Stamped on use AND refusal.
    snapshot.mutation_use_log.append(
        MutationUseLogEntry(
            turn=snapshot.turn_manager.interaction,
            actor=actor,
            mutation_id=mutation.id,
            applied=result.applied,
        )
    )

    if result.applied:
        payload = f"{actor} used {mutation.name} ({mutation.id})."
        if mutation.strain_cost > 0:
            payload += f" It cost {mutation.strain_cost} System Strain."
        if result.uses_remaining >= 0:
            payload += f" {result.uses_remaining} use(s) remain this period."
        if result.save_stat:
            payload += f" The target's {result.save_stat} save: {result.save_result}."
        if result.effect:
            payload += f" Effect: {result.effect}"
        payload += " Narrate THIS mechanical outcome — the use is real and paid for."
    else:
        payload = (
            f"{actor} reached for {mutation.name} ({mutation.id}) but the use "
            f"was REFUSED: {result.reason}. Nothing was spent. Narrate the "
            "refusal as a mechanical fact — the body does not answer."
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
            "applied": result.applied,
            "mutation_id": mutation.id,
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
        # Story 102-6 — psionics: an SWN-family pack that ships a discipline
        # catalog, on a psychic carrying a seeded Effort pool. Surface presence
        # mirrors the cast spine (no spellcasting needed — a psychic commits
        # Effort, not casts). Checked after the cast route so a WWN caster's
        # spell working still takes the cast path.
        psionic_module = _psionic_module(pack)
        if (
            psionic_module is not None
            and _has_psionic_catalog(pack, snapshot.world_slug)
            and any(c.core.effort for c in snapshot.characters)
        ):
            return await _run_psionic_freeplay_activation(
                dispatch, snapshot=snapshot, pack=pack, module=psionic_module
            )
        # Story 102-7 — the third magic surface: an AWN pack's mutation
        # engine (mutations ARE the pack's magic). Surface presence again:
        # a loaded catalog + seeded per-character mutation state.
        mutation_module = _awn_mutation_module(pack)
        if (
            mutation_module is not None
            and snapshot.mutation_state is not None
            and snapshot.mutation_state.characters
        ):
            return await _run_awn_freeplay_mutation(
                dispatch, snapshot=snapshot, pack=pack, module=mutation_module
            )

    magic_result = apply_magic_working(snapshot=snapshot, patch_field=dict(dispatch.params))
    _apply_magic_status_promotions(
        snapshot=snapshot,
        magic_result=magic_result,
        player_name=player_name,
    )
    return SubsystemOutput()


__all__ = ["run_magic_working_dispatch"]
