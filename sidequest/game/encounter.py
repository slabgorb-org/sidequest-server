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
from sidequest.game.taunt import TauntState
from sidequest.protocol.models import EncounterLocationOverlay


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
    win_condition: Literal["dial_threshold", "hp_depletion"] = "dial_threshold"
    player_metric: EncounterMetric
    opponent_metric: EncounterMetric
    beat: int = 0
    structured_phase: EncounterPhase | None = None
    secondary_stats: SecondaryStats | None = None
    actors: list[EncounterActor] = Field(default_factory=list)
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

    # Story 54-7 / ADR-109: per-encounter location overlay. When set,
    # bound_room_id names the region/room whose manifest and prose the
    # overlay contributes to. Read-time merge in
    # sidequest.game.location_view layers entity_delta and prose_suffix
    # on top of the authored base; base never mutates. None for
    # encounters that have nothing to add to the room description.
    location_overlay: EncounterLocationOverlay | None = None

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
