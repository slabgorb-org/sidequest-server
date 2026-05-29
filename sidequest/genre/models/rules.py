"""Game rules, resource declarations, and confrontation types from rules.yaml.

Port of sidequest-genre/src/models/rules.rs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from sidequest.game.beat_kinds import BeatKind
from sidequest.game.disposition import AttitudeThresholds
from sidequest.genre.models.inventory import DamageSpec

# Keys inside ``ConfrontationDef.opponent_default_stats`` that are NOT
# ability scores. ``hp`` seeds the opponent CreatureCore HP pool and
# ``armor_class`` seeds the SWN ascending AC the attack rolls against
# (hp_depletion combats). ``dexterity`` seeds the opponent's SWN
# initiative (1d8 + DEX mod) for hp_depletion combats. ``armor`` is the
# SWN flat damage-soak value for dogfight ship frames. ``pilot_skill``
# and ``attack_bonus`` are dogfight ship-gunnery to-hit terms. All of
# these are popped out before ability-score / modifier resolution so they
# never leak into opposed_check lookups or the ADR-093 calibration ceiling.
OPPONENT_RESERVED_STAT_KEYS: frozenset[str] = frozenset(
    {"hp", "armor_class", "dexterity", "armor", "pilot_skill", "attack_bonus"}
)


class MoraleTrigger(StrEnum):
    """B/X morale check triggers. Per spec §2.2."""

    first_blood = "first_blood"
    half_killed = "half_killed"
    intimidated = "intimidated"
    leader_killed = "leader_killed"


class FleeConsequence(StrEnum):
    """How the opponent side breaks off when morale fails."""

    chase = "chase"
    surrender = "surrender"
    rout = "rout"


class DamageChannel(StrEnum):
    """HP-damage channel tag on a BeatDef (ADR-114 §5).

    - ``none``:   dial-only beat (angle/push) — never touches HP.
    - ``strike``: rolls weapon (or override) damage onto target HP.
    - ``brace``:  mitigates incoming HP damage this round.
    """

    none = "none"
    strike = "strike"
    brace = "brace"


class InitiativeRule(BaseModel):
    """Maps an encounter type to its primary stat for turn ordering."""

    model_config = {"extra": "forbid"}

    primary_stat: str
    description: str


class ResourceThresholdDecl(BaseModel):
    """A threshold on a resource declaration."""

    model_config = {"extra": "forbid"}

    at: float
    event_id: str
    narrator_hint: str


class ResourceDeclaration(BaseModel):
    """Genre resource declaration (e.g., Luck, Humanity, Heat)."""

    model_config = {"extra": "forbid"}

    name: str
    label: str
    min: float
    max: float
    starting: float
    voluntary: bool
    decay_per_turn: float
    thresholds: list[ResourceThresholdDecl] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_range(self) -> ResourceDeclaration:
        if self.max < self.min:
            raise ValueError(
                f"resource '{self.name}': max ({self.max}) must be >= min ({self.min})"
            )
        if not (self.min <= self.starting <= self.max):
            raise ValueError(
                f"resource '{self.name}': starting ({self.starting}) must be in "
                f"[{self.min}, {self.max}]"
            )
        return self


class SecondaryStatDef(BaseModel):
    """A secondary stat derived from an ability score."""

    model_config = {"extra": "forbid"}

    name: str
    source_stat: str
    spendable: bool


class BeatDef(BaseModel):
    """A single action available during a confrontation.

    Schema (spec 2026-04-25-dual-track-momentum-design.md §Schema changes):

    - ``kind``: closed enum driving per-tier delta defaults.
    - ``base``: scalar magnitude; meaning depends on kind.
    - ``deltas``: optional per-tier override map; keys ∈
      {crit_fail, fail, tie, success, crit_success}; values are dicts of
      {own, opponent, grants_tag, grants_fleeting_tag, resolution, ...}.
    - ``target_tag``: required for kind=angle; text of the tag created.
    - Legacy ``metric_delta``/``failure_metric_delta``/``failure_effect``
      are deleted — pack migration is mandatory.
    """

    model_config = {"extra": "forbid"}

    id: str
    label: str
    kind: BeatKind
    base: int = 1
    deltas: dict[str, dict[str, Any]] | None = None
    target_tag: str | None = None
    stat_check: str
    risk: str | None = None  # narrator prose cue only — does not drive engine
    # One-line italic flavor hint for the BeatTile (D2 confrontation panel,
    # 2026-05-13). Optional — when absent the UI either renders no flavor
    # row or falls back to its per-beat-id default library (the legacy hint
    # set the panel shipped with). Authoring this in pack YAML lets a genre
    # author key flavor to specific beat definitions instead of relying on
    # the shared id→string map.
    flavor: str | None = None
    reveals: str | None = None
    resolution: bool | None = (
        None  # legacy "always-resolves" flag (still useful for declarative pushes)
    )
    effect: str | None = None
    consequence: str | None = None
    requires: str | None = None
    narrator_hint: str | None = None
    gold_delta: int | None = None
    edge_delta: int | None = None
    target_edge_delta: int | None = None
    # Per-beat target-resolution mode for ``target_edge_delta`` (Step 2 of
    # numerical-advantage design). ``focus`` (default) hits the first live
    # opposing actor for the full debit. ``spread`` divides the debit
    # ``floor(N)`` across every live opposing actor (remainder dropped).
    # ``swarm`` is focus-targeted but flagged for ally amplification by the
    # numerical-advantage rule (Step 3).
    target_select: str | None = None
    resource_deltas: dict[str, float] | None = None
    class_filter: list[str] | None = None
    # ADR-114 §5 — HP damage channel.  Independent of ``kind``; a beat can be
    # kind=strike (dial semantics) and damage_channel=none (pure dial, no HP
    # hit) — the separation is intentional so social/push beats never
    # accidentally acquire an HP channel.
    damage_channel: DamageChannel = DamageChannel.none
    damage_override: DamageSpec | None = None  # creature natural attack (no catalog weapon)
    mitigation_override: int | None = None  # brace beat with no armor item
    # SWN attack parameters — only meaningful when the pack binds `ruleset: swn`.
    # ``attack_bonus`` is the attacker's class/level attack-bonus progression value.
    # ``combat_skill`` is the relevant Combat/* skill level (0 = untrained).
    # Both default to 0 so native-module packs require no YAML changes.
    attack_bonus: int = 0
    combat_skill: int = 0

    @model_validator(mode="after")
    def _validate(self) -> BeatDef:
        if not self.id:
            raise ValueError("beat id must not be empty")
        if self.kind is BeatKind.angle and not self.target_tag:
            raise ValueError(f"beat '{self.id}' kind=angle requires target_tag")
        if self.deltas is not None:
            valid_tiers = {
                "crit_fail",
                "fail",
                "tie",
                "success",
                "crit_success",
            }
            for tier in self.deltas:
                if tier not in valid_tiers:
                    raise ValueError(f"beat '{self.id}' deltas key {tier!r} not in {valid_tiers}")
        if self.target_select is not None:
            valid_modes = {"focus", "spread", "swarm"}
            if self.target_select not in valid_modes:
                raise ValueError(
                    f"beat '{self.id}' target_select={self.target_select!r} "
                    f"not in {sorted(valid_modes)}"
                )
        if self.class_filter is not None and not self.class_filter:
            raise ValueError(f"beat '{self.id}' class_filter must be None or non-empty list")
        return self


class MetricDef(BaseModel):
    """Per-side ascending metric for a confrontation.

    Spec change: bidirectional/descending metrics are gone — both sides have
    independent ascending dials. ``threshold`` is the cross point.
    """

    model_config = {"extra": "forbid"}

    name: str
    starting: int = 0
    threshold: int

    @model_validator(mode="after")
    def _validate(self) -> MetricDef:
        if self.threshold <= self.starting:
            raise ValueError(
                f"metric '{self.name}' threshold ({self.threshold}) must be "
                f"> starting ({self.starting})"
            )
        return self


class MoraleDef(BaseModel):
    """Optional morale block on a combat ConfrontationDef. B/X port.

    Score is the 2d6 target; total ≤ score = stay, > = flee.
    """

    model_config = {"extra": "forbid"}

    score: int = 8
    triggers: list[MoraleTrigger]
    flee_consequence: FleeConsequence = FleeConsequence.chase

    @model_validator(mode="after")
    def _validate(self) -> MoraleDef:
        if not (2 <= self.score <= 12):
            raise ValueError(f"morale score {self.score} not in 2..12")
        if not self.triggers:
            raise ValueError("morale.triggers must be non-empty")
        return self


class SaveCategory(StrEnum):  # noqa: UP042 — matches project convention
    """B/X B26 saving-throw columns. Closed enum — adding a new
    category is a deliberate edit, not a string-typo accident."""

    death_ray_or_poison = "death_ray_or_poison"
    magic_wands = "magic_wands"
    paralysis_or_stone = "paralysis_or_stone"
    dragon_breath = "dragon_breath"
    rods_staves_spells = "rods_staves_spells"


class SavingThrowsTable(BaseModel):
    """Per-class B/X saving-throw target numbers (B26).

    Targets are flat per Basic-set canon (B26 footnote): no level
    adjustments. Per-level scaling is an Expert-set feature; if/when
    XP advancement crosses a level boundary in C&C, this model
    becomes a per-level dict.
    """

    model_config = {"extra": "forbid"}

    death_ray_or_poison: int
    magic_wands: int
    paralysis_or_stone: int
    dragon_breath: int
    rods_staves_spells: int

    @model_validator(mode="after")
    def _validate(self) -> SavingThrowsTable:
        for f, v in self.model_dump().items():
            if not (2 <= v <= 20):
                raise ValueError(f"saving throw {f}={v} outside legal d20 range 2..20")
        return self

    def target_for(self, category: SaveCategory) -> int:
        return getattr(self, category.value)


class WinCondition(StrEnum):  # noqa: UP042 — matches project convention
    """How a confrontation decides victory.

    - ``dial_threshold``: a side's metric dial reaching ``threshold`` ends it (default; every
      existing pack).
    - ``hp_depletion``: a side's primary combatant reaching 0 HP ends it (SWN combat). Metrics
      are dropped; resolution reads CreatureCore HP.
    """

    dial_threshold = "dial_threshold"
    hp_depletion = "hp_depletion"


class ResolutionMode(StrEnum):  # noqa: UP042 — matches project convention (see protocol/enums.py)
    """How a confrontation resolves each turn.

    - ``beat_selection``: player rolls d20 vs static DC. Tier drives delta
      application. Opponent outcome tier is narrator-fiat (no opposing roll).
    - ``sealed_letter_lookup``: simultaneous-commit cell-table resolution
      (dogfight, ADR-077).
    - ``opposed_check``: BOTH sides roll d20 + modifier; outcome tier is
      derived from the shift between rolls (Fate-style bands). Combat
      encounters use this so the opponent dial only advances when the
      opponent's roll actually beats the player's. The narrator picks
      WHICH beat the opponent took, but never the outcome tier — the
      engine derives it from the dice. See:
      ``.archive/handoffs/opposed-checks-design.md``.
    """

    beat_selection = "beat_selection"
    sealed_letter_lookup = "sealed_letter_lookup"
    opposed_check = "opposed_check"


class InteractionCell(BaseModel):
    """A single cell of a sealed-letter interaction table."""

    model_config = {"extra": "forbid"}

    pair: list[str]  # exactly 2 items: [red, blue]
    name: str = ""
    shape: str = ""
    red_view: Any = None
    blue_view: Any = None
    narration_hint: str = ""
    tags: list[str] = Field(default_factory=list)
    calibration_notes: str | None = None

    @model_validator(mode="after")
    def _validate_pair(self) -> InteractionCell:
        if len(self.pair) != 2:
            raise ValueError(
                f"interaction cell pair must have exactly 2 elements, got {len(self.pair)}"
            )
        return self


class InteractionTable(BaseModel):
    """A sealed-letter interaction table."""

    model_config = {"extra": "forbid"}

    version: str
    starting_state: str
    maneuvers_consumed: list[str] = Field(default_factory=list)
    cells: list[InteractionCell] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> InteractionTable:
        if not self.version:
            raise ValueError("interaction table version must not be empty")
        if not self.cells:
            raise ValueError("interaction table must have at least one cell")
        seen: set[tuple[str, str]] = set()
        for cell in self.cells:
            key = (cell.pair[0], cell.pair[1])
            if key in seen:
                raise ValueError(
                    f"duplicate interaction cell pair: ({cell.pair[0]}, {cell.pair[1]})"
                )
            seen.add(key)
        return self


class GeometryModifiers(BaseModel):
    """Maneuver-cell geometry -> ship-gunnery to-hit modifier (dogfight SWN layer).

    Authored & tunable in content. ``aspect`` keys match the cell view's
    ``target_aspect`` value (tail_on/quartering/crossing/head_on); ``range``
    keys match ``target_range`` (gun/close/medium/far). The matched aspect and
    range modifiers are summed and will feed the ship-gunnery to-hit modifier.
    """

    model_config = {"extra": "forbid"}

    aspect: dict[str, int] = Field(default_factory=dict)
    range: dict[str, int] = Field(default_factory=dict)


class ConfrontationDef(BaseModel):
    """A confrontation type declared by a genre pack in rules.yaml."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    confrontation_type: str = Field(alias="type", serialization_alias="type")
    label: str
    category: str
    resolution_mode: ResolutionMode = ResolutionMode.beat_selection
    win_condition: WinCondition = WinCondition.dial_threshold
    player_metric: MetricDef | None = None
    opponent_metric: MetricDef | None = None
    beats: list[BeatDef] = Field(default_factory=list)
    secondary_stats: list[SecondaryStatDef] = Field(default_factory=list)
    escalates_to: str | None = None
    mood: str | None = None
    interaction_table: InteractionTable | None = None
    # Genre-level opponent stat fallback. Used by opposed_check resolution
    # when an EncounterActor lacks a per_actor_state.stats entry for the
    # beat's stat_check. Maps stat name → raw ability score (the same
    # 3..20 D&D-style score the player side uses; modifier is derived
    # via floor((score-10)/2)). Hard-fail-loud when neither this map nor
    # the per-actor block carries the stat (CLAUDE.md no-silent-fallback).
    # ``None`` means the pack has not migrated this confrontation to
    # opposed_check — only valid when ``resolution_mode`` is something
    # other than ``opposed_check``.
    #
    # RESERVED KEYS: the keys in ``OPPONENT_RESERVED_STAT_KEYS`` (hp,
    # armor_class, dexterity, armor, pilot_skill, attack_bonus) are NOT
    # ability scores. When present they seed the opponent's runtime
    # CreatureCore / SWN combat block (HP pool + ascending AC, initiative
    # DEX, ship-frame soak + gunnery to-hit terms) — see ``opponent_hp`` /
    # ``opponent_armor_class`` and the seating seam in
    # ``encounter_lifecycle._publish_combat_edge_to_npcs``. They are popped
    # out of the ability-score map by ``opponent_ability_scores()`` so they
    # never leak into modifier resolution. All other keys are raw ability
    # scores (3..20 D&D-style; modifier = floor((score-10)/2)).
    opponent_default_stats: dict[str, int] | None = None
    opponent_weapon: str | None = None  # dogfight: opponent ace's weapon catalog id
    player_weapon: str | None = None  # dogfight: PC frame's weapon catalog id
    geometry_modifiers: GeometryModifiers | None = None
    player_default_stats: dict[str, int] = Field(default_factory=dict)
    morale: MoraleDef | None = None
    intent_verbs: list[str] | None = None
    on_intent_mismatch: Literal["warn", "soft_suggest", "reprompt"] = "warn"
    # Derived at construction time; excluded from serialization.
    intent_verb_set: frozenset[str] = Field(default_factory=frozenset, exclude=True, init=False)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_metric(cls, data: object) -> object:
        if isinstance(data, dict) and "metric" in data:
            raise ValueError(
                "confrontation uses legacy single 'metric' shape; "
                "migrate to player_metric + opponent_metric per "
                "docs/superpowers/specs/2026-04-25-dual-track-momentum-design.md"
            )
        return data

    @model_validator(mode="after")
    def _validate(self) -> ConfrontationDef:
        if not self.confrontation_type:
            raise ValueError("confrontation type must not be empty")
        if self.win_condition == WinCondition.dial_threshold and (
            self.player_metric is None or self.opponent_metric is None
        ):
            raise ValueError(
                f"confrontation '{self.confrontation_type}' uses win_condition "
                "'dial_threshold' but is missing player_metric/opponent_metric"
            )
        # Task 9: a COMBAT hp_depletion confrontation resolves vs the
        # opponent's content-authored AC and depletes its content HP — both
        # reserved keys MUST be present at LOAD time so a content author
        # (e.g. Jade authoring space_opera) discovers a missing stat block
        # before a player ever triggers the encounter, not mid-seating.
        # Gated on category=="combat": non-combat hp_depletion confrontations
        # (e.g. a social attrition contest) do not seed an opponent
        # CreatureCore and carry no reserved keys — leave them valid.
        if self.category == "combat" and self.win_condition == WinCondition.hp_depletion:
            ods = self.opponent_default_stats or {}
            hp = ods.get("hp")
            ac = ods.get("armor_class")
            if hp is None or ac is None:
                raise ValueError(
                    f"combat confrontation '{self.confrontation_type}' uses "
                    "win_condition 'hp_depletion' but its opponent_default_stats "
                    f"is missing reserved combat keys (hp={hp!r}, armor_class={ac!r}); "
                    "author both `hp` and `armor_class` under opponent_default_stats"
                )
            if int(hp) < 1:
                raise ValueError(
                    f"combat confrontation '{self.confrontation_type}' has "
                    f"opponent_default_stats.hp={hp!r}; HP must be >= 1 "
                    "(a 0/negative pool would be silently clamped — fail loud instead)"
                )
            if int(ac) < 1:
                raise ValueError(
                    f"combat confrontation '{self.confrontation_type}' has "
                    f"opponent_default_stats.armor_class={ac!r}; AC must be >= 1 "
                    "(a 0/negative AC would auto-hit — fail loud instead)"
                )
            dex = ods.get("dexterity")
            if dex is None:
                raise ValueError(
                    f"combat confrontation '{self.confrontation_type}' uses "
                    "win_condition 'hp_depletion' but its opponent_default_stats is "
                    f"missing reserved combat key (dexterity={dex!r}); author "
                    "`dexterity` (SWN DEX score) so 1d8+DEX initiative can roll for "
                    "the opponent (SWN P4 — no silent +0 fallback)"
                )
            if int(dex) < 3:
                raise ValueError(
                    f"combat confrontation '{self.confrontation_type}' has "
                    f"opponent_default_stats.dexterity={dex!r}; must be >= 3 "
                    "(SWN ability-score floor)"
                )
        valid_categories = {"combat", "social", "pre_combat", "movement"}
        if self.category not in valid_categories:
            raise ValueError(
                f"invalid confrontation category '{self.category}': "
                f"must be one of {valid_categories}"
            )
        if not self.beats:
            raise ValueError(
                f"confrontation '{self.confrontation_type}' must have at least one beat"
            )
        seen: set[str] = set()
        for beat in self.beats:
            if beat.id in seen:
                raise ValueError(
                    f"confrontation '{self.confrontation_type}' has duplicate beat id '{beat.id}'"
                )
            seen.add(beat.id)
        # Derive intent vocabulary from label + every beat label, unioned
        # with any declared intent_verbs. Tokenization is shared with the
        # validator — both call confrontation_intent_validator.tokenize so
        # vocabularies are byte-for-byte identical between load and runtime.
        from sidequest.agents.confrontation_intent_validator import tokenize

        verbs: set[str] = set()
        verbs.update(tokenize(self.label))
        for beat in self.beats:
            verbs.update(tokenize(beat.label))
        if self.intent_verbs:
            for v in self.intent_verbs:
                verbs.update(tokenize(v))
        object.__setattr__(self, "intent_verb_set", frozenset(verbs))
        return self

    def opponent_ability_scores(self) -> dict[str, int] | None:
        """``opponent_default_stats`` with reserved combat keys removed.

        ``hp`` and ``armor_class`` are not ability scores; they seed the
        opponent CreatureCore. This returns only the ability-score entries
        so opposed_check modifier resolution never sees the reserved keys.
        Returns ``None`` when the underlying map is unset.
        """
        if self.opponent_default_stats is None:
            return None
        return {
            k: v
            for k, v in self.opponent_default_stats.items()
            if k not in OPPONENT_RESERVED_STAT_KEYS
        }

    @property
    def opponent_hp(self) -> int | None:
        """Content-authored opponent HP pool, or ``None`` if not authored."""
        if not self.opponent_default_stats:
            return None
        raw = self.opponent_default_stats.get("hp")
        return int(raw) if raw is not None else None

    @property
    def opponent_armor_class(self) -> int | None:
        """Content-authored opponent ascending AC, or ``None`` if not set."""
        if not self.opponent_default_stats:
            return None
        raw = self.opponent_default_stats.get("armor_class")
        return int(raw) if raw is not None else None

    @property
    def opponent_dexterity(self) -> int | None:
        """Content-authored opponent DEX score (SWN P4 initiative), or ``None``."""
        if not self.opponent_default_stats:
            return None
        raw = self.opponent_default_stats.get("dexterity")
        return int(raw) if raw is not None else None

    @property
    def player_hp(self) -> int | None:
        """Content-authored player-frame HP pool, or ``None`` if not authored."""
        if not self.player_default_stats:
            return None
        raw = self.player_default_stats.get("hp")
        return int(raw) if raw is not None else None

    @property
    def player_armor_class(self) -> int | None:
        """Content-authored player-frame ascending AC, or ``None`` if not set."""
        if not self.player_default_stats:
            return None
        raw = self.player_default_stats.get("armor_class")
        return int(raw) if raw is not None else None


class CrossingDirection(StrEnum):
    """Direction in which an EdgeThresholdDecl fires."""

    crossing_down = "crossing_down"


class RecoveryBehaviour(StrEnum):
    """Recovery behaviour for an edge pool at a named cadence."""

    full = "full"


class EdgeThresholdDecl(BaseModel):
    """Downward threshold declared in edge_config.thresholds."""

    model_config = {"extra": "forbid"}

    at: int
    event_id: str
    narrator_hint: str
    direction: CrossingDirection | None = None


class EdgeRecoveryDefaults(BaseModel):
    """Default recovery behaviour for composure pools."""

    model_config = {"extra": "forbid"}

    on_resolution: RecoveryBehaviour | None = None
    on_long_rest: RecoveryBehaviour | None = None
    between_back_to_back: int | None = None


class EdgeConfig(BaseModel):
    """Per-genre Edge / Composure configuration."""

    model_config = {"extra": "forbid"}

    base_max_by_class: dict[str, int] = Field(default_factory=dict)
    recovery_defaults: EdgeRecoveryDefaults = Field(default_factory=EdgeRecoveryDefaults)
    thresholds: list[EdgeThresholdDecl] = Field(default_factory=list)
    display_fields: list[str] = Field(default_factory=list)


class StandoffPhase(BaseModel):
    """One phase of a multi-phase standoff (sizing_up, focus_or_draw, nerve_break, ...).

    Each phase carries authored prose (``description``) plus phase-specific
    fields — ``check`` (stat to roll), ``contested`` (opposed?), ``threshold``
    (failure margin), and bonus deltas (``focus_bonus_hit``, etc). The exact
    field set varies by phase, so ``extra: allow`` keeps the per-phase shape
    open without sacrificing the required ``description`` contract.

    No engine consumer reads these yet — the standoff confrontation is wired
    via ``ConfrontationDef`` in ``rules.yaml``; this block carries pre-combat
    sizing-up flavor + tunables for a future state machine that runs *before*
    the per-beat dial advances. TODO(spaghetti-western): wire as a
    confrontation kind alongside dogfight (ADR-077) and edge/composure
    (ADR-078). See ``docs/content-drift-triage.md`` for triage rationale.
    """

    model_config = {"extra": "allow"}

    description: str


class ReputationFaction(BaseModel):
    """One faction in the per-genre reputation track.

    spaghetti_western models a -100..+100 reputation score per faction
    (outlaws, law, merchants, ...). Each faction has an ``id`` used as a
    key in the future per-character reputation map, plus a display ``name``
    and a short ``description`` injected into narrator context. No engine
    consumer reads these yet. TODO(spaghetti-western): wire as a per-faction
    reputation track with NPC-disposition effects at the high/low
    ``ReputationEffects`` thresholds. See ``docs/content-drift-triage.md``.
    """

    model_config = {"extra": "forbid"}

    id: str
    name: str
    description: str


class ReputationEffects(BaseModel):
    """Narrative effects fired at the high/neutral/low reputation bands.

    These are *prose hooks* — they tell the narrator what NPCs of a faction
    DO when the player is in good/neutral/bad standing. No mechanical
    bonuses are encoded; the mechanical effect lives in the narrator
    context injection. TODO(spaghetti-western): wire alongside
    ``ReputationFaction``. See ``docs/content-drift-triage.md``.
    """

    model_config = {"extra": "forbid"}

    high: list[str] = Field(default_factory=list)
    neutral: list[str] = Field(default_factory=list)
    low: list[str] = Field(default_factory=list)


class LuckSpendEffect(BaseModel):
    """One named luck-spend effect (Cheat Death, Lucky Break, ...).

    ``cost`` is the luck-pool debit; ``effect`` is the narrator prose +
    mechanical hint. No engine consumer reads these yet — the luck
    resource is declared in ``rules.yaml > resources`` (a generic per-actor
    pool) but its *spend menu* needs the narrator to surface options to
    the player. TODO(spaghetti-western): wire as a narrator tool (one tool
    per spend effect, gated on pool ≥ cost). See
    ``docs/content-drift-triage.md``.
    """

    model_config = {"extra": "forbid"}

    name: str
    cost: int
    effect: str


class LuckRecovery(BaseModel):
    """Luck-pool recovery cadence.

    ``per_session`` adds N to every actor's luck at session start (clamped
    to ``max_luck``). ``bonus_triggers`` are narrator prose hooks — events
    that should refresh luck mid-session. TODO(spaghetti-western): wire
    per_session at session-start hook + map bonus_triggers to narrator
    tool emissions.
    """

    model_config = {"extra": "forbid"}

    per_session: int = 0
    bonus_triggers: list[str] = Field(default_factory=list)


class LuckRules(BaseModel):
    """Top-level luck-as-resource configuration.

    spaghetti_western signature resource — the "lucky drifter" archetype.
    Each character starts with ``starting_luck``, capped at ``max_luck``.
    Spend menu lives in ``spend_effects``. The numeric pool itself is
    declared (and currently the *only* hooked-up bit) in
    ``rules.yaml > resources`` as a ``ResourceDeclaration`` named ``luck``;
    this block adds the *menu* and *recovery* that the narrator needs to
    expose. TODO(spaghetti-western): wire luck-spend as a narrator tool.
    See ``docs/content-drift-triage.md``.
    """

    model_config = {"extra": "forbid"}

    starting_luck: int = 0
    max_luck: int = 0
    spend_effects: list[LuckSpendEffect] = Field(default_factory=list)
    recovery: LuckRecovery | None = None


class SwnConfig(BaseModel):
    """SWN universal constants (per-class/per-item numbers live in pack content).

    All values sourced verbatim from Stars Without Number Revised Edition
    Free Edition (Sine Nomine Publishing, 2017):

    - unarmored_ac=10: ascending AC baseline for an unarmoured target (SRD p.51,
      "Examples of Murder": "Yaddle, who has an AC of 10").
    - save_base=15: "Your character's saving throw scores start at 15, and
      decrease by one point each time you advance a level." (SRD p.46,
      Saving Throws section).  The per-call formula is:
          target = save_base - (level - 1) = 16 - level
      modified by the best of two attribute modifiers (Physical: better of
      Str/Con; Evasion: better of Dex/Int; Mental: better of Cha/Wis).
    - difficulties: 2d6 skill-check difficulty ladder (SRD p.47, "Skill Check
      Difficulties" table): easy=6, routine=8, tricky=10, hard=12, formidable=14.
    """

    model_config = {"extra": "forbid"}

    unarmored_ac: int = 10  # SRD p.51 — ascending AC for unarmoured target
    save_base: int = 15  # SRD p.46 — level-1 saving throw target (decreases by 1/level)
    difficulties: dict[str, int] = Field(
        default_factory=lambda: {
            "easy": 6,
            "routine": 8,
            "tricky": 10,
            "hard": 12,
            "formidable": 14,
        }
    )
    # SWN attribute name -> this pack's flavor stat (ability_score_names entry).
    # Required (non-empty, all six keys) when ruleset == "swn"; validated on RulesConfig
    # where ability_score_names is reachable. No default map — fail loud if unauthored.
    attribute_map: dict[str, str] = Field(default_factory=dict)


class SystemStrainConfig(BaseModel):
    """CWN System Strain tuning (genre-level, content-authorable).

    max_source: the CANONICAL attribute whose flavor-stat score caps strain
      (CWN: CONSTITUTION). Validated on RulesConfig to be a key of cwn.attribute_map.
    rest_recovery_per_night: strain removed per night of rest (down to the
      permanent floor).
    first_aid_cost: temporary strain added per first-aid application.
    """

    model_config = {"extra": "forbid"}

    max_source: str = "CONSTITUTION"
    rest_recovery_per_night: int = 1
    first_aid_cost: int = 1


class TraumaConfig(BaseModel):
    """CWN combat-lethality tuning (genre-level, content-authorable).

    default_trauma_target: the Trauma Target an unarmored human presents — the
      number a weapon's Trauma Die must MEET OR EXCEED for a Traumatic Hit
      (CWN: 6). A weapon may override per-strike via DamageSpec.trauma_target.
    mortal_injury_rounds: rounds a downed (0-HP) character survives before death
      unless stabilized (CWN: 6).
    major_injury_save: the save category rolled when a Traumatic Hit dropped the
      character this scene (CWN: a Physical save). Must be a save the bound
      module's save_params understands ("physical", "evasion", "mental", "luck").
    """

    model_config = {"extra": "forbid"}

    default_trauma_target: int = 6
    mortal_injury_rounds: int = 6
    major_injury_save: str = "physical"


class CwnConfig(SwnConfig):
    """Cities Without Number universal constants (Sine Nomine, CC0).

    CWN shares SWN's resolution engine, so this inherits SwnConfig verbatim:
    - unarmored_ac=10, save_base=15 (CWN's "16 - level" == "save_base - (level-1)").
    - the 6/8/10/12/14 difficulty ladder.
    - attribute_map: CWN attribute -> this pack's flavor stat (all six keys
      required when ruleset == 'cwn'; validated on RulesConfig).
    System Strain is configured via ``system_strain`` (System Strain plan).
    Trauma is configured via ``trauma`` (Combat Lethality plan).
    """

    model_config = {"extra": "forbid"}

    system_strain: SystemStrainConfig = Field(default_factory=SystemStrainConfig)
    trauma: TraumaConfig = Field(default_factory=TraumaConfig)


class RulesConfig(BaseModel):
    """Game rules configuration."""

    model_config = {"extra": "forbid"}

    ruleset: str = (
        "native"  # bound RulesetModule slug (pluggable-SRD Spec 0). Default = current dial engine.
    )
    tone: str = ""
    lethality: str = ""
    magic_level: str = ""
    stat_generation: str = ""
    point_buy_budget: int = 0
    ability_score_names: list[str] = Field(default_factory=list)
    allowed_classes: list[str] = Field(default_factory=list)
    allowed_races: list[str] = Field(default_factory=list)
    edge_config: EdgeConfig | None = None
    # Story 50-13: genre-configurable disposition→attitude numeric bands.
    # None ⇒ the loader applies DEFAULT_ATTITUDE_THRESHOLDS (±10, the
    # pre-50-13 ADR-020 contract). The qualitative bands stay the locked
    # three-tier Attitude enum — only the cut points move.
    disposition_thresholds: AttitudeThresholds | None = None
    default_class: str | None = None
    default_race: str | None = None
    race_label: str | None = None
    class_label: str | None = None
    # Per-pack character-sheet vocabulary. Keys are the canonical chargen
    # field names (``name``, ``race``, ``class``, ``personality``,
    # ``pronouns``, ``stats``, ``mutation``, ``affinity``, ``rig``,
    # ``rig_trait``, ``equipment``, ``backstory``); values are the
    # display labels used by the confirmation summary and the
    # client-side character-sheet preview. Empty by default — packs
    # opt in. Defaults preserve the legacy fantasy labels (``Race``,
    # ``Class``, etc.) so existing packs are unaffected. The legacy
    # ``race_label`` / ``class_label`` fields above are honored as
    # secondary defaults for those two keys when this map omits them.
    chargen_field_labels: dict[str, str] = Field(default_factory=dict)
    default_location: str | None = None
    default_time_of_day: str | None = None
    banned_spells: list[str] = Field(default_factory=list)
    custom_rules: dict[str, str] = Field(default_factory=dict)
    stat_display_fields: list[str] = Field(default_factory=list)
    encounter_base_tension: dict[str, float] = Field(default_factory=dict)
    resources: list[ResourceDeclaration] = Field(default_factory=list)
    confrontations: list[ConfrontationDef] = Field(default_factory=list)
    xp_affinity: str | None = None
    initiative_rules: dict[str, InitiativeRule] = Field(default_factory=dict)
    # spaghetti_western authored mechanics — typed-but-unconsumed.
    # The pack ships fully-detailed standoff phases, faction reputation
    # bands, and a luck-spend menu. The engine does NOT yet read any of
    # them; they're loaded so the pack passes strict pydantic validation
    # and so future consumer-wiring stories have a contract to code
    # against. See ``docs/content-drift-triage.md`` ("Triage notes —
    # spaghetti_western") for the wiring backlog.
    standoff_rules: dict[str, StandoffPhase] = Field(default_factory=dict)
    reputation_factions: list[ReputationFaction] = Field(default_factory=list)
    reputation_effects: ReputationEffects | None = None
    luck_rules: LuckRules | None = None
    # Present only when ruleset == "swn"; None for all other rulesets.
    swn: SwnConfig | None = None
    # Present only when ruleset == "cwn"; None for all other rulesets.
    cwn: CwnConfig | None = None
    # ADR-113 confidence gate (Story 71-16): per-subsystem engagement
    # thresholds. Keys are dispatch subsystem names (``confrontation``,
    # ``magic_working``, ``scenario_clue``, ``npc_agency``, ``movement``,
    # ``distinctive_detail_hint``, ``reflect_absence``); values are the minimum
    # router confidence required to engage that subsystem's engine. A subsystem
    # absent from this map uses the 0.6 default (run_dispatch_bank). Empty by
    # default — packs opt in to per-subsystem tuning.
    dispatch_confidence_thresholds: dict[str, float] = Field(default_factory=dict)

    @field_validator("dispatch_confidence_thresholds")
    @classmethod
    def _validate_dispatch_thresholds(cls, value: dict[str, float]) -> dict[str, float]:
        """Fail loud on a malformed per-subsystem threshold (No Silent Fallbacks).

        A threshold outside [0.0, 1.0] is a config error, not something to clamp
        or silently default — raise so the pack fails to load.
        """
        for subsystem, threshold in value.items():
            if not 0.0 <= threshold <= 1.0:
                raise ValueError(
                    f"dispatch_confidence_thresholds[{subsystem!r}] = {threshold} "
                    f"is out of range; confidence thresholds must be in [0.0, 1.0]"
                )
        return value

    @model_validator(mode="after")
    def _validate_swn(self) -> RulesConfig:
        """Enforce a complete attribute_map when ruleset == 'swn'; raises ValueError if the swn block omits one."""
        if self.ruleset != "swn":
            return self
        if self.swn is None:
            object.__setattr__(self, "swn", SwnConfig())
        required = {"STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA"}
        # self.swn cannot be None here — the branch above ensures it; assert for pyright.
        assert self.swn is not None
        amap = self.swn.attribute_map
        if not amap:
            raise ValueError(
                "ruleset 'swn' requires rules.swn.attribute_map (SWN attribute -> flavor stat); "
                "none authored — no silent default"
            )
        missing = required - amap.keys()
        if missing:
            raise ValueError(f"swn attribute_map missing required keys: {sorted(missing)}")
        declared = set(self.ability_score_names)
        for swn_attr, flavor in amap.items():
            if flavor not in declared:
                raise ValueError(
                    f"swn attribute_map[{swn_attr!r}] = {flavor!r} is not in "
                    f"ability_score_names {sorted(declared)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_cwn(self) -> RulesConfig:
        """Enforce a complete attribute_map when ruleset == 'cwn'; raises ValueError if omitted."""
        if self.ruleset != "cwn":
            return self
        if self.cwn is None:
            object.__setattr__(self, "cwn", CwnConfig())
        required = {"STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA"}
        assert self.cwn is not None
        amap = self.cwn.attribute_map
        if not amap:
            raise ValueError(
                "ruleset 'cwn' requires rules.cwn.attribute_map (CWN attribute -> flavor stat); "
                "none authored — no silent default"
            )
        missing = required - amap.keys()
        if missing:
            raise ValueError(f"cwn attribute_map missing required keys: {sorted(missing)}")
        declared = set(self.ability_score_names)
        for cwn_attr, flavor in amap.items():
            if flavor not in declared:
                raise ValueError(
                    f"cwn attribute_map[{cwn_attr!r}] = {flavor!r} is not in "
                    f"ability_score_names {sorted(declared)}"
                )
        strain_source = self.cwn.system_strain.max_source
        if strain_source not in amap:
            raise ValueError(
                f"cwn.system_strain.max_source = {strain_source!r} is not a key of "
                f"cwn.attribute_map {sorted(amap.keys())}"
            )
        valid_saves = {"physical", "evasion", "mental", "luck"}
        if self.cwn.trauma.major_injury_save not in valid_saves:
            raise ValueError(
                f"cwn.trauma.major_injury_save = {self.cwn.trauma.major_injury_save!r} "
                f"is not one of {sorted(valid_saves)}"
            )
        return self

    def ruleset_config(self) -> SwnConfig | None:
        """The config block for the bound ruleset, or None for engines that carry none.

        Dispatch resolves the cfg this way instead of hardcoding `.swn`, so a
        `cwn` pack receives its own block. `native` carries no config (None).
        """
        if self.ruleset == "swn":
            return self.swn
        if self.ruleset == "cwn":
            return self.cwn
        return None

    @property
    def intent_verbs_by_type(self) -> dict[str, frozenset[str]]:
        """Mapping of confrontation_type -> derived intent verb set.

        Consumed by sidequest.agents.confrontation_intent_validator.validate
        (Task 3). The frozensets themselves are shared with each
        ConfrontationDef.intent_verb_set so this property is cheap to call.
        """
        return {cd.confrontation_type: cd.intent_verb_set for cd in self.confrontations}
