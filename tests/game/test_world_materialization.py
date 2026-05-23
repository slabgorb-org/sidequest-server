"""Tests for ``sidequest.game.world_materialization``.

Covers:
- ``CampaignMaturity.from_snapshot`` — turn + beats → tier bucketing.
- ``CampaignMaturity.from_chapter_id`` — id string → tier mapping.
- ``parse_history_chapters`` — raw pack value → typed chapter list,
  including empty/null cases and malformed-data errors.
- ``WorldBuilder.build`` — cumulative chapter apply, maturity filter,
  ``world_history`` populated with applicable chapters, scene context
  (location/time_of_day/atmosphere) set from the latest declaring
  chapter, lore dedup, quest log upserts, NPC upsert-by-name, trope
  upsert-by-id.
- ``materialize_world`` — in-place update of an existing snapshot.
- ``materialize_from_genre_pack`` — dispatch-time entry with slugs set.
"""

from __future__ import annotations

import pytest

from sidequest.game.history_chapter import (
    ChapterCharacter,
    ChapterNarrativeEntry,
    ChapterNpc,
    ChapterTrope,
    HistoryChapter,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.game.world_materialization import (
    CampaignMaturity,
    HistoryParseError,
    WorldBuilder,
    materialize_from_genre_pack,
    materialize_world,
    parse_history_chapters,
)

# ---------------------------------------------------------------------------
# CampaignMaturity
# ---------------------------------------------------------------------------


class TestCampaignMaturity:
    def test_ordering_fresh_lt_early_lt_mid_lt_veteran(self) -> None:
        assert CampaignMaturity.Fresh <= CampaignMaturity.Early
        assert CampaignMaturity.Early <= CampaignMaturity.Mid
        assert CampaignMaturity.Mid <= CampaignMaturity.Veteran
        assert CampaignMaturity.Fresh < CampaignMaturity.Veteran
        assert not (CampaignMaturity.Veteran <= CampaignMaturity.Fresh)

    def test_from_chapter_id_known_values(self) -> None:
        assert CampaignMaturity.from_chapter_id("fresh") == CampaignMaturity.Fresh
        assert CampaignMaturity.from_chapter_id("early") == CampaignMaturity.Early
        assert CampaignMaturity.from_chapter_id("mid") == CampaignMaturity.Mid
        assert CampaignMaturity.from_chapter_id("veteran") == CampaignMaturity.Veteran

    def test_from_chapter_id_unknown_returns_none(self) -> None:
        assert CampaignMaturity.from_chapter_id("legendary") is None
        assert CampaignMaturity.from_chapter_id("") is None
        assert CampaignMaturity.from_chapter_id("FRESH") is None  # case-sensitive

    def test_from_snapshot_tier_boundaries(self) -> None:
        def snap(turn: int, beats: int) -> GameSnapshot:
            tm = TurnManager()
            tm.round = turn
            s = GameSnapshot()
            s.turn_manager = tm
            s.total_beats_fired = beats
            return s

        # effective = round + beats // 2
        assert CampaignMaturity.from_snapshot(snap(0, 0)) == CampaignMaturity.Fresh
        assert CampaignMaturity.from_snapshot(snap(5, 0)) == CampaignMaturity.Fresh
        assert CampaignMaturity.from_snapshot(snap(6, 0)) == CampaignMaturity.Early
        assert CampaignMaturity.from_snapshot(snap(20, 0)) == CampaignMaturity.Early
        assert CampaignMaturity.from_snapshot(snap(21, 0)) == CampaignMaturity.Mid
        assert CampaignMaturity.from_snapshot(snap(50, 0)) == CampaignMaturity.Mid
        assert CampaignMaturity.from_snapshot(snap(51, 0)) == CampaignMaturity.Veteran

    def test_from_snapshot_beats_accelerate_maturity(self) -> None:
        def snap(turn: int, beats: int) -> GameSnapshot:
            tm = TurnManager()
            tm.round = turn
            s = GameSnapshot()
            s.turn_manager = tm
            s.total_beats_fired = beats
            return s

        # 4 turns + 4 beats → effective = 4 + 2 = 6 → Early
        assert CampaignMaturity.from_snapshot(snap(4, 4)) == CampaignMaturity.Early
        # 4 turns + 3 beats → effective = 4 + 1 = 5 → Fresh (integer div)
        assert CampaignMaturity.from_snapshot(snap(4, 3)) == CampaignMaturity.Fresh


# ---------------------------------------------------------------------------
# parse_history_chapters
# ---------------------------------------------------------------------------


class TestParseHistoryChapters:
    def test_none_returns_empty(self) -> None:
        assert parse_history_chapters(None) == []

    def test_missing_chapters_key_returns_empty(self) -> None:
        assert parse_history_chapters({"other": "data"}) == []

    def test_empty_chapters_list_returns_empty(self) -> None:
        assert parse_history_chapters({"chapters": []}) == []

    def test_valid_chapters_parsed(self) -> None:
        chapters = parse_history_chapters(
            {
                "chapters": [
                    {"id": "fresh", "label": "Beginnings", "lore": ["a", "b"]},
                    {"id": "early", "label": "Trouble"},
                ]
            }
        )
        assert len(chapters) == 2
        assert chapters[0].id == "fresh"
        assert chapters[0].label == "Beginnings"
        assert chapters[0].lore == ["a", "b"]
        assert chapters[1].id == "early"

    def test_non_mapping_payload_raises(self) -> None:
        with pytest.raises(HistoryParseError):
            parse_history_chapters(["not", "a", "mapping"])

    def test_non_list_chapters_raises(self) -> None:
        with pytest.raises(HistoryParseError):
            parse_history_chapters({"chapters": "not a list"})

    def test_malformed_chapter_entry_raises_with_index(self) -> None:
        with pytest.raises(HistoryParseError, match="index 1"):
            parse_history_chapters(
                {
                    "chapters": [
                        {"id": "fresh"},
                        # trope status is required; a trope entry missing 'id'
                        # triggers a validation error when pydantic builds.
                        {"id": "early", "tropes": [{"status": "active"}]},
                    ]
                }
            )


# ---------------------------------------------------------------------------
# WorldBuilder.build — cumulative chapter application
# ---------------------------------------------------------------------------


def _fresh_chapter(**overrides: object) -> HistoryChapter:
    kwargs: dict[str, object] = {
        "id": "fresh",
        "label": "Fresh Chapter",
    }
    kwargs.update(overrides)
    return HistoryChapter(**kwargs)


def _early_chapter(**overrides: object) -> HistoryChapter:
    kwargs: dict[str, object] = {"id": "early", "label": "Early Chapter"}
    kwargs.update(overrides)
    return HistoryChapter(**kwargs)


class TestWorldBuilderBuild:
    def test_fresh_maturity_includes_only_fresh_chapters(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Fresh)
            .with_chapters(
                [
                    _fresh_chapter(lore=["f1"]),
                    _early_chapter(lore=["e1"]),
                    HistoryChapter(id="mid", lore=["m1"]),
                ]
            )
            .build()
        )
        assert [ch.id for ch in snap.world_history] == ["fresh"]
        assert snap.lore_established == ["f1"]

    def test_veteran_maturity_includes_all_tiers(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Veteran)
            .with_chapters(
                [
                    _fresh_chapter(lore=["f"]),
                    _early_chapter(lore=["e"]),
                    HistoryChapter(id="mid", lore=["m"]),
                    HistoryChapter(id="veteran", lore=["v"]),
                ]
            )
            .build()
        )
        assert [ch.id for ch in snap.world_history] == [
            "fresh",
            "early",
            "mid",
            "veteran",
        ]
        assert snap.lore_established == ["f", "e", "m", "v"]

    def test_unknown_chapter_id_is_skipped(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Veteran)
            .with_chapters(
                [
                    _fresh_chapter(),
                    HistoryChapter(id="legendary", lore=["never applied"]),
                ]
            )
            .build()
        )
        assert [ch.id for ch in snap.world_history] == ["fresh"]
        assert "never applied" not in snap.lore_established

    def test_scene_context_later_chapter_wins(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(
                        location="The Threshold",
                        time_of_day="dawn",
                        atmosphere="clinical unease",
                        active_stakes="establish foothold",
                    ),
                    _early_chapter(
                        location="The Spillway",
                        atmosphere="louder, more chaotic",
                    ),
                ]
            )
            .build()
        )
        # Early overrides atmosphere; time_of_day keeps Fresh's value because
        # Early didn't declare it. Wave 2B (story 45-48): per-PC location
        # replaces party-level field; with no characters in the snapshot,
        # the chapter's location lives on world_history (chapter 1).
        latest_loc_chapter = next(
            (c for c in reversed(snap.world_history) if c.location),
            None,
        )
        assert latest_loc_chapter is not None
        assert latest_loc_chapter.location == "The Spillway"
        assert snap.atmosphere == "louder, more chaotic"
        assert snap.time_of_day == "dawn"
        assert snap.active_stakes == "establish foothold"

    def test_lore_is_deduplicated(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(lore=["shared fact", "fresh-only"]),
                    _early_chapter(lore=["shared fact", "early-only"]),
                ]
            )
            .build()
        )
        assert snap.lore_established == ["shared fact", "fresh-only", "early-only"]

    def test_quest_log_upserted(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(quests={"q1": "assigned", "q2": "in_progress"}),
                    _early_chapter(quests={"q1": "complete", "q3": "assigned"}),
                ]
            )
            .build()
        )
        assert snap.quest_log == {
            "q1": "complete",  # updated by Early
            "q2": "in_progress",
            "q3": "assigned",
        }

    def test_notes_append(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(notes=["a", "b"]),
                    _early_chapter(notes=["c"]),
                ]
            )
            .build()
        )
        assert snap.notes == ["a", "b", "c"]

    def test_narrative_log_converted_to_entries(self) -> None:
        snap = (
            WorldBuilder()
            .with_chapters(
                [
                    _fresh_chapter(
                        narrative_log=[
                            ChapterNarrativeEntry(speaker="narrator", text="The threshold."),
                            ChapterNarrativeEntry(speaker="Rux", text="I enter."),
                        ]
                    ),
                ]
            )
            .build()
        )
        assert len(snap.narrative_log) == 2
        assert snap.narrative_log[0].author == "narrator"
        assert snap.narrative_log[0].content == "The threshold."
        assert snap.narrative_log[0].speaker == "narrator"
        assert snap.narrative_log[1].author == "Rux"

    def test_npc_upsert_by_name(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(
                        npcs=[
                            ChapterNpc(name="Drakul", description="A sage."),
                        ]
                    ),
                    _early_chapter(
                        npcs=[
                            ChapterNpc(name="Drakul", disposition=-5, location="Crypt"),
                            ChapterNpc(name="Mira", description="A scout."),
                        ]
                    ),
                ]
            )
            .build()
        )
        names = [n.core.name for n in snap.npcs]
        assert names == ["Drakul", "Mira"]
        drakul = snap.npcs[0]
        assert drakul.disposition == -5
        assert drakul.location == "Crypt"
        # description from Fresh persists (Early didn't declare one)
        assert drakul.core.description == "A sage."

    def test_npc_chapter_apply_also_seeds_npc_pool(self) -> None:
        """Story 61-8 §B — npc_pool exhaustiveness.

        ``_apply_phase_c_projections`` drops off-stage NPCs from the
        prompt's in-scene list (Story 61-2). Identity preservation
        depends on ``snap.npc_pool`` carrying every NPC the projection
        might drop. Pre-§B, ``_apply_npc`` populated ``snap.npcs`` only
        and the auto-mint-from-prose path was the sole writer to
        ``npc_pool`` — meaning world-authored NPCs the projection
        dropped were invisible to the narrator until they happened to be
        prose-cited.
        """
        snap = (
            WorldBuilder()
            .with_chapters(
                [
                    _fresh_chapter(
                        npcs=[
                            ChapterNpc(name="Drakul", description="A sage."),
                            ChapterNpc(name="Mira", description="A scout."),
                        ]
                    ),
                ]
            )
            .build()
        )
        pool_names = {m.name for m in snap.npc_pool}
        assert "Drakul" in pool_names, (
            "World-authored NPC Drakul missing from npc_pool — the "
            "61-2 projection's off-scene drop branch would lose this "
            "NPC's identity entirely."
        )
        assert "Mira" in pool_names
        drakul_member = next(m for m in snap.npc_pool if m.name == "Drakul")
        assert drakul_member.drawn_from == "world_authored", (
            "World-authored pool member must carry drawn_from="
            "'world_authored' so the auto-mint pipeline can distinguish "
            "canonical chapter NPCs from prose-extracted ones."
        )
        assert drakul_member.observation_pending is False, (
            "World-authored NPCs enter the pool already ratified — "
            "they are canonical at chapter-apply time, not prose-"
            "auto-minted."
        )

    def test_npc_chapter_apply_seeds_pool_on_existing_npc_branch(self) -> None:
        """Story 61-8 §B (review-fix round 2) — pool exhaustiveness must
        run when the NPC already exists in ``snap.npcs`` but NOT in
        ``snap.npc_pool``. This is the legacy-save scenario: an NPC
        was minted by ``_apply_npc`` before §B existed, so
        ``snap.npcs`` carries the NPC but ``snap.npc_pool`` does not.
        Re-running ``_apply_npc`` (e.g. on chapter re-apply or
        re-materialization) must reach the pool-seed code on the
        existing-NPC branch.

        Round 1 of §B only seeded the pool inside the new-NPC branch,
        which returned early on existing NPCs — leaving the legacy
        case unfixed. The Reviewer's edge-hunter caught this; round 2
        moves pool-seeding to fire on both branches via
        ``_ensure_world_authored_pool_member``.
        """
        from sidequest.game.creature_core import (
            CreatureCore,
            Inventory,
            placeholder_edge_pool,
        )
        from sidequest.game.session import GameSnapshot, Npc
        from sidequest.game.turn import TurnManager
        from sidequest.game.world_materialization import WorldBuilder

        # Construct a snapshot the legacy-save way: Npc directly on
        # snap.npcs, snap.npc_pool empty. WorldBuilder's _apply_npc
        # must seed the pool on the existing-NPC branch.
        snap = GameSnapshot(turn_manager=TurnManager(interaction=0))
        snap.npcs.append(
            Npc(
                core=CreatureCore(
                    name="LegacyDrakul",
                    description="From a save predating §B.",
                    personality="Neutral.",
                    edge=placeholder_edge_pool(),
                    inventory=Inventory(),
                ),
                disposition=0,
            )
        )
        # Sanity: pre-condition is the legacy-save shape.
        assert not any(m.name == "LegacyDrakul" for m in snap.npc_pool)

        builder = WorldBuilder()
        builder._apply_npc(snap, ChapterNpc(name="LegacyDrakul", disposition=3))

        assert any(m.name == "LegacyDrakul" for m in snap.npc_pool), (
            "Existing-NPC branch of _apply_npc did NOT seed npc_pool — "
            "the 61-2 projection's identity-fallback would lose this "
            "NPC on the legacy-save path. Round-2 §B fix must call "
            "_ensure_world_authored_pool_member on both branches."
        )
        # And the in-place disposition update from ChapterNpc still
        # applies (regression guard against the refactor breaking the
        # pre-existing update semantics).
        legacy = next(n for n in snap.npcs if n.core.name == "LegacyDrakul")
        assert legacy.disposition == 3

    def test_npc_chapter_apply_promotes_prose_pending_pool_entry(self) -> None:
        """Story 61-8 §B (review-fix round 2) — the docstring claims
        ``world-authored NPCs override that with observation_pending=False
        since they are canonical at chapter-apply time``. The original
        implementation skipped instead of overriding; the round-2 fix
        promotes a prior prose-extracted pending entry to canonical
        world-authored when a chapter apply hits the same name.
        """
        from sidequest.game.npc_pool import NpcPoolMember
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.game.world_materialization import WorldBuilder

        snap = GameSnapshot(turn_manager=TurnManager(interaction=0))
        # Pre-existing prose-extracted pending entry.
        snap.npc_pool.append(
            NpcPoolMember(
                name="Mrs. Gow",
                role="housekeeper",
                pronouns="she/her",
                drawn_from="dialogue_extraction",
                observation_pending=True,
            )
        )

        builder = WorldBuilder()
        builder._apply_npc(snap, ChapterNpc(name="Mrs. Gow"))

        member = next(m for m in snap.npc_pool if m.name == "Mrs. Gow")
        assert member.drawn_from == "world_authored", (
            "Pre-existing prose-pending entry was not promoted to "
            "drawn_from='world_authored' on chapter apply — the §B "
            "docstring's override-contract is not honored."
        )
        assert member.observation_pending is False, (
            "Pre-existing pending entry was not ratified on chapter "
            "apply — observation_pending stays True, suppressing any "
            "consumer gated on canonical NPCs."
        )
        # Non-prose fields are NOT clobbered (the chapter ChapterNpc
        # carried no role / pronouns; the pre-existing entry's values
        # should survive).
        assert member.role == "housekeeper"
        assert member.pronouns == "she/her"

    def test_npc_chapter_apply_does_not_clobber_canonical_pool_entry(self) -> None:
        """Story 61-8 §B (review-fix round 2) — only PROSE-PENDING
        entries are promoted. A pre-existing non-prose source
        (e.g. ``name_generator``, ``legacy_registry``) with
        ``observation_pending=False`` must be left untouched. Chapter
        apply is canonical over prose-extracted entries only; it does
        not override entries minted by other code paths.
        """
        from sidequest.game.npc_pool import NpcPoolMember
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager
        from sidequest.game.world_materialization import WorldBuilder

        snap = GameSnapshot(turn_manager=TurnManager(interaction=0))
        snap.npc_pool.append(
            NpcPoolMember(
                name="Generated McName",
                drawn_from="name_generator",
                observation_pending=False,
            )
        )

        builder = WorldBuilder()
        builder._apply_npc(snap, ChapterNpc(name="Generated McName"))

        member = next(m for m in snap.npc_pool if m.name == "Generated McName")
        assert member.drawn_from == "name_generator", (
            "Pre-existing name_generator pool entry was clobbered to "
            "'world_authored' — promotion logic must only override "
            "prose-extracted / pending entries, not all sources."
        )

    def test_npc_chapter_apply_pool_seed_is_idempotent(self) -> None:
        """Story 61-8 §B — idempotency on repeated chapter apply.

        Re-applying the same chapter (e.g. cross-chapter NPC re-citation)
        must not duplicate the pool entry; identity stays one-to-one
        with ``snap.npcs``.
        """
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(npcs=[ChapterNpc(name="Drakul")]),
                    _early_chapter(npcs=[ChapterNpc(name="Drakul", disposition=-5)]),
                ]
            )
            .build()
        )
        drakul_members = [m for m in snap.npc_pool if m.name == "Drakul"]
        assert len(drakul_members) == 1, (
            f"Duplicate pool entries for Drakul: {len(drakul_members)} — "
            "the idempotency guard in _apply_npc must skip when a member "
            "with the same name already exists."
        )

    def test_blank_npc_name_skipped(self) -> None:
        snap = (
            WorldBuilder()
            .with_chapters(
                [
                    _fresh_chapter(
                        npcs=[
                            ChapterNpc(name="", description="ignored"),
                            ChapterNpc(name="Real", description="kept"),
                        ]
                    ),
                ]
            )
            .build()
        )
        assert [n.core.name for n in snap.npcs] == ["Real"]

    def test_trope_upsert_by_id(self) -> None:
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(
                        tropes=[
                            ChapterTrope(id="t1", status="dormant", progression=0.1),
                        ]
                    ),
                    _early_chapter(
                        tropes=[
                            ChapterTrope(id="t1", status="active", progression=0.6),
                            ChapterTrope(id="t2", status="progressing"),
                        ]
                    ),
                ]
            )
            .build()
        )
        ids = [t.id for t in snap.active_tropes]
        assert ids == ["t1", "t2"]
        assert snap.active_tropes[0].status == "active"
        assert snap.active_tropes[0].progress == pytest.approx(0.6)

    def test_unknown_trope_status_defaults_to_active(self) -> None:
        snap = (
            WorldBuilder()
            .with_chapters(
                [
                    _fresh_chapter(
                        tropes=[
                            ChapterTrope(id="t1", status="unknown_word"),
                        ]
                    ),
                ]
            )
            .build()
        )
        assert snap.active_tropes[0].status == "active"

    def test_character_created_when_snapshot_empty(self) -> None:
        snap = (
            WorldBuilder()
            .with_chapters(
                [
                    _fresh_chapter(
                        character=ChapterCharacter(
                            name="Rux",
                            race="Gnome",
                            **{"class": "Delver"},  # YAML alias
                            level=3,
                            backstory="Orphan of the Reach.",
                        )
                    ),
                ]
            )
            .build()
        )
        assert len(snap.characters) == 1
        char = snap.characters[0]
        assert char.core.name == "Rux"
        assert char.race == "Gnome"
        assert char.char_class == "Delver"
        assert char.core.level == 3
        assert char.backstory == "Orphan of the Reach."
        # Story 45-7: description tracks selected race/class even when
        # chapter omits an explicit description string.
        assert char.core.description == "A Gnome Delver"

    def test_character_created_with_defaults_when_blank(self) -> None:
        snap = WorldBuilder().with_chapters([_fresh_chapter(character=ChapterCharacter())]).build()
        assert len(snap.characters) == 1
        char = snap.characters[0]
        # Rust-parity fallbacks
        assert char.core.name == "Adventurer"
        assert char.race == "Human"
        assert char.char_class == "Fighter"
        assert char.backstory == "Unknown origins."
        # Story 45-7: blank chapter still produces an auto-template
        # description from the resolved race+class defaults.
        assert char.core.description == "A Human Fighter"

    def test_character_description_refreshed_when_chapter_changes_race(self) -> None:
        """Story 45-7 regression test.

        Playtest 3 evropi shipped saves with race=Half-Orc but
        description="A Human Fighter" because chargen ran with the
        default race ("Human") and a later chapter set race=Half-Orc
        without supplying a fresh description. ``_apply_character``
        must refresh the auto-template description when race or class
        changes and the prior description was the auto-generated
        ``f"A {race} {class}"`` form.
        """
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    # First chapter — chargen-style default (race unset →
                    # falls back to "Human"). Description is the auto-template.
                    _fresh_chapter(
                        character=ChapterCharacter(
                            name="Prot'Thokk",
                            **{"class": "Fighter"},
                        )
                    ),
                    # Second chapter — sets the actual race, no description.
                    _early_chapter(
                        character=ChapterCharacter(
                            race="Half-Orc",
                        )
                    ),
                ]
            )
            .build()
        )
        char = snap.characters[0]
        assert char.race == "Half-Orc"
        assert char.char_class == "Fighter"
        # The auto-template description must follow the new race.
        assert char.core.description == "A Half-Orc Fighter"

    def test_character_description_preserved_when_hand_authored(self) -> None:
        """Story 45-7: only auto-template descriptions are refreshed.

        A hand-authored description (anything that isn't the exact
        ``f"A {race} {class}"`` template) must survive a race-change
        chapter update unchanged.
        """
        hand_authored = "A scarred half-orc, slow to anger and slower to laugh."
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(
                        character=ChapterCharacter(
                            name="Prot'Thokk",
                            race="Human",
                            **{"class": "Fighter"},
                            description=hand_authored,
                        )
                    ),
                    _early_chapter(character=ChapterCharacter(race="Half-Orc")),
                ]
            )
            .build()
        )
        char = snap.characters[0]
        assert char.race == "Half-Orc"
        # Hand-authored description stays put.
        assert char.core.description == hand_authored

    def test_existing_character_name_preserved_when_chapter_name_blank(self) -> None:
        """A second chapter with ``name=""`` must not overwrite an existing
        chargen-built name. The empty-name short-circuit in
        ``_apply_character`` (world_materialization.py:348) protects the
        chargen-owned identity slot.
        """
        snap = (
            WorldBuilder()
            .at_maturity(CampaignMaturity.Early)
            .with_chapters(
                [
                    _fresh_chapter(
                        character=ChapterCharacter(
                            name="Rux",
                            race="Gnome",
                            **{"class": "Delver"},  # YAML alias
                            level=3,
                            backstory="Orphan of the Reach.",
                        )
                    ),
                    _early_chapter(
                        character=ChapterCharacter(
                            name="",
                            level=5,
                            backstory="Now bears the Lantern.",
                        )
                    ),
                ]
            )
            .build()
        )
        assert len(snap.characters) == 1
        char = snap.characters[0]
        # Empty chapter.name short-circuits — existing name preserved.
        assert char.core.name == "Rux"
        # Other non-empty fields from the second chapter still apply.
        assert char.core.level == 5
        assert char.backstory == "Now bears the Lantern."
        # Untouched fields stay from the first chapter.
        assert char.race == "Gnome"
        assert char.char_class == "Delver"


# ---------------------------------------------------------------------------
# materialize_world — in-place, Story 6-6 shape
# ---------------------------------------------------------------------------


class TestMaterializeWorld:
    def test_in_place_update_filters_by_current_maturity(self) -> None:
        snap = GameSnapshot()
        tm = TurnManager()
        tm.round = 10  # → Early tier
        snap.turn_manager = tm
        chapters = [
            HistoryChapter(id="fresh", label="A"),
            HistoryChapter(id="early", label="B"),
            HistoryChapter(id="mid", label="C"),
        ]
        materialize_world(snap, chapters)
        assert [ch.id for ch in snap.world_history] == ["fresh", "early"]
        assert snap.campaign_maturity == CampaignMaturity.Early.value

    def test_idempotent(self) -> None:
        snap = GameSnapshot()
        chapters = [HistoryChapter(id="fresh", label="A")]
        materialize_world(snap, chapters)
        materialize_world(snap, chapters)
        assert len(snap.world_history) == 1


# ---------------------------------------------------------------------------
# materialize_from_genre_pack — dispatch entry point
# ---------------------------------------------------------------------------


class TestMaterializeFromGenrePack:
    def test_sets_slugs(self) -> None:
        snap = materialize_from_genre_pack(
            None, CampaignMaturity.Fresh, "caverns_and_claudes", "grimvault"
        )
        assert snap.genre_slug == "caverns_and_claudes"
        assert snap.world_slug == "grimvault"
        assert snap.world_history == []  # no history → no chapters

    def test_parses_and_applies_chapters(self) -> None:
        raw = {
            "chapters": [
                {
                    "id": "fresh",
                    "label": "First Look",
                    "lore": ["The vault is clean."],
                    "location": "The Threshold",
                    "atmosphere": "clinical unease",
                },
            ]
        }
        snap = materialize_from_genre_pack(
            raw, CampaignMaturity.Fresh, "caverns_and_claudes", "grimvault"
        )
        assert snap.genre_slug == "caverns_and_claudes"
        assert snap.world_slug == "grimvault"
        assert len(snap.world_history) == 1
        assert snap.lore_established == ["The vault is clean."]
        # Wave 2B: chapter's location persists on world_history; per-PC
        # snapshot.character_locations is populated downstream by dispatch
        # once a character is attached.
        assert snap.world_history[0].location == "The Threshold"
        assert snap.atmosphere == "clinical unease"

    def test_parse_failure_propagates(self) -> None:
        # Caller (dispatch) is responsible for catch-and-fallback —
        # the function itself raises rather than silently swallowing.
        with pytest.raises(HistoryParseError):
            materialize_from_genre_pack("not a mapping", CampaignMaturity.Fresh, "g", "w")
