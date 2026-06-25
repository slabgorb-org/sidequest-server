"""Monster Manual cross-world coherence — purge sibling-world bestiary creatures.

Story 158-33 (sq-playtest-pingpong 2026-06-25, heavy_metal/barsoom): barsoom's
persisted Monster Manual (``~/.sidequest/manuals/heavy_metal_barsoom.json``)
carried encounter enemies authored ONLY in the SIBLING world long_foundry's
``bestiary.yaml`` — "Foundry Automaton", "Grave Knight", "Knight of the Ashen
Banner". Salensus Oll's "Throw him the Knight" then latched the long_foundry
"Knight of the Ashen Banner" as the Barsoom arena champion — a Genre/World-Truth
break (SOUL: Crunch in the Genre, Flavor in the World).

Root: the Manual cache is keyed by genre+world and persists across sessions. It
was seeded when a *genre-tier* ``heavy_metal/bestiary.yaml`` still existed (commit
69b78cf), mixing every world's creatures into one pool. ADR-120 (b3bc387) then
relocated rosters to per-world ``worlds/<slug>/bestiary.yaml`` and 158-21 curated
barsoom's — but the already-persisted Manual was never re-validated against the
new, world-scoped bestiary. The existing
:meth:`MonsterManual.purge_ruleset_incoherent_encounters` does NOT catch this:
the foreign enemies are ``class="creature"`` (bestiary-sourced), so they pass the
native-class stale signal. We need a sibling purge keyed on world membership.

``purge_foreign_bestiary_encounters(bestiary)`` is that sibling: it drops cached
encounters whose ``class="creature"`` enemies are absent from the CURRENT world's
effective bestiary. Pure — the caller persists + emits the OTEL span. Conservative
like its sibling: a ``None`` bestiary (unresolvable scope) purges nothing (never
empty the pool — the 87-4 failure mode), and only an explicit ``class="creature"``
enemy with a name is judged (native-class enemies are the other purge's domain;
missing class/name/empty enemy lists are left untouched).
"""

from __future__ import annotations

from sidequest.game.monster_manual import ManualEncounter, MonsterManual
from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry


def _bestiary(*names: str) -> Bestiary:
    """A minimal valid world bestiary holding ``names`` as creature entries."""
    return Bestiary(
        entries=[
            BestiaryEntry(
                id=n.lower().replace(" ", "_"),
                name=n,
                level=1,
                hp=9,
                armor_class=10,
                attack_bonus=0,
            )
            for n in names
        ]
    )


def _enc(enemies: list, *, label: str = "e", tier: int = 2) -> ManualEncounter:
    return ManualEncounter(data={"enemies": enemies}, label=label, tier=tier)


def _creature(name: str, *, hp: int = 9) -> dict:
    """A bestiary-sourced enemy block (encountergen stamps class='creature')."""
    return {"name": name, "class": "creature", "hp": hp}


# ---------------------------------------------------------------------------
# AC-1 / AC-3: a sibling-world creature is purged
# ---------------------------------------------------------------------------


def test_purges_encounter_with_sibling_world_creature():
    # barsoom's world bestiary owns Banth; the Knight is long_foundry's.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    m.encounters = [_enc([_creature("Knight of the Ashen Banner")], label="foreign")]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth", "White Ape"))
    assert [e.label for e in purged] == ["foreign"]
    assert m.encounters == []


def test_purges_encounter_when_any_enemy_is_foreign():
    # Whole-encounter granularity (mirrors purge_ruleset_incoherent_encounters):
    # an in-world creature does NOT rescue an encounter that also fields a foreign one.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    enc = _enc([_creature("Banth"), _creature("Foundry Automaton")], label="mixed")
    m.encounters = [enc]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert [e.label for e in purged] == ["mixed"]
    assert m.encounters == []


# ---------------------------------------------------------------------------
# AC-2: don't empty the pool — in-world creatures are kept
# ---------------------------------------------------------------------------


def test_keeps_encounter_with_in_world_creature():
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    enc = _enc([_creature("Banth")], label="canon")
    m.encounters = [enc]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth", "Thern Zealot"))
    assert purged == []
    assert m.encounters == [enc]


def test_match_is_case_insensitive():
    # encountergen copies entry.name verbatim, but the join must not be brittle.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    enc = _enc([_creature("banth")], label="canon")
    m.encounters = [enc]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert purged == []
    assert m.encounters == [enc]


def test_returns_empty_when_all_in_world():
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    m.encounters = [
        _enc([_creature("Banth")], label="a"),
        _enc([_creature("White Ape")], label="b"),
    ]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth", "White Ape"))
    assert purged == []
    assert len(m.encounters) == 2


def test_mixed_cache_purges_only_the_foreign_encounters():
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    good = _enc([_creature("Banth")], label="good")
    bad = _enc([_creature("Grave Knight")], label="bad")
    m.encounters = [good, bad]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert [e.label for e in purged] == ["bad"]
    assert [e.label for e in m.encounters] == ["good"]


# ---------------------------------------------------------------------------
# Conservative: never over-purge / never empty the pool blindly
# ---------------------------------------------------------------------------


def test_none_bestiary_purges_nothing():
    # Unresolvable scope (neither world nor genre tier supplies a bestiary) must
    # NOT wipe the pool — that is the 87-4 silently-empty-pool failure mode.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    enc = _enc([_creature("Knight of the Ashen Banner")], label="unknown")
    m.encounters = [enc]
    purged = m.purge_foreign_bestiary_encounters(None)
    assert purged == []
    assert m.encounters == [enc]


def test_ignores_native_class_enemies():
    # A native player-class enemy (class != "creature") is the OTHER purge's
    # domain (purge_ruleset_incoherent_encounters). This world-membership purge
    # judges only bestiary-sourced creatures, so it leaves a native-class enemy
    # untouched even when its name is absent from the bestiary.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    enc = _enc([{"name": "Bandit", "class": "Fighter", "hp": 16}], label="native")
    m.encounters = [enc]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert purged == []
    assert m.encounters == [enc]


def test_does_not_purge_missing_class_name_or_empty_enemies():
    # Conservative on partial/ambiguous data — only an explicit class="creature"
    # enemy WITH a name is judged.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    m.encounters = [
        _enc([{"name": "X", "hp": 10}], label="no_class"),
        _enc([{"class": "creature", "hp": 10}], label="no_name"),
        _enc([], label="empty_enemies"),
        ManualEncounter(data={}, label="no_enemies_key", tier=2),
    ]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert purged == []
    assert len(m.encounters) == 4


def test_purge_is_pure_and_returns_removed_encounters():
    # The method does not persist (no save side effect) — it returns the removed
    # encounters so the caller persists + emits the span, exactly like its sibling.
    m = MonsterManual(genre="heavy_metal", world="barsoom")
    foreign = _enc([_creature("Foundry Automaton")], label="foreign")
    m.encounters = [foreign]
    purged = m.purge_foreign_bestiary_encounters(_bestiary("Banth"))
    assert purged == [foreign]
    # NPC pool is never touched by an encounter purge.
    assert m.npcs == []
