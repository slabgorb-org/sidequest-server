"""Seed a :class:`LoreStore` from genre pack + character creation data.

Called at chargen confirmation so backstory choices made during
character creation are visible to the later RAG retrieval pipeline.
Without this seed the character's narrative anchors only live on the
builder, which is discarded immediately after confirmation.

Fragment id formats:

- Genre pack: ``lore_genre_history`` / ``lore_genre_geography`` /
  ``lore_genre_cosmology`` / ``lore_genre_faction_<slug>``
- Character creation: ``lore_char_creation_<scene_id>_<choice_index>``
- Arc promotion (Story 45-23): ``lore_arc_<chapter_id>_<lore_index>``

Duplicate ids are silently skipped — seeding is idempotent so a
reconnect that re-seeds won't hard-fail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sidequest.game.history_chapter import HistoryChapter
from sidequest.game.lore_store import (
    DuplicateLoreId,
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)
from sidequest.game.session import NarrativeEntry
from sidequest.genre.models.character import CharCreationScene
from sidequest.genre.models.lore import WorldLore
from sidequest.genre.models.pack import GenrePack


def _try_add(store: LoreStore, fragment: LoreFragment) -> bool:
    """Insert ``fragment``; return ``True`` on success, ``False`` on duplicate."""
    try:
        store.add(fragment)
    except DuplicateLoreId:
        return False
    return True


def seed_lore_from_genre_pack(store: LoreStore, pack: GenrePack) -> int:
    """Seed ``store`` with fragments derived from ``pack.lore``.

    Returns the number of fragments successfully added (duplicates
    skipped).
    """
    count = 0

    if pack.lore.history and _try_add(
        store,
        LoreFragment.new(
            id="lore_genre_history",
            category=LoreCategory.History,
            content=pack.lore.history,
            source=LoreSource.GenrePack,
        ),
    ):
        count += 1

    if pack.lore.geography and _try_add(
        store,
        LoreFragment.new(
            id="lore_genre_geography",
            category=LoreCategory.Geography,
            content=pack.lore.geography,
            source=LoreSource.GenrePack,
        ),
    ):
        count += 1

    if pack.lore.cosmology and _try_add(
        store,
        LoreFragment.new(
            id="lore_genre_cosmology",
            # Cosmology fragments bucket into the History category.
            category=LoreCategory.History,
            content=pack.lore.cosmology,
            source=LoreSource.GenrePack,
        ),
    ):
        count += 1

    for faction in pack.lore.factions:
        slug = faction.name.lower().replace(" ", "_")
        if _try_add(
            store,
            LoreFragment.new(
                id=f"lore_genre_faction_{slug}",
                category=LoreCategory.Faction,
                content=f"{faction.name}: {faction.description}",
                source=LoreSource.GenrePack,
                metadata={"faction_name": faction.name},
            ),
        ):
            count += 1

    return count


def seed_lore_from_world(store: LoreStore, world_lore: WorldLore, world_slug: str) -> int:
    """Seed ``store`` with fragments derived from a world's ``lore.yaml``.

    Returns the number of fragments successfully added (duplicates skipped).
    Fragment ids are scoped by ``world_slug`` (``lore_world_<slug>_history``
    etc.) so a player who switches worlds within a genre pack doesn't see
    the prior world's lore leaking into the new world's RAG retrieval.

    Pingpong 2026-04-30 ("Lore RAG returns empty_query_or_store for all 4
    PCs every turn"): pre-fix the genre pack's ``Lore`` and the world's
    ``WorldLore`` were never seeded into the per-session lore store —
    only chargen-choice fragments via :func:`seed_lore_from_char_creation`
    were added, and chargen choices are short snippets that do not cover
    the genre's history/geography/cosmology/factions. The narrator was
    therefore composing every turn with zero hits from the genre lore
    corpus. SOUL-violation territory: the narrator improvises faction
    references that the world cosmology never sanctions.
    """
    count = 0
    slug = world_slug.strip().lower().replace(" ", "_") or "unknown_world"

    if world_lore.history and _try_add(
        store,
        LoreFragment.new(
            id=f"lore_world_{slug}_history",
            category=LoreCategory.History,
            content=world_lore.history,
            source=LoreSource.GenrePack,
            metadata={"world_slug": world_slug},
        ),
    ):
        count += 1

    if world_lore.geography and _try_add(
        store,
        LoreFragment.new(
            id=f"lore_world_{slug}_geography",
            category=LoreCategory.Geography,
            content=world_lore.geography,
            source=LoreSource.GenrePack,
            metadata={"world_slug": world_slug},
        ),
    ):
        count += 1

    if world_lore.cosmology and _try_add(
        store,
        LoreFragment.new(
            id=f"lore_world_{slug}_cosmology",
            # Cosmology fragments bucket into the History category
            # (matches seed_lore_from_genre_pack precedent).
            category=LoreCategory.History,
            content=world_lore.cosmology,
            source=LoreSource.GenrePack,
            metadata={"world_slug": world_slug},
        ),
    ):
        count += 1

    for faction in world_lore.factions:
        faction_slug = faction.name.lower().replace(" ", "_")
        if _try_add(
            store,
            LoreFragment.new(
                id=f"lore_world_{slug}_faction_{faction_slug}",
                category=LoreCategory.Faction,
                content=f"{faction.name}: {faction.description}",
                source=LoreSource.GenrePack,
                metadata={
                    "faction_name": faction.name,
                    "world_slug": world_slug,
                },
            ),
        ):
            count += 1

    return count


def seed_world_lore(
    store: LoreStore,
    pack: GenrePack,
    world_slug: str | None,
    *,
    emit: Callable[..., None] | None = None,
) -> tuple[int, int]:
    """Seed ``store`` with the deterministic genre + world lore pair.

    Single source of truth for the genre/world seeding the chargen
    confirmation flow used to do inline (two ``seed_lore_from_*`` calls
    plus a ``lore_store_loaded`` emission). Extracted so the slug-resume
    connect path can re-seed an empty in-memory ``LoreStore`` with the
    same fragments — pre-fix only the fresh chargen flow seeded it, so
    every *resumed* save had an empty store, ``query_lore`` returned
    ``hit_count=0``, and the SDK narrator confabulated world canon
    instead of recalling it.

    Returns ``(genre_fragments_added, world_fragments_added)``.

    Idempotency: both underlying seeders use stable fragment ids and
    swallow :class:`DuplicateLoreId`, so re-running this against a store
    that already holds the genre/world fragments adds zero and never
    grows the store unboundedly — safe to call on every connect (fresh
    or any reconnect).

    ``emit`` (optional) is invoked exactly once after seeding with the
    ``lore_store_loaded`` watcher payload kwargs so the GM panel / Jaeger
    can prove lore loaded — on the resume path as well as the fresh one.
    It is a callback (rather than a direct watcher import) because
    ``sidequest.game`` must not depend on ``sidequest.server`` /
    ``sidequest.telemetry`` at module load — same import-cycle reasoning
    as :func:`seed_lore_from_arc_promotion`'s local span import.
    """
    genre_added = seed_lore_from_genre_pack(store, pack)
    world_added = 0
    if world_slug:
        # Inside ``if world_slug:`` pyright narrows ``world_slug`` from
        # ``str | None`` to ``str`` so the ``seed_lore_from_world``
        # (``world_slug: str``) call type-checks. Behaviour identical to
        # the prior ``... if world_slug else None`` guard: falsy
        # world_slug or unresolved world_obj still yields world_added=0.
        world_obj = pack.worlds.get(world_slug)
        if world_obj is not None:
            world_added = seed_lore_from_world(store, world_obj.lore, world_slug)

    if emit is not None:
        emit(
            genre_fragments_added=genre_added,
            world_fragments_added=world_added,
            total_fragments=len(store),
            total_tokens=store.total_tokens(),
        )

    return genre_added, world_added


def seed_lore_from_char_creation(store: LoreStore, scenes: list[CharCreationScene]) -> int:
    """Seed ``store`` with one fragment per chargen choice.

    Returns the number of fragments successfully added. Fragment ids
    follow ``lore_char_creation_<scene_id>_<choice_index>`` so saves
    survive the backend swap cleanly.
    """
    count = 0
    for scene in scenes:
        for index, choice in enumerate(scene.choices):
            fragment = LoreFragment.new(
                id=f"lore_char_creation_{scene.id}_{index}",
                category=LoreCategory.Character,
                content=f"{choice.label}: {choice.description}",
                source=LoreSource.CharacterCreation,
                metadata={
                    "scene_id": scene.id,
                    "choice_index": str(index),
                    "choice_label": choice.label,
                },
            )
            if _try_add(store, fragment):
                count += 1
    return count


@dataclass
class ArcSeedResult:
    """Aggregate counts from a :func:`seed_lore_from_arc_promotion` call.

    Surfaced on the ``world_history.arc_embedding_seed`` OTEL span so
    the GM panel can chart Lane B throughput per promotion turn (Story
    45-23). ``content_bytes_seeded`` sums every appended-or-minted body
    by ``len(str)`` so the chart axis reads in source-character units
    rather than utf-8 bytes — same units the narrator's prompt budget
    is denominated in.
    """

    narrative_entries_appended: int = 0
    lore_fragments_minted: int = 0
    lore_fragments_skipped_duplicate: int = 0
    content_bytes_seeded: int = 0


def seed_lore_from_arc_promotion(
    snapshot: Any,
    store: Any,
    lore_store: LoreStore,
    chapters: list[HistoryChapter],
) -> ArcSeedResult:
    """Seed runtime arc-promoted chapters into the durable narrative
    log + the RAG-retrievable lore store.

    Closes Story 45-23 — Felix's Playtest 3 gap where 71 turns of dense
    play left ``narrative_log`` and ``lore_store`` empty of arc-sourced
    content because the chapter-promotion path never wrote back.

    For each chapter in ``chapters`` (the ``chapters_added`` diff
    returned by 45-19's :func:`recompute_arc_history`):

    1. Each ``ChapterNarrativeEntry`` becomes a ``NarrativeEntry`` row
       on ``snapshot.narrative_log`` (so the next narrator's
       ``state_summary`` reads it) AND a ``store.append_narrative()``
       call (so the durable SQL log carries it for GM-panel replay).
       Entries carry ``entry_type="arc_promotion"`` and the snapshot's
       current ``turn_manager.round`` so the panel can anchor the
       writeback in time.
    2. Each ``chapter.lore`` string becomes a ``LoreFragment`` with id
       ``lore_arc_<chapter_id>_<lore_index>``,
       ``category=LoreCategory.History``,
       ``source=LoreSource.GameEvent``, and
       ``embedding_pending=True`` so the existing per-turn
       ``_dispatch_embed_worker`` picks it up on the next turn.

    Blank/whitespace lore strings are skipped before ``LoreFragment``
    construction (the content validator at ``lore_store.py:90`` rejects
    them, and a malformed pack must not crash the dispatch loop —
    Felix's silent-absence bug becoming a hard crash would be a strictly
    worse failure mode).

    Duplicate fragment ids (idempotent re-seed of the same chapter
    list) are swallowed via ``_try_add`` and counted into
    ``lore_fragments_skipped_duplicate`` so the OTEL span can
    distinguish a real second promotion from an idempotent re-tick.

    Per-write OTEL spans (Story 45-23):
    - ``world_history.narrative_log_writeback`` — once per chapter
      with non-empty ``narrative_log``, attributes carry
      ``entries_count`` and ``entry_type="arc_promotion"``.
    - ``world_history.lore_writeback`` — once per minted fragment,
      attributes carry the ``pending_embedding=True`` confirmation.

    The outer ``world_history.arc_embedding_seed`` span is opened by
    the caller (``_execute_narration_turn``) so the per-chapter counts
    on the result struct can be attached to it at close time.
    """
    # Local import: spans → telemetry → game/session is a tighter cycle
    # than this module needs to participate in module-load time. The
    # registered constants are stable; importing inside the helper
    # keeps the chargen-time seeders' import graph unchanged.
    from sidequest.telemetry.spans import (  # noqa: PLC0415
        SPAN_WORLD_HISTORY_LORE_WRITEBACK,
        SPAN_WORLD_HISTORY_NARRATIVE_LOG_WRITEBACK,
        Span,
    )

    result = ArcSeedResult()

    # ``round`` is the durable-log timestamp the GM panel charts; pull
    # it once from the snapshot's turn manager so all entries land on
    # the same tick. Defensive ``getattr`` mirrors the recompute
    # helper's defensive shape — a malformed snapshot must not crash
    # the dispatch loop on a content-authoring bug.
    round_value = int(getattr(getattr(snapshot, "turn_manager", None), "round", 0) or 0)
    interaction = int(getattr(getattr(snapshot, "turn_manager", None), "interaction", 0) or 0)

    for chapter in chapters:
        chapter_id = getattr(chapter, "id", "") or ""

        # ----- narrative_log writeback ---------------------------------
        narrative_entries = list(getattr(chapter, "narrative_log", []) or [])
        if narrative_entries:
            entries_count = 0
            for entry in narrative_entries:
                speaker = getattr(entry, "speaker", "") or ""
                text = getattr(entry, "text", "") or ""
                if not speaker.strip():
                    # The 45-22 NarrativeEntry validator rejects blank
                    # authors. A chapter entry with a blank speaker is a
                    # content-authoring bug; skip it rather than crash
                    # the dispatch loop (No Silent Fallbacks: the loud
                    # failure is the schema validator at construction
                    # time — we honour it by not constructing).
                    continue
                snap_entry = NarrativeEntry(
                    timestamp=0,
                    round=round_value,
                    author=speaker,
                    content=text,
                    tags=[],
                    speaker=speaker,
                    entry_type="arc_promotion",
                )
                snapshot.narrative_log.append(snap_entry)
                store.append_narrative(snap_entry)
                result.narrative_entries_appended += 1
                result.content_bytes_seeded += len(text)
                entries_count += 1

            if entries_count:
                with Span.open(
                    SPAN_WORLD_HISTORY_NARRATIVE_LOG_WRITEBACK,
                    {
                        "chapter_id": chapter_id,
                        "entries_count": entries_count,
                        "interaction": interaction,
                        "entry_type": "arc_promotion",
                    },
                ):
                    pass

        # ----- lore_store writeback ------------------------------------
        for index, lore_text in enumerate(getattr(chapter, "lore", []) or []):
            text = lore_text or ""
            if not text.strip():
                # ``LoreFragment.content`` validator rejects blank /
                # whitespace-only strings (lore_store.py:89). Skip
                # before construction to keep the dispatch loop alive
                # on a malformed pack — same reasoning as the speaker
                # guard above.
                continue
            fragment_id = f"lore_arc_{chapter_id}_{index}"
            fragment = LoreFragment.new(
                id=fragment_id,
                category=LoreCategory.History,
                content=text,
                source=LoreSource.GameEvent,
                turn_created=interaction,
                metadata={"chapter_id": chapter_id, "lore_index": str(index)},
            )
            if _try_add(lore_store, fragment):
                result.lore_fragments_minted += 1
                result.content_bytes_seeded += len(text)
                with Span.open(
                    SPAN_WORLD_HISTORY_LORE_WRITEBACK,
                    {
                        "chapter_id": chapter_id,
                        "fragment_id": fragment_id,
                        "category": LoreCategory.History,
                        "content_bytes": len(text),
                        "pending_embedding": True,
                    },
                ):
                    pass
            else:
                result.lore_fragments_skipped_duplicate += 1

    return result


__all__ = [
    "ArcSeedResult",
    "seed_lore_from_arc_promotion",
    "seed_lore_from_char_creation",
    "seed_lore_from_genre_pack",
    "seed_lore_from_world",
    "seed_world_lore",
]
