"""RED-phase unit tests for runtime lore accretion (story 75-1).

Restoration of the Rust ``accumulate_and_persist_lore`` loop
(``/tmp/sidequest-rust-ref/crates/sidequest-server/src/dispatch/lore_sync.rs:26-120``):
runtime narrator-discovered facts must become embedded, retrievable
``LoreFragment`` entries instead of dead-ending in the un-embedded
``KnownFact`` list.

CHOSEN SEAM (TEA design decision — see session Design Deviations):
the story deferred "bridge KnownFact → fragment (A)" vs "parallel
post-turn hook (B)". These tests pin **A**: ``KnownFact`` is the
canonical runtime-fact carrier, so accretion mints one ``LoreFragment``
per fact. The pure minting logic lives in
``sidequest.game.lore_accretion`` and mirrors the established
``seed_lore_from_arc_promotion`` / ``mint_threshold_lore`` conventions
(result dataclass + idempotent-by-id minting).

These tests are INTENTIONALLY RED until 75-1 lands — the module does
not exist yet.
"""

from __future__ import annotations

import pytest

from sidequest.game.character import KnownFact
from sidequest.game.lore_store import (
    DuplicateLoreId,
    LoreCategory,
    LoreSource,
    LoreStore,
)
from sidequest.protocol.models import FactCategory

# The module under construction (RED: does not exist yet).
from sidequest.game import lore_accretion


def _fact(content: str, *, category: FactCategory = FactCategory.Lore) -> KnownFact:
    """A runtime-discovered fact as committed by the narrator mid-turn."""
    return KnownFact(content=content, category=category, source="GameEvent")


# ---------------------------------------------------------------------------
# AC1 — runtime facts become lore fragments
# ---------------------------------------------------------------------------


def test_accrete_mints_one_fragment_per_fact() -> None:
    """AC1: each KnownFact yields a retrievable LoreFragment in the store."""
    store = LoreStore()
    facts = [_fact("The Sunken Vault floods at the third bell.")]

    result = lore_accretion.accrete_facts_to_lore(
        store, facts, interaction=7, pc_name="Rux"
    )

    assert result.accreted == 1
    assert len(result.fragment_ids) == 1
    frag = store.fragments[result.fragment_ids[0]]
    assert frag.content == "The Sunken Vault floods at the third bell."
    # Runtime-discovered → GameEvent provenance (matches Rust lore_sync).
    assert frag.source == LoreSource.GameEvent
    assert frag.turn_created == 7


def test_accreted_fragment_id_is_deterministic_from_fact_id() -> None:
    """Idempotency hinges on a stable id derived from the fact's id."""
    store = LoreStore()
    fact = _fact("A deterministic landmark.")

    result = lore_accretion.accrete_facts_to_lore(
        store, [fact], interaction=1, pc_name="Rux"
    )

    assert result.fragment_ids == [f"lore_kf_{fact.fact_id}"]


def test_accreted_fragment_starts_embedding_pending() -> None:
    """The whole point: accreted fragments must be picked up by the
    existing embed worker, so they start pending (cleared once the daemon
    embeds them). A fragment minted already-embedded would never reach
    the daemon and silently never become semantically retrievable."""
    store = LoreStore()
    result = lore_accretion.accrete_facts_to_lore(
        store, [_fact("pending please")], interaction=1, pc_name="Rux"
    )
    frag = store.fragments[result.fragment_ids[0]]
    assert frag.embedding_pending is True
    assert frag.embedding is None


def test_fact_metadata_carries_provenance() -> None:
    """fact_id + pc_name on the fragment metadata so accretion is traceable
    back to its source fact (GM-panel provenance, dedup)."""
    store = LoreStore()
    fact = _fact("traceable")
    result = lore_accretion.accrete_facts_to_lore(
        store, [fact], interaction=3, pc_name="Rux"
    )
    frag = store.fragments[result.fragment_ids[0]]
    assert frag.metadata["fact_id"] == fact.fact_id
    assert frag.metadata["pc_name"] == "Rux"


# ---------------------------------------------------------------------------
# FactCategory → LoreCategory mapping (total — every variant maps)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fact_category", "expected_lore_category"),
    [
        (FactCategory.Lore, LoreCategory.History),
        (FactCategory.Place, LoreCategory.Geography),
        (FactCategory.Person, LoreCategory.Character),
        (FactCategory.Quest, LoreCategory.Event),
        (FactCategory.Ability, LoreCategory.Item),
    ],
)
def test_fact_category_maps_to_lore_category(
    fact_category: FactCategory, expected_lore_category: str
) -> None:
    """Mapping must be total — every FactCategory has a LoreCategory home,
    or retrieval silently drops a class of facts."""
    assert (
        lore_accretion.fact_category_to_lore_category(fact_category)
        == expected_lore_category
    )


def test_accreted_fragment_uses_mapped_category() -> None:
    store = LoreStore()
    result = lore_accretion.accrete_facts_to_lore(
        store,
        [_fact("The Brass Cartel runs the docks.", category=FactCategory.Person)],
        interaction=1,
        pc_name="Rux",
    )
    frag = store.fragments[result.fragment_ids[0]]
    assert frag.category == LoreCategory.Character


# ---------------------------------------------------------------------------
# Idempotency — accretion runs every turn over the full fact list
# ---------------------------------------------------------------------------


def test_accretion_is_idempotent_across_turns() -> None:
    """Accretion sweeps the PC's known_facts every turn. A fact already
    accreted on a prior turn must be skipped — not re-minted, and not
    raise DuplicateLoreId (which would crash the turn)."""
    store = LoreStore()
    fact = _fact("Repeated each turn.")

    first = lore_accretion.accrete_facts_to_lore(
        store, [fact], interaction=1, pc_name="Rux"
    )
    second = lore_accretion.accrete_facts_to_lore(
        store, [fact], interaction=2, pc_name="Rux"
    )

    assert first.accreted == 1
    assert second.accreted == 0
    assert second.skipped_duplicate == 1
    assert len(store.fragments) == 1


def test_idempotency_does_not_raise_duplicate_lore_id() -> None:
    """Belt-and-suspenders: re-accreting must never surface the raw
    DuplicateLoreId — it is handled internally as a skip."""
    store = LoreStore()
    fact = _fact("once")
    lore_accretion.accrete_facts_to_lore(store, [fact], interaction=1, pc_name="Rux")
    try:
        lore_accretion.accrete_facts_to_lore(
            store, [fact], interaction=2, pc_name="Rux"
        )
    except DuplicateLoreId:  # pragma: no cover - asserts the failure mode
        pytest.fail("accretion leaked DuplicateLoreId instead of skipping")


# ---------------------------------------------------------------------------
# No Silent Fallbacks — blank facts must be handled explicitly
# ---------------------------------------------------------------------------


def test_blank_content_fact_is_skipped_explicitly_not_minted() -> None:
    """A fact with blank content cannot become a valid LoreFragment
    (content has min_length=1). It must be skipped *explicitly* and
    counted — never silently minted as a broken/empty fragment, and
    never crash the sweep (No Silent Fallbacks: visible, not silent)."""
    store = LoreStore()
    facts = [_fact("   "), _fact("real content here")]

    result = lore_accretion.accrete_facts_to_lore(
        store, facts, interaction=1, pc_name="Rux"
    )

    assert result.accreted == 1
    assert result.skipped_blank == 1
    # Only the real fact produced a fragment; no empty fragment minted.
    assert len(store.fragments) == 1
    assert all(f.content.strip() for f in store.fragments.values())


def test_empty_fact_list_is_a_clean_no_op() -> None:
    """A turn that discovered nothing must report all-zeros so the OTEL
    span distinguishes "engaged, nothing to accrete" from "never engaged"
    (mirrors seed_lore_from_arc_promotion's empty-chapters contract)."""
    store = LoreStore()
    result = lore_accretion.accrete_facts_to_lore(
        store, [], interaction=1, pc_name="Rux"
    )
    assert result.accreted == 0
    assert result.skipped_duplicate == 0
    assert result.skipped_blank == 0
    assert result.fragment_ids == []
    assert store.is_empty()


# ---------------------------------------------------------------------------
# AC2 — persistence round-trip (embeddings survive save/load)
# ---------------------------------------------------------------------------


def test_accreted_fragment_roundtrips_embedding_on_serialize() -> None:
    """AC2: once embedded, an accreted fragment survives a save/load
    round-trip with its embedding intact — no re-embed cost on replay.
    LoreStore is a pydantic model; save files serialize ``fragments``."""
    store = LoreStore()
    result = lore_accretion.accrete_facts_to_lore(
        store, [_fact("durable knowledge")], interaction=1, pc_name="Rux"
    )
    frag_id = result.fragment_ids[0]

    # Simulate the embed worker attaching a vector.
    store.update_embedding(frag_id, [0.25, 0.75], expected_dim=2)

    restored = LoreStore.model_validate_json(store.model_dump_json())
    rf = restored.fragments[frag_id]

    assert rf.embedding == [0.25, 0.75]
    assert rf.embedding_pending is False
    assert rf.content == "durable knowledge"
    assert rf.source == LoreSource.GameEvent
