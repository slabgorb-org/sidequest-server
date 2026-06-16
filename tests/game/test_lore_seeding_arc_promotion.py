"""Story 45-23 — unit tests for ``seed_lore_from_arc_promotion``.

The runtime arc-promotion seeder. Closes Felix's Playtest 3 gap: 71
turns of dense play, ``narrative_log`` and ``lore_store`` empty of arc-
sourced content because the chapter-promotion path never wrote back.

The helper consumes the ``chapters_added`` diff produced by 45-19's
``recompute_arc_history`` and turns each chapter's narrative_log + lore
strings into:

- ``NarrativeEntry`` rows on ``snapshot.narrative_log`` AND a
  ``store.append_narrative()`` call (durable + in-snapshot history).
- ``LoreFragment`` rows on the in-memory ``LoreStore``, ``embedding_
  pending=True`` so the existing per-turn embed worker picks them up.

Sibling to ``seed_lore_from_char_creation`` — same ``_try_add``
duplicate-skip semantics, same idempotent contract on re-seed. Distinct
seam: chargen runs once at confirmation, this helper runs every time
45-19's recompute reports new chapters.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.history_chapter import (
    ChapterNarrativeEntry,
    HistoryChapter,
)
from sidequest.game.lore_seeding import seed_lore_from_arc_promotion
from sidequest.game.lore_store import (
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager


def _snapshot(round_value: int = 7) -> GameSnapshot:
    """Build a fresh snapshot with the turn manager pre-bumped to a
    known round so the helper-stamped ``round`` field is testable."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken_keep",
        turn_manager=TurnManager(interaction=10, round=round_value),
    )


def _chapter_with_narrative(
    chapter_id: str = "early",
    *,
    speaker_text_pairs: list[tuple[str, str]] | None = None,
    lore: list[str] | None = None,
) -> HistoryChapter:
    """Construct a chapter with narrative_log + lore. Either field
    may be empty so callers can probe partial-content shapes.
    """
    pairs = speaker_text_pairs or []
    return HistoryChapter(
        id=chapter_id,
        label=f"{chapter_id.title()} arc",
        narrative_log=[ChapterNarrativeEntry(speaker=spk, text=txt) for spk, txt in pairs],
        lore=list(lore or []),
    )


# ---------------------------------------------------------------------------
# narrative_log writeback — per ChapterNarrativeEntry one NarrativeEntry,
# both on snapshot.narrative_log AND via store.append_narrative.
# ---------------------------------------------------------------------------


class TestNarrativeLogWriteback:
    """The first gap Felix saw — 71 turns, narrative_log carried only
    per-turn appends because the chapter-promotion path never seeded it.
    """

    def test_one_narrative_entry_per_chapter_narrative_log_row(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "early",
            speaker_text_pairs=[
                ("narrator", "The keep stirs after a year empty."),
                ("Rux", "I do not like the silence."),
            ],
        )

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        # Both entries land on the in-snapshot list (the narrator's
        # state_summary reads this; per context-story-45-23.md AC1).
        snapshot_authors = [e.author for e in snap.narrative_log if e.entry_type == "arc_promotion"]
        assert snapshot_authors == ["narrator", "Rux"]
        assert result.narrative_entries_appended == 2

    def test_writeback_calls_store_append_narrative_per_entry(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "early",
            speaker_text_pairs=[
                ("narrator", "Tier crossed; the past speaks again."),
                ("Mira", "Then we listen."),
            ],
        )

        seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        # Felix's bug was the absence of the durable write — assert
        # the persistence call happened, not just the in-snapshot append.
        assert store.append_narrative.call_count == 2

    def test_entry_carries_arc_promotion_entry_type_and_round(self) -> None:
        snap = _snapshot(round_value=12)
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "early",
            speaker_text_pairs=[("narrator", "The arc seam holds.")],
        )

        seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        promo_entries = [e for e in snap.narrative_log if e.entry_type == "arc_promotion"]
        assert len(promo_entries) == 1
        entry = promo_entries[0]
        # Per context-story-45-23.md "Seeding helper" §1.
        assert entry.entry_type == "arc_promotion"
        assert entry.author == "narrator"
        assert entry.content == "The arc seam holds."
        # Round comes from snapshot.turn_manager.round so the durable
        # log timestamps match the post-bump dispatch state.
        assert entry.round == 12

    def test_chapter_with_no_narrative_log_appends_nothing(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative("early", speaker_text_pairs=[])

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        assert result.narrative_entries_appended == 0
        assert store.append_narrative.call_count == 0
        assert all(e.entry_type != "arc_promotion" for e in snap.narrative_log)


# ---------------------------------------------------------------------------
# lore_store writeback — per chapter.lore string one LoreFragment with
# the canonical id format and the GameEvent source tag.
# ---------------------------------------------------------------------------


class TestLoreStoreWriteback:
    """The second gap Felix saw — lore_store carried only chargen-time
    fragments because the chapter-promotion path never seeded the store.
    """

    def test_one_lore_fragment_per_chapter_lore_string(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "mid",
            lore=[
                "The crown was lost during the third winter.",
                "Wolves carry the scent of old gold.",
            ],
        )

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        assert len(lore_store) == 2
        assert result.lore_fragments_minted == 2

    def test_fragment_id_follows_lore_arc_chapter_index_format(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "mid",
            lore=["entry zero", "entry one", "entry two"],
        )

        seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        # Per context-story-45-23.md "Seeding helper" §2.
        for index in range(3):
            assert f"lore_arc_mid_{index}" in lore_store.fragments

    def test_fragment_carries_history_category_and_game_event_source(
        self,
    ) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative("early", lore=["a load-bearing fact"])

        seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        frag = lore_store.fragments["lore_arc_early_0"]
        assert frag.category == LoreCategory.History
        # GameEvent — runtime-minted, not chargen — so the GM panel can
        # filter chapter-promoted fragments from chargen-seeded ones.
        assert frag.source == LoreSource.GameEvent
        assert frag.content == "a load-bearing fact"

    def test_seeded_fragment_starts_with_embedding_pending_true(self) -> None:
        """The whole point of seeding into lore_store is that the
        existing per-turn embed worker picks the fragment up. If the
        fragment is added with ``embedding_pending=False`` the worker
        skips it permanently — exact failure mode of Felix's bug.
        """
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative("early", lore=["one fact"])

        seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        frag = lore_store.fragments["lore_arc_early_0"]
        assert frag.embedding_pending is True
        # And ``pending_embedding_ids`` is the worker's input — assert
        # the seeded fragment shows up there so the worker will see it.
        assert "lore_arc_early_0" in lore_store.pending_embedding_ids()

    def test_chapter_with_no_lore_strings_mints_no_fragments(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative("early", lore=[])

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        assert result.lore_fragments_minted == 0
        assert lore_store.is_empty()

    def test_blank_lore_string_is_skipped_not_raised(self) -> None:
        """``LoreFragment.content`` rejects blank/whitespace strings
        (lore_store.py:90). A chapter with an empty-string lore entry
        is a content-authoring bug; the helper must skip the bad entry
        rather than crash the whole arc-promotion path. Felix's bug was
        a silent absence — turning that into a dispatch-loop crash on
        a malformed pack would be a strictly worse failure mode.
        """
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative("early", lore=["valid entry", "   ", "another valid"])

        # Must not raise.
        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        # The two valid entries land; the blank one is dropped.
        assert result.lore_fragments_minted == 2
        assert "lore_arc_early_0" in lore_store.fragments
        assert "lore_arc_early_2" in lore_store.fragments
        assert "lore_arc_early_1" not in lore_store.fragments


# ---------------------------------------------------------------------------
# Idempotency — the seeding helper consumes ONLY ``chapters_added`` from
# 45-19's recompute, but a re-tick that re-passes the same chapter must
# not double-seed. ``_try_add`` swallows DuplicateLoreId; the result
# struct surfaces the count so OTEL can distinguish "real re-seed" from
# "duplicate skip" in the GM panel.
# ---------------------------------------------------------------------------


class TestIdempotentReseed:
    def test_second_seed_with_same_chapters_skips_duplicates(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapter = _chapter_with_narrative(
            "early",
            speaker_text_pairs=[("narrator", "echo")],
            lore=["fact one", "fact two"],
        )

        first = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])
        second = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        assert first.lore_fragments_minted == 2
        assert first.lore_fragments_skipped_duplicate == 0
        # Idempotent on re-seed — neither raises nor double-mints.
        assert second.lore_fragments_minted == 0
        assert second.lore_fragments_skipped_duplicate == 2
        assert len(lore_store) == 2

    def test_partial_overlap_skips_only_the_duplicates(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        # Pre-seed one fragment so the helper sees a partial duplicate
        # in the next call. Mirrors the realistic "one chapter already
        # seeded by a prior tick, plus a freshly-promoted chapter."
        lore_store.add(
            LoreFragment.new(
                id="lore_arc_early_0",
                category=LoreCategory.History,
                content="prior content",
                source=LoreSource.GameEvent,
            )
        )
        chapter = _chapter_with_narrative("early", lore=["new content A", "new content B"])

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [chapter])

        # Index 0 collides; indexes 1 + 2 are fresh. Helper does not
        # rewrite the existing fragment (matches LoreStore.add()
        # contract — duplicate raises) and does not hard-fail.
        # Note: indexes follow the chapter's lore array, not insertion
        # density; the 0-collision means the chapter's ``lore[0]`` was
        # already minted under the same id, the chapter's ``lore[1]``
        # mints as ``lore_arc_early_1``, and so on.
        assert result.lore_fragments_skipped_duplicate == 1
        assert result.lore_fragments_minted == 1
        assert lore_store.fragments["lore_arc_early_0"].content == "prior content"


# ---------------------------------------------------------------------------
# Result aggregation — counts roll up across multiple chapters in a
# single seed call, since one tier transition can promote multiple
# chapters at once (Fresh → Mid in a single recompute on a high-beats
# session).
# ---------------------------------------------------------------------------


class TestResultAggregationAcrossChapters:
    def test_counts_aggregate_across_chapters(self) -> None:
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()
        chapters = [
            _chapter_with_narrative(
                "early",
                speaker_text_pairs=[("narrator", "scene one")],
                lore=["fact one"],
            ),
            _chapter_with_narrative(
                "mid",
                speaker_text_pairs=[
                    ("narrator", "scene two"),
                    ("Rux", "I object."),
                ],
                lore=["fact two", "fact three"],
            ),
        ]

        result = seed_lore_from_arc_promotion(snap, store, lore_store, chapters)

        assert result.narrative_entries_appended == 3
        assert result.lore_fragments_minted == 3
        # content_bytes_seeded — sum of every appended-or-minted body's
        # length so the OTEL panel can chart Lane B's actual throughput.
        # Per AC3: ``content_bytes_seeded`` set on the seed span. This
        # is the scaffold for that attribute; exact byte total depends
        # on encoding policy chosen by Dev (str length, utf-8 bytes,
        # etc.) — assert > 0 here to keep the test framework-agnostic.
        assert result.content_bytes_seeded > 0

    def test_empty_chapter_list_is_a_clean_no_op(self) -> None:
        """The negative case 45-23 owns: an off-tier-transition tick
        passes ``chapters_added=[]`` (Fresh→Fresh is a no-op). The
        helper must not raise and must report all-zeros so the OTEL
        span (per context-story-45-23.md AC4) emits attribute-zero
        values that distinguish "engaged but nothing to seed" from
        "never engaged".
        """
        snap = _snapshot()
        store = MagicMock()
        lore_store = LoreStore()

        result = seed_lore_from_arc_promotion(snap, store, lore_store, [])

        assert result.narrative_entries_appended == 0
        assert result.lore_fragments_minted == 0
        assert result.lore_fragments_skipped_duplicate == 0
        assert result.content_bytes_seeded == 0
        assert lore_store.is_empty()
        assert store.append_narrative.call_count == 0
