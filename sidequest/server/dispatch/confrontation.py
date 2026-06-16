"""Confrontation-def lookup + CONFRONTATION payload assembly.

Port of sidequest-api/crates/sidequest-server/src/dispatch/response.rs
confrontation-def resolution and payload construction. Story 3.4.

Story 47-3 (Phase 5) extends this with magic-confrontation outcome
resolution: ``resolve_magic_confrontation`` looks up a magic
confrontation by id, applies its branch's mandatory_outputs, and
returns a CONFRONTATION_OUTCOME payload for the WebSocket dispatcher.
"""

from __future__ import annotations

from typing import Any

from sidequest.game.encounter import StructuredEncounter
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import ConfrontationDef
from sidequest.magic.confrontations import BranchName
from sidequest.magic.outputs import apply_mandatory_outputs

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
) -> dict[str, Any]:
    """Assemble the CONFRONTATION payload the UI overlay consumes.

    Shape fixed by sidequest-ui/src/components/ConfrontationOverlay.tsx:42-58.
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

    ``recipient_actor_name`` is the PC actor's display name and is used
    only to stamp the OTEL span attributes — it has no effect on the
    payload shape. Defaults to ``"recipient"`` when omitted.

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
        filtered = beats_available_for(
            cdef,
            class_def,
            spell_slots_remaining=spell_slots,
            prepared_spells=prepared_spells,
        )
        rejection_reason = cast_spell_rejection_reason(
            cdef,
            class_def,
            spell_slots_remaining=spell_slots,
            prepared_spells=prepared_spells,
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

    return {
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
