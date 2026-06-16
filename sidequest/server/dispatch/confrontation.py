"""Confrontation-def lookup + CONFRONTATION payload assembly.

Port of sidequest-api/crates/sidequest-server/src/dispatch/response.rs
confrontation-def resolution and payload construction. Story 3.4.

Story 47-3 (Phase 5) extends this with magic-confrontation outcome
resolution: ``resolve_magic_confrontation`` looks up a magic
confrontation by id, applies its branch's mandatory_outputs, and
returns a CONFRONTATION_OUTCOME payload for the WebSocket dispatcher.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, StructuredEncounter

# ADR-147 / story 122-2: find_confrontation_def is pure combat-rules logic that
# moved down to the game tier. Re-exported here so existing server-tier callers
# keep importing it from this path (server -> game is the legal direction).
from sidequest.game.ruleset.combat_rules import find_confrontation_def  # noqa: F401
from sidequest.game.session import GameSnapshot
from sidequest.game.table.types import TableState
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    ResolutionMode,
    RulesConfig,
)
from sidequest.magic.confrontations import BranchName
from sidequest.magic.outputs import apply_mandatory_outputs

if TYPE_CHECKING:
    from sidequest.protocol.messages import ConfrontationPayload

# Story 49-7: a per-recipient PC context — (class_def, spell_slots_remaining,
# prepared_spells) — matching the existing pc_classes_by_name tuple shape
# used in agents/narrator.py:327-330. Single shape for one decision: the
# narrator-prompt builder and the panel-projection emitter both feed the
# same beats_available_for filter, so they take the same context.
RecipientPc = tuple[ClassDef, float, dict[int, list[str]] | None]


def resolve_recipient_pc(
    *,
    snapshot: GameSnapshot,
    genre_pack: Any,
    player_id: str,
) -> tuple[RecipientPc | None, str | None]:
    """Resolve ``(class_def, total_spell_slots, prepared_spells)`` for
    the PC seated as ``player_id``. Returns ``((recipient_pc, actor_name))``
    or ``((None, None))`` when there is no PC to resolve.

    Returning ``None`` is non-fatal: the caller falls back to the
    unfiltered payload for that recipient and logs a warning. CLAUDE.md
    'No Silent Fallbacks' applies to *configuration* drift, not to lobby
    sockets — a player who has not yet seated genuinely has no PC to
    filter against, and refusing to broadcast at all would hide the
    encounter card from a player about to seat in.

    spell_slots_remaining is the sum of every ``slots_l<N>`` LedgerBar
    value the actor owns. prepared_spells is the actor's per-level
    prepared-list snapshot (None if the world has no MagicState — e.g.
    non-magic-aware genres — which keeps the 47-10 prepared-list gate
    dormant per beat_filter's backward-compat contract).
    """
    pc_name = snapshot.player_seats.get(player_id)
    if pc_name is None:
        return (None, None)
    character = next((c for c in snapshot.characters if c.core.name == pc_name), None)
    if character is None:
        return (None, None)
    class_def = next(
        (c for c in genre_pack.classes if c.display_name == character.char_class),
        None,
    )
    if class_def is None:
        return (None, pc_name)
    total_slots = 0.0
    prepared: dict[int, list[str]] | None = None
    magic_state = snapshot.magic_state
    if magic_state is not None:
        prefix = f"character|{pc_name}|slots_l"
        for serialized, bar in magic_state.ledger.items():
            if serialized.startswith(prefix):
                total_slots += float(bar.value)
        # Empty dict (not None) engages the 47-10 prepared-list gate when
        # the world has MagicState — a Mage with slots but nothing
        # memorized must still be rejected from cast_spell.
        prepared = magic_state.prepared_spells.get(pc_name, {})
    return ((class_def, total_slots, prepared), pc_name)


def build_confrontation_payload(
    *,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    genre_slug: str,
    recipient_pc: RecipientPc | None = None,
    recipient_actor_name: str | None = None,
    core_resolver: Callable[[str], CreatureCore | None] | None = None,
    spellcasting: SpellcastingState | None = None,
    active_stakes: str | None = None,
    portrait_resolver: Callable[[str], str | None] | None = None,
    rules: RulesConfig | None = None,
) -> dict[str, Any]:
    """Assemble the CONFRONTATION payload the UI overlay consumes.

    Shape fixed by sidequest-ui/src/components/ConfrontationOverlay.tsx:42-58
    (+ win_condition/player_hp/opponent_hp pending UI mirror).
    Encounter mood_override beats the confrontation-def default mood.

    Story 49-7: when ``recipient_pc=(class_def, spell_slots, prepared_spells)``
    is supplied, the payload's ``beats`` field is filtered through
    ``sidequest.game.beat_filter.beats_available_for`` so the per-recipient
    UI overlay only renders class-legal choices (Fighter does not see
    Backstab/Cast Spell/Turn Undead, etc.). The single source of truth for
    filter semantics lives in ``beat_filter.py`` — this is purely a wiring
    site. When ``recipient_pc`` is ``None`` the payload keeps the pre-fix
    full-union shape so callers that have not migrated (narrator-prompt
    builder, tests, etc.) continue to work.

    ``recipient_actor_name`` is the PC actor's display name; besides
    stamping the OTEL span attributes it is used (with ``core_resolver``)
    to DERIVE the recipient's WWN ``SpellcastingState`` for the cast_spell
    gate (Task 5). Defaults to ``"recipient"`` when omitted.

    WWN cast_spell gate (Task 5): when ``recipient_pc`` is supplied the
    cast_spell beat is gated on the recipient's ``core.spellcasting``
    (WWN casts/prepared economy) instead of the B/X slot economy. The
    ``SpellcastingState`` is resolved centrally HERE — explicit
    ``spellcasting`` wins; otherwise it is derived from
    ``core_resolver(recipient_actor_name).spellcasting``. This single
    derivation point covers every per-PC caller (panel projection AND the
    yield-projection path AND any future site) without per-caller
    threading. B/X packs have ``spellcasting is None`` on every core, so
    the derived value is None and the B/X arm runs byte-for-byte unchanged.

    The filter call also emits a ``confrontation_beat_filter_span``
    tagged ``source='ui_panel_projection'`` so the GM panel can
    distinguish the panel-projection filter run from the existing
    narrator-prompt one (``source='narrator_prompt'``). Without that
    discriminator the two spans look identical in the watcher
    dashboard and a regression at either site is invisible.

    Story 85-3 (Tier B confrontation panel):
    ``active_stakes`` is the session's current stakes string; an empty string
    normalizes to ``None`` so the UI banner collapses on a single falsy check.
    The ``stakes`` key is ALWAYS present in the returned dict (not
    additive-conditional like the hp keys) — every confrontation reports its
    stakes or explicit absence. Emits the ``confrontation.stakes_attached``
    OTEL span on every call so the GM panel can tell a no-stakes confrontation
    from a dropped emit.
    ``portrait_resolver``, when supplied, is called for each
    ``side == "opponent"`` actor to resolve a portrait URL (returns ``None`` on
    miss — No Silent Fallbacks); player/companion actors are never resolved here
    (they get portraits via PARTY_STATUS). The resolved URL rides the serialized
    actor dict under ``portrait_url``. Build the resolver at call sites with
    ``make_confrontation_portrait_resolver``.

    Story 97-3: ``rules``, when supplied (the pack's ``RulesConfig``), makes
    this function the single author of the pre-roll DC — every serialized
    beat dict gains a ``difficulty`` key carrying the number the matching
    resolver will use: opposed_check → the per-side native-formula DC
    (``_opposed_dc``), cwn hacking → security-tier DC + alert escalation,
    everything else → ``ruleset.offer_difficulty`` (native: beat DC;
    SWN family: target armor class, single-sourced with ``attack_params``).
    When ``None`` (legacy/bootstrap callers with no pack in hand — e.g.
    slug-resume ConfrontationPayload reconstruction), no ``difficulty`` keys
    are emitted and the UI treats the offer as uncommittable rather than
    silently computing a client-side number.
    """
    if encounter.mood_override is not None:
        mood = encounter.mood_override
    elif cdef.mood is not None:
        mood = cdef.mood
    else:
        mood = ""

    # Story 102-2: hoisted above the recipient branch so the payload's
    # ``spellcasting`` projection (below) sees the derived value. Explicit
    # ``spellcasting`` wins; the recipient branch refines it from the
    # recipient core when None. Non-recipient callers keep whatever was
    # passed (None for every legacy/bootstrap path).
    effective_spellcasting = spellcasting

    if recipient_pc is not None:
        # Local imports keep the legacy (recipient_pc=None) call path
        # free of telemetry / filter imports — important for the
        # ConfrontationPayload bootstrap construction in slug-resume
        # paths that may run before the telemetry tracer is fully
        # initialized.
        from sidequest.game.beat_filter import (
            beats_available_for,
            cast_spell_rejection_reason,
        )
        from sidequest.telemetry.spans import confrontation_beat_filter_span

        class_def, spell_slots, prepared_spells = recipient_pc
        # WWN arm (Task 5): centralize SpellcastingState derivation so EVERY
        # per-PC caller benefits (panel projection AND yield-projection AND
        # any future per-PC site) without per-caller threading. Explicit
        # `spellcasting` wins; otherwise derive from the recipient core via
        # ``core_resolver``. A B/X core has ``spellcasting is None``, so the
        # derived value is None and the B/X arm runs unchanged (no-op) — the
        # WWN/B/X behavior split lives entirely in beat_filter.
        #
        # Story 106-4 Part C: the SAME recipient core supplies the carried
        # inventory so beats_available_for can append transient "Drink <potion>"
        # item-use beats (gated to hp_depletion combat in the filter). Resolved
        # once here; None when no core resolves (lobby/pre-chargen) → no item
        # beats, which is correct.
        recipient_inventory_items: list[dict[str, Any]] | None = None
        if core_resolver is not None and recipient_actor_name:
            recipient_core = core_resolver(recipient_actor_name)
            if recipient_core is not None:
                if effective_spellcasting is None:
                    effective_spellcasting = recipient_core.spellcasting
                recipient_inventory_items = recipient_core.inventory.items
        filtered = beats_available_for(
            cdef,
            class_def,
            spell_slots_remaining=spell_slots,
            prepared_spells=prepared_spells,
            spellcasting=effective_spellcasting,
            inventory_items=recipient_inventory_items,
        )
        rejection_reason = cast_spell_rejection_reason(
            cdef,
            class_def,
            spell_slots_remaining=spell_slots,
            prepared_spells=prepared_spells,
            spellcasting=effective_spellcasting,
        )
        span_kwargs: dict[str, Any] = {
            "actor": recipient_actor_name or "recipient",
            "class_name": class_def.display_name,
            "confrontation_type": cdef.confrontation_type,
            "available_beat_ids": ",".join(b.id for b in filtered),
            "spell_slots_remaining": spell_slots,
            "pool_size": len(cdef.beats),
            "filtered_size": len(filtered),
            "source": "ui_panel_projection",
        }
        if rejection_reason is not None:
            span_kwargs["cast_spell_rejection_reason"] = rejection_reason
        with confrontation_beat_filter_span(**span_kwargs):
            pass
        beats_for_payload = filtered
    else:
        beats_for_payload = cdef.beats

    # Story 85-3 (Tier B): the session's active stakes, surfaced on the
    # CONFRONTATION channel for the promoted dockview panel's stakes banner.
    # ALWAYS present (empty string normalizes to None) — NOT additive-conditional
    # like the hp keys (Architect decision 2026-06-04).
    stakes_value = active_stakes or None

    # Story 85-3: per-opponent portrait so the dial reads as *against someone*
    # (ADR-116 — a confrontation requires an Other). Resolved via an INJECTED
    # ``portrait_resolver`` (production wires it to _resolve_npc_portrait_url),
    # scoped to ``side == "opponent"`` — players/companions get portraits via
    # PARTY_STATUS, never here. None on resolver miss (No Silent Fallbacks).
    def _actor_with_portrait(actor: EncounterActor) -> dict[str, Any]:
        portrait_url = (
            portrait_resolver(actor.name)
            if (portrait_resolver is not None and actor.side == "opponent")
            else None
        )
        return {**actor.model_dump(mode="json"), "portrait_url": portrait_url}

    payload: dict[str, Any] = {
        "type": encounter.encounter_type,
        "label": cdef.label,
        "category": cdef.category,
        "actors": [_actor_with_portrait(a) for a in encounter.actors],
        "player_metric": encounter.player_metric.model_dump(mode="json"),
        "opponent_metric": encounter.opponent_metric.model_dump(mode="json"),
        "beats": [b.model_dump(mode="json") for b in beats_for_payload],
        "secondary_stats": (
            encounter.secondary_stats.model_dump(mode="json")
            if encounter.secondary_stats is not None
            else None
        ),
        "genre_slug": genre_slug,
        "mood": mood,
        "active": not encounter.resolved,
        "stakes": stakes_value,
        # Story 102-2: project the recipient's WWN cast economy so the
        # overlay's "Work a Spell" picker can list prepared spells and show
        # casts_remaining (player-visible math — the Sebastien/Jade lane).
        # None for non-casters / B/X packs — never a fabricated empty
        # economy. Always present on the wire (like ``stakes``), so the UI
        # gates the picker on the value, not on key presence.
        "spellcasting": (
            {
                "casts_remaining": effective_spellcasting.casts_remaining,
                "casts_per_day": effective_spellcasting.casts_per_day,
                "prepared": list(effective_spellcasting.prepared),
            }
            if effective_spellcasting is not None
            else None
        ),
    }

    # Story 102-4: the WN sealed-round commit ledger, projected as the
    # player-side actor names whose Main Action is sealed this round — the
    # overlay's committed-vs-waiting seam (ADR-036 submit-and-wait
    # visibility; collaborative, never a rush cue). Key present only while
    # commits are sealed: native/dial payloads and between-round WN payloads
    # keep the legacy shape byte-for-byte, which the UI reads as "render no
    # indicators".
    if encounter.wn_commits:
        payload["committed_actors"] = [c.actor for c in encounter.wn_commits]

    # Story 97-3 — server-authored pre-roll difficulty on every offered beat.
    # The TARGET banner used to be a CLIENT-side formula (App.tsx rawDc =
    # clamp(10 + |base|*2)) while resolution used ruleset.attack_params —
    # two sources of truth that diverge under SWN/hp_depletion, where the
    # effective DC is the opponent's armor class (a number the client cannot
    # know). The server is now the only DC author: each beat dict carries the
    # ``difficulty`` resolution will use, single-sourced through the ruleset's
    # ``offer_difficulty`` seam (SWN's ``attack_params`` reads the same
    # method, so offer and resolution are structurally one number). The UI
    # renders ``beat.difficulty`` and refuses loudly when it is absent.
    # ``rules=None`` (legacy/bootstrap callers that have no pack in hand —
    # e.g. slug-resume ConfrontationPayload reconstruction) keeps the prior
    # payload shape: no ``difficulty`` keys, which the UI treats as an
    # uncommittable offer rather than silently inventing a number.
    if rules is not None:
        from sidequest.game.beat_kinds import _opposite_side_first_actor
        from sidequest.game.ruleset import get_ruleset_module
        from sidequest.telemetry.spans import confrontation_beat_dc_authored_span

        ruleset_module = get_ruleset_module(rules.ruleset)
        target_name = _opposite_side_first_actor(encounter, "player")
        target_core = (
            core_resolver(target_name)
            if (target_name is not None and core_resolver is not None)
            else None
        )
        # Resolution-mode dispatch (rework round 1, review HIGH-1/MEDIUM):
        # the number the offer advertises must be the number the matching
        # RESOLVER will use — and which resolver runs depends on the cdef,
        # not just the ruleset:
        #
        #  - opposed_check resolves each side's d20 vs the per-side
        #    ``_opposed_dc(beat)`` (narration_apply) — the native dial
        #    formula — for EVERY ruleset (road_warrior=cwn ships live
        #    opposed_check defs). Author via the native module's
        #    ``compute_dc``, the formula ``_opposed_dc`` documents itself
        #    as mirroring.
        #  - cwn hacking (net_run) resolves 2d6+mods vs the security-tier
        #    DC + alert escalation (dispatch ``is_net_run`` branch) — never
        #    vs AC. Mirror that arithmetic; a hacking cdef with no
        #    resolvable tier is the same lifecycle bug the dispatch branch
        #    refuses, so fail loud here too (No Silent Fallbacks).
        #  - everything else (beat_selection / dial attacks) resolves via
        #    ``ruleset.attack_params`` — author through the ruleset's
        #    ``offer_difficulty`` seam (single-sourced with attack_params).
        is_net_run_offer = rules.ruleset == "cwn" and cdef.category == "hacking"
        if cdef.resolution_mode == ResolutionMode.opposed_check:
            native_module = get_ruleset_module("native")

            def _offer_dc(beat_def: BeatDef) -> int:
                return native_module.compute_dc(beat_def)
        elif is_net_run_offer:
            from sidequest.genre.models.rules import CwnConfig

            cfg = rules.ruleset_config()
            if (
                not isinstance(cfg, CwnConfig)
                or cfg.hacking is None
                or encounter.security_tier is None
                or encounter.security_tier not in cfg.hacking.security_tiers
            ):
                raise ValueError(
                    "hacking beat offer reached without a resolvable security "
                    f"tier (tier={encounter.security_tier!r}); the lifecycle "
                    "seam should have stamped it (No Silent Fallbacks)"
                )
            net_run_dc = cfg.hacking.security_tiers[encounter.security_tier] + int(
                encounter.opponent_metric.current
            )

            def _offer_dc(beat_def: BeatDef) -> int:
                return net_run_dc
        else:

            def _offer_dc(beat_def: BeatDef) -> int:
                return ruleset_module.offer_difficulty(beat=beat_def, target_core=target_core)

        from sidequest.game.beat_filter import is_item_use_beat

        for beat_def, beat_dict in zip(beats_for_payload, payload["beats"], strict=True):
            # Story 106-4 Part C: item-use beats ("Drink <potion>") are
            # auto-success, no-roll actions — they carry NO to-hit difficulty.
            # Leaving ``difficulty`` absent is the wire signal the UI reads to
            # commit them without a dice tray (the cast_spell-tile precedent of
            # a non-dice beat path). Stamping an attack DC here would make the
            # client roll a d20 to drink a potion (wrong) — skip them.
            if is_item_use_beat(beat_def.id):
                continue
            beat_dict["difficulty"] = _offer_dc(beat_def)
        # GM-panel lie-detector (CLAUDE.md OTEL discipline): the offered
        # numbers here must match the resolution-time dice.request_sent
        # difficulty; an offer frame with no preceding beat_dc_authored span
        # means the banner had nothing legitimate to display.
        with confrontation_beat_dc_authored_span(
            genre_slug=genre_slug,
            confrontation_type=encounter.encounter_type,
            ruleset=rules.ruleset,
            target_name=target_name or "",
            beat_difficulties=",".join(
                f"{b['id']}={b['difficulty']}" for b in payload["beats"] if "difficulty" in b
            ),
        ):
            pass

    # Story 85-3 — GM-panel lie-detector for the stakes wiring (CLAUDE.md OTEL
    # discipline). Fires on EVERY build; has_stakes=False distinguishes a
    # no-stakes confrontation from a dropped emit. Local import keeps the span
    # off the module load path (mirrors the beat_filter local import above).
    from sidequest.telemetry.spans import confrontation_stakes_attached_span

    with confrontation_stakes_attached_span(
        genre_slug=genre_slug,
        confrontation_type=encounter.encounter_type,
        has_stakes=stakes_value is not None,
        stakes_len=len(stakes_value) if stakes_value else 0,
    ):
        pass

    # space_opera → SWN binding (Task 6): surface the resolution model and,
    # under hp_depletion, the primary combatants' HP so the player-facing
    # overlay can render the math (Sebastien/Jade legibility goal). The
    # ``win_condition`` key is ADDITIVE (dial packs emit "dial_threshold");
    # the hp keys are only added under hp_depletion when a name→CreatureCore
    # resolver is threaded in — legacy/test call paths that omit it keep the
    # pre-existing payload shape (no hp keys).
    payload["win_condition"] = encounter.win_condition
    if encounter.win_condition == "hp_depletion" and core_resolver is not None:

        def _primary_hp(side: str) -> dict[str, int] | None:
            # Skip withdrawn/yielded actors: a player who has yielded but is
            # still first-seated on their side is not the primary combatant,
            # so surfacing their HP would mislabel the dial.
            for a in encounter.actors:
                if a.side == side and not a.withdrawn:
                    core = core_resolver(a.name)
                    if core is not None:
                        return {"current": core.hp.current, "max": core.hp.max}
            return None

        player_hp = _primary_hp("player")
        opponent_hp = _primary_hp("opponent")
        if player_hp is not None:
            payload["player_hp"] = player_hp
        if opponent_hp is not None:
            payload["opponent_hp"] = opponent_hp

    if encounter.initiative:
        payload["initiative_order"] = [
            {"name": e.token_id, "roll": e.value} for e in encounter.initiative
        ]

    # Story 73-4 — surface the PLAYER-side beat-kind impact descriptor so the
    # overlay can explain a no-dial-move CritSuccess (clean exit / tag grant, by
    # design) instead of rendering a bare 0 that reads as a broken roll
    # (Sebastien/Jade legibility). Additive + player-focused, mirroring the
    # win_condition/player_hp legibility keys above; absent when no beat has been
    # applied yet (fresh encounter / table types).
    player_impact = encounter.last_beat_impacts.get("player")
    if player_impact is not None:
        payload["last_beat_impact"] = player_impact

    # Story 73-7 — surface the OPPONENT-side beat-kind impact alongside the
    # player's so mechanics-first players (Sebastien/Jade) can see both numeric
    # deltas and stop reading a no-dial-move CritSuccess as an unfair outcome.
    # Additive sibling to last_beat_impact; absent when the opponent hasn't acted.
    opponent_impact = encounter.last_beat_impacts.get("opponent")
    if opponent_impact is not None:
        payload["opponent_last_beat_impact"] = opponent_impact

    return payload


def resolve_magic_confrontation(
    *,
    snapshot: GameSnapshot,
    confrontation_id: str,
    branch: BranchName,
    actor: str,
) -> dict[str, Any] | None:
    """Resolve a magic confrontation outcome — Story 47-3.

    Looks up ``confrontation_id`` on ``snapshot.magic_state.confrontations``;
    if found, applies the branch's mandatory_outputs via
    ``apply_mandatory_outputs`` and returns a CONFRONTATION_OUTCOME
    payload dict matching the UI's ``ConfrontationOutcome`` shape:

        {
          "confrontation_id": str,
          "label": str,
          "branch": "clear_win" | "pyrrhic_win" | "clear_loss" | "refused",
          "mandatory_outputs": list[str],
        }

    Returns ``None`` when the confrontation is not in MagicState (not a
    magic confrontation, or magic_state not loaded). Caller decides
    whether to dispatch ``CONFRONTATION_OUTCOME`` over the WebSocket.

    No silent fallback (CLAUDE.md): a confrontation that exists but
    lacks the requested branch raises KeyError — that's a content bug
    in confrontations.yaml, not a runtime fallback decision.
    """
    if snapshot.magic_state is None:
        return None
    magic_conf = next(
        (c for c in snapshot.magic_state.confrontations if c.id == confrontation_id),
        None,
    )
    if magic_conf is None:
        return None
    branch_def = magic_conf.outcomes[branch]
    mandatory_outputs = list(branch_def.mandatory_outputs)
    apply_mandatory_outputs(
        snapshot=snapshot,
        outputs=mandatory_outputs,
        actor=actor,
    )
    return {
        "confrontation_id": confrontation_id,
        "label": magic_conf.label,
        "branch": branch,
        "mandatory_outputs": mandatory_outputs,
    }


def build_clear_confrontation_payload(
    *,
    encounter_type: str,
    genre_slug: str,
) -> dict[str, Any]:
    """Minimal payload that tells the UI to unmount the overlay.

    App.tsx:435 — ``payload.active !== false`` is the dispatch branch; an
    explicit ``false`` is what clears the overlay. Other fields are
    required by the TS interface but ignored when active=false.
    """
    return {
        "type": encounter_type,
        "label": "",
        "category": "",
        "actors": [],
        "player_metric": {},
        "opponent_metric": {},
        "beats": [],
        "secondary_stats": None,
        "genre_slug": genre_slug,
        "mood": None,
        "active": False,
    }


def project_table_frame_for_seat(table_state: TableState, *, seat_id: str | None) -> dict[str, Any]:
    """Per-recipient table frame: own private_state only (+ public state) until
    showdown, when all hands reveal. ``seat_id=None`` (unseated/lobby socket)
    gets public-only. The perception firewall pointed at table_state — no new
    perception infra (ADR-104/105 reuse)."""
    revealed = table_state.resolved_winner is not None
    seats_out = []
    for seat in table_state.seats:
        show_private = revealed or (seat_id is not None and seat.seat_id == seat_id)
        seats_out.append(
            {
                "seat_id": seat.seat_id,
                "party_name": seat.party_name,
                "is_pc": seat.is_pc,
                "status": seat.status,
                "private_state": copy.deepcopy(seat.private_state) if show_private else {},
            }
        )
    return {
        "game_kind": table_state.game_kind,
        "seats": seats_out,
        "pot": {
            "stake_kind": table_state.pot.stake_kind,
            "stake_descriptor": table_state.pot.stake_descriptor,
            "contributions": dict(table_state.pot.contributions),
        },
        "order": list(table_state.order),
        "dealer_seat": table_state.dealer_seat,
        "decision_point": table_state.decision_point,
        "max_decision_points": table_state.max_decision_points,
        "resolved_winner": table_state.resolved_winner,
    }


def make_confrontation_portrait_resolver(
    *,
    snapshot: GameSnapshot,
    genre_pack: Any,
    genre_slug: str,
) -> Callable[[str], str | None]:
    """Build the opponent-portrait resolver for ``build_confrontation_payload``'s
    ``portrait_resolver`` arg (Story 85-3). Precomputes the world's manifest
    slugs ONCE and closes over them so a multi-opponent confrontation doesn't
    rebuild the set per actor (Architect note 2026-06-04). Reuses the proven
    scrapbook resolver (``emitters._resolve_npc_portrait_url``), which emits the
    resolved/not-found OTEL spans. Opponent-only scoping and None-on-miss live in
    ``build_confrontation_payload``; this just resolves a name → world-scoped URL.
    One source of truth shared by every per-frame build site (supplier, dice
    union, websocket union, yield projection)."""
    from sidequest.server.emitters import _resolve_npc_portrait_url, _world_portrait_slugs

    world_slug = snapshot.world_slug
    manifest_slugs = _world_portrait_slugs(genre_pack, world_slug)

    def _resolve(npc_name: str) -> str | None:
        return _resolve_npc_portrait_url(
            pack=genre_pack,
            genre_slug=genre_slug,
            world_slug=world_slug,
            npc_name=npc_name,
            manifest_slugs=manifest_slugs,
        )

    return _resolve


def make_confrontation_frame_supplier(
    *,
    snapshot: GameSnapshot,
    genre_pack: Any,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    genre_slug: str,
) -> Callable[[str], ConfrontationPayload | None]:
    """Build the per-recipient CONFRONTATION supplier for ``emit_event``.

    Story 59-16 introduced this single-filtered-delivery contract on the
    post-narration path; Story 59-20 shares it across the dice mid-turn path
    and the connect-resume path so "class-filtered delivery" has one source of
    truth. The returned ``supplier(player_id)`` resolves the seated PC's class
    and returns:

    - a class-filtered ``ConfrontationPayload`` when the PC resolves;
    - ``None`` for an unseated/lobby socket (``resolve_recipient_pc`` →
      ``(None, None)``) — deliver nothing, silently;
    - ``None`` for a seated PC whose class will not resolve
      (``(None, actor)``) AFTER firing the fail-loud
      ``confrontation.recipient_unresolved`` ERROR span — never the union.

    ``emit_event`` persists the canonical union to the EventLog and delivers
    ``supplier(pid)`` to every connected socket (incl. the emitter); the union
    is never sent to a client socket.
    """
    from sidequest.protocol.messages import ConfrontationPayload
    from sidequest.telemetry.spans.encounter import (
        confrontation_recipient_unresolved_span,
        confrontation_unfiltered_delivery_span,
    )

    # Story 85-3: opponent portraits on each delivered frame (shared resolver).
    _portrait_for = make_confrontation_portrait_resolver(
        snapshot=snapshot, genre_pack=genre_pack, genre_slug=genre_slug
    )

    def _frame_for(player_id: str) -> ConfrontationPayload | None:
        recipient_pc, recipient_actor = resolve_recipient_pc(
            snapshot=snapshot,
            genre_pack=genre_pack,
            player_id=player_id,
        )
        if recipient_pc is None:
            # (None, None) ⇒ unseated/lobby socket: deliver nothing, silently.
            if recipient_actor is None:
                return None
            # (None, actor) ⇒ a SEATED PC with no resolved ClassDef. Two cases:
            #   • Pack declares NO classes at all (``genre_pack.classes`` empty).
            #     The loader explicitly permits classes.yaml-less packs
            #     (loader.py:_validate_class_filter_refs early-returns when
            #     ``not classes``), so there is simply nothing to class-filter:
            #     the seated PC gets the unfiltered beat UNION. This is NOT a
            #     silent fallback — a no-classes pack legitimately has no
            #     per-class beat restriction, so the union IS the correct
            #     projection (sq-playtest 2026-06-02 wry_whimsy/oz: the escape
            #     confrontation was invisible in solo because every seat was
            #     suppressed here).
            #   • Pack HAS classes but this PC's class is not among them
            #     (genuine config drift, e.g. a save referencing a removed
            #     class) → fail LOUD and deliver nothing, never the union.
            if genre_pack.classes:
                with confrontation_recipient_unresolved_span(
                    player_id=player_id,
                    actor=recipient_actor,
                    reason="class_def_not_found",
                    confrontation_type=encounter.encounter_type,
                ):
                    pass
                return None
            # No-classes pack: deliver the unfiltered union. Emit the
            # GM-panel lie-detector span (its absence on a no-classes turn would
            # mean the frame was dropped) and fall through with recipient_pc=None
            # so build_confrontation_payload emits the union below.
            with confrontation_unfiltered_delivery_span(
                player_id=player_id,
                actor=recipient_actor,
                confrontation_type=encounter.encounter_type,
            ):
                pass
        # WWN arm (Task 5): build_confrontation_payload now derives the
        # recipient's SpellcastingState internally from ``core_resolver`` +
        # ``recipient_actor_name`` (centralized so the yield path and any
        # future per-PC caller get WWN cast_spell gating for free). No
        # explicit ``spellcasting`` pass is needed here.
        per_pc_dict = build_confrontation_payload(
            encounter=encounter,
            cdef=cdef,
            genre_slug=genre_slug,
            recipient_pc=recipient_pc,
            recipient_actor_name=recipient_actor,
            core_resolver=snapshot.find_creature_core,
            active_stakes=snapshot.active_stakes,
            portrait_resolver=_portrait_for,
            # Story 97-3: server-authored per-beat difficulty on the offer.
            # Direct attribute access (review rework-1): live packs ALWAYS
            # have .rules (validated at load) — a missing attribute is a bug
            # that must fail loud, not degrade to a difficulty-less offer.
            rules=genre_pack.rules,
        )
        # Free-for-all N-seat table: attach a per-seat private projection so
        # each socket sees only its own hand (+ public state) until showdown.
        # Map recipient_actor (PC name) → seat_id via table_state.party_name;
        # None when the recipient is not seated at the table (lobby socket
        # or non-participant) → public-only projection.
        if encounter.table_state is not None:
            resolved_seat_id: str | None = None
            if recipient_actor is not None:
                for seat in encounter.table_state.seats:
                    if seat.party_name == recipient_actor:
                        resolved_seat_id = seat.seat_id
                        break
            per_pc_dict["table_state"] = project_table_frame_for_seat(
                encounter.table_state, seat_id=resolved_seat_id
            )
        return ConfrontationPayload(**per_pc_dict)

    return _frame_for
