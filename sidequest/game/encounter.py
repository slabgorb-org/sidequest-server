"""Structured Encounter System — dual-track momentum (spec 2026-04-25).

Replaces the single-dial bidirectional ``metric`` with two ascending dials
routed by actor side. ``MetricDirection`` is removed — both dials are
ascending; bidirectional was the workaround for actor-blind routing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sidequest.game.encounter_tag import EncounterTag
from sidequest.game.fate_sheet import Aspect
from sidequest.game.table.types import TableState
from sidequest.game.taunt import TauntState
from sidequest.protocol.models import EncounterLocationOverlay, FateExchangeLine, InitiativeEntry


class RigType(StrEnum):
    """Rig archetype determines base stats."""

    Interceptor = "Interceptor"
    WarRig = "WarRig"
    Bike = "Bike"
    Hauler = "Hauler"
    Frankenstein = "Frankenstein"

    def base_stats(self) -> tuple[int, int, int, int, int]:
        return _RIG_BASE_STATS[self]


_RIG_BASE_STATS: dict[RigType, tuple[int, int, int, int, int]] = {
    RigType.Interceptor: (15, 5, 1, 3, 8),
    RigType.WarRig: (30, 2, 5, 1, 12),
    RigType.Bike: (8, 4, 0, 5, 5),
    RigType.Hauler: (25, 2, 3, 1, 20),
    RigType.Frankenstein: (18, 3, 2, 3, 10),
}


def _rig_damage_tier_label(hp: int, max_hp: int) -> str:
    if max_hp == 0:
        return "WRECK"
    pct = (hp / max_hp) * 100.0
    if pct <= 0.0:
        return "WRECK"
    if pct <= 25.0:
        return "SKELETON"
    if pct <= 50.0:
        return "FAILING"
    if pct <= 75.0:
        return "COSMETIC"
    return "PRISTINE"


class EncounterPhase(StrEnum):
    Setup = "Setup"
    Opening = "Opening"
    Escalation = "Escalation"
    Climax = "Climax"
    Resolution = "Resolution"

    def drama_weight(self) -> float:
        return _DRAMA_WEIGHTS[self]


_DRAMA_WEIGHTS: dict[EncounterPhase, float] = {
    EncounterPhase.Setup: 0.70,
    EncounterPhase.Opening: 0.75,
    EncounterPhase.Escalation: 0.80,
    EncounterPhase.Climax: 0.95,
    EncounterPhase.Resolution: 0.70,
}


ActorSide = Literal["player", "opponent", "neutral"]


class StatValue(BaseModel):
    model_config = {"extra": "forbid"}
    current: int
    max: int


class SecondaryStats(BaseModel):
    model_config = {"extra": "forbid"}
    stats: dict[str, StatValue] = Field(default_factory=dict)
    damage_tier: str | None = None

    @classmethod
    def rig(cls, rig_type: RigType) -> SecondaryStats:
        hp, speed, armor, maneuver, fuel = rig_type.base_stats()
        stats: dict[str, StatValue] = {
            "hp": StatValue(current=hp, max=hp),
            "speed": StatValue(current=speed, max=speed),
            "armor": StatValue(current=armor, max=armor),
            "maneuver": StatValue(current=maneuver, max=maneuver),
            "fuel": StatValue(current=fuel, max=fuel),
        }
        return cls(stats=stats, damage_tier=_rig_damage_tier_label(hp, hp))


class EncounterActor(BaseModel):
    """A character assigned to an encounter role.

    ``side`` is closed: ``player`` (allies), ``opponent`` (anyone the party
    is fighting), ``neutral`` (bystanders, narrators, audience). The narrator's
    payload sets ``side`` when it names participants; when it doesn't, the
    engine seats an opponent for an *adversarial* confrontation (combat /
    movement) from the location roster — ADR-116 ("A Confrontation Requires an
    Other"), see ``_npc_fallback_at_location`` / ``_is_adversarial``. Non-
    adversarial fallback participants default to ``neutral``.

    ``withdrawn`` flips True when the actor yields. Withdrawn actors are
    skipped by ``_apply_beat`` and emit a ``beat_skipped`` watcher event.
    """

    model_config = {"extra": "forbid"}

    name: str
    role: str
    side: ActorSide
    withdrawn: bool = False
    per_actor_state: dict[str, Any] = Field(default_factory=dict)


class WnSealedCommit(BaseModel):
    """One sealed Main Action in a WN round (story 102-4).

    The WN turn model is blind commitment, initiative-ordered resolution:
    a player's DICE_THROW resolves its to-hit at commit time but the beat
    does NOT apply until every seated player-side participant has committed
    and the round walk reaches the actor's initiative slot. ``outcome``
    carries the commit-time RollOutcome value (string form — the enum lives
    in the protocol layer); ``target`` pins the premise (the opponent the
    action was aimed at) so the walk can detect a dead premise without
    auto-retargeting (SOUL: The Test).
    """

    model_config = {"extra": "forbid"}

    actor: str
    beat_id: str
    outcome: str
    target: str | None = None
    spell_id: str | None = None


FateAction = Literal["overcome", "create_advantage", "attack"]
"""The three proactive Fate Core actions committable in an exchange (ADR-144 F1c).
Defend is reactive (engine-rolled), never committed; there is no ``full_defense``
(not a Fate SRD action)."""


class FateSealedCommit(BaseModel):
    """One sealed Fate action in an exchange (ADR-144 F1c).

    Mirrors :class:`WnSealedCommit` one tier over. A proactive Fate action seals
    here until every seated PC has committed; ``run_fate_exchange`` consumes and
    clears the ledger. The attacker's 4dF roll is resolved AT COMMIT TIME (like
    the WN to-hit): ``ladder_total`` = 4dF + skill + invoke bonus, ``dice`` the
    raw faces. The reactive defense roll happens at the actor's slot.

    ``action`` is one of the three proactive Fate actions; ``defend`` is
    reactive (the engine rolls it for an attack's target) and is never a
    committed value. There is no ``full_defense`` action — not in the Fate SRD.
    Opposition is ACTIVE when ``target`` is set (the engine rolls that actor's
    defense) or PASSIVE when ``difficulty`` is set (a set number on the ladder).
    ``aspect_text`` carries the situation aspect a create-advantage means to
    place.
    """

    model_config = {"extra": "forbid"}

    actor: str
    action: FateAction
    skill: str
    target: str | None = None
    difficulty: int = 0
    ladder_total: int = 0
    dice: tuple[int, int, int, int] = (0, 0, 0, 0)
    aspect_text: str = ""


class FatePendingDefense(BaseModel):
    """One incoming attack on a PC awaiting that PC's interactive defense (ADR-148/
    151, Story 126-8 §5).

    Written at REVEAL when an attack targets a seated PC; filled by the PC's
    ``FATE_THROW(action="defend")``. ``defense_total is None`` and ``not conceded``
    means the DEFEND barrier is still waiting on this defender — an unfilled entry
    IS the "we are parked at DEFEND" signal. Sibling to ``fate_commits``; the
    ledger rides ``snapshot.encounter`` (resume-safe, ADR-128) and is cleared when
    the exchange resumes and resolves.
    """

    model_config = {"extra": "forbid"}

    request_id: str
    attacker: str
    defender: str
    attack_skill: str
    attack_total: int
    mental: bool = False
    defense_total: int | None = None
    conceded: bool = False
    #: FATE-CONFLICT-SEQUENCE-OPAQUE (sq-playtest 2026-06-20): the skill the PC chose
    #: to defend with (free-pick — the Zork Problem), recorded from the player's throw
    #: in ``dispatch_fate_defense`` so the resolution ledger can show "you defend Will
    #: = N". Empty until the defense is thrown (or on a concession, which carries no
    #: skill).
    defense_skill: str = ""


class ContestState(BaseModel):
    """First-to-N victory tally for a Fate Contest (ADR-144, spec 2026-06-17).

    The Contest analogue of the Conflict's stress track: each exchange the side
    with the higher 4dF total scores 1 victory (2 on a 3+ margin); a tie grants
    each side a boost and no victory. First side to ``target`` victories wins.
    There is no stress and no consequences — that is what distinguishes a Contest
    from a Conflict. ``target`` is seeded from the cdef's metric threshold (the
    re-authored ``0->3`` victory tally that replaced the ``0->7`` dial)."""

    model_config = {"extra": "forbid"}

    target: int = 3
    player_victories: int = 0
    opponent_victories: int = 0


class PendingCompel(BaseModel):
    """One narrator-offered compel awaiting the player's accept/refuse (ADR-144 F3e).

    F2b's ``propose_fate_compel`` fired ``fate.compel.offered`` but persisted
    nothing — the offer evaporated. F3e persists it here so the FATE_STATE
    projection can surface it to the player and the accept/refuse round-trip can
    resolve it. ``target`` is the compelled PC, ``aspect`` the compelled aspect
    (verbatim), ``reason`` the complication the narrator proposed. ``offered_delta``
    is the SRD accept reward (+1) the player GAINS by accepting — projected onto
    ``FatePendingCompel`` (``fate_projection``) so the player surface renders a real,
    server-sourced delta instead of a hardcoded label that could drift from the SRD.
    Refusing is the separate SRD-fixed −1, a client-rendered constant (it has no
    stored field — there is only one cost, never a variable one).
    """

    model_config = {"extra": "forbid"}

    target: str
    aspect: str
    reason: str = ""
    offered_delta: int = 1


class EncounterMetric(BaseModel):
    """Ascending dial. ``current`` advances toward ``threshold``; the side
    that reaches ``threshold`` first triggers resolution.
    """

    model_config = {"extra": "forbid"}

    name: str
    current: int = 0
    starting: int = 0
    threshold: int


class StructuredEncounter(BaseModel):
    """A structured encounter with two side-routed dials.

    ``outcome`` values written by the engine:
    ``player_victory`` | ``opponent_victory`` | ``resolution_beat:<beat_id>``
    | ``yielded`` | ``None`` (unresolved).
    """

    model_config = {"extra": "forbid"}

    encounter_type: str
    # "dial_threshold" (default) | "hp_depletion" | "table_showdown" | "fate_conflict".
    # Stamped from ConfrontationDef.win_condition at init (encounter_lifecycle).
    # String-literal (NOT the WinCondition enum) to avoid a game->genre.models import cycle;
    # the Literal still rejects typos at validation time.
    #
    # ``fate_conflict`` (story 126-30, Keith ruling 2026-06-19) is an ENGINE-only runtime
    # value — never authored as content (it has no WinCondition enum member). The Fate
    # seating de-nativization stamps it so a Fate standoff/conflict resolves through the
    # 4dF conflict engine against the Other's FateSheet stress (ADR-143/144 "Bind the
    # Ruleset") instead of the native ``tension`` dial. Like ``hp_depletion`` it routes OFF
    # every native dial reader (``dial_threshold_outcome`` returns None below; the
    # narration_apply dial-advance gate is ``!= "dial_threshold"``); its metrics are inert
    # placeholders (the native dial is removed, not seated alongside Fate).
    win_condition: Literal["dial_threshold", "hp_depletion", "table_showdown", "fate_conflict"] = (
        "dial_threshold"
    )
    # Confrontation category ("combat" | "social" | "movement" | "hacking" | ...),
    # stamped from ConfrontationDef.category at init (encounter_lifecycle), sibling
    # to win_condition. Lets the engine answer "is this confrontation MOBILE?" — a
    # chase/escape (category="movement") moves WITH the party, so a scene/location
    # change CONTINUES it rather than abandoning it — without re-threading the
    # GenrePack into narration_apply. Empty string for legacy saves predating this
    # field and direct-construction tests that don't set it; callers that need the
    # category for those fall back to a pack lookup (narration_apply._encounter_is_mobile).
    category: str = ""
    # Free-for-all N-seat table (poker / auction). None for every non-table
    # confrontation — the dual dials go unused for table types; the resolver
    # reads table_state, not the metrics. See
    # docs/superpowers/specs/2026-05-29-free-for-all-n-seat-table-design.md.
    table_state: TableState | None = None
    # ADR-153 §3 state graph: current relative-position state id of a
    # sealed-letter dogfight (merge / tail_chase / beam / ...). Stamped with
    # the entry state at instantiation, advanced each turn by the resolved
    # cell's next_state (extend-and-return resets to merge). None for every
    # non-dogfight encounter and for legacy saves predating the graph.
    # Serialized so a mid-duel reload resumes in the correct state.
    dogfight_state: str | None = None
    player_metric: EncounterMetric
    opponent_metric: EncounterMetric
    beat: int = 0
    structured_phase: EncounterPhase | None = None
    secondary_stats: SecondaryStats | None = None
    actors: list[EncounterActor] = Field(default_factory=list)
    initiative: list[InitiativeEntry] = Field(default_factory=list)
    """SWN P4: 1d8+DEX resolution order, rolled once at instantiation. Empty for
    rulesets with no ordering (native) and non-combat encounters."""
    wn_commits: list[WnSealedCommit] = Field(default_factory=list)
    """Story 102-4: the WN sealed-commit ledger for the CURRENT round. Player-side
    Main Actions seal here until every live seated participant has committed; the
    round walk consumes and clears it. Always empty for native/dial encounters and
    between WN rounds."""
    fate_commits: list[FateSealedCommit] = Field(default_factory=list)
    """ADR-144 F1c: the Fate sealed-commit ledger for the CURRENT exchange.
    Proactive actions seal here until every live seated PC has committed; the
    exchange walk consumes and clears it. Always empty for native/WN encounters
    and between Fate exchanges (sibling to ``wn_commits``)."""
    pending_defenses: list[FatePendingDefense] = Field(default_factory=list)
    """ADR-148/151 (Story 126-8 §5): incoming attacks on PCs awaiting interactive
    defense. An unfilled entry is the "we are parked at DEFEND" signal; the exchange
    is suspended at a persisted checkpoint until every entry is filled (defense_total
    set or conceded), then resumes and resolves. Always empty for native/WN
    encounters and between Fate exchanges (sibling to ``fate_commits``)."""
    #: Set when this encounter resolves as a Fate Contest (cdef.resolution_mode ==
    #: contest). None for every Conflict / dial / table encounter. Selects the
    #: contest exchange engine in dispatch_fate_action (spec 2026-06-17 §2).
    contest: ContestState | None = None
    situation_aspects: list[Aspect] = Field(default_factory=list)
    """ADR-144 F1c: scene-scoped Fate aspects placed by create-advantage (and
    boosts from ties). Distinct from character/consequence aspects, which live on
    the actor's FateSheet. Cleared on scene end (F2/F3 lifecycle)."""
    pending_compels: list[PendingCompel] = Field(default_factory=list)
    """ADR-144 F3e: compels the narrator has offered this conflict, awaiting the
    player's accept/refuse. ``offer_compel`` appends; the accept/refuse dispatch
    consumes the matching entry. The FATE_STATE projection surfaces these to the
    player surface. Always empty for native/WN encounters."""
    zones: list[str] = Field(default_factory=list)
    """ADR-144 F1c: named Fate zones for this scene (reuses the encounter as the
    spatial notion — design §4.2 / open-Q3). An actor's current zone lives in
    ``EncounterActor.per_actor_state['zone']``. Empty for non-Fate encounters."""
    tags: list[EncounterTag] = Field(default_factory=list)
    # Story 150-3 (sq-playtest 2026-06-20, five_points): the interaction-turn on
    # which this encounter was instantiated, stamped at the single seating
    # chokepoint (instantiate_encounter_from_trigger). Lets the location-change
    # abandon ladder in narration_apply EXEMPT an encounter that was born THIS
    # same turn. A table scene (poker/auction) is inherently a NEW location — the
    # narrator seats the table AND moves the scene to "The Groggery — Poker Table"
    # in one response, and deactivate-on-location-change then killed the
    # freshly-dealt table before the player could ever play it (table NARRATION
    # but no table tab). An encounter cannot have been "walked away from" on the
    # turn it was created — the location change that's firing is the one that
    # CREATED its scene — so it CONTINUES across its birth-location-change; a
    # genuine later departure (created_turn < interaction) abandons normally.
    # None for legacy saves / direct-construction tests predating this field
    # (treated as "not fresh" → old abandon behavior preserved).
    created_turn: int | None = None
    outcome: str | None = None
    resolved: bool = False
    mood_override: str | None = None
    narrator_hints: list[str] = Field(default_factory=list)
    fate_resolution_log: list[FateExchangeLine] = Field(default_factory=list)
    """FATE-CONFLICT-SEQUENCE-OPAQUE (sq-playtest 2026-06-20): the per-action
    resolution ledger for the MOST RECENT Fate exchange — the legible attack/defend
    math the player surface renders. ``run_fate_exchange`` clears + repopulates it
    every walk (last-exchange semantics, never an ever-growing stack);
    ``build_fate_state_payload`` projects it onto ``FateConflictEntry.last_exchange``
    so the result reaches the player deterministically, not via narrator prose.
    Always empty for native/WN encounters and Fate Contests (Conflict-only)."""
    # B/X morale tracking (Task 9 — C&C class-beats + morale).
    # ``morale_events`` records "trigger:side_label" strings so the
    # deduplication logic in ``_emit_morale_triggers`` can prevent
    # first_blood from firing twice on the same side. Not persisted to
    # the save file (encounter state is ephemeral per session). Non-None
    # list (initialized to empty) so callers can always append without
    # a guard.
    morale_events: list[str] = Field(default_factory=list)
    # B/X flee consequences (Task 11 — C&C class-beats + morale).
    # ``flee_consequence_pending`` is set to "chase"|"surrender"|"rout"
    # when a morale flee outcome is applied. Chase is a V1 placeholder —
    # full chase-launch needs a follow-up story. Surrender/rout also set
    # ``resolved=True`` and ``outcome`` to their disposition value.
    # ``opponents_disposition`` is "surrendered"|"routed" when applicable.
    # Both fields are server-only (not forwarded to UI in V1).
    flee_consequence_pending: str | None = None
    opponents_disposition: str | None = None
    # Story 2026-05-10 — taunt mechanic (Task 3 wire-up).
    # Tracks which Fighter PC is currently taunting and how many rounds remain.
    # Always present (default_factory) so callers can always read/write
    # without a None guard. See sidequest/game/taunt.py and spec §8.
    taunt: TauntState = Field(default_factory=TauntState)

    # Story 73-4 — player-facing beat-kind impact descriptor, keyed by actor
    # side ("player"/"opponent"). apply_beat stamps the serialized BeatImpact for
    # the acting side each beat; per-side so an opposed_check opponent beat can't
    # clobber the player's readout. build_confrontation_payload surfaces the
    # player-side entry so a no-dial-move CritSuccess reads as intended (clean
    # exit, by design) instead of a bare 0. Ephemeral (rebuilt each beat).
    last_beat_impacts: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Story 54-7 / ADR-109: per-encounter location overlay. When set,
    # bound_room_id names the region/room whose manifest and prose the
    # overlay contributes to. Read-time merge in
    # sidequest.game.location_view layers entity_delta and prose_suffix
    # on top of the authored base; base never mutates. None for
    # encounters that have nothing to add to the room description.
    location_overlay: EncounterLocationOverlay | None = None

    # net_run (CWN hacking) only — the named security tier this run targets,
    # stamped at instantiation from the dispatch param or the pack's
    # cwn.hacking.default_tier. The effective DC at resolution time is
    # cwn.hacking.security_tiers[security_tier] + alert escalation. None for
    # every non-hacking confrontation.
    security_tier: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_metric(cls, data: object) -> object:
        if isinstance(data, dict) and "metric" in data:
            raise ValueError(
                "StructuredEncounter uses dual dials; legacy 'metric' field "
                "is rejected. Use player_metric + opponent_metric."
            )
        return data

    def find_actor(self, name: str) -> EncounterActor | None:
        for a in self.actors:
            if a.name == name:
                return a
        return None

    def add_pending_compel(self, *, target: str, aspect: str, reason: str = "") -> PendingCompel:
        """Persist a narrator-offered compel awaiting accept/refuse (ADR-144 F3e)."""
        compel = PendingCompel(target=target, aspect=aspect, reason=reason)
        self.pending_compels.append(compel)
        return compel

    def find_pending_compel(self, *, target: str, aspect: str) -> PendingCompel | None:
        """The pending compel on ``aspect`` offered to ``target``, or None."""
        for c in self.pending_compels:
            if c.target == target and c.aspect == aspect:
                return c
        return None

    def remove_pending_compel(self, compel: PendingCompel) -> None:
        """Consume a resolved compel (accepted or refused)."""
        self.pending_compels.remove(compel)

    def find_actor_for_player(self, player_name: str) -> EncounterActor | None:
        for a in self.actors:
            if a.side == "player" and a.name == player_name:
                return a
        return None

    def resolve_from_trope(self, trope_id: str) -> None:
        if self.resolved:
            return
        self.resolved = True
        self.structured_phase = EncounterPhase.Resolution
        self.outcome = f"resolved_by_trope:{trope_id}"

    def dial_threshold_outcome(self) -> str | None:
        """Return the victory outcome if a dial-threshold win condition is
        already met, else ``None``.

        Mirrors the canonical crossing check in
        ``sidequest.game.beat_kinds.apply_beat`` (player dial first, then
        opponent — "first crossing wins") so a met threshold resolves
        consistently whether it was crossed via a beat OR via a non-beat
        momentum path (sq-playtest 2026-06-02 wry_whimsy/oz: an escape dial
        reached 8/8 with ``total_beats_fired == 0``, so apply_beat's check
        never ran and the encounter stayed unresolved). Only ``dial_threshold``
        encounters resolve here — ``hp_depletion``, ``table_showdown`` and
        ``fate_conflict`` have their own resolution channels (HP, table showdown,
        and the 4dF conflict engine reading the Other's FateSheet stress,
        respectively) and return ``None``.
        """
        if self.win_condition != "dial_threshold":
            return None
        if self.player_metric.current >= self.player_metric.threshold:
            return "player_victory"
        if self.opponent_metric.current >= self.opponent_metric.threshold:
            return "opponent_victory"
        return None

    def opponent_yield_outcome(self) -> str | None:
        """Return ``"opponent_yielded"`` if the OPPONENT side has yielded, else
        ``None``.

        Story 59-32 normalized this to the **mechanical-truth** label
        ``"opponent_yielded"`` (was the credit label ``"player_victory"``): each
        producer emits mechanical truth and the shared ``is_player_victory()``
        classifier owns the credit mapping (``opponent_yielded`` → victory).
        Consumer-safe — production consumers None-check the return only
        (``narration_apply.py:2824,4615``); ``enc.outcome`` is set independently
        by ``_resolve_opponent_yield``.

        Sibling to ``dial_threshold_outcome`` (Story 59-31): a yielded opponent
        is a player VICTORY, distinct from a dial win, from the player-side
        ``yielded`` (a loss), and from ``abandoned_on_location_change`` (a
        genuine walk-away). The engine confirms the yield from existing actor /
        disposition state the narrator already sets — it is engine-checked, not
        pure-LLM-compliance (CLAUDE.md: the GM panel is the lie detector).

        An opponent has yielded iff there ARE opponent-side actors (ADR-116 — a
        confrontation requires an Other) AND either every opponent actor is
        ``withdrawn`` OR ``opponents_disposition`` is a yield disposition
        (``surrendered`` / ``routed``, set by the B/X morale path without
        necessarily flipping each actor's ``withdrawn``). Unlike
        ``dial_threshold_outcome`` this is NOT gated to ``dial_threshold`` — a
        monster surrendering mid-combat is just as much a player victory.

        A PLAYER-side withdrawal never triggers this (that is the player-side
        ``yielded`` loss path) — this checks ``side == "opponent"`` only.
        """
        opponents = [a for a in self.actors if a.side == "opponent"]
        if not opponents:
            return None
        all_withdrawn = all(a.withdrawn for a in opponents)
        disposition_yield = self.opponents_disposition in ("surrendered", "routed")
        if all_withdrawn or disposition_yield:
            return "opponent_yielded"
        return None


def is_live_wn_combat(encounter: StructuredEncounter | None, bound_ruleset: str | None) -> bool:
    """True iff a Without-Number-bound ``hp_depletion`` combat is live.

    The single predicate for the sq-playtest 2026-06-22 WWN-combat fix. Under a
    WN binding the ruleset OWNS the round (ADR-143): combat resolves on the
    player's DICE_THROW via ``run_wn_round`` (epic 108), and the narrator must
    NARRATE the seated/resolved beat — it must not be handed the combat-
    resolution toolset or told to drive beats (the max-turns starve). This
    predicate gates the two ENCOUNTER-level halves of the fix: (2) the
    de-nativized narrator prompt branch, and (3) the narration-apply stray-beat
    drop — both only matter once a combat is seated. The (1) narrator TOOL filter
    was split out to :func:`wn_binding_owns_combat_resolution` (binding-level, no
    live encounter required) by the sq-playtest 2026-06-24 criticals — see that
    function for why.

    Gated on the WN family (``swn``/``wwn``/``cwn``/``awn``) — NOT win_condition
    alone — so a native ``dial`` pack's ``hp_depletion`` combat keeps the legacy
    beat-driven path (the ADR-143 "don't balance the native engine" guard cuts
    both ways: leave native packs alone). Resolved encounters return False.
    """
    if encounter is None or encounter.resolved:
        return False
    if bound_ruleset is None:
        return False
    # Local import avoids any chance of an import-order cycle at module load.
    from sidequest.genre.ruleset_reference import WN_FAMILY

    if bound_ruleset not in WN_FAMILY:
        return False
    return encounter.win_condition == "hp_depletion"


def wn_binding_owns_combat_resolution(bound_ruleset: str | None) -> bool:
    """True iff a Without-Number binding owns mechanical resolution, so the
    narrator must never hold the combat-RESOLUTION tools — live encounter or not.

    This is the BINDING-level half of the tool gate, split out from
    :func:`is_live_wn_combat` by the sq-playtest 2026-06-24 criticals. The
    encounter-level predicate still gates the de-nativized prompt branch and the
    narration-apply stray-beat drop (both only matter once a combat is seated).
    The TOOL filter is broader: under a WN binding the ruleset owns the round
    (ADR-143) AND out-of-combat checks resolve on the player's throw (ADR-074 —
    determinative dice / ``check_throw``), so the narrator never resolves
    mechanics on a WN pack at all.

    Gating the tool filter on a *live* encounter left the gap the criticals hit:
    on a fresh descent the player's attack fails to seat (a surfaced creature's
    stale zone, or literary-verb routing), so no encounter is live, the narrator
    keeps the full combat toolset and grinds ``roll_dice``/``apply_damage``/
    ``advance_encounter_beat``/``advance_confrontation`` past ``max_turns`` — a
    fatal turn crash. Keying the filter on the BINDING closes it: the narrator
    can't grind tools it never holds. Native (``dial``) and Fate packs are
    untouched (ADR-143's "leave the native engine alone" guard cuts both ways).
    """
    if bound_ruleset is None:
        return False
    # Local import avoids any chance of an import-order cycle at module load.
    from sidequest.genre.ruleset_reference import WN_FAMILY

    return bound_ruleset in WN_FAMILY
