"""Story 153-1 (RED) — CWN opponent reprisal deals 0 HP: no SRD unarmed floor.

PLAYTEST BUG (road_warrior/the_circuit, CWN — sq-playtest 150-20, session
2026-06-21-the_circuit-e214aec0): the seated opponent's reprisal to-hit SUCCEEDS
but the beat carries no resolvable damage, so the player takes 0 HP. The server
logs ``dice.opponent_reprisal_damage_spec_missing`` and the player's HP stayed
10/10 across a 5+ exchange fight — combat is unlosable.

ROOT CAUSE (confirmed read-only):
``resolve_damage_spec_from_beat_and_actor`` resolves a strike beat's DamageSpec
through a 4-priority cascade whose only unarmed floor is **priority 4 =
``pack.rules.unarmed_damage``**. NO content pack authors ``unarmed_damage``
(grep: zero hits across genre_packs/*/rules.yaml), so a weaponless WN opponent —
no ``beat.damage_override``, no inventory weapon, no genre floor — resolves to
``None`` and the reprisal HIT path skips damage entirely. SWN survives this only
because its ``combat`` cdef *authors* ``opponent_damage`` (the
``test_opponent_reprisal_e2e`` weaponless case); road_warrior's CWN ``combat``
cdef ("Roadside Firefight") does NOT, so a weaponless CWN opponent deals 0.

``wn_action_beat``'s own docstring promises a weaponless WN attack resolves
"from the actor's inventory **or the genre unarmed floor**" — but that floor is
never populated, making the promise hollow.

FIX CONTRACT (server, doctrine — SOUL "Bind the Ruleset, Don't Balance It" /
"Defer to SRD for mechanics"): a Without Number ruleset must supply its **SRD
unarmed-strike floor** as a last resort so a weaponless WN opponent's landed hit
always ablates HP — not a per-pack authoring burden, not a hand-balanced number.
The floor is the LAST resort: an equipped weapon (priority 1–3) and an authored
``pack.rules.unarmed_damage`` (priority 4) must both still win.

These tests pin the BEHAVIOR (a weaponless WN strike resolves real positive
damage; the reprisal ablates the player's HP and emits the damage-resolved
watcher op, never ``opponent_damage_spec_missing``), not the Dev's chosen die.

``otel_capture`` / content-skip mirror ``test_opponent_reprisal_e2e``.
"""

from __future__ import annotations

import random
import re

import pytest

from sidequest.game.beat_filter import WN_ATTACK_BEAT_ID, wn_action_beat
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.ruleset.registry import get_ruleset_module
from sidequest.genre.models.inventory import DamageSpec
from tests._helpers.genre_paths import PackNotFound, find_pack_path

# Every Without Number sibling shares the WN-core combat path (ADR-142): the SRD
# unarmed floor must live on the WN core so all four inherit it.
WN_SLUGS = ["swn", "cwn", "wwn", "awn"]

# Watcher ops the reprisal DAMAGE seam emits (dice.py). The bug's signature is
# the absence of the first and the presence of... nothing (a silent 0-damage hit).
OP_DAMAGE_RESOLVED = "opponent_damage_roll_resolved"
OP_DAMAGE_MISSING = "opponent_damage_spec_missing"


def _min_damage(spec: DamageSpec) -> int:
    """Minimum guaranteed damage of an ``NdM`` spec (all-ones roll) + bonus."""
    m = re.fullmatch(r"(\d+)d(\d+)", spec.dice.strip())
    assert m is not None, f"damage spec dice {spec.dice!r} is not NdM notation"
    return int(m.group(1)) + spec.bonus


def _weaponless_core(name: str = "Scrap Raider") -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A road scav with empty hands.",
        personality="feral",
        inventory=Inventory(items=[]),
        hp={"current": 8, "max": 8, "base_max": 8},
        armor_class=13,
    )


# ---------------------------------------------------------------------------
# RED — the ruleset seam: a weaponless WN strike must resolve the SRD floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_wn_weaponless_strike_resolves_srd_unarmed_floor(slug):
    """RED: ``<wn>.resolve_damage`` for a plain WN attack by a weaponless actor —
    with NO pack-level unarmed floor (pack=None) — must return a real positive
    DamageSpec (the ruleset's SRD unarmed strike), not ``None``.

    Today every WN sibling returns ``None`` here (cascade falls through priority
    1–4), which is the 0-damage reprisal. The floor is intrinsic to the ruleset,
    so it must fire even with no pack — a pack can never be relied on to author
    it (none do)."""
    module = get_ruleset_module(slug)
    beat = wn_action_beat(WN_ATTACK_BEAT_ID)  # the production weaponless strike beat
    assert beat.damage_override is None, "precondition: the WN attack beat carries no override"

    spec = module.resolve_damage(
        beat=beat, actor_core=_weaponless_core(), pack=None, world_slug=None
    )

    assert spec is not None, (
        f"{slug}: a weaponless WN strike must fall back to the SRD unarmed floor, "
        f"not None — None is the 0-damage reprisal (dice.opponent_reprisal_"
        f"damage_spec_missing). The floor is the ruleset's, not the pack's."
    )
    assert _min_damage(spec) >= 1, (
        f"{slug}: the unarmed floor must deal real positive damage so a landed "
        f"hit ablates HP; got spec={spec!r} (min {_min_damage(spec)})"
    )


@pytest.mark.parametrize("slug", WN_SLUGS)
def test_wn_armed_strike_uses_weapon_not_unarmed_floor(slug):
    """Regression guard: an EQUIPPED weapon (priority 2) must win over the new
    SRD floor — the floor never caps an armed actor. An actor carrying a 2d6
    weapon resolves 2d6, not the ~1d2 unarmed floor."""
    module = get_ruleset_module(slug)
    armed = CreatureCore(
        name="Gunhand",
        description="armed",
        personality="cold",
        inventory=Inventory(
            items=[{"id": "scrap_smg", "name": "Scrap SMG", "damage": {"dice": "2d6"}}]
        ),
        hp={"current": 10, "max": 10, "base_max": 10},
        armor_class=12,
    )
    spec = module.resolve_damage(
        beat=wn_action_beat(WN_ATTACK_BEAT_ID), actor_core=armed, pack=None, world_slug=None
    )
    assert spec is not None and spec.dice == "2d6", (
        f"{slug}: an equipped weapon must win over the unarmed floor; got {spec!r}"
    )


# ---------------------------------------------------------------------------
# Regression guard — a non-WN ruleset must NOT sprout an unarmed floor
# ---------------------------------------------------------------------------


def test_non_wn_ruleset_has_no_unarmed_floor():
    """The SRD floor is a WN-core mechanic. The vestigial ``dial`` ruleset must
    NOT gain one (a weaponless dial strike still resolves None) — the fix must
    not leak the floor into non-WN paths."""
    dial = get_ruleset_module("dial")
    spec = dial.resolve_damage(
        beat=wn_action_beat(WN_ATTACK_BEAT_ID), actor_core=_weaponless_core(), pack=None
    )
    assert spec is None, f"dial (non-WN) must not gain a WN SRD unarmed floor; got {spec!r}"


# ---------------------------------------------------------------------------
# Pack-backed regression guard — an authored pack floor still wins over the SRD
# ---------------------------------------------------------------------------


def _load_pack(slug: str):
    from sidequest.genre.loader import load_genre_pack

    try:
        path = find_pack_path(slug)
    except PackNotFound:
        return None
    return load_genre_pack(path)


def _combat_cdef(pack):
    return next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )


def test_authored_pack_unarmed_damage_wins_over_srd_floor(monkeypatch):
    """Regression guard: when a pack DOES author ``rules.unarmed_damage``
    (priority 4), it must win over the new SRD floor (priority 5). Pins the SRD
    floor as the LAST resort, not an override of authored content."""
    pack = _load_pack("road_warrior")
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    monkeypatch.setattr(pack.rules, "unarmed_damage", DamageSpec(dice="1d8"), raising=False)
    cwn = get_ruleset_module("cwn")
    spec = cwn.resolve_damage(
        beat=wn_action_beat(WN_ATTACK_BEAT_ID),
        actor_core=_weaponless_core(),
        pack=pack,
        world_slug=None,
    )
    assert spec is not None and spec.dice == "1d8", (
        f"an authored pack unarmed_damage (1d8) must win over the SRD floor; got {spec!r}"
    )


# ---------------------------------------------------------------------------
# RED — wiring: the real CWN reprisal must ablate the player's HP
# ---------------------------------------------------------------------------


class _MaxRng(random.Random):
    """An rng whose ``randint`` always returns the high bound — forces the
    opponent's d20 to a guaranteed hit so the test exercises the DAMAGE path
    deterministically (we are testing damage sourcing, not the to-hit roll)."""

    def randint(self, a, b):  # noqa: D401 - deterministic max roll
        return b


def _make_combat_snapshot(*, player_name: str, player_ac: int, opponent_name: str):
    from sidequest.game.character import Character
    from sidequest.game.session import GameSnapshot, Npc
    from sidequest.game.turn import TurnManager

    player_core = CreatureCore(
        name=player_name,
        description="Wheelman, on foot.",
        personality="wary",
        inventory=Inventory(items=[]),
        hp={"current": 12, "max": 12, "base_max": 12},
        armor_class=player_ac,
    )
    player = Character(core=player_core, char_class="Wheelman", race="Survivor", backstory="-")

    snap = GameSnapshot(
        genre_slug="road_warrior",
        world_slug="the_circuit",
        turn_manager=TurnManager(),
    )
    snap.characters.append(player)
    snap.npcs.append(Npc(core=_weaponless_core(opponent_name)))
    return snap


def _make_combat_encounter(player_name: str, opponent_name: str):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=player_name, role="combatant", side="player"),
            EncounterActor(name=opponent_name, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def test_cwn_weaponless_reprisal_ablates_player_hp(monkeypatch):
    """RED (the playtest symptom): a real CWN ``combat`` reprisal by a weaponless
    opponent, with no authored ``opponent_damage``, must ABLATE the player's HP
    on a landed hit and emit ``opponent_damage_roll_resolved`` — never
    ``opponent_damage_spec_missing``.

    Today: ``resolve_damage`` returns None → the hit lands but deals 0 HP, the
    ``opponent_damage_spec_missing`` warning fires, and the player is unkillable.
    """
    import sidequest.server.dispatch.dice as dice_mod
    from sidequest.server.dispatch.dice import _resolve_opponent_reprisal

    pack = _load_pack("road_warrior")
    if pack is None:
        pytest.skip("sidequest-content not on disk in this checkout")

    cdef = _combat_cdef(pack)
    assert cdef is not None, (
        "road_warrior must expose a personal 'combat' (Roadside Firefight) cdef"
    )
    # Isolate the unarmed-floor path regardless of any future content authoring:
    # force the no-authored-opponent_damage shape the playtest hit.
    cdef = cdef.model_copy(update={"opponent_damage": None})

    player_name, opponent_name = "Riggs", "Scrap Raider"
    snap = _make_combat_snapshot(player_name=player_name, player_ac=2, opponent_name=opponent_name)
    enc = _make_combat_encounter(player_name, opponent_name)
    player_core = snap.find_creature_core(player_name)
    assert player_core is not None
    hp_before = player_core.hp.current

    captured: list[dict] = []
    monkeypatch.setattr(
        dice_mod,
        "_watcher_publish",
        lambda event_type, fields, **kw: captured.append({"event_type": event_type, **fields}),
    )

    _resolve_opponent_reprisal(
        encounter=enc,
        cdef=cdef,
        ruleset=get_ruleset_module("cwn"),
        pack=pack,
        snapshot=snap,
        player_name=player_name,
        session_id="153-1-session",
        round_number=1,
        rng=_MaxRng(),
    )

    ops = [e.get("op") for e in captured]
    # Precondition: the to-hit landed (else there's no damage path to test).
    attack = next((e for e in captured if e.get("op") == "opponent_attack_resolved"), None)
    assert attack is not None and attack.get("hit") is True, (
        f"precondition: the max-roll reprisal must HIT the AC-2 player; ops={ops}"
    )

    assert player_core.hp.current < hp_before, (
        f"a weaponless CWN opponent's landed reprisal must ablate the player's HP "
        f"via the SRD unarmed floor; before={hp_before} after={player_core.hp.current}"
    )
    assert OP_DAMAGE_RESOLVED in ops, (
        f"the reprisal must emit {OP_DAMAGE_RESOLVED!r} (GM-panel lie-detector); ops={ops}"
    )
    assert OP_DAMAGE_MISSING not in ops, (
        f"the reprisal must NOT emit {OP_DAMAGE_MISSING!r} — a weaponless WN opponent "
        f"now has the SRD unarmed floor; ops={ops}"
    )
