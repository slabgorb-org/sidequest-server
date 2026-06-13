"""Shared ADR-110 snapshot slimming — Phase B drop + Phase C projections.

Story 82-10. Extracted verbatim from ``session_helpers._build_turn_context``
(the narrator consumer) so the ADR-113 Intent Router pass — the second
consumer of a ``snapshot.model_dump()`` JSON payload — applies the SAME
audited cut before layering its router-specific projections
(``confrontation_types``, ``witnessed_act_vocabulary``).

Per the ADR-110 amendment ("extract-and-reuse, not a second slimmer"):
the narrator's prompt-specific mutations (sealed-letter handshake /
``party_formation`` / ``shared_world_delta`` merge, notorious-party
redaction, room-state-injection span) are NOT extracted — they stay in
``_build_turn_context``. This module owns only the consumer-agnostic cut:

* **Phase B** (``_PHASE_B_DROP_FIELDS``) — field-pruning drop list.
* **Phase C** (``_apply_phase_c_projections``) — per-field projections for
  the four growing snapshot fields (room_states → current room only, npcs →
  in-scene only + belief_state strip, characters[*].known_facts → tail-K,
  scenario_state.discovered_clues → cap).

``session_helpers`` re-exports these names so the 61-5 governance gate
(``test_snapshot_field_governance.py``) and the 61-2 projection contract
tests keep their import surface unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sidequest.game.npc_scene import is_npc_anchored_by_encounter, is_npc_in_scene

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot

# Story 57-5 / ADR-110 Phase B — field-pruning drop list. These fields are
# either re-rendered in dedicated prompt sections (active_tropes,
# axis_values) or belong to deferred subsystems (genie_wishes,
# achievement_tracker). ``pop`` with default tolerates the Phase-A case
# where ``exclude_defaults`` has already removed an empty entry. The named
# registry is the single source of truth for "dropped top-level fields"
# enforced by the ``test_snapshot_field_governance`` gate.
_PHASE_B_DROP_FIELDS: tuple[str, ...] = (
    "active_tropes",
    "axis_values",
    "genie_wishes",
    "achievement_tracker",
    "narrative_log",
    # political_state (wry_whimsy, Plan 2). Stripped from the narrator
    # state-summary payload: the narrator learns what moved via the
    # witnessed_act subsystem's must_narrate directive, not from the raw
    # dials, and the ledger grows one entry per witnessed act (unbounded by
    # construction). DROP affects only the prompt payload — the dials
    # persist in the save and feed the engine + the future UI Standing panel
    # (Plan 4), which may promote this to a bounded _PHASE_C projection.
    "political_state",
    # region_transitions (Story 59-30 - movement engagement ledger). Same
    # rationale as political_state: a per-PC relocation ledger that grows one
    # entry per move (unbounded by construction) and exists for the movement
    # engagement witness + GM-panel/forensics (ADR-124), NOT the narrator -
    # the narrator learns the PC moved from the scene prose, not the ledger.
    # DROP affects only the prompt payload; the ledger persists in the save.
    "region_transitions",
    # wwn_spell_cast_log (Story 102-3 - magic_working engagement ledger).
    # Same rationale as region_transitions: turn-stamped WN cast receipts
    # that grow one entry per cast (unbounded by construction) and exist for
    # the magic_working engagement witness + GM-panel/forensics (ADR-124),
    # NOT the narrator - the narrator learns the cast outcome from the
    # handler's must_narrate directive, not the ledger. DROP affects only
    # the prompt payload; the ledger persists in the save.
    "wwn_spell_cast_log",
    # mutation_use_log (Story 102-7 - AWN mutation engagement ledger). Exact
    # analog of wwn_spell_cast_log: turn-stamped mutation-use receipts that
    # grow one entry per use (unbounded by construction) and exist for the
    # magic_working engagement witness + GM-panel/forensics (ADR-124), NOT
    # the narrator - the narrator learns the mutation outcome from the
    # handler's must_narrate directive, not the ledger. DROP affects only
    # the prompt payload; the ledger persists in the save.
    "mutation_use_log",
    # next_turn_directives (sq-playtest 2026-06-13 directive-leak). The
    # one-shot directive queue is rendered into the dedicated `intent_directives`
    # Recency guardrail (orchestrator `_consume_next_turn_directives`) — the
    # narrator reads the directive CONTENT there, in natural language. It must
    # NOT also ride the `<game_state>` blob as a raw JSON field: when it did,
    # the narrator echoed the literal field name into player prose ("…per the
    # next_turn_directives, the entity's Strike missed…"). DROP affects only the
    # prompt payload; the queue persists in the save (populate-this-turn /
    # consume-next-turn discipline survives a save/reload — see
    # tests/game/test_snapshot_next_turn_directives.py).
    "next_turn_directives",
)

# Story 61-2 / ADR-110 — projection tunings for the four growing fields
# that DO ride into ``snapshot.model_dump()``. See
# ``.session/61-2-session.md`` and the test contract at
# ``tests/server/test_61_2_snapshot_seven_field_projection.py``.
#
# ``known_facts`` tail mirrors ``persistence.py:889`` (journal-render
# tail-of-8 pattern). ``discovered_clues`` cap is from ADR-110 Phase C
# (size-only — discovered_clues is a set, ordering keyed by clue id for
# determinism — open question #3 in red-phase notes settled here).
_KNOWN_FACTS_TAIL_K = 8
_DISCOVERED_CLUES_CAP = 12


def _apply_phase_c_projections(
    snapshot: GameSnapshot,
    payload: dict,
    *,
    current_room_id: str | None,
) -> dict[str, int]:
    """Mutate ``payload`` in place to apply the 61-2 per-field projections.

    ``current_room_id`` is resolved ONCE by the caller from
    ``snapshot.party_location(perspective=...)``; the caller is also
    responsible for emitting the ``actor_location_empty`` warning (and
    deciding whether the turn is salvageable). This split exists because
    a per-NPC ``snapshot.party_location(...)`` call would fan out N+1
    ``snapshot.party_location_query`` OTEL spans per turn and drown
    the lie-detector signal in the GM panel.

    When ``current_room_id`` is ``None`` or empty the ``room_states``
    and ``npcs`` projections are SKIPPED — degraded actor location is
    NOT the same as "no rooms exist" / "no NPCs exist" and silently
    stripping them is the projection-as-gaslighter pattern this story
    exists to NOT introduce. The ``known_facts`` and
    ``discovered_clues`` projections are PC/scenario-scoped and still
    run (they don't depend on actor location).

    Returns a dict of projection counts (for the
    ``prompt.game_state.bytes`` span attributes — the GM-panel
    lie-detector contract):

    * ``room_states_dropped`` — count of room ids removed from
      ``payload["room_states"]`` (zero when the projection was skipped).
    * ``npcs_dropped`` — count of NPCs filtered out by the in-scene
      predicate as legitimately off-stage (zero when the projection
      was skipped). Excludes unresolvable-name drops (counted
      separately below).
    * ``known_facts_truncated_total`` — sum across PCs of facts
      truncated past the tail-K window.
    * ``clues_truncated`` — count of ``discovered_clues`` over the cap.
    * ``encounter_anchored_count`` (Story 61-7) — count of NPCs kept
      because they appear in the active encounter's ``actors`` list,
      not because their location matched. Zero when the projection
      was skipped.
    * ``npcs_unresolvable_name_dropped`` (Story 61-8 §D1) — count of
      NPC payload entries whose ``core.name`` / top-level ``name``
      extraction yielded a falsy value (data-shape drift,
      GM-panel-actionable). Reported separately from ``npcs_dropped``
      so a serialization regression doesn't masquerade as legitimate
      off-scene filtering.
    """
    counts: dict[str, int] = {
        "room_states_dropped": 0,
        "npcs_dropped": 0,
        "known_facts_truncated_total": 0,
        "clues_truncated": 0,
        # Story 61-7 (review-fix round 2) — OTEL Observability Principle:
        # the GM panel must distinguish NPCs kept by location match from
        # NPCs kept by the encounter-actor override branch. ``npcs_dropped``
        # alone is silent on which branch fired. See
        # ``sidequest.game.npc_scene.is_npc_anchored_by_encounter``.
        "encounter_anchored_count": 0,
        # Story 61-8 §D1 — silent-failure-hunter follow-up. The
        # in-scene filter loop below silently drops payload entries
        # whose ``core.name`` / ``name`` extraction yields a falsy
        # value (typically ``None`` from a malformed dict in the
        # pre-projection snapshot serialization). Pre-§D1 these were
        # rolled into ``npcs_dropped`` and indistinguishable from
        # legitimate off-scene drops — the GM panel had no way to see
        # data-shape drift. Now reported separately.
        "npcs_unresolvable_name_dropped": 0,
    }

    # ---------------------------------------------------------------
    # room_states + npcs — both depend on actor location. When the
    # caller couldn't resolve it (and has already logged the
    # actor_location_empty warning), skip both projections rather than
    # gaslight the narrator with "no rooms / no NPCs". The pass-through
    # path costs a few more bytes; the alternative is the narrator
    # confabulating into an empty world.
    # ---------------------------------------------------------------
    if current_room_id:
        # room_states — keep only the acting PC's current room.
        room_states = payload.get("room_states", {})
        before = len(room_states)
        if current_room_id in room_states:
            payload["room_states"] = {current_room_id: room_states[current_room_id]}
        else:
            # current_room_id is set but no RoomState exists for it yet
            # (no container retrieval recorded). Empty dict preserves
            # the structural anchor; every other room id is dropped.
            payload["room_states"] = {}
        counts["room_states_dropped"] = before - len(payload["room_states"])

        # npcs — in-scene-only projection + nested belief_state strip.
        # Story 61-7 unifies the in-scene predicate with the
        # ``list_npcs_in_scene`` tool — see
        # ``sidequest.game.npc_scene.is_npc_in_scene``. ``npc.core.name``
        # is guaranteed non-empty by ``CreatureCore.name_non_blank``
        # field validator (``sidequest/game/creature_core.py:239``);
        # the set-add cannot silently collapse identities under the
        # current model invariant. See
        # ``test_upstream_creaturecore_validator_blocks_empty_npc_names``
        # for the regression guard that pins the invariant.
        encounter = snapshot.encounter
        encounter_anchored = 0
        npcs_payload = payload.get("npcs", [])
        in_scene_names: set[str] = set()
        for npc in snapshot.npcs:
            if is_npc_in_scene(npc, current_room=current_room_id, encounter=encounter):
                in_scene_names.add(npc.core.name)
                if is_npc_anchored_by_encounter(npc, encounter):
                    encounter_anchored += 1
        counts["encounter_anchored_count"] = encounter_anchored
        before = len(npcs_payload)
        kept: list[dict] = []
        unresolvable_name_dropped = 0
        for entry in npcs_payload:
            core = entry.get("core")
            entry_name = core.get("name") if isinstance(core, dict) else entry.get("name")
            # Story 61-8 §D1 — distinguish "name shape was wrong" (data
            # drift, GM-panel actionable) from "NPC is off-scene" (the
            # projection working as designed). A name-key that yields a
            # falsy value (None, empty string) cannot match
            # ``in_scene_names`` (which is sourced from
            # ``CreatureCore.name_non_blank``-validated NPCs); count
            # those separately so the GM panel can spot serialization
            # regressions instead of attributing them to legitimate
            # drops.
            if not entry_name:
                unresolvable_name_dropped += 1
                continue
            if entry_name in in_scene_names:
                # Strip nested belief_state — dispatch-side state, not
                # prompt-side (ADR-053 gossip propagation).
                entry.pop("belief_state", None)
                kept.append(entry)
        payload["npcs"] = kept
        counts["npcs_dropped"] = before - len(kept) - unresolvable_name_dropped
        counts["npcs_unresolvable_name_dropped"] = unresolvable_name_dropped

    # ---------------------------------------------------------------
    # characters[*].known_facts — per-PC tail-K projection. PC-scoped,
    # runs regardless of actor location.
    # ---------------------------------------------------------------
    chars_payload = payload.get("characters", [])
    truncated_total = 0
    for char_entry in chars_payload:
        facts = char_entry.get("known_facts")
        if isinstance(facts, list) and len(facts) > _KNOWN_FACTS_TAIL_K:
            truncated_total += len(facts) - _KNOWN_FACTS_TAIL_K
            char_entry["known_facts"] = facts[-_KNOWN_FACTS_TAIL_K:]
    counts["known_facts_truncated_total"] = truncated_total

    # ---------------------------------------------------------------
    # scenario_state.discovered_clues — size cap (ordered by clue id
    # for determinism; the field is a set in the source so insertion
    # order is not preserved). Scenario-scoped, runs regardless of
    # actor location.
    # ---------------------------------------------------------------
    scenario_payload = payload.get("scenario_state")
    if isinstance(scenario_payload, dict):
        clues = scenario_payload.get("discovered_clues")
        if isinstance(clues, list) and len(clues) > _DISCOVERED_CLUES_CAP:
            counts["clues_truncated"] = len(clues) - _DISCOVERED_CLUES_CAP
            scenario_payload["discovered_clues"] = sorted(clues)[:_DISCOVERED_CLUES_CAP]

    return counts


def apply_snapshot_slimming(
    snapshot: GameSnapshot,
    payload: dict,
    *,
    current_room_id: str | None,
) -> dict[str, int]:
    """Apply the consumer-agnostic ADR-110 cut: Phase B drop + Phase C
    projections. Mutates ``payload`` in place; returns the Phase C
    projection counts (the GM-panel lie-detector contract).

    Phase B/C were audited against the *narrator's* anti-confabulation
    needs; the Intent Router has a strictly weaker need (it only picks
    which dispatch handlers fire), so every field safe to drop for the
    narrator is safe to drop for the router (ADR-110 amendment, "Why this
    is safe for the router").
    """
    for _drop_field in _PHASE_B_DROP_FIELDS:
        payload.pop(_drop_field, None)
    return _apply_phase_c_projections(snapshot, payload, current_room_id=current_room_id)
