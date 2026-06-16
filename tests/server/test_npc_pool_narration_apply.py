"""Failing tests for Wave 2A — NPC Pool / NPC State Split (story 45-47, Task 3).

The narration_apply NPC-mention loop is rewritten with a 3-step lookup:
1. Lookup name in ``snapshot.npcs`` (case-folded). If found: update
   ``last_seen_*`` on the ``Npc``; run drift detection.
2. Lookup name in ``snapshot.npc_pool``. If found: additive upsert
   identity fields onto the pool member.
3. No match: append ``NpcPoolMember(drawn_from="narrator_invented")``
   to the pool.

PC names skip all three branches via the existing pre-filter.

Every cite (after PC-skip) emits ``SPAN_NPC_REFERENCED`` with attributes
``name``, ``match_strategy ∈ {"npcs_hit", "pool_hit", "invented"}``, and
``pool_origin: str | None``.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import _apply_npc_mentions


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _pc(name: str) -> Character:
    """Construct a minimal Character (PC) for the snapshot."""
    return Character(
        core=_core(name),
        backstory="A wanderer.",
        char_class="adventurer",
        race="human",
    )


def _mention(
    name: str,
    *,
    role: str = "",
    pronouns: str = "",
    appearance: str = "",
) -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance=appearance)


# ---------------------------------------------------------------------------
# Branch 1: npcs_hit — cite matches existing Npc, last_seen_* updated
# ---------------------------------------------------------------------------


def test_cite_known_npc_updates_last_seen_on_npc() -> None:
    snapshot = GameSnapshot(
        characters=[_pc("Hero")],
        npcs=[Npc(core=_core("Boris"))],
    )
    snapshot.character_locations["Hero"] = "TavernRow"
    mention = _mention("Boris")
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[mention],
        turn_num=7,
        acting_character_name="Hero",
    )
    npc = snapshot.npcs[0]
    assert npc.last_seen_location == "TavernRow"
    assert npc.last_seen_turn == 7


def test_cite_known_npc_does_not_append_to_pool() -> None:
    snapshot = GameSnapshot(
        location="TavernRow",
        npcs=[Npc(core=_core("Boris"))],
    )
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Boris")],
        turn_num=1,
    )
    assert snapshot.npc_pool == []


def test_cite_known_npc_is_case_insensitive() -> None:
    snapshot = GameSnapshot(
        location="Bridge",
        npcs=[Npc(core=_core("Boris"))],
    )
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("BORIS")],
        turn_num=2,
    )
    assert snapshot.npcs[0].last_seen_turn == 2
    assert snapshot.npc_pool == []


def test_cite_known_npc_does_not_clobber_npc_identity_fields() -> None:
    """Drift detection logs a warning but additive upsert does NOT
    overwrite the existing Npc's pronouns/role with mention values."""
    snapshot = GameSnapshot(
        location="X",
        npcs=[
            Npc(
                core=_core("Boris"),
                pronouns="he/him",
                appearance="bearded",
            )
        ],
    )
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Boris", pronouns="they/them", appearance="clean-shaven")],
        turn_num=3,
    )
    npc = snapshot.npcs[0]
    # Existing identity fields preserved — drift logged, not silently overwritten.
    assert npc.pronouns == "he/him"
    assert npc.appearance == "bearded"


# ---------------------------------------------------------------------------
# Branch 2: pool_hit — cite matches pool member, no Npc created
# ---------------------------------------------------------------------------


def test_cite_known_pool_member_does_not_create_npc() -> None:
    pool_member = NpcPoolMember(name="Marya", drawn_from="legacy_registry")
    snapshot = GameSnapshot(npc_pool=[pool_member])
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Marya")],
        turn_num=1,
    )
    assert snapshot.npcs == []
    # Pool member is not consumed — re-citable per design.
    assert len(snapshot.npc_pool) == 1
    assert snapshot.npc_pool[0].name == "Marya"


def test_cite_pool_member_additively_upserts_identity_fields() -> None:
    """A pool member seeded with partial identity gains additional
    fields when the narrator's mention provides them. Existing values
    win on conflict (additive only)."""
    pool_member = NpcPoolMember(
        name="Marya",
        role="barkeep",  # already set
        drawn_from="legacy_registry",
    )
    snapshot = GameSnapshot(npc_pool=[pool_member])
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[
            _mention(
                "Marya",
                role="merchant",  # different, should NOT overwrite
                pronouns="she/her",  # new, should fill in
                appearance="weathered hands",
            )
        ],
        turn_num=1,
    )
    member = snapshot.npc_pool[0]
    assert member.role == "barkeep"  # preserved
    assert member.pronouns == "she/her"  # filled in
    assert member.appearance == "weathered hands"  # filled in


def test_cite_pool_member_is_case_insensitive() -> None:
    pool_member = NpcPoolMember(name="Marya", drawn_from="legacy_registry")
    snapshot = GameSnapshot(npc_pool=[pool_member])
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("marya")],
        turn_num=1,
    )
    assert len(snapshot.npc_pool) == 1
    assert snapshot.npcs == []


# ---------------------------------------------------------------------------
# Branch 3: invented — cite name not in any store, append to pool
# ---------------------------------------------------------------------------


def test_cite_unknown_name_appends_to_pool_with_invented_provenance() -> None:
    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[
            _mention(
                "Erewhon",
                role="hermit",
                pronouns="they/them",
                appearance="grey cloak",
            )
        ],
        turn_num=1,
    )
    assert len(snapshot.npc_pool) == 1
    member = snapshot.npc_pool[0]
    assert member.name == "Erewhon"
    assert member.role == "hermit"
    assert member.pronouns == "they/them"
    assert member.appearance == "grey cloak"
    assert member.drawn_from == "narrator_invented"
    assert member.archetype_id is None


def test_invented_member_does_not_create_npc() -> None:
    snapshot = GameSnapshot()
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("NewFace")],
        turn_num=1,
    )
    assert snapshot.npcs == []


# ---------------------------------------------------------------------------
# PC-skip pre-filter (preserved from current behavior)
# ---------------------------------------------------------------------------


def test_pc_name_in_mentions_is_skipped_entirely() -> None:
    snapshot = GameSnapshot(
        characters=[_pc("Rux")],
    )
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Rux", role="ally")],
        turn_num=1,
    )
    # PC name skipped — never lands in pool, npcs, or anywhere.
    assert snapshot.npc_pool == []
    assert snapshot.npcs == []


def test_pc_skip_is_case_insensitive() -> None:
    snapshot = GameSnapshot(characters=[_pc("Rux")])
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("RUX")],
        turn_num=1,
    )
    assert snapshot.npc_pool == []


# ---------------------------------------------------------------------------
# Mixed: multiple mentions in one apply call hit different branches
# ---------------------------------------------------------------------------


def test_multiple_mentions_route_to_different_branches() -> None:
    snapshot = GameSnapshot(
        characters=[_pc("Rux")],
        npcs=[Npc(core=_core("Boris"))],
        npc_pool=[NpcPoolMember(name="Marya", drawn_from="legacy_registry")],
    )
    snapshot.character_locations["Rux"] = "Crossroads"
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[
            _mention("Rux"),  # PC skip
            _mention("Boris"),  # npcs_hit
            _mention("Marya"),  # pool_hit
            _mention("NewFace"),  # invented
        ],
        turn_num=4,
        acting_character_name="Rux",
    )
    assert snapshot.npcs[0].last_seen_location == "Crossroads"
    assert snapshot.npcs[0].last_seen_turn == 4
    pool_names = {m.name for m in snapshot.npc_pool}
    assert pool_names == {"Marya", "NewFace"}
    invented = next(m for m in snapshot.npc_pool if m.name == "NewFace")
    assert invented.drawn_from == "narrator_invented"


def test_npc_lookup_shadows_pool_member_with_same_name() -> None:
    """If a name is in BOTH npcs and npc_pool (e.g. promoted but pool
    entry not reaped), npcs lookup wins — last_seen_* lands on Npc, not
    on the pool member."""
    snapshot = GameSnapshot(
        location="Inn",
        npcs=[Npc(core=_core("Boris"), pool_origin="Boris")],
        npc_pool=[NpcPoolMember(name="Boris", drawn_from="legacy_registry")],
    )
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Boris")],
        turn_num=2,
    )
    assert snapshot.npcs[0].last_seen_turn == 2
    # Pool entry still present (not reaped); shadowed by npcs lookup.
    assert len(snapshot.npc_pool) == 1
