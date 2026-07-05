"""Derive-don't-cache Monster Manual — model contract (story 162-1, spec D3+D4).

The Monster Manual was a shared *mutable* cache keyed only by ``genre+world`` and
written by ~4 clones with divergent content checkouts (spec
``docs/superpowers/specs/2026-07-05-npc-generation-inventory.md`` §4-5). Two
opposite failure modes resulted: beneath_sunden's purge/reseed livelock (0/0
pool) and flickering_reach/glenross's 310/1,153-NPC runaway. Targeted
"purge-repair" tourniquets (``purge_ruleset_incoherent_encounters`` +
``purge_foreign_bestiary_encounters``) were band-aids on a stale shared cache.

This suite pins the derive-don't-cache contract that REPLACES that model:

- **content-sha + session-seed keyed pool** — the manual records the
  ``content_sha`` and ``session_seed`` it was derived under.
- **discard-on-mismatch replaces purge-repair** — ``reconcile_content`` clears
  the *whole* pool when either key changed (a different content checkout or a
  new session), so staleness is impossible and the two purge methods become
  unnecessary. No targeted, name-matched purge.
- **accumulation cap** — ``add_npc`` / ``add_encounter`` bound pool growth so the
  1,153-NPC runaway cannot recur; the cap never evicts an authored NPC (V3).
- **fail-loud empty world-slug** — ``_file_path`` / ``load`` / ``save`` raise on a
  blank genre or world instead of silently minting ``caverns_and_claudes_.json``
  (No Silent Fallbacks).
- **seed/purge idempotence (no livelock)** — once a pool is discarded and
  re-stamped, the *same* content key never discards again.

These tests are RED until story 162-1 lands. Written by TEA (Amos) — prove it
breaks before Naomi makes it work.
"""

from __future__ import annotations

import pytest

import sidequest.game.monster_manual as mm_mod
from sidequest.game.monster_manual import MonsterManual

# ── builders ──────────────────────────────────────────────────────


def _npc(name: str) -> dict:
    return {"name": name, "role": "role", "culture": "culture"}


def _enc_data(name: str) -> dict:
    return {"enemies": [{"name": name, "class": "creature", "hp": 6}]}


def _populated(*, content_sha: str, session_seed: str) -> MonsterManual:
    """A manual stamped with a content key and holding one npc + one encounter."""
    m = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        content_sha=content_sha,
        session_seed=session_seed,
    )
    m.add_npc(_npc("Grib"), [])
    m.add_encounter(_enc_data("Grib"), 1, [])
    return m


# ── content-sha + session-seed keying (fields exist and round-trip) ─


def test_manual_records_content_sha_and_session_seed():
    m = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        content_sha="sha-A",
        session_seed="seed-1",
    )
    assert m.content_sha == "sha-A"
    assert m.session_seed == "seed-1"


def test_content_sha_and_session_seed_survive_json_round_trip():
    m = _populated(content_sha="sha-A", session_seed="seed-1")
    restored = MonsterManual.model_validate_json(m.model_dump_json())
    assert restored.content_sha == "sha-A"
    assert restored.session_seed == "seed-1"
    assert len(restored.npcs) == 1


# ── discard-on-mismatch REPLACES purge-repair ─────────────────────


def test_reconcile_content_discards_whole_pool_on_content_sha_mismatch():
    m = _populated(content_sha="sha-A", session_seed="seed-1")
    assert m.npcs and m.encounters  # populated before reconcile

    result = m.reconcile_content(content_sha="sha-B", session_seed="seed-1")

    # discard-on-mismatch: the ENTIRE pool is dropped, not a name-matched subset.
    assert m.npcs == []
    assert m.encounters == []
    # ...and the manual is re-stamped to the new content version.
    assert m.content_sha == "sha-B"
    # reconcile reports that it discarded (truthy result carries the forensics).
    assert result is not None


def test_reconcile_content_does_not_discard_on_session_seed_change_alone():
    # CONTENT is the only staleness axis: a new session (new seed) with the SAME
    # content REUSES the pool rather than emptying it. session_seed is recorded
    # for attribution (refreshed) but never triggers a discard — making session
    # identity a discard key emptied valid pools on every new session (a
    # regression the design deliberately avoids; accumulation is bounded by the
    # caps, not by nuking the pool each session).
    m = _populated(content_sha="sha-A", session_seed="seed-1")
    npcs_before = list(m.npcs)
    encs_before = list(m.encounters)

    result = m.reconcile_content(content_sha="sha-A", session_seed="seed-2")

    assert m.npcs == npcs_before  # pool preserved across the session change
    assert m.encounters == encs_before
    assert m.session_seed == "seed-2"  # attribution refreshed
    assert result is None  # no discard


def test_reconcile_content_preserves_pool_on_full_match():
    m = _populated(content_sha="sha-A", session_seed="seed-1")
    npcs_before = list(m.npcs)
    encs_before = list(m.encounters)

    result = m.reconcile_content(content_sha="sha-A", session_seed="seed-1")

    # Both keys match — nothing is discarded (this is the cross-turn reuse path).
    assert m.npcs == npcs_before
    assert m.encounters == encs_before
    assert result is None


def test_reconcile_content_reports_authored_deletion_count():
    # V3 forensic: when a discard drops previously-inserted AUTHORED NPCs, the
    # result must report how many — this is the "what deleted beneath_sunden's 4
    # authored NPCs" detector the spec asks for.
    m = MonsterManual(
        genre="caverns_and_claudes",
        world="beneath_sunden",
        content_sha="sha-A",
        session_seed="seed-1",
    )
    m.add_npc(_npc("Warden Molgrath"), [], authored=True)
    m.add_npc(_npc("Sister Vane"), [], authored=True)
    m.add_npc(_npc("generated-walkon"), [])

    result = m.reconcile_content(content_sha="sha-B", session_seed="seed-1")

    assert result is not None
    assert result.npcs_discarded == 3
    assert result.authored_discarded == 2


# ── seed/purge idempotence — no livelock ──────────────────────────


def test_reconcile_content_is_idempotent_once_restamped():
    # The beneath_sunden livelock was two writers discarding+reseeding each
    # other. After a discard re-stamps the manual to the current content, a
    # SECOND reconcile against the SAME content must NOT discard again.
    m = _populated(content_sha="sha-A", session_seed="seed-1")

    first = m.reconcile_content(content_sha="sha-B", session_seed="seed-1")
    assert first is not None  # first sees stale content -> discard
    assert m.npcs == []

    # Simulate the re-seed that follows a discard.
    m.add_npc(_npc("fresh-seed"), [])

    second = m.reconcile_content(content_sha="sha-B", session_seed="seed-1")
    assert second is None  # same content key -> stable, no re-discard (no livelock)
    assert len(m.npcs) == 1  # the freshly-seeded entry survives


# ── accumulation cap (no 1,153-NPC runaway) ───────────────────────


def test_add_npc_enforces_accumulation_cap():
    cap = getattr(mm_mod, "MAX_MANUAL_NPCS", None)
    assert cap is not None, "expected a MAX_MANUAL_NPCS accumulation cap constant"

    m = MonsterManual(genre="g", world="w")
    # Fixed-width names so no two are substrings of each other — the fuzzy
    # find_npc_by_name dedup would otherwise collapse "walkon-1"/"walkon-10" and
    # mask the cap. Distinct same-length names exercise the cap for real.
    for i in range(cap + 50):
        m.add_npc(_npc(f"walkon-{i:04d}"), [])

    assert len(m.npcs) <= cap


def test_add_encounter_enforces_accumulation_cap():
    cap = getattr(mm_mod, "MAX_MANUAL_ENCOUNTERS", None)
    assert cap is not None, "expected a MAX_MANUAL_ENCOUNTERS accumulation cap constant"

    m = MonsterManual(genre="g", world="w")
    for i in range(cap + 50):
        m.add_encounter(_enc_data(f"beast-{i}"), 1, [])

    assert len(m.encounters) <= cap


def test_accumulation_cap_does_not_evict_authored_npcs():
    # The cap protects V3: filling the pool with generated walk-ons must never
    # crowd out an authored NPC. An authored insert at cap evicts a generated
    # entry instead of being silently refused.
    cap = getattr(mm_mod, "MAX_MANUAL_NPCS", None)
    assert cap is not None, "expected a MAX_MANUAL_NPCS accumulation cap constant"

    m = MonsterManual(genre="g", world="w")
    # Fixed-width names (see test_add_npc_enforces_accumulation_cap) so the fuzzy
    # dedup doesn't collapse them — the pool genuinely fills to the cap.
    for i in range(cap):
        m.add_npc(_npc(f"walkon-{i:04d}"), [])
    assert len(m.npcs) == cap  # pool is full of generated walk-ons

    m.add_npc(_npc("Named Boss"), [], authored=True)

    assert m.find_npc_by_exact_name("Named Boss") is not None
    assert len(m.npcs) <= cap  # still bounded — a generated walk-on was evicted


def test_accumulation_cap_eviction_prefers_available_over_active():
    # An ACTIVE walk-on is anchored to a location and projected into narration —
    # evicting it mid-scene vanishes an NPC the players may be engaging (Diamonds
    # and Coal: an engaged walk-on is a diamond in the making). The authored
    # insert must evict an AVAILABLE (never-activated) walk-on instead, even when
    # the ACTIVE one is oldest.
    cap = getattr(mm_mod, "MAX_MANUAL_NPCS", None)
    assert cap is not None, "expected a MAX_MANUAL_NPCS accumulation cap constant"

    m = MonsterManual(genre="g", world="w")
    for i in range(cap):
        m.add_npc(_npc(f"walkon-{i:04d}"), [])
    # The OLDEST walk-on is in play — the naive evict-oldest choice.
    m.mark_active("walkon-0000", "The Hub")

    m.add_npc(_npc("Named Boss"), [], authored=True)

    assert m.find_npc_by_exact_name("Named Boss") is not None
    assert m.find_npc_by_exact_name("walkon-0000") is not None  # in-play NPC survives
    assert m.find_npc_by_exact_name("walkon-0001") is None  # oldest AVAILABLE evicted
    assert len(m.npcs) <= cap


# ── fail-loud empty world-slug keys (No Silent Fallbacks) ──────────


@pytest.mark.parametrize("blank", ["", "   "])
def test_file_path_rejects_blank_world(blank: str):
    # The `caverns_and_claudes_.json` bug: a session bound pre-world-resolution
    # keyed the manual on an empty world. The key must fail loud, never mint a
    # `<genre>_.json` file.
    with pytest.raises(ValueError):
        MonsterManual._file_path("caverns_and_claudes", blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_file_path_rejects_blank_genre(blank: str):
    with pytest.raises(ValueError):
        MonsterManual._file_path(blank, "beneath_sunden")


def test_load_rejects_blank_world():
    with pytest.raises(ValueError):
        MonsterManual.load("caverns_and_claudes", "")


def test_save_rejects_blank_world(tmp_path, monkeypatch):
    # A manual that somehow carries a blank world must refuse to persist rather
    # than write `<genre>_.json` to the shared cache dir.
    monkeypatch.setattr(MonsterManual, "_manuals_dir", staticmethod(lambda: tmp_path))
    m = MonsterManual(genre="caverns_and_claudes", world="")
    with pytest.raises(ValueError):
        m.save()
    # And nothing was written under the redirected cache dir.
    assert list(tmp_path.glob("*.json")) == []
