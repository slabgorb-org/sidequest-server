"""Story 158-54 RED — the dice path routes a mutation-marked beat into the AWN use spine.

THE GAP (measured 2026-07-03, this story's diagnosis pass): ``awn.mutation.used``
never fires in a LIVE mutant_wasteland combat. The engine is green — use_ops,
the awn.* spans, the freeplay magic_working route, the chargen seams all pass —
but the ONLY in-combat route ever built was the narrator-apply beat path
(``_apply_mutation_beat``, story 102-7), and ADR-143 de-nativization now drops
ALL stray narrator beat selections in a live WN-family ``hp_depletion`` combat
(``wn_combat_beat_dropped_engine_owns_round``; ``awn`` IS WN-family per
``is_live_wn_combat``). Meanwhile the dice path — the path that OWNS a live WN
round — has no mutation route at all:

  * ``DiceThrowPayload`` carries ``spell_id`` but no ``mutation_id``
    (``extra=forbid`` — probed 2026-07-03, ValidationError)
  * ``WnSealedCommit`` carries ``spell_id`` but no ``mutation_id``
  * ``_apply_committed_player_beat`` has the WWN cast spine + strike channels,
    zero mutation branches

So a player committing the pack's mutation beat (``mutant_ability``, the
``mutation_resolution``-marked "Use Mutation" texture) resolves as a BARE WIS
STRIKE: generic damage, no use_ops, no Strain cost, no usage tick, no span.
The pack's marquee mechanic is improv wearing dice noise — the exact
Illusionism the OTEL doctrine exists to catch.

Contract pinned here, on the REAL mutant_wasteland pack (ruleset: awn), through
the production dice seam — the 102-2 cast-spine contract retold for mutations
(one use_ops implementation, two entry points; do NOT exempt the beat from the
ADR-143 drop — bind the ruleset, don't balance it):

  1. DICE_THROW with the mutation-marked beat + ``mutation_id`` reaches the
     SAME use_ops spine the freeplay/narrator paths use: ``awn.mutation.used``
     fires with actor + mutation_id, the Strain cost lands on the PC's pool.
  2. The use is NOT gated on the d20 face — AWN mutation powers fire and the
     TARGET saves (use_ops: "the power fires, the target saves"). A face of 1
     still uses the mutation. A face-gated use is a generic WIS throw wearing
     a robe.
  3. Malformed mutation commits are LOUD, TYPED rejections
     (``DiceDispatchError``, the dice-path idiom mirroring 102-2's cast
     guards): a marked-beat commit with no mutation_id (today's silent
     bare-strike bug shape), a mutation_id riding an unmarked beat, a
     mutation_id on a non-AWN ruleset. No silent generic-strike resolution
     may remain possible for mutation beats.
  4. Economy refusals are valid requests the spine refuses-but-records
     (``awn.mutation.refused``, no Strain paid, no raise) — parity with the
     freeplay/narrator route refusals (102-3 doctrine: refusal IS engagement).

P2-4 discipline: wiring assertions only (spans fire, pools move, rejections
raise) — never catalog content details. Skips cleanly when sidequest-content
is not on disk.

Determinism: rng pinned via the stdlib ``random`` module object shared by
every importer (``random.randint``) — covers initiative, saves, damage dice,
and opponent reprisal, whichever module rolls.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

_GENRE = "mutant_wasteland"
_SPAN_USED = "awn.mutation.used"
_SPAN_REFUSED = "awn.mutation.refused"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


pytestmark = pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")


def _load_pack(genre: str = _GENRE):
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(genre))
    except PackNotFound as exc:  # pragma: no cover - environment guard
        pytest.skip(str(exc))


def _costed_mutation(pack):
    """A Strain-costed, usable positive from the REAL catalog (P2-4: one must
    exist; nothing else about content is pinned)."""
    assert pack.mutations is not None, "mutant_wasteland must ship mutations.yaml (102-7)"
    costed = next((m for m in pack.mutations.positives if m.strain_cost > 0), None)
    assert costed is not None, (
        "the catalog must offer at least one Strain-costed positive mutation "
        "(the cost economy is the crunch)"
    )
    return costed


def _mutation_beat(pack):
    combat = next((c for c in pack.rules.confrontations if c.category == "combat"), None)
    assert combat is not None, "mutant_wasteland must declare a combat confrontation"
    beat = next((b for b in combat.beats if getattr(b, "mutation_resolution", False)), None)
    assert beat is not None, (
        "the combat confrontation must carry a mutation_resolution beat (102-7); "
        f"beats present: {[b.id for b in combat.beats]}"
    )
    return beat


def _unmarked_beat(pack):
    combat = next(c for c in pack.rules.confrontations if c.category == "combat")
    beat = next((b for b in combat.beats if not getattr(b, "mutation_resolution", False)), None)
    assert beat is not None, "fixture premise: the combat def needs at least one unmarked beat"
    return beat


def _make_mutant(pack, name: str, *, positive_ids: list[str]):
    """A mutant-class PC with a hydrated CharacterMutationState.

    Built directly (not through chargen) — chargen seeding is proven green by
    test_102_7_mutant_wasteland_mutations_live.py; this suite tests the
    dispatch seam, which reads snapshot.mutation_state + core.system_strain.
    """
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
    from sidequest.game.system_strain import SystemStrainPool

    stats = {n: 10 for n in pack.rules.ability_score_names}
    core = CreatureCore(
        name=name,
        description="A mutant of the flickering wastes.",
        personality="watchful",
        inventory=Inventory(),
        hp=HpPool(current=10, max=10, base_max=10),
        armor_class=12,
        system_strain=SystemStrainPool(current=0, max=10),
    )
    char_class = (
        pack.mutations.mp_economy.mutant_classes[0] if pack.mutations is not None else "Mutant"
    )
    pc = Character(
        core=core,
        char_class=char_class,
        race="Mutant Human",
        backstory="Born under the fallout sky.",
        stats=stats,
    )
    return pc, stats


def _seat_combat(pack, pc, pc_name: str, opponent: str, *, genre: str = _GENRE):
    """Seat the real combat confrontation via the production seam, initiative
    pinned (102-4: the WN walk resolves in PERSISTED order; the seam's real
    1d8+DEX roll must never flip the choreography)."""
    from sidequest.agents.orchestrator import NpcMention
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager
    from sidequest.protocol.models import InitiativeEntry
    from sidequest.server.dispatch.encounter_lifecycle import (
        instantiate_encounter_from_trigger,
    )

    snap = GameSnapshot(
        genre_slug=genre,
        world_slug="flickering_reach" if genre == _GENRE else "test_world",
        turn_manager=TurnManager(interaction=2),
    )
    snap.characters.append(pc)
    snap.character_locations[pc_name] = "The Glass Flats"

    enc = instantiate_encounter_from_trigger(
        snapshot=snap,
        pack=pack,
        encounter_type="combat",
        player_name=pc_name,
        npcs_present=[NpcMention(name=opponent, side="opponent")],
        genre_slug=genre,
    )
    assert enc is not None, "seating the real combat confrontation must succeed"
    snap.encounter = enc
    enc.initiative = [
        InitiativeEntry(token_id=pc_name, value=9),
        InitiativeEntry(token_id=opponent, value=2),
    ]
    return snap, enc


def _dispatch(
    *,
    pack,
    snap,
    enc,
    pc_name: str,
    stats: dict[str, int],
    beat_id: str,
    mutation_id: str | None,
    face: int = 20,
    genre: str = _GENRE,
):
    """Drive the production dice seam with a beat commit.

    ``mutation_id`` rides the DiceThrowPayload — the exact transport
    ``spell_id`` uses for the WWN cast route (102-2): the overlay's mutation
    picker names WHICH owned mutation manifests, because a mutant may own
    several and the beat is the generic "Use Mutation" texture.
    """
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    payload_kwargs: dict[str, object] = {
        "request_id": "req-158-54",
        "throw_params": ThrowParams(
            velocity=(0.0, 5.0, -2.0),
            angular=(1.0, 1.0, 1.0),
            position=(0.5, 0.5),
        ),
        "face": [face],
        "beat_id": beat_id,
    }
    if mutation_id is not None:
        payload_kwargs["mutation_id"] = mutation_id

    broadcasts: list[object] = []
    return dispatch_dice_throw(
        payload=DiceThrowPayload(**payload_kwargs),  # type: ignore[arg-type]
        rolling_player_id="player-rux",
        character_name=pc_name,
        character_stats=dict(stats),
        encounter=enc,
        pack=pack,
        genre_slug=genre,
        session_id="mw-158-54-session",
        round_number=1,
        room_broadcast=broadcasts.append,
        snapshot=snap,
    )


def _spans(otel_capture, name: str) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == name]


def _strain_current(pc) -> int:
    """Fail-loud Strain read — the fixture always seeds a SystemStrainPool."""
    pool = pc.core.system_strain
    assert pool is not None, "fixture premise: the PC carries a SystemStrainPool"
    return pool.current


def _hydrate_mutation_state(snap, pc_name: str, positive_ids: list[str]) -> None:
    from sidequest.mutation.state import CharacterMutationState, MutationState

    snap.mutation_state = MutationState(
        characters={pc_name: CharacterMutationState(mp_remaining=0, positive_ids=positive_ids)}
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1: the mutation beat fires the use_ops spine through the dice path
# ─────────────────────────────────────────────────────────────────────────────


def test_mutation_beat_with_mutation_id_fires_use_spine(otel_capture, monkeypatch):
    """DICE_THROW(mutation beat, mutation_id) in a LIVE AWN combat must reach
    use_ops: awn.mutation.used fires with actor + mutation_id, and the Strain
    cost lands on the PC's pool. Today the commit resolves as a bare WIS
    strike — zero awn.* spans, zero Strain — the measured 158-54 gap."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    assert pack.rules.ruleset == "awn", "mutant_wasteland must stay bound ruleset: awn"
    costed = _costed_mutation(pack)
    beat = _mutation_beat(pack)

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])
    strain_before = _strain_current(pc)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=costed.id,
        face=20,
    )

    used = _spans(otel_capture, _SPAN_USED)
    assert len(used) == 1, (
        "a mutation-marked beat committed through the production dice seam in "
        f"a live AWN combat must emit exactly one {_SPAN_USED}; got {len(used)} "
        "— the marquee mechanic resolved as a bare strike (improv)"
    )
    attrs = used[0].attributes or {}
    assert attrs.get("mutation_id") == costed.id, "the span must name WHICH mutation"
    assert attrs.get("actor") == "Rux", "the span must name WHO manifested it"
    assert not _spans(otel_capture, _SPAN_REFUSED), (
        "an owned, affordable mutation must not also record a refusal"
    )
    assert _strain_current(pc) == strain_before + costed.strain_cost, (
        "the Strain cost must land on the PC's pool through the dice path; "
        f"before={strain_before} cost={costed.strain_cost} "
        f"after={_strain_current(pc)}"
    )


def test_mutation_use_is_not_gated_on_the_d20_face(otel_capture, monkeypatch):
    """face=1 must still use the mutation: awn.mutation.used fires and the
    Strain cost is paid. AWN mutation powers fire and the TARGET saves
    (use_ops doctrine) — gating the use on the commit throw would be the
    generic WIS resolution wearing a robe (the 102-2 cast parity)."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    costed = _costed_mutation(pack)
    beat = _mutation_beat(pack)

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])
    strain_before = _strain_current(pc)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=costed.id,
        face=1,
    )

    used = _spans(otel_capture, _SPAN_USED)
    assert len(used) == 1, (
        f"a face of 1 must still fire {_SPAN_USED} (the power fires, the "
        f"target saves); got {len(used)} — a face-gated use is a generic "
        "stat throw, not a mutation"
    )
    assert _strain_current(pc) == strain_before + costed.strain_cost, (
        "the Strain cost is paid on use, not on a winning face"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2: malformed mutation commits are LOUD (No Silent Fallbacks)
# ─────────────────────────────────────────────────────────────────────────────


def test_mutation_beat_commit_without_mutation_id_is_loud(otel_capture, monkeypatch):
    """A mutation-marked beat commit with NO mutation_id is today's bug shape
    (the silent bare-strike resolution). It must become a LOUD, TYPED
    rejection — DiceDispatchError, zero state change — mirroring the 102-2
    'cast_spell commit missing spell_id' guard. Today this dispatch SUCCEEDS
    as a generic WIS throw, which is exactly the disease."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    costed = _costed_mutation(pack)
    beat = _mutation_beat(pack)

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])
    strain_before = _strain_current(pc)

    with pytest.raises(DiceDispatchError, match="mutation_id"):
        _dispatch(
            pack=pack,
            snap=snap,
            enc=enc,
            pc_name="Rux",
            stats=stats,
            beat_id=beat.id,
            mutation_id=None,
        )

    assert _strain_current(pc) == strain_before, (
        "a rejected commit must change no state (validation precedes mutation)"
    )
    assert not _spans(otel_capture, _SPAN_USED), "a rejected commit must not record a use"


def test_mutation_id_on_unmarked_beat_is_loud(monkeypatch):
    """A mutation_id riding a beat WITHOUT the mutation_resolution marker is a
    client bug — silently ignoring a mechanical request field is the exact
    silent-fallback failure mode (mirror: spell_id on a non-cast beat)."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    costed = _costed_mutation(pack)
    unmarked = _unmarked_beat(pack)

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])

    with pytest.raises(DiceDispatchError, match="mutation_id"):
        _dispatch(
            pack=pack,
            snap=snap,
            enc=enc,
            pc_name="Rux",
            stats=stats,
            beat_id=unmarked.id,
            mutation_id=costed.id,
        )


def test_mutation_beat_on_opposed_check_confrontation_is_loud(otel_capture, monkeypatch):
    """Review rework (158-54 round 1): the cast guard's opposed_check clause
    (dice.py ~500, added in 102-2's own review round) must have a mutation
    twin. The mutation spine runs only on the non-opposed branches — an
    opposed_check cdef sets ``opposed_pending`` and defers beat application
    to narration_apply's opposed branch (plain apply_beat, both sides), so a
    VALID owned-mutation commit would be SILENTLY skipped: no
    awn.mutation.used/.refused span, no Strain, bare stat throw — the exact
    pre-158-54 disease reopened for one gate combination. Probed 2026-07-03:
    dispatch returns opposed_pending=True, strain unchanged, no raise.

    No shipped content authors opposed_check on the AWN pack (rules.yaml
    declares "zero opposed_check now") — but content authors add
    confrontations without touching engine code, so content-unreachability
    is not a guard. Reject loudly until a story defines opposed-mutation
    semantics (the 102-2 precedent, verbatim)."""
    from sidequest.genre.models.rules import ResolutionMode
    from sidequest.server.dispatch.dice import DiceDispatchError

    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    costed = _costed_mutation(pack)
    beat = _mutation_beat(pack)

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])

    # The misconfigured-homebrew scenario: flip the seated cdef to
    # opposed_check AFTER seating (ConfrontationDef is not frozen; dispatch
    # re-resolves this same freshly-loaded, unshared object — load_genre_pack
    # is uncached, so the mutation cannot leak into sibling tests).
    combat = next(c for c in pack.rules.confrontations if c.category == "combat")
    combat.resolution_mode = ResolutionMode.opposed_check

    strain_before = _strain_current(pc)
    with pytest.raises(DiceDispatchError, match="opposed_check"):
        _dispatch(
            pack=pack,
            snap=snap,
            enc=enc,
            pc_name="Rux",
            stats=stats,
            beat_id=beat.id,
            mutation_id=costed.id,
        )

    assert _strain_current(pc) == strain_before, (
        "a rejected opposed-mutation commit must change no state (validation precedes mutation)"
    )
    assert not _spans(otel_capture, _SPAN_USED), "a rejected commit must not record a use"


def test_mutation_id_on_non_awn_ruleset_is_loud(monkeypatch):
    """A mutation_id on a WWN pack's commit must be rejected loudly — the
    mutation route is AWN-gated exactly as the cast route is WWN-gated. No
    phantom cross-ruleset engine engagement (ADR-143 cuts both ways)."""
    from sidequest.server.dispatch.dice import DiceDispatchError

    monkeypatch.setattr("random.randint", lambda a, b: a)
    hm = _load_pack("heavy_metal")
    assert hm.rules.ruleset == "wwn", "fixture premise: heavy_metal binds wwn"

    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, HpPool, Inventory

    stats = {n: 10 for n in hm.rules.ability_score_names}
    core = CreatureCore(
        name="Vesska",
        description="A blade of the reliquary roads.",
        personality="cold",
        inventory=Inventory(),
        hp=HpPool(current=12, max=12, base_max=12),
    )
    pc = Character(core=core, char_class="Warrior", race="Human", backstory="—", stats=stats)
    snap, enc = _seat_combat(hm, pc, "Vesska", "Furnace Thrall", genre="heavy_metal")

    with pytest.raises(DiceDispatchError, match="mutation_id"):
        _dispatch(
            pack=hm,
            snap=snap,
            enc=enc,
            pc_name="Vesska",
            stats=stats,
            beat_id="attack",
            mutation_id="structure/oversized_limb",
            genre="heavy_metal",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3: economy refusals are engagement, not silence (102-3 doctrine)
# ─────────────────────────────────────────────────────────────────────────────


def test_unowned_mutation_refuses_and_pays_no_strain(otel_capture, monkeypatch):
    """A commit naming a real-catalog mutation the PC does NOT own is a valid
    request the spine refuses-but-records: awn.mutation.refused (not_owned),
    zero awn.mutation.used, no Strain paid — parity with the freeplay and
    narrator-route refusals. Refusal IS engagement; the GM panel must see it."""
    monkeypatch.setattr("random.randint", lambda a, b: a)
    pack = _load_pack()
    costed = _costed_mutation(pack)
    assert pack.mutations is not None
    unowned = next((m for m in pack.mutations.positives if m.id != costed.id), None)
    assert unowned is not None, "fixture premise: the catalog offers a second positive"

    pc, stats = _make_mutant(pack, "Rux", positive_ids=[costed.id])
    snap, enc = _seat_combat(pack, pc, "Rux", "Raider Scav")
    _hydrate_mutation_state(snap, "Rux", [costed.id])
    beat = _mutation_beat(pack)
    strain_before = _strain_current(pc)

    _dispatch(
        pack=pack,
        snap=snap,
        enc=enc,
        pc_name="Rux",
        stats=stats,
        beat_id=beat.id,
        mutation_id=unowned.id,
    )

    refused = _spans(otel_capture, _SPAN_REFUSED)
    assert len(refused) == 1, (
        f"an unowned mutation commit must record exactly one {_SPAN_REFUSED}; "
        f"got {len(refused)} — silence is indistinguishable from improv"
    )
    attrs = refused[0].attributes or {}
    assert "not_owned" in str(attrs.get("reason", "")), (
        f"the refusal must carry the not_owned reason; got {attrs.get('reason')!r}"
    )
    assert not _spans(otel_capture, _SPAN_USED), "a refused use must not also record a use"
    assert _strain_current(pc) == strain_before, (
        "a refused use must pay no Strain (refusal precedes cost)"
    )
