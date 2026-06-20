"""Fate state projection — the single source of truth for "what Fate state exists
this turn" (ADR-144 F2b, Story 116-2).

F2a built this projection inside ``server/intent_router_pass.py`` (as
``_build_fate_summary``) for the Haiku router. F2b relocates it down to the ``game``
layer so BOTH consumers import one function:

  * the router (``_build_state_summary`` appends it to the classifier state summary), and
  * the narrator prompt builder (``orchestrator.build_narrator_prompt`` renders it into the
    ``fate_state`` prompt section via ``_build_fate_state_section``).

Keeping one projector means the router and the narrator can never drift on the aspects,
skills, and fate points in play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sidequest.game.ruleset.fate_resolution import FateOutcome, FateTier, ladder_name
from sidequest.protocol.dice import ThrowParams
from sidequest.protocol.models import (
    FateAspectEntry,
    FateCharacterEntry,
    FateConflictEntry,
    FateConflictParticipant,
    FateConsequenceEntry,
    FatePendingCompel,
    FateRollPayload,
    FateSkillEntry,
    FateStatePayload,
    FateStressBox,
    FateStuntEntry,
)
from sidequest.protocol.sanitize import sanitize_player_text

if TYPE_CHECKING:
    from sidequest.game.encounter import EncounterActor
    from sidequest.game.fate_sheet import FateSheet
    from sidequest.game.session import GameSnapshot


def build_fate_projection(snapshot: GameSnapshot) -> dict[str, Any]:
    """Compact Fate vocabulary for a turn (ADR-144 F2a/F2b).

    The per-PC skills the narrator/router can name, the fate points they can spend, the
    character aspects + live situation aspects they can invoke, and whether a conflict is
    active (the in-conflict scope of ``dispatch_fate_action``). Only PCs with a Fate sheet
    contribute; on a non-Fate pack the caller never invokes this builder.
    """
    skills: dict[str, dict[str, int]] = {}
    fate_points: dict[str, int] = {}
    character_aspects: dict[str, list[str]] = {}
    for ch in snapshot.characters:
        sheet = ch.core.fate_sheet
        if sheet is None:
            continue
        skills[ch.core.name] = dict(sheet.skills)
        fate_points[ch.core.name] = sheet.fate_points
        # Aspect text is player-authored (chargen) or LLM-authored (create_advantage);
        # it flows into the narrator prompt, so sanitize at this single source of truth
        # (ADR-047) — covers both the narrator section and the router state summary.
        character_aspects[ch.core.name] = [
            sanitize_player_text(a.text) for a in sheet.all_aspects()
        ]
    enc = snapshot.encounter
    # A resolved encounter's situation aspects are stale fiction — gate them on
    # `not enc.resolved`, the same condition `active_conflict` reads below.
    scene_aspects = (
        [sanitize_player_text(a.text) for a in enc.situation_aspects]
        if enc is not None and not enc.resolved
        else []
    )
    return {
        "skills": skills,
        "fate_points": fate_points,
        "character_aspects": character_aspects,
        "scene_aspects": scene_aspects,
        "active_conflict": enc is not None and not enc.resolved,
    }


#: The narrator's live-aspect vocabulary. The router does NOT need it: it
#: classifies a freeform action into one of the four Fate actions from the PCs'
#: skills + whether a conflict is live, not from the aspect text. These keys
#: carry the unbounded aspect strings that bloat the structured Haiku router
#: prompt and spiked ``intent_router_pass`` to 37-81s on Fate worlds (Story
#: 126-10), so they are stripped for the router.
_ROUTER_DROP_KEYS: tuple[str, ...] = ("character_aspects", "scene_aspects")


def trim_fate_projection_for_router(full: dict[str, Any]) -> dict[str, Any]:
    """Trim the full Fate projection to the routing-critical subset (Story 126-10).

    Drops the narrator's live-aspect vocabulary (``character_aspects`` +
    ``scene_aspects``) while keeping the PCs' skills, fate points, and the
    ``active_conflict`` flag the router classifies against. The narrator builds
    its own full projection (``build_fate_projection`` with no trim) and is
    untouched — ADR-144 F2b's one source of truth, two consumers, now with the
    router consuming strictly less.
    """
    return {k: v for k, v in full.items() if k not in _ROUTER_DROP_KEYS}


def _project_stress(sheet: FateSheet) -> dict[str, list[FateStressBox]]:
    """A Fate sheet's stress tracks in the wire shape — the single mapping shared by
    the PC ``FateCharacterEntry`` and an opponent ``FateConflictParticipant``."""
    return {
        track_name: [FateStressBox(value=b.value, checked=b.checked) for b in track.boxes]
        for track_name, track in sheet.stress.items()
    }


def _project_consequences(sheet: FateSheet) -> list[FateConsequenceEntry]:
    """A Fate sheet's consequence slots in the wire shape (open vs filled) — shared
    by the PC ``FateCharacterEntry`` and an opponent ``FateConflictParticipant``."""
    return [
        FateConsequenceEntry(
            level=c.level,
            value=c.value,
            filled=c.aspect is not None,
            text=c.aspect.text if c.aspect is not None else "",
        )
        for c in sheet.consequences
    ]


def _project_conflict_participant(
    actor: EncounterActor, snapshot: GameSnapshot
) -> FateConflictParticipant:
    """Project one seated actor onto the wire (playtest 150-2 server follow-up).

    An OPPONENT-side actor carries its stress/consequence track, resolved from its
    NPC ``core.fate_sheet`` (seeded by #966), so the UI can draw the opponent track +
    the win meter — per ADR-143 the meter is the opponent's stress fill toward
    taken-out, NOT the native tension dial (we read the FateSheet, never the dial). A
    player actor leaves the track empty: its full sheet already rides in
    ``FateStatePayload.characters``, so the participant never duplicates it. A
    non-player actor with no resolvable sheet projects an empty track — the honest
    empty state; the seated-without-a-sheet invariant is enforced loudly in
    ``decide_opponent_action`` (the #966 guard), never masked here.
    """
    stress: dict[str, list[FateStressBox]] = {}
    consequences: list[FateConsequenceEntry] = []
    if actor.side != "player":
        core = snapshot.find_creature_core(actor.name)
        sheet = core.fate_sheet if core is not None else None
        if sheet is not None:
            stress = _project_stress(sheet)
            consequences = _project_consequences(sheet)
    return FateConflictParticipant(
        name=actor.name, side=actor.side, stress=stress, consequences=consequences
    )


def conflict_opponent_progress(conflict: FateConflictEntry | None) -> list[tuple[str, float]]:
    """Per opponent-side participant with a projected track, the taken-out progress.

    The ADR-143 win-meter number: used absorption (checked stress boxes + filled
    consequence slots) over total absorption. ``1.0`` means the track is full — the
    next overflowing hit takes the opponent out. Computed from the WIRE payload (the
    projected participant), so the GM-panel ``fate.conflict.projected`` span confirms
    exactly what the client received, not a parallel read of server state. A sheetless
    participant (capacity 0) and every player participant are excluded — they have no
    opponent meter to draw.
    """
    out: list[tuple[str, float]] = []
    if conflict is None:
        return out
    for p in conflict.participants:
        if p.side == "player":
            continue
        capacity = sum(b.value for boxes in p.stress.values() for b in boxes) + sum(
            c.value for c in p.consequences
        )
        if capacity <= 0:
            continue
        used = sum(b.value for boxes in p.stress.values() for b in boxes if b.checked) + sum(
            c.value for c in p.consequences if c.filled
        )
        out.append((p.name, used / capacity))
    return out


def build_fate_state_payload(snapshot: GameSnapshot) -> FateStatePayload:
    """The full client Fate projection (ADR-144 F3a / Story 118-1).

    The rich, structured sibling of :func:`build_fate_projection`: where the
    compact projection feeds the router/narrator a flat vocabulary, this builds
    the player-facing wire payload (``FATE_STATE``) — per-PC fate points/refresh,
    skills on the ladder, named aspects with kind + free-invoke counts, the two
    stress tracks, the four consequence slots (open vs filled), the scene's
    situation aspects + boosts, and the active conflict's participants by side.

    Reads the SAME snapshot state as the compact projection (one source of
    truth) — only PCs with a Fate sheet contribute; on a non-Fate pack the
    emitter never invokes this builder. Display text is presented raw: it feeds
    the UI (which escapes it), not the narrator prompt (the compact projection
    is the prompt path that sanitizes per ADR-047).
    """
    characters: list[FateCharacterEntry] = []
    for ch in snapshot.characters:
        sheet = ch.core.fate_sheet
        if sheet is None:
            continue
        characters.append(
            FateCharacterEntry(
                name=ch.core.name,
                fate_points=sheet.fate_points,
                refresh=sheet.refresh,
                skills=[
                    FateSkillEntry(name=name, rating=rating, ladder=ladder_name(rating))
                    for name, rating in sheet.skills.items()
                ],
                # Named aspects only — a FILLED consequence is invokable but
                # surfaces in ``consequences`` below, never duplicated here.
                aspects=[
                    FateAspectEntry(text=a.text, kind=a.kind, free_invokes=a.free_invokes)
                    for a in sheet.aspects
                ],
                stress=_project_stress(sheet),
                consequences=_project_consequences(sheet),
                # Playtest 150-2: under Fate a PC's special abilities ARE their
                # stunts — project them so the Character/Fate panel can render them
                # in place of the native class-move surface. Display text is raw
                # (the UI escapes it), consistent with the rest of this builder.
                stunts=[
                    FateStuntEntry(
                        name=st.name, description=st.description, source_gear=st.source_gear
                    )
                    for st in sheet.stunts
                ],
            )
        )

    enc = snapshot.encounter
    # A resolved encounter's situation aspects are stale fiction and the
    # conflict is over — gate both on `not enc.resolved` (same condition the
    # compact projection's `active_conflict` reads). The guard is inlined per
    # branch so the type checker narrows `enc` to non-None.
    scene_aspects = (
        [
            FateAspectEntry(text=a.text, kind=a.kind, free_invokes=a.free_invokes)
            for a in enc.situation_aspects
        ]
        if enc is not None and not enc.resolved
        else []
    )
    conflict = (
        FateConflictEntry(
            active=True,
            # Seating order is the engine's tiebreak order
            # (fate_opponent._live_player_actors); preserve encounter.actors order.
            # An opponent-side participant carries its stress/consequence track for
            # the win meter (ADR-143); a player participant's sheet rides in
            # `characters` above (see _project_conflict_participant).
            participants=[_project_conflict_participant(a, snapshot) for a in enc.actors],
            # ADR-144 F3e: surface the narrator's offered compels so the player
            # surface can render its accept/refuse control. Display text is raw
            # (the UI escapes it), consistent with the rest of this builder.
            pending_compels=[
                FatePendingCompel(
                    aspect=c.aspect,
                    target=c.target,
                    reason=c.reason,
                    # The SRD accept reward (+1) travels to the player surface so the
                    # Accept control shows a real delta, not a hardcoded literal.
                    offered_delta=c.offered_delta,
                )
                for c in enc.pending_compels
            ],
        )
        if enc is not None and not enc.resolved
        else None
    )
    return FateStatePayload(characters=characters, scene_aspects=scene_aspects, conflict=conflict)


# A non-degenerate default tumble for the 3D FateDiceTray (Story 125-4 / ADR-144
# F3g). Mirrors the dF fallback gesture in handlers/dice_throw.py — velocity +
# angular are constant; the per-roll variation lives in the seed (which drives the
# dice' initial rotation in replayThrowParams on the client).
_DEFAULT_FATE_THROW = ThrowParams(
    velocity=(0.0, 4.0, -1.0),
    angular=(0.5, 0.5, 0.5),
    position=(0.5, 0.5),
)


def _fallback_seed(dice: tuple[int, int, int, int]) -> int:
    """A deterministic dice-derived seed for callers that don't supply a session
    seed (tests / non-handler projection). The production caller
    (``handlers/fate_action``) passes a per-turn seed via ``generate_dice_seed``
    so each roll re-throws even when two rolls land on the same faces."""
    s = 0
    for face in dice:
        s = s * 7 + (face + 1)  # face in {-1, 0, 1} -> {0, 1, 2}
    return s + 1


def build_fate_roll_payload(
    outcome: FateOutcome,
    *,
    seed: int | None = None,
    throw_params: ThrowParams | None = None,
) -> FateRollPayload:
    """Project a resolved 4dF roll onto the wire (ADR-144 F3c / Story 118-3).

    Faithful, lossless map of the engine's ``FateOutcome`` to the player-facing
    ``FATE_ROLL`` payload, adding the two derived legibility fields: the ladder
    ADJECTIVE (the player reads "Great", not "+4") and the succeed-with-style
    flag. The raw dice tuple previously reached only the OTEL span.

    ``throw_params`` + ``seed`` drive the 3D FateDiceTray replay animation (Story
    125-4 / ADR-144 F3g): the dice tumble instead of rendering the idle pickup
    row. For a PLAYER throw (ADR-148, Story 126-7) the caller passes the THROWER's
    own ``throw_params`` so every seat replays the identical tumble; for an NPC
    roll (no client gesture) it defaults to the synthesized ``_DEFAULT_FATE_THROW``.
    The production caller supplies a per-turn ``seed`` so each roll re-throws,
    while a dice-derived fallback keeps the standalone projection self-consistent.
    """
    return FateRollPayload(
        dice=outcome.dice,
        roll_total=outcome.roll_total,
        ladder_total=outcome.ladder_total,
        ladder_name=ladder_name(outcome.ladder_total),
        opposition=outcome.opposition,
        shifts=outcome.shifts,
        tier=str(outcome.tier),
        succeeded_with_style=outcome.tier == FateTier.SucceedWithStyle,
        throw_params=throw_params if throw_params is not None else _DEFAULT_FATE_THROW,
        seed=seed if seed is not None else _fallback_seed(outcome.dice),
    )
