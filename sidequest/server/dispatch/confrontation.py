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
from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.game.table.types import TableState
from sidequest.game.wwn_magic import SpellcastingState
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import ConfrontationDef
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


def find_confrontation_def(
    defs: list[ConfrontationDef],
    encounter_type: str,
) -> ConfrontationDef | None:
    """Return the ConfrontationDef whose ``confrontation_type`` equals ``encounter_type``.

    Exact string match — mirrors Rust's ``iter().find(|d| d.type == ty)``.
    Returns ``None`` when no def matches; callers MUST handle the miss
    (CLAUDE.md: no silent fallback — caller decides whether to error).
    """
    for d in defs:
        if d.confrontation_type == encounter_type:
            return d
    return None


def build_confrontation_payload(
    *,
    encounter: StructuredEncounter,
    cdef: ConfrontationDef,
    genre_slug: str,
    recipient_pc: RecipientPc | None = None,
    recipient_actor_name: str | None = None,
    core_resolver: Callable[[str], CreatureCore | None] | None = None,
    spellcasting: SpellcastingState | None = None,
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
    """
    if encounter.mood_override is not None:
        mood = encounter.mood_override
    elif cdef.mood is not None:
        mood = cdef.mood
    else:
        mood = ""

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
        effective_spellcasting = spellcasting
        if effective_spellcasting is None and core_resolver is not None and recipient_actor_name:
            recipient_core = core_resolver(recipient_actor_name)
            if recipient_core is not None:
                effective_spellcasting = recipient_core.spellcasting
        filtered = beats_available_for(
            cdef,
            class_def,
            spell_slots_remaining=spell_slots,
            prepared_spells=prepared_spells,
            spellcasting=effective_spellcasting,
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

    payload: dict[str, Any] = {
        "type": encounter.encounter_type,
        "label": cdef.label,
        "category": cdef.category,
        "actors": [a.model_dump(mode="json") for a in encounter.actors],
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
    }

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
