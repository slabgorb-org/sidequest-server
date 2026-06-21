"""Monster Manual stale-cache coherence — purge native-era encounter enemies.

Playtest 2026-06-20 (sq-playtest-pingpong) [BUG / CWN-OTHER-SEATING]:
road_warrior/the_circuit (ruleset CWN) seated a combat Other named "Shadow"
with **48 HP** and ``creature_id="Wheelman"`` — a PLAYER CLASS — against a
level-1 PC. The world's authored bestiary is harbor wildlife (max 27 HP). The
class-typed, 48-HP (=8x level-6) mooks are the **native** ``generate_enemy``
encountergen output (``class = rng.choice(allowed_classes)``, ``hp = 8*level``),
which should only run for ``ruleset: "dial"``. road_warrior binds ``cwn``, whose
encountergen path samples the bestiary and always stamps ``class="creature"``.

Root cause: the Monster Manual cache is keyed by genre+world (NOT session) and
persists across sessions; it was seeded under the native path (pre-bestiary
binding / an old ``dial`` era) and reused because ``needs_seeding()`` checks only
population, never coherence with the bound ruleset. ``purge_ruleset_incoherent_
encounters`` drops those stale encounters so ``needs_seeding()`` re-fires and the
manual re-seeds via the correct bestiary path — removing the over-scaled,
PC-class-typed mooks the seater was grabbing as the Other.
"""

from __future__ import annotations

from sidequest.game.monster_manual import ManualEncounter, MonsterManual


def _enc(enemies: list, *, label: str = "e", tier: int = 2) -> ManualEncounter:
    return ManualEncounter(data={"enemies": enemies}, label=label, tier=tier)


def test_purges_native_class_typed_encounter_for_ruleset_pack():
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    m.encounters = [_enc([{"name": "Shadow", "class": "Wheelman", "hp": 48}])]
    purged = m.purge_ruleset_incoherent_encounters(is_ruleset_module=True)
    assert len(purged) == 1
    assert m.encounters == []


def test_keeps_bestiary_creature_typed_encounter():
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    enc = _enc([{"name": "Keeper of the Lower Road", "class": "creature", "hp": 27}])
    m.encounters = [enc]
    purged = m.purge_ruleset_incoherent_encounters(is_ruleset_module=True)
    assert purged == []
    assert m.encounters == [enc]


def test_noop_for_native_pack_even_with_player_class_enemies():
    # A native (ruleset='dial') pack LEGITIMATELY has player-class humanoid
    # enemies — never purge them.
    m = MonsterManual(genre="some_native", world="w")
    m.encounters = [_enc([{"name": "Bandit", "class": "Fighter", "hp": 16}])]
    purged = m.purge_ruleset_incoherent_encounters(is_ruleset_module=False)
    assert purged == []
    assert len(m.encounters) == 1


def test_purges_only_the_native_encounters_in_a_mixed_cache():
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    good = _enc([{"name": "Harbor Rat", "class": "creature", "hp": 4}], label="good")
    bad = _enc([{"name": "Shadow", "class": "Wheelman", "hp": 48}], label="bad")
    m.encounters = [good, bad]
    purged = m.purge_ruleset_incoherent_encounters(is_ruleset_module=True)
    assert [e.label for e in purged] == ["bad"]
    assert [e.label for e in m.encounters] == ["good"]


def test_does_not_purge_missing_class_or_empty_enemies():
    # Conservative: only an EXPLICIT non-"creature" class is a stale-native
    # signal. A missing class key or empty enemy list is left untouched (no
    # over-purging of ambiguous/partial data).
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    m.encounters = [
        _enc([{"name": "X", "hp": 10}], label="no_class"),
        _enc([], label="empty_enemies"),
        ManualEncounter(data={}, label="no_enemies_key", tier=2),
    ]
    purged = m.purge_ruleset_incoherent_encounters(is_ruleset_module=True)
    assert purged == []
    assert len(m.encounters) == 3


def test_purged_cache_then_needs_seeding():
    # After purging the only (stale) encounter, needs_seeding() must re-fire so
    # ensure_loaded re-seeds via the correct bestiary path.
    m = MonsterManual(genre="road_warrior", world="the_circuit")
    m.encounters = [_enc([{"name": "Shadow", "class": "Wheelman", "hp": 48}])]
    # Give it enough NPCs that population alone wouldn't trip needs_seeding.
    for i in range(5):
        m.add_npc({"name": f"N{i}", "role": "r", "culture": "c"}, [])
    assert not m.needs_seeding()  # populated: 5 npcs + 1 encounter
    m.purge_ruleset_incoherent_encounters(is_ruleset_module=True)
    assert m.needs_seeding()  # encounter purged -> re-seed fires
