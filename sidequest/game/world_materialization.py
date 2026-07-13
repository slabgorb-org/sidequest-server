"""Campaign maturity + world materialization from genre pack history.

Maps:
- ``CampaignMaturity`` — Fresh / Early / Mid / Veteran progression tier.
- ``parse_history_chapters`` — extract a typed chapter list from raw
  pack history (history.yaml is loaded as a nested dict/value).
- ``WorldBuilder`` — fluent builder that produces a GameSnapshot at a
  given maturity by applying chapters cumulatively.
- ``materialize_world`` — apply chapters in-place to an existing
  snapshot (Story 6-6 shape).
- ``materialize_from_genre_pack`` — the dispatch-time entry point;
  parses pack history and returns a materialized snapshot.

Chapter DTOs (``HistoryChapter``, ``ChapterCharacter`` etc.) live in
``sidequest.game.history_chapter`` to avoid a circular import with
``session.py``.

No silent fallbacks: unparseable chapter data raises
``HistoryParseError``; the dispatch layer decides whether to log-and-
continue or propagate. Chapter ids outside {fresh, early, mid, veteran}
are skipped — ``CampaignMaturity.from_chapter_id`` returns None, so the
chapter doesn't match any maturity level.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from opentelemetry import trace

from sidequest.game.character import Character
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.disposition import CHAPTER_BEAT_REASON
from sidequest.game.green_room import MaterializationCandidate, admit
from sidequest.game.history_chapter import (
    ChapterCharacter,
    ChapterNpc,
    ChapterTrope,
    HistoryChapter,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.origin import Origin, OriginKind
from sidequest.game.session import NarrativeEntry, Npc, TropeState, upsert_quest_status
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.genre.models.ocean import OceanProfile


def _auto_description(race: str, char_class: str) -> str:
    """Compose the chargen-style 'A {race} {class}' description.

    Mirrors the format used by ``GenericCharacterBuilder.build`` in
    ``builder.py`` so that race-changing chapter updates can detect
    and refresh an auto-generated description (Story 45-7).
    """
    return f"A {race} {char_class}"


def _is_auto_description(description: str, race: str, char_class: str) -> bool:
    """True when a description matches the chargen auto-template format.

    Used by ``_apply_character`` to detect 'A {race} {class}' descriptions
    that should be regenerated when race/class change in a chapter update.
    Match is exact: any human-edited description (including additional
    sentences or whitespace) is preserved untouched.
    """
    return description == _auto_description(race, char_class)


# ---------------------------------------------------------------------------
# CampaignMaturity
# ---------------------------------------------------------------------------


class CampaignMaturity(StrEnum):
    """Campaign maturity tier derived from turn count + beats fired."""

    Fresh = "Fresh"
    """Turns 0-5 effective: minimal history, world is new."""

    Early = "Early"
    """Turns 6-20 effective: factions introduced, stakes emerging."""

    Mid = "Mid"
    """Turns 21-50 effective: established relationships, tensions rising."""

    Veteran = "Veteran"
    """Turns 51+ effective: deep history, faction conflicts in motion."""

    @classmethod
    def from_snapshot(cls, snapshot: Any) -> CampaignMaturity:
        """Derive maturity from a snapshot's turn count + beats fired.

        Beats accelerate maturity: a dramatic early game matures faster.
        ``effective = round + beats / 2``.
        """
        try:
            turn = int(snapshot.turn_manager.round)
        except AttributeError:
            turn = 0
        beats = int(getattr(snapshot, "total_beats_fired", 0))
        effective = max(0, turn) + max(0, beats) // 2
        if effective <= 5:
            return cls.Fresh
        if effective <= 20:
            return cls.Early
        if effective <= 50:
            return cls.Mid
        return cls.Veteran

    @classmethod
    def from_chapter_id(cls, chapter_id: str) -> CampaignMaturity | None:
        """Map a chapter id to its maturity tier; ``None`` for unknown ids."""
        match chapter_id:
            case "fresh":
                return cls.Fresh
            case "early":
                return cls.Early
            case "mid":
                return cls.Mid
            case "veteran":
                return cls.Veteran
            case _:
                return None

    def _ordinal(self) -> int:
        """Ordering helper — chapters at or below the target tier apply."""
        order = {
            CampaignMaturity.Fresh: 0,
            CampaignMaturity.Early: 1,
            CampaignMaturity.Mid: 2,
            CampaignMaturity.Veteran: 3,
        }
        return order[self]

    def __le__(self, other: CampaignMaturity) -> bool:  # type: ignore[override]
        if not isinstance(other, CampaignMaturity):
            return NotImplemented
        return self._ordinal() <= other._ordinal()

    def __lt__(self, other: CampaignMaturity) -> bool:  # type: ignore[override]
        if not isinstance(other, CampaignMaturity):
            return NotImplemented
        return self._ordinal() < other._ordinal()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HistoryParseError(ValueError):
    """Raised when pack ``history.yaml`` chapter data can't be parsed.

    The dispatch layer catches this and logs-and-continues with an
    empty chapter list so a malformed history never hard-fails a
    session.
    """


# ---------------------------------------------------------------------------
# Parsing — history.yaml → HistoryChapter list
# ---------------------------------------------------------------------------


def parse_history_chapters(value: Any) -> list[HistoryChapter]:
    """Extract a typed chapter list from raw pack history.

    The genre pack loader stores ``history.yaml`` as an untyped dict.
    The outer shape is ``{"chapters": [...]}`` when present; we return
    an empty list for null, missing ``chapters`` key, or empty chapter
    list.
    """
    if value is None:
        return []
    if not isinstance(value, dict):
        raise HistoryParseError(f"history payload must be a mapping, got {type(value).__name__}")
    chapters_raw = value.get("chapters")
    if chapters_raw is None:
        return []
    if not isinstance(chapters_raw, list):
        raise HistoryParseError(
            f"history 'chapters' must be a list, got {type(chapters_raw).__name__}"
        )
    parsed: list[HistoryChapter] = []
    for i, entry in enumerate(chapters_raw):
        try:
            parsed.append(HistoryChapter.model_validate(entry))
        except Exception as exc:
            raise HistoryParseError(
                f"failed to parse history chapter at index {i}: {exc!r}"
            ) from exc
    return parsed


# ---------------------------------------------------------------------------
# WorldBuilder
# ---------------------------------------------------------------------------


class WorldBuilder:
    """Fluent builder that produces a GameSnapshot at a given maturity.

    Methods:
    - ``__init__`` — default Fresh maturity, no chapters.
    - ``at_maturity`` — set the target maturity tier.
    - ``with_chapters`` — supply history chapters to apply.
    - ``build`` — materialize the snapshot, returning a new GameSnapshot.
    """

    def __init__(self) -> None:
        self.maturity: CampaignMaturity = CampaignMaturity.Fresh
        self.chapters: list[HistoryChapter] = []

    def at_maturity(self, maturity: CampaignMaturity) -> WorldBuilder:
        """Set the target campaign maturity level."""
        self.maturity = maturity
        return self

    def with_chapters(self, chapters: list[HistoryChapter]) -> WorldBuilder:
        """Provide history chapters to apply."""
        self.chapters = list(chapters)
        return self

    def build(self) -> Any:
        """Build a GameSnapshot at the configured maturity.

        Filters chapters by maturity (cumulative — includes every
        chapter whose tier is at or below the target), applies each
        chapter's data to the snapshot in order, then stores the
        applied chapters as ``snap.world_history`` so the save-file
        round-trips them.
        """
        # Local import to avoid circular dependency: session → world_materialization.
        from sidequest.game.session import GameSnapshot

        snap = GameSnapshot(campaign_maturity=self.maturity.value)

        applicable: list[HistoryChapter] = [
            ch
            for ch in self.chapters
            if (maturity := CampaignMaturity.from_chapter_id(ch.id)) is not None
            and maturity <= self.maturity
        ]

        for chapter in applicable:
            self._apply_chapter(snap, chapter)

        snap.world_history = list(applicable)
        return snap

    # ------------------------------------------------------------------
    # apply_chapter — the heart of materialization
    # ------------------------------------------------------------------

    def _apply_chapter(self, snap: Any, chapter: HistoryChapter) -> None:
        """Apply a single chapter to the snapshot, cumulatively.

        Semantics:
        - Character data populates a new Character when snapshot is empty,
          otherwise selectively updates existing fields.
        - NPCs upsert by name (update existing, else append new).
        - Quests insert/update in the ``quest_log`` dict.
        - Lore entries append to ``lore_established`` (deduplicated).
        - Notes append (no dedup).
        - Narrative log entries append, converting to ``NarrativeEntry``.
        - Scene context (location/time_of_day/atmosphere/active_stakes)
          OVERWRITES from the latest chapter that declares it — later
          chapters win.
        - Tropes upsert by definition id.
        """
        if chapter.character is not None:
            self._apply_character(snap, chapter.character)

        for npc_data in chapter.npcs:
            self._apply_npc(snap, npc_data)

        for quest_name, status in chapter.quests.items():
            # Story 77-2: status-only upsert under the widened QuestEntry type.
            upsert_quest_status(snap.quest_log, quest_name, status)

        for entry in chapter.lore:
            if entry not in snap.lore_established:
                snap.lore_established.append(entry)

        snap.notes.extend(chapter.notes)

        for entry in chapter.narrative_log:
            snap.narrative_log.append(
                NarrativeEntry(
                    timestamp=0,
                    round=0,
                    author=entry.speaker,
                    content=entry.text,
                    tags=[],
                    speaker=entry.speaker,
                    entry_type=None,
                )
            )

        if chapter.location is not None:
            # Wave 2B (story 45-48): party-level snapshot.location removed.
            # Stamp the chapter's location onto every existing character's
            # per-character entry so subsequent narrator framing and views
            # see the materialized scene. Characters added later (e.g. by
            # chargen confirmation) will populate their own entry via
            # init_room_graph_location.
            for ch in snap.characters:
                snap.character_locations[ch.core.name] = chapter.location
        if chapter.time_of_day is not None:
            snap.time_of_day = chapter.time_of_day
        if chapter.atmosphere is not None:
            snap.atmosphere = chapter.atmosphere
        if chapter.active_stakes is not None:
            snap.active_stakes = chapter.active_stakes

        for trope_data in chapter.tropes:
            self._apply_trope(snap, trope_data)

    # ------------------------------------------------------------------
    # apply_character — create-or-update the player character
    # ------------------------------------------------------------------

    def _apply_character(self, snap: Any, char_data: ChapterCharacter) -> None:
        """Build or update the player character from chapter data.

        When the snapshot has no character, creates one from chapter
        fields with sensible defaults for any missing identity fields.
        When a character already exists, selectively updates in place —
        only non-empty fields on the chapter overwrite.

        Note: hp/max_hp/ac from chapter are intentionally unused — the
        HpPool seeds from hp_pool_from_config at chargen; these chapter
        fields are advisory defaults, not a silent fallback (ADR-114).
        """
        if not snap.characters:
            name = char_data.name if char_data.name else "Adventurer"
            race = char_data.race if char_data.race else "Human"
            cls = char_data.class_name if char_data.class_name else "Fighter"
            # Story 45-7: when chapter omits a description, fall back to
            # the chargen auto-template so the description reflects the
            # actual race/class instead of an opaque 'An adventurer.' stub.
            description = char_data.description or _auto_description(race, cls)
            personality = char_data.personality or "Determined."
            backstory = char_data.backstory or "Unknown origins."

            core = CreatureCore(
                name=name,
                description=description,
                personality=personality,
                level=max(1, char_data.level) if char_data.level else 1,
                xp=0,
                inventory=Inventory(),
                statuses=[],
                hp=HpPool(current=10, max=10, base_max=10),
                acquired_advancements=[],
            )
            snap.characters.append(
                Character(
                    core=core,
                    backstory=backstory,
                    narrative_state="",
                    hooks=[],
                    char_class=cls,
                    race=race,
                    pronouns="",
                    stats={},
                    abilities=[],
                    affinities=[],
                    is_friendly=True,
                    known_facts=[],
                    resolved_archetype=None,
                    archetype_provenance=None,
                )
            )
            return

        # Update existing character — selective, non-empty fields only.
        char = snap.characters[0]
        # Capture pre-update race/class so we can detect auto-template
        # descriptions written against the previous identity (Story 45-7).
        prev_race = char.race
        prev_class = char.char_class
        prev_description = char.core.description
        if char_data.level > 0:
            char.core.level = char_data.level
        if char_data.name:
            char.core.name = char_data.name
        if char_data.race:
            char.race = char_data.race
        if char_data.class_name:
            char.char_class = char_data.class_name
        if char_data.backstory:
            char.backstory = char_data.backstory
        if char_data.personality:
            char.core.personality = char_data.personality
        if char_data.description:
            char.core.description = char_data.description
        else:
            # Story 45-7: if the chapter changes race or class without
            # supplying a fresh description, refresh an auto-generated
            # description ('A {race} {class}') so it tracks the new
            # identity. Hand-authored descriptions are detected by
            # comparing against the prior auto-template format and left
            # untouched. No silent fallback: only the exact prior
            # auto-template string is rewritten.
            race_or_class_changed = char.race != prev_race or char.char_class != prev_class
            if race_or_class_changed and _is_auto_description(
                prev_description, prev_race, prev_class
            ):
                new_desc = _auto_description(char.race, char.char_class)
                char.core.description = new_desc
                span = trace.get_current_span()
                span.add_event(
                    "world_materialization.description_refreshed",
                    {
                        "reason": "race_or_class_changed",
                        "prev_race": prev_race,
                        "prev_class": prev_class,
                        "new_race": char.race,
                        "new_class": char.char_class,
                        "prev_description": prev_description,
                        "new_description": new_desc,
                    },
                )
        # hp/max_hp/ac/gold: advisory only, see docstring.

    # ------------------------------------------------------------------
    # apply_npc — instantiate or update an NPC
    # ------------------------------------------------------------------

    def _ensure_world_authored_pool_member(self, snap: Any, name: str) -> None:
        """Story 61-8 §B (review-fix round 2) — pool exhaustiveness on
        BOTH branches of ``_apply_npc``.

        Round 1 only seeded the pool on the new-NPC branch, leaving
        legacy saves and any NPC created by other code paths invisible
        to the 61-2 projection's identity-fallback. This helper is
        called from both branches of ``_apply_npc`` so every world-
        authored NPC has a canonical pool entry after chapter apply.

        Idempotency contract:

        * No same-name member → append a new ``world_authored`` member.
        * Existing member with ``drawn_from="dialogue_extraction"`` or
          ``observation_pending=True`` (auto-minted from prose) →
          PROMOTE to canonical world-authored: set
          ``drawn_from="world_authored"`` and ``observation_pending=False``.
          This makes the docstring-promised "world-authored NPCs override
          a prior pending entry" contract actually hold.
        * Existing member already canonical (``drawn_from="world_authored"``
          or any non-prose source with ``observation_pending=False``) →
          leave untouched. Re-applying the same chapter is a no-op.
        """
        existing_member = next((m for m in snap.npc_pool if m.name == name), None)
        if existing_member is None:
            snap.npc_pool.append(
                NpcPoolMember(
                    name=name,
                    role=None,
                    pronouns=None,
                    appearance=None,
                    archetype_id=None,
                    drawn_from="world_authored",
                    observation_pending=False,
                )
            )
            return
        # Promote prose-pending entries to canonical world-authored.
        # Other source tags (name_generator, legacy_registry, etc.) are
        # left alone — chapter apply is not a strict authority over
        # entries minted by those paths, only over prose-extracted ones.
        if (
            existing_member.observation_pending
            or existing_member.drawn_from == "dialogue_extraction"
        ):
            existing_member.drawn_from = "world_authored"
            existing_member.observation_pending = False

    def _apply_npc(self, snap: Any, npc_data: ChapterNpc) -> None:
        """Upsert an NPC by name.

        Blank name → skip (short-circuit). Existing NPC → update
        disposition, description, personality, location in place. New
        NPC → append a new ``Npc`` with chapter data and defaults for
        Phase-1-deferred fields (OCEAN, belief state, resolution tier,
        archetype axes). In BOTH branches, ensure ``snap.npc_pool``
        carries an identity-only ``NpcPoolMember`` (Story 61-8 §B —
        closes the exhaustiveness gap that the 61-2 review flagged: the
        61-2 projection drops off-stage NPCs from ``snap.npcs`` and
        falls back to ``snap.npc_pool`` for identity; every world-
        authored NPC must have a pool entry regardless of which branch
        of ``_apply_npc`` it traversed, including legacy saves where
        the NPC already exists in ``snap.npcs`` but pre-dates §B's
        pool-seed code).
        """
        if not npc_data.name:
            return

        existing: Npc | None = next((n for n in snap.npcs if n.core.name == npc_data.name), None)
        if existing is not None:
            if npc_data.disposition is not None:
                before = int(existing.disposition)
                existing.disposition = int(npc_data.disposition)
                after = int(existing.disposition)
                # ADR-136 (site 4/4): a chapter that moves an already-known
                # NPC's standing is a relationship beat. The new-NPC branch
                # below sets a baseline (no shift), so it records nothing.
                # Zero-delta no-ops are dropped by the seam's guard.
                existing.record_disposition_beat(
                    turn=snap.turn_manager.interaction,
                    delta=after - before,
                    reason=CHAPTER_BEAT_REASON,
                    location=npc_data.location or existing.location,
                )
            if npc_data.description:
                existing.core.description = npc_data.description
            if npc_data.location:
                existing.location = npc_data.location
            if npc_data.personality:
                existing.core.personality = npc_data.personality
            # Story 61-8 §B (review-fix round 2): pool exhaustiveness
            # MUST run for legacy saves where snap.npcs has the NPC but
            # snap.npc_pool does not (the §B round-1 fix only covered
            # the new-NPC branch below).
            self._ensure_world_authored_pool_member(snap, npc_data.name)
            return

        core = CreatureCore(
            name=npc_data.name,
            description=npc_data.description or "An NPC.",
            personality=npc_data.personality or "Neutral.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
            acquired_advancements=[],
        )
        new_npc = Npc(
            core=core,
            disposition=int(npc_data.disposition or 0),
            location=npc_data.location,
            pronouns=None,
            appearance=None,
            age=None,
            build=None,
            height=None,
            distinguishing_features=[],
            ocean=None,
            resolution_tier="spawn",
            non_transactional_interactions=0,
            jungian_id=None,
            rpg_role_id=None,
            npc_role_id=None,
            resolved_archetype=None,
        )
        # Green Room Task 3 (ADR-156): a history-chapter-authored NPC is
        # world-authored content — AUTHORED tier, same as npcs.yaml preload,
        # just with no per-row id (ChapterNpc carries no ``id`` field).
        admit(
            snap,
            [
                MaterializationCandidate(
                    npc=new_npc,
                    origin=new_npc.origin
                    or Origin(kind=OriginKind.AUTHORED, creature_id=new_npc.creature_id),
                    source="worldbuilder_history",
                )
            ],
        )
        self._ensure_world_authored_pool_member(snap, npc_data.name)

    # ------------------------------------------------------------------
    # apply_trope — upsert a trope state by definition id
    # ------------------------------------------------------------------

    def _apply_trope(self, snap: Any, trope_data: ChapterTrope) -> None:
        """Upsert a trope state on the snapshot.

        Blank id → skip. Unknown status string → defaults to "active"
        (no guard beyond the four known values). ``TropeState`` stores
        the id under ``id``.
        """
        if not trope_data.id:
            return

        status = trope_data.status
        if status not in {"dormant", "active", "progressing", "resolved"}:
            status = "active"

        existing: TropeState | None = next(
            (t for t in snap.active_tropes if t.id == trope_data.id), None
        )
        if existing is not None:
            existing.status = status
            existing.progress = float(trope_data.progression)
            return

        snap.active_tropes.append(
            TropeState(
                id=trope_data.id,
                status=status,
                progress=float(trope_data.progression),
                beats_fired=0,
            )
        )


# ---------------------------------------------------------------------------
# Story 45-19 — arc-recompute cadence
#
# Felix's Playtest 3 (2026-04-19) reached turn 72 with a snapshot that
# was still reporting ``campaign_maturity="Fresh"`` and four chapters
# covering turns 1-30 only. The chargen path materialized once and no
# subsequent caller ever invoked ``materialize_world`` again — so the
# bug is the cadence, not the formula.
#
# ``ARC_RECOMPUTE_INTERVAL`` is the module-level tunable that the
# dispatch loop consults via ``should_recompute_arc``. Default 5 means
# a cadence tick every five interactions; the recompute is idempotent
# on stable maturity so ticking past Veteran is a cheap confirmation
# rather than a regression.
# ---------------------------------------------------------------------------

ARC_RECOMPUTE_INTERVAL: int = 5


def should_recompute_arc(interaction: int) -> bool:
    """Return True when the just-completed interaction is a tick turn.

    Called by ``_execute_narration_turn`` after ``record_interaction``,
    so ``interaction`` is the post-bump value. Interaction 0 is the
    chargen materialization site — we never tick there because the
    chargen path has already done a fresh ``materialize_from_genre_pack``
    call. Negative values are defensive (a programming bug, not a
    legitimate call), but they must not trip a recompute.
    """
    if interaction <= 0:
        return False
    return interaction % ARC_RECOMPUTE_INTERVAL == 0


def recompute_arc_history(snapshot: Any, chapters: list[HistoryChapter]) -> list[HistoryChapter]:
    """Recompute ``world_history`` / ``campaign_maturity`` and emit the
    arc-tick OTEL spans.

    The wrapper around ``materialize_world`` that the dispatch loop
    calls on the cadence. Two spans always fire one of:

    - ``world_history.arc_tick`` — every call (the lie-detector signal
      Sebastien needs on the GM panel; a stable-tier no-op is still
      observable).
    - ``world_history.arc_promoted`` — only when the maturity tier
      crosses upward, scoped for filtered views of the meaningful
      transitions (Fresh→Early, Early→Mid, Mid→Veteran).

    ``materialize_world`` keeps its own ``world.materialized`` span,
    so the existing chargen-time materialization remains observable.

    Returns the list of newly-promoted ``HistoryChapter`` objects (the
    diff between ``chapters_before`` and ``chapters_after``). Empty list
    when the recompute is a stable-tier no-op. The 45-23 dispatch site
    consumes this list to drive the chapter-promotion writeback into
    ``snapshot.narrative_log`` and the lore store; computing the diff
    here keeps the math single-sourced rather than re-derived at the
    call site.
    """
    from sidequest.telemetry.spans import (
        SPAN_WORLD_HISTORY_ARC_PROMOTED,
        SPAN_WORLD_HISTORY_ARC_TICK,
        Span,
    )

    interaction = int(getattr(getattr(snapshot, "turn_manager", None), "interaction", 0) or 0)
    round_value = int(getattr(getattr(snapshot, "turn_manager", None), "round", 0) or 0)

    chapters_before_ids = [getattr(ch, "id", "") for ch in snapshot.world_history]
    chapters_before = len(chapters_before_ids)
    # ``from_maturity`` is the maturity STRING the snapshot was last
    # written with (chargen wrote ``Fresh``; subsequent ticks update the
    # field). Comparing the stored string against the freshly-derived
    # maturity is what makes the promotion observable — the formula
    # itself is stable across a single recompute call (it depends only
    # on turn_manager.round + total_beats_fired, neither of which the
    # recompute touches), so deriving "from" from the snapshot would
    # always equal "to".
    from_maturity_str = str(getattr(snapshot, "campaign_maturity", "") or "")

    materialize_world(snapshot, chapters)

    chapters_after_ids = [getattr(ch, "id", "") for ch in snapshot.world_history]
    chapters_after = len(chapters_after_ids)
    to_maturity = CampaignMaturity.from_snapshot(snapshot)
    if not from_maturity_str:
        # First-tick fallback: snapshot has never been materialized, so
        # the stored string is empty. Treat that as Fresh — anything
        # other than Fresh on the new side is a real promotion.
        from_maturity_str = CampaignMaturity.Fresh.value
    tier_changed = from_maturity_str != to_maturity.value

    with Span.open(
        SPAN_WORLD_HISTORY_ARC_TICK,
        {
            "interaction": interaction,
            "round": round_value,
            "from_maturity": from_maturity_str,
            "to_maturity": to_maturity.value,
            "chapters_before": chapters_before,
            "chapters_after": chapters_after,
            "tier_changed": tier_changed,
            "cadence_interval": ARC_RECOMPUTE_INTERVAL,
        },
    ):
        pass

    added_chapters: list[HistoryChapter] = []
    if tier_changed:
        added_ids = [ch_id for ch_id in chapters_after_ids if ch_id not in chapters_before_ids]
        added_set = set(added_ids)
        # Resolve chapter-id strings back to the HistoryChapter objects
        # the 45-23 seeding helper consumes. Walk ``snapshot.world_history``
        # (the post-materialize list) so the order matches the panel's
        # `chapters_added` attribute.
        added_chapters = [ch for ch in snapshot.world_history if getattr(ch, "id", "") in added_set]
        with Span.open(
            SPAN_WORLD_HISTORY_ARC_PROMOTED,
            {
                "interaction": interaction,
                "from_maturity": from_maturity_str,
                "to_maturity": to_maturity.value,
                "chapters_added": added_ids,
            },
        ):
            pass

    return added_chapters


# ---------------------------------------------------------------------------
# Stateless materialize API — the Story 6-6 shape
# ---------------------------------------------------------------------------


def materialize_world(snapshot: Any, chapters: list[HistoryChapter]) -> None:
    """Apply history chapters to a GameSnapshot based on campaign maturity.

    In-place update: calculates maturity from the current snapshot,
    filters chapters at-or-below that tier, and sets
    ``snapshot.world_history`` + ``snapshot.campaign_maturity``.
    Idempotent — safe to call repeatedly.
    """
    from sidequest.telemetry.spans import SPAN_WORLD_MATERIALIZED, Span

    maturity = CampaignMaturity.from_snapshot(snapshot)
    applicable = [
        ch
        for ch in chapters
        if (m := CampaignMaturity.from_chapter_id(ch.id)) is not None and m <= maturity
    ]
    with Span.open(
        SPAN_WORLD_MATERIALIZED,
        {
            "genre_slug": getattr(snapshot, "genre_slug", "") or "",
            "world_slug": getattr(snapshot, "world_slug", "") or "",
            "maturity": maturity.value,
            "chapters_input": len(chapters),
            "chapters_applied": len(applicable),
        },
    ):
        snapshot.world_history = list(applicable)
        snapshot.campaign_maturity = maturity.value


# ---------------------------------------------------------------------------
# Dispatch-time entry point
# ---------------------------------------------------------------------------


def materialize_from_genre_pack(
    history_value: Any,
    maturity: CampaignMaturity,
    genre_slug: str,
    world_slug: str,
) -> Any:
    """Parse pack history + materialize a GameSnapshot at the target maturity.

    This is the function the dispatch layer calls during chargen
    confirmation. Produces a fully materialized snapshot with
    ``genre_slug`` / ``world_slug`` set on it.

    On parse failure, raises ``HistoryParseError`` — the dispatch
    wrapper decides whether to log-and-fall-back to an empty snapshot
    or propagate. We push that decision to the caller rather than
    hiding it in a silent fallback here.
    """
    chapters = parse_history_chapters(history_value)
    snap = WorldBuilder().at_maturity(maturity).with_chapters(chapters).build()
    snap.genre_slug = genre_slug
    snap.world_slug = world_slug
    return snap


# ---------------------------------------------------------------------------
# Canned-openings Phase 3 (Task 13) — AuthoredNpc pre-load
# ---------------------------------------------------------------------------


def prune_orphan_character_locations(snapshot: Any) -> int:
    """Drop ``character_locations`` keys with no matching current character.

    Story 153-19 (oddity 2): at chargen finalization the materialized
    ``"Adventurer"`` placeholder Character is discarded (``chargen_mixin``:
    ``materialized.characters = [character]``) but the ``character_locations``
    dict keeps the placeholder's key alongside the real PC's — a stale projection
    artifact (playtest: barsoom carried both ``Adventurer`` and ``Kantos``). Prune
    every key that does not match a current character's ``core.name``. Mutates in
    place; returns the count pruned.

    Always emits ``character_locations.orphan_pruned`` (even on a 0-prune) so the
    cleanup is GM-panel-visible and never a silent skip (CLAUDE.md "No Silent
    Fallbacks" + OTEL Observability Principle).
    """
    from sidequest.telemetry.spans import character_locations_pruned_span

    valid = {ch.core.name for ch in snapshot.characters}
    orphans = [name for name in snapshot.character_locations if name not in valid]
    for name in orphans:
        del snapshot.character_locations[name]
    character_locations_pruned_span(pruned_count=len(orphans), pruned_keys=orphans)
    return len(orphans)


def preload_authored_npcs(
    state: Any,
    authored: list[AuthoredNpc],
) -> None:
    """Pre-load AuthoredNpcs into ``state.npcs`` as runtime ``Npc`` instances.

    Fresh sessions only — discriminated by the **absence of a seated player
    character** (``state.characters == []``). The sole production caller is the
    chargen first-commit seam, which appends the player character *after* this
    preload runs, so an empty ``characters`` list is the reliable fresh signal.
    Resumed sessions (a PC already present) skip pre-loading; their ``npcs`` /
    ``npc_pool`` are already populated from prior turns and we do not
    retroactively rewrite them.

    Story 71-7: we deliberately do NOT gate on ``turn_manager.interaction``. A
    freshly materialized snapshot baselines at ``interaction == 1`` (the
    ``TurnManager`` default), never 0, so the former ``interaction == 0`` clause
    was unsatisfiable in production and silently skipped the authored crew on
    every real fresh session.

    Emits ``npc.authored_loaded`` per pre-loaded NPC for GM-panel visibility
    (CLAUDE.md "OTEL Observability Principle"). On the resumed-session skip,
    emits ``npc.authored_load_skipped`` with a ``reason`` so the skip is never a
    silent fallback (CLAUDE.md "No Silent Fallbacks"). Empty ``authored`` list is
    a hard no-op — there is genuinely nothing to load and nothing to observe.

    See ``docs/superpowers/specs/2026-05-01-canned-openings-design.md``
    §2.2.
    """
    if not authored:
        return

    from sidequest.telemetry.spans import (
        SPAN_NPC_AUTHORED_LOAD_SKIPPED,
        SPAN_NPC_AUTHORED_LOADED,
        Span,
    )

    # Resumed session — a player character is already seated. Skip the preload,
    # but emit a reason-carrying span so the GM panel can see the decision.
    if getattr(state, "characters", None):
        with Span.open(
            SPAN_NPC_AUTHORED_LOAD_SKIPPED,
            {
                "reason": "resumed_session_player_character_present",
                "authored_count": len(authored),
                "genre_slug": getattr(state, "genre_slug", "") or "",
                "world_slug": getattr(state, "world_slug", "") or "",
            },
        ):
            pass
        return

    # Story 150-3 (sq-playtest 2026-06-20, five_points): a canonical figure the
    # world's history.yaml chapters ALSO name is already seeded into state.npcs
    # by ``materialize_from_genre_pack``, which runs at the chargen seam BEFORE
    # this preload (Isaiah Rynders is in two five_points chapters' ``npcs:`` AND
    # in npcs.yaml). The chapter seeder dedups by name (``_apply_npc`` upserts an
    # existing entry), but this preload did not — so the authored roster
    # RE-APPENDED the same canonical name → a DOUBLE "Isaiah Rynders" in
    # /snapshot npcs (DATA-LOW: dedup / disposition confusion if the two
    # diverge). Track seen canonical names and skip any authored NPC already
    # present; the chapter seed is maturity-aware (it picked the Fresh-
    # appropriate disposition for this session), so we keep it rather than
    # clobber it with the static npcs.yaml baseline. Also dedups WITHIN the
    # authored list (id-uniqueness is validated at load, but two ids could share
    # a name). Mirrors ``_apply_npc``'s dedup-by-name invariant.
    seen_names = {n.core.name.casefold() for n in state.npcs}
    for authored_npc in authored:
        if authored_npc.name.casefold() in seen_names:
            # LOUD skip (CLAUDE.md "No Silent Fallbacks"): the GM panel sees the
            # overlap fire rather than a silently-dropped duplicate.
            with Span.open(
                SPAN_NPC_AUTHORED_LOAD_SKIPPED,
                {
                    "reason": "duplicate_name_already_seeded",
                    "authored_id": authored_npc.id,
                    "npc_name": authored_npc.name,
                    "genre_slug": getattr(state, "genre_slug", "") or "",
                    "world_slug": getattr(state, "world_slug", "") or "",
                },
            ):
                pass
            continue
        # CreatureCore requires non-blank description + personality
        # (validators), so synthesize sensible defaults from the authored
        # fields rather than passing empty strings.
        description = (
            authored_npc.appearance or authored_npc.role or f"Authored NPC {authored_npc.id}."
        )
        personality = authored_npc.role or "Authored."
        core = CreatureCore(
            name=authored_npc.name,
            description=description,
            personality=personality,
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
            acquired_advancements=[],
        )
        runtime = Npc(
            core=core,
            disposition=int(authored_npc.initial_disposition),
            location=None,
            pronouns=authored_npc.pronouns or None,
            appearance=authored_npc.appearance or None,
            age=authored_npc.age or None,
            build=None,
            height=None,
            distinguishing_features=list(authored_npc.distinguishing_features),
            ocean=(
                OceanProfile.from_authored(authored_npc.ocean).model_dump()
                if authored_npc.ocean
                else None
            ),
            resolution_tier="spawn",
            non_transactional_interactions=0,
            jungian_id=None,
            rpg_role_id=None,
            npc_role_id=None,
            resolved_archetype=None,
            # Typed provenance (story 162-2): AUTHORED is not derivable from
            # legacy fields — the session-start cast stamps it, carrying the
            # AuthoredNpc.id the id-keyed identity surface needs.
            origin=Origin(kind=OriginKind.AUTHORED, authored_id=authored_npc.id),
        )
        # Green Room Task 3 (ADR-156): the session-start authored cast —
        # AUTHORED tier, carrying the real AuthoredNpc.id.
        admit(
            state,
            [
                MaterializationCandidate(
                    npc=runtime,
                    origin=runtime.origin
                    or Origin(kind=OriginKind.AUTHORED, authored_id=authored_npc.id),
                    source="preload_authored",
                )
            ],
        )
        seen_names.add(authored_npc.name.casefold())
        with Span.open(
            SPAN_NPC_AUTHORED_LOADED,
            {
                "authored_id": authored_npc.id,
                "npc_name": authored_npc.name,
                "role": authored_npc.role or "",
                "initial_disposition": int(authored_npc.initial_disposition),
                "genre_slug": getattr(state, "genre_slug", "") or "",
                "world_slug": getattr(state, "world_slug", "") or "",
            },
        ):
            pass
