"""Fate conflict fixtures for the DEFEND-barrier slice (story 126-8).

These builders construct *real* ``StructuredEncounter`` + ``GameSnapshot`` state
with seated Fate PCs/NPCs so the dispatch-layer tests drive the actual exchange,
not a stub (server CLAUDE.md: behavioral fixtures, never source-text wiring).

Two of the builders (``parked_conflict`` / ``parked_conflict_filled``) reference
``FatePendingDefense`` / ``encounter.pending_defenses`` — the ledger that story
126-8 ADDS. They import it lazily so that during RED the failure is "the model
does not exist yet" (missing production code), not a collection-time crash that
also takes down the builder that needs none of it (``conflict_with_pc_and_npc``).
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    FateSealedCommit,
    StructuredEncounter,
)
from sidequest.game.fate_sheet import FateSheet
from sidequest.game.session import GameSnapshot, Npc


def _pc(name: str, skills: dict[str, int]) -> Character:
    """A player character with a Fate sheet (mirrors the canonical wiring fixture)."""
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _healthy_npc(name: str, skills: dict[str, int]) -> Npc:
    """A LIVE opponent with a fresh Fate sheet — full stress, empty consequences —
    so it is not taken out at REVEAL and ``decide_opponent_action`` seats it
    attacking the highest-threat live PC (deterministic per fate_opponent.py)."""
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Npc(core=core)


def conflict_with_pc_and_npc(
    *,
    pc: str = "Rux",
    npc: str = "Bandit",
    npc_targets_pc: bool = True,
) -> tuple[GameSnapshot, StructuredEncounter]:
    """A physical Fate conflict ready for ``dispatch_fate_action``.

    ``npc_targets_pc=True`` (default): seats one PC and one LIVE opponent. When the
    barrier closes the opponent is seated attacking the PC (deterministic
    targeting), so the round PARKS at the DEFEND barrier.

    ``npc_targets_pc=False``: seats the PC only (no live opponent), so a proactive
    ``overcome`` against a passive difficulty resolves immediately — nothing
    targets a PC, so there is no DEFEND barrier.
    """
    pc_skills = {"Fight": 2, "Athletics": 2, "Notice": 1, "Will": 1}
    actors = [EncounterActor(name=pc, role="lead", side="player")]
    if npc_targets_pc:
        actors.append(EncounterActor(name=npc, role="foe", side="opponent"))

    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )
    snap = GameSnapshot(genre_slug="fate_test", characters=[_pc(pc, pc_skills)], encounter=enc)
    if npc_targets_pc:
        snap.npcs.append(_healthy_npc(npc, {"Fight": 2, "Athletics": 1, "Notice": 1}))
    return snap, enc


def _parked_base(
    *,
    defender: str,
    attacker: str,
    attack_total: int,
    defend_skill: str,
    defend_skill_rating: int,
) -> tuple[GameSnapshot, StructuredEncounter]:
    """Common spine for a parked encounter: PC defender + NPC attacker both seated
    with Fate sheets, plus the NPC's sealed attack commit (so RESUME has something
    to walk). The caller appends the ``pending_defenses`` entry."""
    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name=defender, role="lead", side="player"),
            EncounterActor(name=attacker, role="foe", side="opponent"),
        ],
    )
    # The NPC's attack was sealed + locked at REVEAL (4dF rolled there, never
    # re-rolled across the suspend). dice are decoration; ladder_total is the math.
    enc.fate_commits.append(
        FateSealedCommit(
            actor=attacker,
            action="attack",
            skill="Fight",
            target=defender,
            ladder_total=attack_total,
            dice=(1, 1, 1, 0),
        )
    )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[_pc(defender, {defend_skill: defend_skill_rating, "Notice": 1})],
        encounter=enc,
    )
    snap.npcs.append(_healthy_npc(attacker, {"Fight": 2, "Athletics": 1, "Notice": 1}))
    return snap, enc


def parked_conflict(
    *,
    defender: str = "Rux",
    attacker: str = "Bandit",
    request_id: str = "d1",
    attack_total: int = 4,
    defend_skill: str = "Athletics",
    defend_skill_rating: int = 2,
) -> tuple[GameSnapshot, StructuredEncounter]:
    """A conflict PARKED at the DEFEND barrier: one unfilled ``pending_defenses``
    entry awaiting the PC's interactive defense (defense_total is None)."""
    from sidequest.game.encounter import FatePendingDefense

    snap, enc = _parked_base(
        defender=defender,
        attacker=attacker,
        attack_total=attack_total,
        defend_skill=defend_skill,
        defend_skill_rating=defend_skill_rating,
    )
    enc.pending_defenses.append(
        FatePendingDefense(
            request_id=request_id,
            attacker=attacker,
            defender=defender,
            attack_skill="Fight",
            attack_total=attack_total,
        )
    )
    return snap, enc


def parked_conflict_filled(
    *,
    defender: str = "Rux",
    attacker: str = "Bandit",
    request_id: str = "d1",
    attack_total: int = 5,
    recorded_defense_total: int = 2,
    defend_skill: str = "Athletics",
    defend_skill_rating: int = 2,
) -> tuple[GameSnapshot, StructuredEncounter]:
    """A parked conflict whose lone ``pending_defenses`` entry is ALREADY filled
    (the PC's defense_total is recorded) — ready for RESUME. The defender carries a
    fresh sheet so an unabsorbed hit lands real stress/consequences/taken-out."""
    from sidequest.game.encounter import FatePendingDefense

    snap, enc = _parked_base(
        defender=defender,
        attacker=attacker,
        attack_total=attack_total,
        defend_skill=defend_skill,
        defend_skill_rating=defend_skill_rating,
    )
    enc.pending_defenses.append(
        FatePendingDefense(
            request_id=request_id,
            attacker=attacker,
            defender=defender,
            attack_skill="Fight",
            attack_total=attack_total,
            defense_total=recorded_defense_total,
        )
    )
    return snap, enc


def parked_conflict_two_defenders(
    *,
    defenders: tuple[str, str] = ("Rux", "Vala"),
    attackers: tuple[str, str] = ("Bandit", "Brigand"),
    request_ids: tuple[str, str] = ("d1", "d2"),
    attack_total: int = 4,
    defend_skill: str = "Athletics",
    defend_skill_rating: int = 2,
) -> tuple[GameSnapshot, StructuredEncounter]:
    """A conflict PARKED at the DEFEND barrier with TWO PC defenders — two unfilled
    ``pending_defenses`` entries. ``parked_conflict`` only ever builds ONE entry, so
    this is the fixture that exercises a PARTIAL fill: filling one defender's entry
    leaves ``ledger_full`` False (the ``all(...)`` gate keeps the barrier open until
    EVERY defender has answered), and filling both closes it.

    Each defender is seated opposite their own NPC attacker, each with a sealed
    attack commit, mirroring ``_parked_base`` per (attacker, defender) pair."""
    from sidequest.game.encounter import FatePendingDefense

    (d_a, d_b), (a_a, a_b), (r_a, r_b) = defenders, attackers, request_ids
    pairs = ((a_a, d_a, r_a), (a_b, d_b, r_b))

    enc = StructuredEncounter(
        encounter_type="duel",
        category="combat",
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=[
            EncounterActor(name=d_a, role="lead", side="player"),
            EncounterActor(name=d_b, role="lead", side="player"),
            EncounterActor(name=a_a, role="foe", side="opponent"),
            EncounterActor(name=a_b, role="foe", side="opponent"),
        ],
    )
    for attacker, defender, _rid in pairs:
        enc.fate_commits.append(
            FateSealedCommit(
                actor=attacker,
                action="attack",
                skill="Fight",
                target=defender,
                ladder_total=attack_total,
                dice=(1, 1, 1, 0),
            )
        )
    snap = GameSnapshot(
        genre_slug="fate_test",
        characters=[
            _pc(d_a, {defend_skill: defend_skill_rating, "Notice": 1}),
            _pc(d_b, {defend_skill: defend_skill_rating, "Notice": 1}),
        ],
        encounter=enc,
    )
    for attacker in (a_a, a_b):
        snap.npcs.append(_healthy_npc(attacker, {"Fight": 2, "Athletics": 1, "Notice": 1}))
    for attacker, defender, rid in pairs:
        enc.pending_defenses.append(
            FatePendingDefense(
                request_id=rid,
                attacker=attacker,
                defender=defender,
                attack_skill="Fight",
                attack_total=attack_total,
            )
        )
    return snap, enc


def _faces_for_sum(total: int) -> tuple[int, int, int, int]:
    """Build a 4dF face tuple summing to ``total`` (each face in {-1, 0, 1}). Fails
    loud if ``total`` is outside the [-4, 4] a single 4dF throw can express — a test
    asking for an impossible defense should break, not silently clamp."""
    if not -4 <= total <= 4:
        raise ValueError(f"4dF cannot sum to {total} (range is [-4, 4])")
    faces = [0, 0, 0, 0]
    step = 1 if total >= 0 else -1
    for i in range(abs(total)):
        faces[i] = step
    return (faces[0], faces[1], faces[2], faces[3])


def resolve_parked_defenses(
    *,
    encounter,
    snapshot,
    ruleset,
    round_number: int = 0,
    defense_skill: str = "Athletics",
    target_shift: int | None = None,
    rng=None,
    _tracer=None,
):
    """Drive a PARKED Fate exchange (story 126-8) through to RESOLVE: record one
    thrown PC defense per unfilled ``encounter.pending_defenses`` entry, then RESUME.

    Mirrors what the live handler does when the ledger fills, so a pre-126-8 test
    that used to resolve in a single ``dispatch_fate_action`` call can reach the
    SAME resolved state through the new two-phase (COMMIT → DEFEND → RESOLVE) flow.
    Reads the ledger off the encounter (not a ``FateDispatchResult``) so it works
    whether the round was parked via ``dispatch_fate_action`` or the subsystem bank.

    ``target_shift=None`` throws a maximal defense — used where the PC's own
    proactive action decides the outcome (e.g. a depleted opponent the PC takes out
    regardless of its doomed counter-swing). An int throws faces calibrated so each
    incoming attack lands with EXACTLY that many shifts
    (``defense_total = attack_total - target_shift``), reproducing the old
    server-rolled-defense calibration deterministically now that the PC defense is
    physics-is-the-roll (ADR-148/149). Returns the ``FateExchangeResult``."""
    from sidequest.server.dispatch.fate_conflict import (
        dispatch_fate_defense,
        resume_fate_exchange,
    )

    for entry in list(encounter.pending_defenses):
        if entry.defense_total is not None or entry.conceded:
            continue
        if target_shift is None:
            faces = (1, 1, 1, 1)
        else:
            core = snapshot.find_creature_core(entry.defender)
            rating = core.fate_sheet.skills.get(defense_skill, 0) if core and core.fate_sheet else 0
            faces = _faces_for_sum(entry.attack_total - target_shift - rating)
        dispatch_fate_defense(
            encounter=encounter,
            snapshot=snapshot,
            ruleset=ruleset,
            actor_name=entry.defender,
            request_id=entry.request_id,
            skill=defense_skill,
            thrown_faces=faces,
            _tracer=_tracer,
        )
    return resume_fate_exchange(
        encounter=encounter,
        snapshot=snapshot,
        ruleset=ruleset,
        round_number=round_number,
        rng=rng,
        _tracer=_tracer,
    )
