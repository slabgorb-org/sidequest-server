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
from sidequest.game.table.types import TableState
from sidequest.game.taunt import TauntState
from sidequest.protocol.models import EncounterLocationOverlay, InitiativeEntry


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
    # "dial_threshold" (default) | "hp_depletion". Stamped from ConfrontationDef.win_condition
    # at init (encounter_lifecycle). String-literal (NOT the WinCondition enum) to avoid a
    # game->genre.models import cycle; the Literal still rejects typos at validation time.
    win_condition: Literal["dial_threshold", "hp_depletion", "table_showdown"] = "dial_threshold"
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
    tags: list[EncounterTag] = Field(default_factory=list)
    outcome: str | None = None
    resolved: bool = False
    mood_override: str | None = None
    narrator_hints: list[str] = Field(default_factory=list)
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
        encounters resolve here — ``hp_depletion`` and ``table_showdown`` have
        their own resolution channels and return ``None``.
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
