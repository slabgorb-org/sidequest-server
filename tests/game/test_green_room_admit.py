"""Green Room admit() — ADR-156 §4: precedence, additive merge, idempotence."""

import pytest

from sidequest.game.creature_core import CreatureCore, Inventory, hp_pool_from_hp
from sidequest.game.disposition import Disposition
from sidequest.game.green_room import (
    LADDER,
    MaterializationCandidate,
    admit,
    attach_alias,
)
from sidequest.game.origin import Origin, OriginKind, identity_key
from sidequest.game.session import GameSnapshot, Npc


@pytest.fixture
def snapshot() -> GameSnapshot:
    return GameSnapshot(genre_slug="caverns_and_claudes", world_slug="beneath_sunden")


def _npc(name: str, *, hp: int = 10, creature_id: str | None = None,
         authored_id: str | None = None, kind: OriginKind = OriginKind.MANUAL_POOL) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name, description="d", personality="p",
            inventory=Inventory(), hp=hp_pool_from_hp(hp), armor_class=10,
        ),
        creature_id=creature_id,
        origin=Origin(kind=kind, creature_id=creature_id, authored_id=authored_id),
    )


def _cand(npc: Npc, source: str = "test") -> MaterializationCandidate:
    assert npc.origin is not None
    return MaterializationCandidate(npc=npc, origin=npc.origin, source=source)


def test_ladder_ranks_match_adr_156() -> None:
    assert LADDER[OriginKind.AUTHORED] == 1
    assert LADDER[OriginKind.GENERIC] == 1          # authored content, ADR-156 §5
    assert LADDER[OriginKind.ROOM_BOUND] == 2
    assert LADDER[OriginKind.REGION_POPULATION] == 3
    assert LADDER[OriginKind.MANUAL_POOL] == 4
    assert LADDER[OriginKind.NARRATOR_INVENTED] == 5
    assert OriginKind.EPHEMERAL_STUB not in LADDER  # must never reach admit


def test_admit_appends_new_identity(snapshot: GameSnapshot) -> None:
    result = admit(snapshot, [_cand(_npc("Grazer", creature_id="grazer"))])
    assert [n.core.name for n in result.admitted] == ["Grazer"]
    assert any(n.core.name == "Grazer" for n in snapshot.npcs)


def test_same_identity_two_tiers_highest_wins_dropped_recorded(snapshot: GameSnapshot) -> None:
    authored = _npc("Molgrath", creature_id="thief",
                    authored_id="molgrath", kind=OriginKind.AUTHORED)
    pool = _npc("Molgrath", creature_id="thief", kind=OriginKind.MANUAL_POOL)
    result = admit(snapshot, [_cand(pool, "mm.encounters"), _cand(authored, "preload")])
    assert len(result.admitted) == 1
    seated = result.admitted[0]
    assert seated.origin is not None and seated.origin.kind == OriginKind.AUTHORED
    assert result.dropped == [identity_key(pool.origin, "Molgrath")]


def test_additive_merge_fills_absent_fields_only(snapshot: GameSnapshot) -> None:
    winner = _npc("Molgrath", authored_id="molgrath", kind=OriginKind.AUTHORED)
    winner.pronouns = None
    loser = _npc("Molgrath", authored_id="molgrath", kind=OriginKind.MANUAL_POOL)
    loser.pronouns = "he/him"
    loser.core.description = "SHOULD NOT OVERWRITE"
    admit(snapshot, [_cand(winner), _cand(loser)])
    seated = next(n for n in snapshot.npcs if n.core.name == "Molgrath")
    assert seated.pronouns == "he/him"            # absent → filled
    assert seated.core.description == "d"         # present → untouched


def test_idempotence_never_resets_live_state(snapshot: GameSnapshot) -> None:
    admit(snapshot, [_cand(_npc("Grazer", hp=8, creature_id="grazer"))])
    seated = next(n for n in snapshot.npcs if n.core.name == "Grazer")
    seated.core.apply_hp_delta(-5)                 # wounded in the fight
    # Disposition is a value type with no in-place mutator; this is the
    # production idiom (sidequest/agents/tools/update_npc_disposition.py).
    seated.disposition = Disposition(int(seated.disposition) - 30)
    admit(snapshot, [_cand(_npc("Grazer", hp=8, creature_id="grazer"))])  # re-inject
    assert seated.core.hp.current == 3             # ADR-139 Inv-2, structural
    assert len([n for n in snapshot.npcs if n.core.name == "Grazer"]) == 1


def test_prose_name_lands_as_alias_not_identity(snapshot: GameSnapshot) -> None:
    bound = _npc("Thief", creature_id="thief")
    mint = _npc("Molgrath the Eyeless", creature_id="thief",
                kind=OriginKind.NARRATOR_INVENTED)
    result = admit(snapshot, [_cand(bound), _cand(mint)])
    assert len(result.admitted) == 1
    seated = result.admitted[0]
    assert seated.core.name == "Thief"
    assert "Molgrath the Eyeless" in seated.aliases


def test_stub_candidate_raises(snapshot: GameSnapshot) -> None:
    stub = _npc("Hold-Dead", kind=OriginKind.EPHEMERAL_STUB)
    with pytest.raises(ValueError, match="EPHEMERAL_STUB"):
        admit(snapshot, [_cand(stub)])


def test_attach_alias_dedups(snapshot: GameSnapshot) -> None:
    npc = _npc("Thief", creature_id="thief")
    assert attach_alias(npc, "Molgrath the Eyeless", from_source="test") is True
    assert attach_alias(npc, "molgrath the eyeless", from_source="test") is False
    assert npc.aliases == ["Molgrath the Eyeless"]
