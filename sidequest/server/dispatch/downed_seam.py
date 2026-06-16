"""Shared CWN-family/WWN 0-HP downed seam — cwn, awn, wwn (spec 2026-05-28 Task 11; WWN content Plan 3 Task 7).

The strike path (``dispatch.dice.dispatch_dice_throw``), the WWN cast path
(``server.narration_apply._resolve_wwn_cast_for_beat``), and the opponent-
reprisal close (``dispatch.dice._close_reprisal_depletion``, story 102-1 — the
downed defender there is the PC) must run the SAME Mortal/Major Injury
resolution after a defender is ablated to 0 HP. This module holds the single
implementation so the paths cannot drift (CLAUDE.md "Don't reinvent — wire up
what exists").

``physical_save_target_for`` resolves the downed actor's Physical-save target
number the SAME way for both paths: PC stats come from the seated
``Character.stats`` block; opponent stats come from the confrontation's
``opponent_default_stats`` (reserved combat keys removed). Fails loud — never
silently defaults the target number (No Silent Fallbacks).

``run_cwn_wwn_downed_seam`` runs the gated seam: it is a no-op unless the bound
ruleset config IS a ``CwnConfig``/``WwnConfig`` (covers ``cwn``, ``wwn``, and
``awn`` — whose ``AwnConfig`` subclasses ``CwnConfig``) AND the named defender is
at <= 0 HP, and otherwise computes the save target and calls
``ruleset.resolve_downed`` (which emits ``wwn.mortal_injury.declared`` /
``cwn.mortal_injury.declared`` and, on a failed Traumatic-Hit save, the
``*.major_injury.roll`` span).
"""

from __future__ import annotations

import random

from sidequest.game.beat_kinds import _opposite_side_first_actor
from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import ConfrontationDef


class DiceDispatchError(Exception):
    """A DICE_THROW (or shared dispatch seam) could not be resolved.

    Wrapper for validation / resolution failures so the session handler can
    surface them as ERROR messages without string-matching the exception
    chain. Defined here (not in ``dice``) so the shared downed seam can raise
    it without importing the dice module; ``dice`` re-exports it.
    """


def physical_save_target_for(
    *,
    ruleset,
    snapshot: GameSnapshot,
    cdef: ConfrontationDef,
    name: str,
    core,
    cfg,
) -> int:
    """Physical-save target number for the downed actor (CWN/WWN Major Injury gate).

    Computes ``ruleset.save_params(...).difficulty`` for the downed actor's
    Physical save. The downed actor's stats + level are resolved the SAME way
    the rest of dispatch does (CreatureCore/Npc carry no ability scores):

    - PC (a ``snapshot.characters`` entry by name) → that ``Character.stats``
      block + ``core.level``.
    - Opponent (no matching Character) → the confrontation's
      ``opponent_ability_scores()`` (reserved hp/armor_class/dexterity keys
      removed) + ``core.level``.

    Only reached inside the CWN/WWN 0-HP branch, so ``cfg`` is a
    Cwn/WwnConfig. Fails loud (No Silent Fallbacks) if ``cfg`` is None, the
    opponent has no authored ability scores, or ``save_params`` rejects the
    stat block — never silently defaults the target number.
    """
    from sidequest.genre.models.rules import CwnConfig, WwnConfig

    if not isinstance(cfg, (CwnConfig, WwnConfig)):
        raise DiceDispatchError(
            "CWN/WWN downed seam reached with a non-CwnConfig/WwnConfig ruleset config "
            f"({type(cfg).__name__}); cannot compute the Physical save target "
            "(CLAUDE.md No Silent Fallbacks — refusing to default to a fixed number)"
        )

    pc = next((c for c in snapshot.characters if c.core.name == name), None)
    if pc is not None:
        stats = pc.stats
    else:
        stats = cdef.opponent_ability_scores()
        if not stats:
            raise DiceDispatchError(
                f"CWN/WWN downed seam: opponent {name!r} has no ability scores to "
                "resolve a Physical save — author them under "
                "opponent_default_stats (No Silent Fallbacks)"
            )

    level = int(getattr(core, "level", 1) or 1)
    return ruleset.save_params(
        stats=stats,
        save=cfg.trauma.major_injury_save,
        level=level,
        label="major-injury",
        cfg=cfg,
    ).difficulty


def _has_live_hostile_on_side(snapshot, encounter, *, hostile_side: str) -> bool:
    """True if any non-withdrawn actor on ``hostile_side`` still has HP > 0.

    Story 108-6: ``actor_side`` IS the hostile side relative to the downed PC
    (the PC is the first live actor on the OPPOSITE side, per
    ``_opposite_side_first_actor``). A live hostile means the PC gets no last
    stand — terminal, as today. Field cleared (no live hostile) is the scoped
    solo case — open the WWN dying window. This is a seating/branch decision, NOT
    a native-mechanic tune (ADR-143).
    """
    for actor in encounter.actors:
        if actor.side == hostile_side and not getattr(actor, "withdrawn", False):
            hostile_core = snapshot.find_creature_core(actor.name)
            if hostile_core is not None and hostile_core.hp.current > 0:
                return True
    return False


def run_cwn_wwn_downed_seam(
    *,
    ruleset,
    snapshot: GameSnapshot,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    pack: GenrePack | None,
    actor_side: str,
    rng: random.Random = random,  # type: ignore[assignment]
) -> None:
    """Run the CWN/WWN Mortal/Major Injury seam for a defender just dropped to 0 HP.

    Gated on the bound ruleset config being a ``CwnConfig``/``WwnConfig`` — which
    covers cwn, wwn, AND awn (``AwnConfig`` subclasses ``CwnConfig``). Base
    ``resolve_downed`` is a no-op for native/swn, but ``physical_save_target_for``
    calls ``save_params`` (which native/swn DO have) and reads ``cfg.trauma``
    (present on CwnConfig AND WwnConfig, absent on SwnConfig), so we gate the
    WHOLE seam on the config capability rather than relying on the no-op return.

    The defender is the first live actor on the side OPPOSITE ``actor_side``
    (the same ``_opposite_side_first_actor`` idiom the strike path uses). No-op
    when there is no opposing actor, the defender can't be resolved, or the
    defender still has HP > 0.

    ``resolve_downed`` always declares a Mortal Injury (emits
    ``{ruleset}.mortal_injury.declared``); if a Traumatic Hit landed this scene
    it additionally rolls a Physical save and, on failure, the Major Injury
    table (emits ``{ruleset}.major_injury.roll``).
    """
    from sidequest.genre.models.rules import CwnConfig, WwnConfig

    # Capability gate (not a slug string): the seam runs for any ruleset whose
    # config IS a Cwn/WwnConfig — covers cwn, wwn, AND awn (AwnConfig subclasses
    # CwnConfig) plus future sister modules, instead of silently falling through
    # on a `ruleset in ("cwn","wwn")` string check.
    if not (
        pack and pack.rules and isinstance(pack.rules.ruleset_config(), (CwnConfig, WwnConfig))
    ):
        return
    down_name = _opposite_side_first_actor(encounter, actor_side)
    if down_name is None:
        return
    down_core = snapshot.find_creature_core(down_name)
    if down_core is None or down_core.hp.current > 0:
        return
    cfg = pack.rules.ruleset_config()
    scene_traumatic = any(t.text == "Traumatic Hit Landed" for t in encounter.tags)
    save_target = physical_save_target_for(
        ruleset=ruleset,
        snapshot=snapshot,
        cdef=cdef,
        name=down_name,
        core=down_core,
        cfg=cfg,
    )
    # sq-playtest #239 (death dual-status): if the genre lethality policy has
    # ALREADY ruled this actor terminally dead, an incapacitating terminal
    # status is present (the structured out-of-play marker set by
    # ``post_resolution_lethality`` for LETHAL verdicts). In the reprisal close
    # that verdict runs BEFORE this seam, so a dying PC arrives here already
    # flagged dead; a dropped OPPONENT (strike/cast paths) never carries one.
    # When terminally dead we SUPERSEDE the WN dying-window so the PC shows ONE
    # coherent status — the WN ``mortal_injury.declared`` span still fires
    # (GM-panel proof WN lethality engaged), marked ``superseded_by_terminal``.
    superseded = any(getattr(s, "incapacitating", False) for s in down_core.statuses)
    # Story 108-6: the stabilizable WWN dying window (incapacitating + player-
    # drivable) opens ONLY for a down with the field cleared — no live hostile
    # can still act on the downed actor. That is the scoped solo last-stand case.
    # A down WITH a live hostile (every opponent the player just dropped — the
    # live player IS the hostile — and a PC dropped at sword-point) takes the
    # ORDINARY Mortal Injury death clock, exactly as before 108-6. ``superseded``
    # (an already-stamped terminal verdict) still mints nothing. This is a
    # seating/branch decision, not a native-mechanic tune (ADR-143).
    live_hostile = _has_live_hostile_on_side(snapshot, encounter, hostile_side=actor_side)
    open_window = not superseded and not live_hostile
    created_turn = snapshot.turn_manager.interaction
    ruleset.resolve_downed(
        core=down_core,
        save_target=save_target,
        scene_traumatic=scene_traumatic,
        cfg=cfg,
        rng=rng,
        created_turn=created_turn,
        created_in_encounter=encounter.encounter_type,
        superseded_by_terminal=superseded,
        dying_window=open_window,
    )
    if open_window and isinstance(cfg, (CwnConfig, WwnConfig)):
        from sidequest.telemetry.spans.wn import dying_window_opened_span

        rounds = cfg.trauma.mortal_injury_rounds
        dying_window_opened_span(
            ruleset=ruleset.slug,
            actor=down_name,
            created_turn=created_turn,
            mortal_injury_rounds=rounds,
            deadline_round=created_turn + rounds,
            reason="no_live_hostile",
        )
