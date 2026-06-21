"""sq-playtest 2026-06-20 (150-9 wonderland, glenross) — the Fate attack→defend
exchange was OPAQUE to both the player and the GM panel:

  * FATE-CONFLICT-SEQUENCE-OPAQUE (HIGH, Keith-flagged): after an attack/defend the
    player got no legible "you attacked N → defender N → outcome" feedback — only
    narrator prose, which can omit or improvise the math.
  * FATE-ATTACK-RESOLUTION-UNOBSERVABLE (medium, OTEL): the resolution step emitted
    NO span carrying attacker_total vs defender_total vs shifts, so the GM panel
    could not tell "the NPC defended legitimately" (shifts <= 0) from "the shifts
    never committed". ``fate.harm.routed`` only fires on a LANDED hit (shifts >= 1),
    so a miss/tie/fully-absorbed attack left zero evidence.

Both share one root: ``_resolve_attack`` produced no structured output. The fix:

NEW-SPAN CONTRACT (the GM-panel lie detector):
  ``fate.attack.resolved`` — emitted once per resolved attack in
  ``run_fate_exchange`` for EVERY outcome (miss / tie / absorbed / taken_out /
  conceded). Attributes:
    field          = "attack_resolved"
    attacker       = the acting actor
    defender       = the attack's target
    track          = "physical" | "mental"
    attacker_total = the attacker's ladder total (4dF + skill + invoke)
    defender_total = the defender's rolled/recorded total (NPC dice stay hidden,
                     the TOTAL does not — ADR-148)
    shifts         = attacker_total - defender_total
    outcome        = "miss" | "tie" | "absorbed" | "taken_out" | "conceded"

NEW LEDGER CONTRACT (the player-facing legibility surface):
  ``run_fate_exchange`` writes one ``FateExchangeLine`` per resolved action onto
  ``encounter.fate_resolution_log`` (cleared + repopulated each exchange —
  last-exchange semantics). ``build_fate_state_payload`` projects it onto
  ``FateConflictEntry.last_exchange`` so the FATE_STATE the player surface renders
  carries the derived math without the narrator.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.fate_sheet import Aspect, FateSheet
from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.fate_projection import build_fate_state_payload
from sidequest.game.ruleset.fate_resolution import Opposition
from sidequest.game.session import GameSnapshot, Npc
from sidequest.protocol.fate import FateActionPayload
from sidequest.server.dispatch.fate_conflict import (
    dispatch_fate_action,
    run_fate_exchange,
    seal_fate_commit,
)
from tests._helpers.fate_fixtures import resolve_parked_defenses

_ATTACK_SPAN = "fate.attack.resolved"


class _FixedRng:
    """Deterministic stand-in for random.Random: every 4dF face is ``value``."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def choice(self, seq):
        return self._value


def _pc(name: str, skills: dict[str, int]) -> Character:
    core = CreatureCore(
        name=name, description="d", personality="p", fate_sheet=FateSheet(skills=skills)
    )
    return Character(core=core, char_class="Agent", race="Human", backstory="b")


def _npc(name: str, skills: dict[str, int], *, sheet: FateSheet | None = None) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            fate_sheet=sheet if sheet is not None else FateSheet(skills=skills),
        )
    )


def _enc(actors: list[EncounterActor], *, category: str = "combat") -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="duel",
        category=category,
        player_metric=EncounterMetric(name="p", threshold=10),
        opponent_metric=EncounterMetric(name="o", threshold=10),
        actors=actors,
    )


def _otel():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def _seal_attack(enc, module, attacker, skill_rating, target, *, skill="Fight"):
    outcome = module.resolve_action(
        skill_rating=skill_rating, opposition=Opposition(value=0, kind="active"), rng=_FixedRng(0)
    )
    seal_fate_commit(
        encounter=enc,
        actor=enc.find_actor(attacker),
        action="attack",
        skill=skill,
        target=target,
        ladder_total=outcome.ladder_total,
        dice=outcome.dice,
    )


def _hero_vs_thug(*, hero_fight: int = 4, thug_athletics: int = 1, thug_sheet=None):
    """Seated physical Fate conflict: Hero (PC) vs Thug (NPC). The NPC defends with
    Athletics, server-rolled at 4dF=0 → defender_total = thug_athletics."""
    module = get_ruleset_module("fate")
    enc = _enc(
        [
            EncounterActor(name="Hero", role="lead", side="player"),
            EncounterActor(name="Thug", role="foe", side="opponent"),
        ]
    )
    snap = GameSnapshot(
        genre_slug="fate_test", characters=[_pc("Hero", {"Fight": hero_fight})], encounter=enc
    )
    snap.npcs.append(_npc("Thug", {"Athletics": thug_athletics}, sheet=thug_sheet))
    return module, enc, snap


def _attack_span(exporter):
    spans = [s for s in exporter.get_finished_spans() if s.name == _ATTACK_SPAN]
    assert spans, f"no {_ATTACK_SPAN} span — the attack resolution is unobservable"
    return spans[0].attributes or {}


# --------------------------------------------------------------------------- #
# FATE-ATTACK-RESOLUTION-UNOBSERVABLE: the span fires for EVERY outcome        #
# --------------------------------------------------------------------------- #


def test_absorbed_attack_emits_attack_resolved_span_with_full_math():
    """A 3-shift hit the NPC absorbs: the span carries attacker 4 vs defender 1 →
    shifts 3 → outcome=absorbed. This is the math her stress track alone can't
    confirm (the playtest's '0/26 throughout' ambiguity)."""
    module, enc, snap = _hero_vs_thug(hero_fight=4, thug_athletics=1)
    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    attrs = _attack_span(exporter)
    assert attrs.get("attacker") == "Hero"
    assert attrs.get("defender") == "Thug"
    assert attrs.get("track") == "physical"
    assert attrs.get("attacker_total") == 4
    assert attrs.get("defender_total") == 1
    assert attrs.get("shifts") == 3
    assert attrs.get("outcome") == "absorbed"


def test_missed_attack_still_emits_attack_resolved_span():
    """The bug's core gap: a MISS (shifts <= 0) emits NO fate.harm.routed today, so
    the GM panel sees nothing. fate.attack.resolved MUST fire so 'NPC defended' is
    distinguishable from 'shifts never committed'."""
    module, enc, snap = _hero_vs_thug(hero_fight=0, thug_athletics=3)
    _seal_attack(enc, module, "Hero", 0, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    attrs = _attack_span(exporter)
    assert attrs.get("attacker_total") == 0
    assert attrs.get("defender_total") == 3
    assert attrs.get("shifts") == -3
    assert attrs.get("outcome") == "miss"
    # And the landed-hit span correctly did NOT fire (no harm on a miss).
    assert "fate.harm.routed" not in [s.name for s in exporter.get_finished_spans()]


def test_tied_attack_emits_attack_resolved_span_tie():
    """A tie (shifts == 0) grants the target a boost and lands no harm — still
    observable as outcome=tie."""
    module, enc, snap = _hero_vs_thug(hero_fight=1, thug_athletics=1)
    _seal_attack(enc, module, "Hero", 1, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    attrs = _attack_span(exporter)
    assert attrs.get("shifts") == 0
    assert attrs.get("outcome") == "tie"


def test_taken_out_attack_emits_attack_resolved_span_taken_out():
    """A hit that exceeds the target's capacity: outcome=taken_out, with the full
    attacker/defender math still present."""
    depleted = FateSheet(skills={"Athletics": 1})
    for b in depleted.stress["physical"].boxes:
        b.checked = True
    for c in depleted.consequences:
        c.aspect = Aspect(text="old wound", kind="consequence", free_invokes=0)
    module, enc, snap = _hero_vs_thug(hero_fight=4, thug_athletics=1, thug_sheet=depleted)
    _seal_attack(enc, module, "Hero", 4, "Thug")
    exporter, tracer = _otel()
    run_fate_exchange(
        encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0), _tracer=tracer
    )

    attrs = _attack_span(exporter)
    assert attrs.get("outcome") == "taken_out"
    assert attrs.get("shifts") == 3


def test_attack_resolved_span_is_routed_for_the_gm_panel():
    """The span must be registered in SPAN_ROUTES (component='fate') so the watcher /
    GM panel surfaces it as a typed state_transition, not just the generic fan-out."""
    from sidequest.telemetry.spans._core import SPAN_ROUTES

    assert _ATTACK_SPAN in SPAN_ROUTES
    route = SPAN_ROUTES[_ATTACK_SPAN]
    assert route.component == "fate"
    extracted = route.extract(
        type(
            "S",
            (),
            {
                "attributes": {
                    "attacker": "Hero",
                    "defender": "Thug",
                    "track": "physical",
                    "attacker_total": 4,
                    "defender_total": 1,
                    "shifts": 3,
                    "outcome": "absorbed",
                }
            },
        )()
    )
    assert extracted["attacker"] == "Hero"
    assert extracted["shifts"] == 3
    assert extracted["outcome"] == "absorbed"


# --------------------------------------------------------------------------- #
# FATE-CONFLICT-SEQUENCE-OPAQUE: the player-facing resolution ledger           #
# --------------------------------------------------------------------------- #


def test_run_fate_exchange_records_resolution_ledger_line():
    """The exchange walk writes a structured line per resolved action onto
    encounter.fate_resolution_log so the math survives to the FATE_STATE projection
    (not just narrator prose). The walk seats the NPC's counter too, so a one-PC
    attack produces BOTH directions — exactly the legibility the bug wants. We assert
    on the PC's attack line. (Hero gets Athletics so the NPC counter misses and Hero
    survives, keeping the conflict active.)"""
    module, enc, snap = _hero_vs_thug(hero_fight=4, thug_athletics=1)
    snap.characters[0].core.fate_sheet.skills["Athletics"] = 3
    _seal_attack(enc, module, "Hero", 4, "Thug")
    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    hero_lines = [ln for ln in enc.fate_resolution_log if ln.actor == "Hero"]
    assert len(hero_lines) == 1
    line = hero_lines[0]
    assert line.action == "attack"
    assert line.skill == "Fight"
    assert line.target == "Thug"
    assert line.defense_skill == "Athletics"  # NPC default defense skill, physical track
    assert line.actor_total == 4
    assert line.opposition_total == 1
    assert line.shifts == 3
    assert line.outcome == "absorbed"
    assert line.detail  # a non-empty human-readable result line
    # The NPC's counter-attack on the PC is ALSO in the ledger (both directions show).
    assert any(ln.actor == "Thug" and ln.target == "Hero" for ln in enc.fate_resolution_log)


def test_ledger_is_cleared_each_exchange_last_exchange_semantics():
    """A second exchange REPLACES the first's ledger — the player sees the LATEST
    exchange, never an ever-growing stack on the server."""
    module, enc, snap = _hero_vs_thug(hero_fight=4, thug_athletics=1)
    snap.characters[0].core.fate_sheet.skills["Athletics"] = 3
    _seal_attack(enc, module, "Hero", 4, "Thug")
    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))
    first = len(enc.fate_resolution_log)
    assert first >= 1

    _seal_attack(enc, module, "Hero", 4, "Thug")
    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))
    assert len(enc.fate_resolution_log) == first  # replaced, not appended


def test_fate_state_payload_projects_last_exchange_onto_conflict():
    """build_fate_state_payload surfaces the ledger on FateConflictEntry.last_exchange
    so the player surface can render the legible attack/defend math."""
    module, enc, snap = _hero_vs_thug(hero_fight=4, thug_athletics=1)
    snap.characters[0].core.fate_sheet.skills["Athletics"] = 3
    _seal_attack(enc, module, "Hero", 4, "Thug")
    run_fate_exchange(encounter=enc, snapshot=snap, ruleset=module, rng=_FixedRng(0))

    payload = build_fate_state_payload(snap)
    assert payload.conflict is not None
    hero_lines = [ln for ln in payload.conflict.last_exchange if ln.actor == "Hero"]
    assert len(hero_lines) == 1
    line = hero_lines[0]
    assert line.target == "Thug"
    assert line.actor_total == 4
    assert line.opposition_total == 1
    assert line.outcome == "absorbed"


# --------------------------------------------------------------------------- #
# Bidirectional + end-to-end wiring: the NPC's counter-attack on a PC carries  #
# the PC's chosen defense skill, reached through the real dispatch path.       #
# --------------------------------------------------------------------------- #


def test_npc_attack_on_pc_records_defenders_chosen_skill_end_to_end():
    """Drive the production dispatch: a single PC attack closes the barrier, the NPC
    counter-attack PARKS at DEFEND, the PC throws a Will defense, and on RESUME the
    NPC-attack ledger line carries the PC's CHOSEN defense skill (the 'you defend
    Will = N' half of the expected ledger). The defense skill is recorded from the
    player's throw, not the server."""
    module, enc, snap = _hero_vs_thug(hero_fight=2, thug_athletics=2)
    # Give the PC a Will skill so a chosen-skill defense is meaningful.
    snap.characters[0].core.fate_sheet.skills["Will"] = 3
    exporter, tracer = _otel()

    result = dispatch_fate_action(
        payload=FateActionPayload(request_id="r1", action="attack", skill="Fight", target="Thug"),
        actor_name="Hero",
        encounter=enc,
        ruleset=module,
        snapshot=snap,
        rng=_FixedRng(0),
        _tracer=tracer,
    )
    assert result.awaiting_defense is True
    resolve_parked_defenses(
        encounter=enc,
        snapshot=snap,
        ruleset=module,
        defense_skill="Will",
        rng=_FixedRng(0),
        _tracer=tracer,
    )

    # The NPC's attack on the PC is in the ledger with the PC's thrown defense skill.
    npc_lines = [ln for ln in enc.fate_resolution_log if ln.actor == "Thug" and ln.target == "Hero"]
    assert npc_lines, "the NPC's counter-attack on the PC was not recorded in the ledger"
    assert npc_lines[0].defense_skill == "Will"
    # And the attack-resolved span fired for it (observable from the real dispatch).
    assert _ATTACK_SPAN in [s.name for s in exporter.get_finished_spans()]
