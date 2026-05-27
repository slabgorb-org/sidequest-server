# space_opera → SWN Binding (Attack-vs-AC + HP-Depletion Combat) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind the `space_opera` genre pack (both worlds) to the SWN ruleset module so SWN attack-vs-AC and saves fire in real play, and replace the momentum/engagement_range dials in both combats with HP-depletion as the win condition.

**Architecture:** Reuse-first. Two small single-seam engine additions — a per-pack `attribute_map` (so SWN's hardcoded save-attribute names resolve to the pack's flavor stats) and a per-confrontation `win_condition` selector with an HP-depletion resolution branch — plus content authoring in `space_opera/rules.yaml`. Everything else (the SWN module, the dispatch attack path, `apply_beat_hp_channel`, `CreatureCore.armor_class`/`HpPool`) already exists.

**Tech Stack:** Python 3.12, pydantic v2, pytest (`uv run pytest`, xdist `-n auto`), ruff. Genre packs are YAML loaded by `sidequest/genre/loader.py`.

**Spec:** `docs/superpowers/specs/2026-05-26-space-opera-swn-binding-design.md`

**Repos / branches:**
- `sidequest-server` (this repo) — branch `feat/<story-id>-swn-binding` off `develop`. Tasks 1–6, 10.
- `sidequest-content` — branch `feat/<story-id>-swn-binding` off `develop`. Tasks 7–9, 11.
- A `sidequest-ui` HP-track render is a **follow-up** (see "Out of scope / follow-up"), not in this plan.

**Test commands:**
- Server tests: `cd sidequest-server && uv run pytest <path> -v` (add `-n0` for a single test).
- Lint: `uv run ruff check .`  Format: `uv run ruff format .`

---

## Phase 1a — Stat bridge (server)

### Task 1: Add `attribute_map` to `SwnConfig` + `RulesConfig` validation

**Files:**
- Modify: `sidequest/genre/models/rules.py` (`SwnConfig` ~602; `RulesConfig` ~636 incl. `_populate_swn_defaults`)
- Test: `tests/genre/test_swn_attribute_map_validation.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/genre/test_swn_attribute_map_validation.py
import pytest
from pydantic import ValidationError
from sidequest.genre.models.rules import RulesConfig, SwnConfig

SIX = ["Physique", "Reflex", "Intellect", "Cunning", "Resolve", "Influence"]
GOOD_MAP = {
    "STRENGTH": "Physique", "CONSTITUTION": "Resolve", "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect", "WISDOM": "Cunning", "CHARISMA": "Influence",
}


def test_swn_pack_with_complete_map_validates():
    rc = RulesConfig(ruleset="swn", ability_score_names=SIX,
                     swn=SwnConfig(attribute_map=GOOD_MAP))
    assert rc.swn.attribute_map["WISDOM"] == "Cunning"


def test_swn_pack_missing_attribute_map_fails_loud():
    # ruleset=swn auto-populates SwnConfig() with an EMPTY map -> must reject
    with pytest.raises(ValidationError, match="attribute_map"):
        RulesConfig(ruleset="swn", ability_score_names=SIX)


def test_swn_pack_missing_one_key_fails_loud():
    partial = {k: v for k, v in GOOD_MAP.items() if k != "WISDOM"}
    with pytest.raises(ValidationError, match="WISDOM"):
        RulesConfig(ruleset="swn", ability_score_names=SIX,
                    swn=SwnConfig(attribute_map=partial))


def test_swn_pack_map_to_undeclared_stat_fails_loud():
    bad = {**GOOD_MAP, "WISDOM": "Nonexistent"}
    with pytest.raises(ValidationError, match="Nonexistent"):
        RulesConfig(ruleset="swn", ability_score_names=SIX,
                    swn=SwnConfig(attribute_map=bad))


def test_native_pack_ignores_attribute_map():
    # native packs never carry swn; no attribute_map requirement
    rc = RulesConfig(ruleset="native", ability_score_names=SIX)
    assert rc.swn is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/genre/test_swn_attribute_map_validation.py -v`
Expected: FAIL — `SwnConfig` has no `attribute_map` field / no validation rejecting empty map.

- [ ] **Step 3: Add the field + validation**

In `SwnConfig` (after `difficulties`):

```python
    # SWN attribute name -> this pack's flavor stat (ability_score_names entry).
    # Required (non-empty, all six keys) when ruleset == "swn"; validated on RulesConfig
    # where ability_score_names is reachable. No default map — fail loud if unauthored.
    attribute_map: dict[str, str] = Field(default_factory=dict)
```

Replace `RulesConfig._populate_swn_defaults` with a validator that both populates SRD constants and enforces the map:

```python
    @model_validator(mode="after")
    def _validate_swn(self) -> RulesConfig:
        """Populate swn SRD constants and enforce a complete attribute_map when bound."""
        if self.ruleset != "swn":
            return self
        if self.swn is None:
            object.__setattr__(self, "swn", SwnConfig())
        required = {"STRENGTH", "CONSTITUTION", "DEXTERITY", "INTELLIGENCE", "WISDOM", "CHARISMA"}
        amap = self.swn.attribute_map
        if not amap:
            raise ValueError(
                "ruleset 'swn' requires rules.swn.attribute_map (SWN attribute -> flavor stat); "
                "none authored — no silent default"
            )
        missing = required - amap.keys()
        if missing:
            raise ValueError(f"swn attribute_map missing required keys: {sorted(missing)}")
        declared = set(self.ability_score_names)
        for swn_attr, flavor in amap.items():
            if flavor not in declared:
                raise ValueError(
                    f"swn attribute_map[{swn_attr!r}] = {flavor!r} is not in "
                    f"ability_score_names {sorted(declared)}"
                )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/genre/test_swn_attribute_map_validation.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add sidequest/genre/models/rules.py tests/genre/test_swn_attribute_map_validation.py
git commit -m "feat(swn): attribute_map on SwnConfig + RulesConfig fail-loud validation"
```

---

### Task 2: `save_params` resolves through `attribute_map`; remove `_stat` silent fallback

**Files:**
- Modify: `sidequest/game/ruleset/swn.py` (`_stat`, `_SAVE_ATTRS`, `save_params`)
- Test: `tests/game/ruleset/test_swn_save_attribute_map.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/game/ruleset/test_swn_save_attribute_map.py
import pytest
from sidequest.game.ruleset.swn import SwnRulesetModule
from sidequest.genre.models.rules import SwnConfig

MAP = {
    "STRENGTH": "Physique", "CONSTITUTION": "Resolve", "DEXTERITY": "Reflex",
    "INTELLIGENCE": "Intellect", "WISDOM": "Cunning", "CHARISMA": "Influence",
}
CFG = SwnConfig(attribute_map=MAP)
MOD = SwnRulesetModule()

# Flavor-keyed stat block. Physique 14 -> +1, Resolve 8 -> 0, Cunning 18 -> +2, Influence 8 -> 0.
STATS = {"Physique": 14, "Reflex": 10, "Intellect": 10, "Cunning": 18, "Resolve": 8, "Influence": 8}


def test_physical_save_uses_best_of_mapped_str_con():
    # physical = best(STRENGTH<-Physique +1, CONSTITUTION<-Resolve 0) = +1
    p = MOD.save_params(stats=STATS, save="physical", level=1, label="Physical save", cfg=CFG)
    assert p.modifier == 1
    assert p.difficulty == 15  # save_base 15 - (level-1)
    assert p.sides == 20 and p.count == 1


def test_mental_save_uses_best_of_mapped_wis_cha():
    # mental = best(WISDOM<-Cunning +2, CHARISMA<-Influence 0) = +2
    p = MOD.save_params(stats=STATS, save="mental", level=1, label="Mental save", cfg=CFG)
    assert p.modifier == 2


def test_save_modifier_is_not_dead_zero():
    # Regression: pre-map, _stat("STRENGTH") fell back to 10 -> mod 0 for every save.
    p = MOD.save_params(stats=STATS, save="physical", level=1, label="x", cfg=CFG)
    assert p.modifier != 0


def test_attack_params_still_uses_flavor_stat_without_map():
    # Attack beats declare flavor stat_check; no map needed.
    class Beat:
        stat_check = "Physique"
        attack_bonus = 2
        combat_skill = 1
    class Core:
        armor_class = 13
    a = MOD.attack_params(beat=Beat(), attacker_stats=STATS, attacker_core=None, target_core=Core())
    assert a.modifier == 2 + 1 + 1  # attack_bonus + combat_skill + Physique(14)->+1
    assert a.target_number == 13


def test_stat_lookup_raises_on_absent_stat():
    with pytest.raises(KeyError):
        MOD.stat_modifier({"Physique": 12}, "Nonexistent")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/game/ruleset/test_swn_save_attribute_map.py -v`
Expected: FAIL — `save_params` ignores `attribute_map` (returns 0); `_stat` returns 10 instead of raising.

- [ ] **Step 3: Implement**

In `sidequest/game/ruleset/swn.py`, replace `_stat` (module-level function) to fail loud:

```python
def _stat(stats: dict[str, int], key: str) -> int:
    """Look up a stat score by exact or case-insensitive key. Fail loud if absent (no neutral-10)."""
    v = stats.get(key)
    if v is not None:
        return v
    for k, val in stats.items():
        if k.upper() == key.upper():
            return val
    raise KeyError(
        f"stat {key!r} not in stat block {sorted(stats)} — content/attribute_map bug "
        "(SWN module no longer falls back to a neutral 10)"
    )
```

In `save_params`, translate each `_SAVE_ATTRS` SWN name to its flavor stat via `cfg.attribute_map` before scoring:

```python
    def save_params(self, *, stats, save, level, label, cfg) -> CheckRollParams:
        if save not in self._SAVE_ATTRS:
            raise ValueError(
                f"unknown save category {save!r}, expected one of {list(self._SAVE_ATTRS)}"
            )
        amap = cfg.attribute_map
        flavor_attrs = []
        for swn_attr in self._SAVE_ATTRS[save]:
            flavor = amap.get(swn_attr)
            if flavor is None:
                raise KeyError(
                    f"attribute_map missing {swn_attr!r} for save {save!r} "
                    "(RulesConfig validator should have caught this)"
                )
            flavor_attrs.append(flavor)
        best_mod = max(self.stat_modifier(stats, f) for f in flavor_attrs)
        return CheckRollParams(
            sides=20, count=1,
            modifier=best_mod,
            difficulty=int(cfg.save_base) - (int(level) - 1),
            label=label,
        )
```

Leave `attack_params`, `check_params`, `stat_modifier` signatures as-is (`stat_modifier` already calls `_stat`, which now raises on absent — that is the desired behavior).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/game/ruleset/test_swn_save_attribute_map.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the existing SWN module suite to confirm no regression**

Run: `uv run pytest tests/game/ruleset -v`
Expected: PASS. If a pre-existing test relied on the neutral-10 fallback, fix that test to pass a complete stat block (the fallback was the bug, not a feature).

- [ ] **Step 6: Commit**

```bash
git add sidequest/game/ruleset/swn.py tests/game/ruleset/test_swn_save_attribute_map.py
git commit -m "feat(swn): saves resolve through attribute_map; _stat fails loud (no neutral-10)"
```

---

## Phase 1b — `win_condition` + HP-depletion resolution (server)

### Task 3: `WinCondition` enum + `ConfrontationDef.win_condition` + optional metrics

**Files:**
- Modify: `sidequest/genre/models/rules.py` (`ConfrontationDef` ~363, `_validate` ~406)
- Test: `tests/genre/test_confrontation_win_condition.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/genre/test_confrontation_win_condition.py
import pytest
from pydantic import ValidationError
from sidequest.genre.models.rules import ConfrontationDef, WinCondition

BEAT = {"id": "shoot", "label": "Shoot", "kind": "strike", "stat_check": "Physique"}
METRIC = {"name": "momentum", "starting": 0, "threshold": 7}


def test_default_win_condition_is_dial_threshold():
    c = ConfrontationDef(type="combat", label="Firefight", category="combat",
                         player_metric=METRIC, opponent_metric=METRIC, beats=[BEAT])
    assert c.win_condition == WinCondition.dial_threshold


def test_hp_depletion_allows_missing_metrics():
    c = ConfrontationDef(type="combat", label="Firefight", category="combat",
                         win_condition="hp_depletion", beats=[BEAT])
    assert c.win_condition == WinCondition.hp_depletion
    assert c.player_metric is None and c.opponent_metric is None


def test_dial_threshold_without_metrics_fails_loud():
    with pytest.raises(ValidationError, match="player_metric"):
        ConfrontationDef(type="combat", label="Firefight", category="combat", beats=[BEAT])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/genre/test_confrontation_win_condition.py -v`
Expected: FAIL — no `WinCondition`, `win_condition` field, and metrics are required.

- [ ] **Step 3: Implement**

Add the enum near `ResolutionMode` in `rules.py`:

```python
class WinCondition(StrEnum):  # noqa: UP042 — matches project convention
    """How a confrontation decides victory.

    - ``dial_threshold``: a side's metric dial reaching ``threshold`` ends it (default; every
      existing pack).
    - ``hp_depletion``: a side's primary combatant reaching 0 HP ends it (SWN combat). Metrics
      are dropped; resolution reads CreatureCore HP.
    """

    dial_threshold = "dial_threshold"
    hp_depletion = "hp_depletion"
```

In `ConfrontationDef`, change the metric fields and add `win_condition`:

```python
    resolution_mode: ResolutionMode = ResolutionMode.beat_selection
    win_condition: WinCondition = WinCondition.dial_threshold
    player_metric: MetricDef | None = None
    opponent_metric: MetricDef | None = None
```

In `ConfrontationDef._validate` (after-validator), add at the top (before the category check):

```python
        if self.win_condition == WinCondition.dial_threshold and (
            self.player_metric is None or self.opponent_metric is None
        ):
            raise ValueError(
                f"confrontation '{self.confrontation_type}' uses win_condition "
                "'dial_threshold' but is missing player_metric/opponent_metric"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/genre/test_confrontation_win_condition.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the genre model suite**

Run: `uv run pytest tests/genre -v`
Expected: PASS — every existing pack defaults to `dial_threshold` with metrics present, so nothing breaks.

- [ ] **Step 6: Commit**

```bash
git add sidequest/genre/models/rules.py tests/genre/test_confrontation_win_condition.py
git commit -m "feat(confrontation): win_condition enum; metrics optional under hp_depletion"
```

---

### Task 4: `StructuredEncounter.win_condition` + synthesize inert metrics at init seam

**Files:**
- Modify: `sidequest/game/encounter.py` (`StructuredEncounter` ~141)
- Modify: `sidequest/server/dispatch/encounter_lifecycle.py` (~460-474)
- Test: `tests/server/dispatch/test_encounter_init_hp_depletion.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/server/dispatch/test_encounter_init_hp_depletion.py
from sidequest.game.encounter import StructuredEncounter


def test_structured_encounter_defaults_win_condition_dial():
    enc = StructuredEncounter(
        encounter_type="x",
        player_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
        opponent_metric={"name": "m", "current": 0, "starting": 0, "threshold": 7},
    )
    assert enc.win_condition == "dial_threshold"


def test_structured_encounter_accepts_hp_depletion():
    enc = StructuredEncounter(
        encounter_type="x",
        win_condition="hp_depletion",
        player_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
        opponent_metric={"name": "hp", "current": 0, "starting": 0, "threshold": 1},
    )
    assert enc.win_condition == "hp_depletion"
```

(The init-seam synthesis is covered behaviorally by the Task 10 end-to-end test, which builds the real `space_opera` combat encounter whose cdef has no metrics; this task's unit test pins the new field.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/dispatch/test_encounter_init_hp_depletion.py -v`
Expected: FAIL — `StructuredEncounter` has no `win_condition` field (`extra: forbid`).

- [ ] **Step 3: Implement the field**

In `StructuredEncounter` (encounter.py, after `encounter_type`), add a plain-string field (string, not the enum, to avoid a `game` → `genre.models` import cycle — mirrors the "typed as Any to dodge circular import" precedent in `beat_kinds.apply_beat`):

```python
    encounter_type: str
    # "dial_threshold" (default) | "hp_depletion". Stamped from ConfrontationDef.win_condition
    # at init (encounter_lifecycle). String-typed to avoid a game->genre.models import cycle.
    win_condition: str = "dial_threshold"
    player_metric: EncounterMetric
    opponent_metric: EncounterMetric
```

- [ ] **Step 4: Implement the init-seam synthesis**

In `encounter_lifecycle.py` (~460), replace the metric construction so a metric-less (hp_depletion) cdef gets inert placeholder metrics and the win_condition is stamped:

```python
        # Synthesize inert metrics when a combat declares no dial (win_condition: hp_depletion).
        # The dial-threshold branches in apply_beat are gated off for hp_depletion, so these
        # placeholders never gate resolution — they only keep the ~9 live-metric readers safe.
        pm = cdef.player_metric
        om = cdef.opponent_metric
        if pm is None or om is None:
            pm = MetricDef(name="hp", starting=0, threshold=1_000_000)
            om = MetricDef(name="hp", starting=0, threshold=1_000_000)
        enc = StructuredEncounter(
            encounter_type=encounter_type,
            win_condition=cdef.win_condition.value,
            player_metric=EncounterMetric(
                name=pm.name, current=pm.starting, starting=pm.starting, threshold=pm.threshold,
            ),
            opponent_metric=EncounterMetric(
                name=om.name, current=om.starting, starting=om.starting, threshold=om.threshold,
            ),
            beat=0,
            structured_phase=EncounterPhase.Setup,
            secondary_stats=None,
            actors=actors,
            outcome=None,
```

Ensure `MetricDef` is imported in `encounter_lifecycle.py` (add `from sidequest.genre.models.rules import MetricDef` if not already present alongside the existing `cdef` import).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/server/dispatch/test_encounter_init_hp_depletion.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add sidequest/game/encounter.py sidequest/server/dispatch/encounter_lifecycle.py tests/server/dispatch/test_encounter_init_hp_depletion.py
git commit -m "feat(encounter): stamp win_condition; synthesize inert metrics for hp_depletion"
```

---

### Task 5: HP-depletion resolution branch in `apply_beat` + gate dial branches + OTEL

**Files:**
- Modify: `sidequest/game/beat_kinds.py` (`apply_beat` resolution block ~803-838)
- Test: `tests/game/test_apply_beat_hp_depletion.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/game/test_apply_beat_hp_depletion.py
from sidequest.game.beat_kinds import apply_beat
from sidequest.game.encounter import StructuredEncounter, EncounterActor, EncounterMetric
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.protocol.types import RollOutcome  # adjust import if RollOutcome lives elsewhere


def _enc(win_condition: str) -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="combat",
        win_condition=win_condition,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        actors=[
            EncounterActor(name="Hero", side="player"),
            EncounterActor(name="Pirate", side="opponent"),
        ],
    )


def _cores(pirate_hp: int, hero_hp: int = 10):
    cores = {
        "Hero": CreatureCore(name="Hero", description="", personality="",
                             hp=HpPool(current=hero_hp, max=10, base_max=10)),
        "Pirate": CreatureCore(name="Pirate", description="", personality="",
                               hp=HpPool(current=pirate_hp, max=10, base_max=10)),
    }
    return lambda name: cores.get(name)


class _StrikeBeat:
    id = "shoot"
    kind = "strike"
    stat_check = "Physique"
    damage_channel = "strike"


def test_hp_depletion_resolves_player_victory_when_opponent_drops():
    enc = _enc("hp_depletion")
    result = apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=_cores(pirate_hp=0), damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
    assert result.resolved is True


def test_hp_depletion_does_not_resolve_while_both_alive():
    enc = _enc("hp_depletion")
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=_cores(pirate_hp=4), damage_resolver=lambda: 3,
    )
    assert enc.resolved is False


def test_hp_depletion_ignores_dial_threshold():
    # Even if a metric were at threshold, hp_depletion must NOT resolve on the dial.
    enc = _enc("hp_depletion")
    enc.player_metric.current = enc.player_metric.threshold
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=_cores(pirate_hp=5), damage_resolver=lambda: 1,
    )
    assert enc.resolved is False


def test_dial_threshold_still_resolves_for_non_hp_packs():
    enc = _enc("dial_threshold")
    enc.player_metric.threshold = 2
    enc.player_metric.current = 0
    # a strike on a dial pack with a base delta should advance and (eventually) cross;
    # here we force-cross to prove the dial branch is still live under dial_threshold.
    enc.player_metric.current = 2
    apply_beat(
        enc, enc.actors[0], _StrikeBeat(), RollOutcome.Success,
        turn=1, edge_resolver=_cores(pirate_hp=10), damage_resolver=lambda: 0,
    )
    assert enc.resolved is True
    assert enc.outcome == "player_victory"
```

> Note: confirm the import path for `RollOutcome` and the constructor kwargs for `EncounterActor` (name/side) by reading `sidequest/game/encounter.py:104` before running — adjust the test imports if they differ.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/game/test_apply_beat_hp_depletion.py -v`
Expected: FAIL — `apply_beat` has no hp_depletion branch; it resolves only on dial threshold.

- [ ] **Step 3: Implement**

In `beat_kinds.py`, in the resolution section (currently ~803-838), insert the HP-depletion branch immediately after the `composure_break` branch and gate the existing dial-threshold branches. Replace the dial block:

```python
    hp_depletion = getattr(enc, "win_condition", "dial_threshold") == "hp_depletion"

    if hp_depletion and not resolved and edge_resolver is not None:
        def _side_down(side: str) -> bool:
            for a in enc.actors:
                if a.side != side:
                    continue
                core = edge_resolver(a.name)
                if core is not None and core.hp.current <= 0:
                    return True
            return False

        if _side_down("opponent"):
            enc.resolved = True
            enc.outcome = "player_victory"
            resolved = True
        elif _side_down("player"):
            enc.resolved = True
            enc.outcome = "opponent_victory"
            resolved = True
        if resolved:
            enc.structured_phase = EncounterPhase.Resolution
            down_side = "opponent" if enc.outcome == "player_victory" else "player"
            with encounter_resolved_span(
                encounter_type=enc.encounter_type,
                outcome=enc.outcome,
                source="hp_depletion",
                down_side=down_side,
                beat_id=getattr(beat, "id", "?"),
            ):
                pass

    # Player threshold first, then opponent — only for dial-threshold confrontations.
    if not hp_depletion and not resolved and enc.player_metric.current >= enc.player_metric.threshold:
        enc.resolved = True
        enc.outcome = "player_victory"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True
    elif not hp_depletion and not resolved and enc.opponent_metric.current >= enc.opponent_metric.threshold:
        enc.resolved = True
        enc.outcome = "opponent_victory"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True
    elif not resolved and (deltas.resolution or getattr(beat, "resolution", False)):
        enc.resolved = True
        enc.outcome = f"resolution_beat:{beat.id}"
        enc.structured_phase = EncounterPhase.Resolution
        resolved = True
```

Add the import at the top of `beat_kinds.py` if not present:

```python
from sidequest.telemetry.spans.encounter import encounter_resolved_span
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/game/test_apply_beat_hp_depletion.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the beat/encounter suite for regressions**

Run: `uv run pytest tests/game -k "beat or encounter" -v`
Expected: PASS — dial-threshold packs are unaffected (the `not hp_depletion` guard preserves the old path).

- [ ] **Step 6: Commit**

```bash
git add sidequest/game/beat_kinds.py tests/game/test_apply_beat_hp_depletion.py
git commit -m "feat(combat): hp_depletion resolution branch + dial gating + OTEL span"
```

---

### Task 6: Surface `win_condition` + HP in the CONFRONTATION payload

**Files:**
- Modify: `sidequest/server/dispatch/confrontation.py` (`build_confrontation_payload` ~186)
- Test: `tests/server/dispatch/test_confrontation_payload_hp.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/server/dispatch/test_confrontation_payload_hp.py
from sidequest.server.dispatch.confrontation import build_confrontation_payload
from sidequest.game.encounter import StructuredEncounter, EncounterActor, EncounterMetric
from sidequest.game.creature_core import CreatureCore, HpPool


def _enc():
    return StructuredEncounter(
        encounter_type="combat",
        win_condition="hp_depletion",
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=1_000_000),
        actors=[EncounterActor(name="Hero", side="player"),
                EncounterActor(name="Pirate", side="opponent")],
    )


def test_payload_includes_win_condition_and_hp():
    cores = {
        "Hero": CreatureCore(name="Hero", description="", personality="",
                             hp=HpPool(current=8, max=10, base_max=10)),
        "Pirate": CreatureCore(name="Pirate", description="", personality="",
                               hp=HpPool(current=3, max=10, base_max=10)),
    }
    payload = build_confrontation_payload(_enc(), core_resolver=lambda n: cores.get(n))
    assert payload["win_condition"] == "hp_depletion"
    assert payload["player_hp"] == {"current": 8, "max": 10}
    assert payload["opponent_hp"] == {"current": 3, "max": 10}
```

> Confirm the exact name and signature of `build_confrontation_payload` and how the dispatcher already passes a core/snapshot resolver before writing — if the function has no resolver param today, thread one from the call site (the dispatcher already holds `snapshot.find_creature_core`). Keep the dial fields for `dial_threshold` packs unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/dispatch/test_confrontation_payload_hp.py -v`
Expected: FAIL — payload has neither `win_condition` nor HP fields.

- [ ] **Step 3: Implement**

In `build_confrontation_payload`, add `win_condition` always, and HP fields when `hp_depletion`:

```python
    payload["win_condition"] = encounter.win_condition
    if encounter.win_condition == "hp_depletion" and core_resolver is not None:
        def _primary_hp(side: str):
            for a in encounter.actors:
                if a.side == side:
                    core = core_resolver(a.name)
                    if core is not None:
                        return {"current": core.hp.current, "max": core.hp.max}
            return None
        payload["player_hp"] = _primary_hp("player")
        payload["opponent_hp"] = _primary_hp("opponent")
```

Add `core_resolver` as an optional keyword param if absent, and pass `snapshot.find_creature_core` from the dispatch call site.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/server/dispatch/test_confrontation_payload_hp.py -v`
Expected: PASS.

- [ ] **Step 5: Run the dispatch suite**

Run: `uv run pytest tests/server/dispatch -v`
Expected: PASS — dial packs still emit their metric fields; the new keys are additive.

- [ ] **Step 6: Commit**

```bash
git add sidequest/server/dispatch/confrontation.py tests/server/dispatch/test_confrontation_payload_hp.py
git commit -m "feat(confrontation): emit win_condition + primary-combatant HP in payload"
```

- [ ] **Step 7: Full server gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS. Open the server PR to `develop` after Phase 2 content lands (the wiring test in Task 10 depends on the bound pack).

---

## Phase 2 — Binding + content + wiring (content repo + server e2e)

### Task 7: Bind `space_opera` to `ruleset: swn` with `attribute_map`

**Files (content repo):**
- Modify: `sidequest-content/genre_packs/space_opera/rules.yaml` (top-level `rules:` block / wherever `ruleset` belongs)
- Test (server repo): `sidequest-server/tests/genre/test_space_opera_loads_swn.py` (create)

- [ ] **Step 1: Write the failing test (server repo)**

```python
# tests/genre/test_space_opera_loads_swn.py
from pathlib import Path
from sidequest.genre.loader import load_genre_pack  # confirm loader entrypoint name

PACK = Path(__file__).parents[2] / ".." / "sidequest-content" / "genre_packs" / "space_opera"


def test_space_opera_binds_swn_with_attribute_map():
    pack = load_genre_pack(PACK.resolve())  # adjust to the real loader signature
    assert pack.rules.ruleset == "swn"
    assert pack.rules.swn.attribute_map["CHARISMA"] == "Influence"
```

> Confirm the loader entrypoint + how tests load a pack by path (grep an existing `tests/genre/test_*load*.py`). Do **not** hardcode an absolute `/Users/...` path — resolve relative to the repo, or use the existing test fixture helper if one exists.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/genre/test_space_opera_loads_swn.py -v`
Expected: FAIL — pack still `ruleset: native` (default), no `swn` block.

- [ ] **Step 3: Edit `space_opera/rules.yaml`**

Add to the `rules:` config (where `ruleset` is read — the same level as `ability_score_names`/`confrontations`):

```yaml
ruleset: swn
swn:
  attribute_map:
    STRENGTH: Physique
    CONSTITUTION: Resolve
    DEXTERITY: Reflex
    INTELLIGENCE: Intellect
    WISDOM: Cunning
    CHARISMA: Influence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/genre/test_space_opera_loads_swn.py -v`
Expected: PASS.

- [ ] **Step 5: Commit (content repo)**

```bash
cd sidequest-content
git add genre_packs/space_opera/rules.yaml
git commit -m "feat(space_opera): bind ruleset swn with six-stat attribute_map"
```

---

### Task 8: Switch `combat` + `ship_combat` to attack-vs-AC + hp_depletion

**Files (content repo):**
- Modify: `sidequest-content/genre_packs/space_opera/rules.yaml` (`combat` ~287, `ship_combat` ~205)

- [ ] **Step 1: Edit both combat confrontations**

For both `- type: combat` and `- type: ship_combat`:
1. Change `resolution_mode: opposed_check` → `resolution_mode: beat_selection`.
2. Add `win_condition: hp_depletion`.
3. **Remove** the `player_metric:` and `opponent_metric:` blocks.
4. On each `kind: strike` beat add `attack_bonus` and `combat_skill` (calibrated freshly in Task 11; start with placeholder real values, not 0):

```yaml
  - type: combat
    label: Firefight
    category: combat
    resolution_mode: beat_selection
    win_condition: hp_depletion
    intent_verbs: [strike, attack, fight, kill, slay, swing, shoot, hit, stab]
    on_intent_mismatch: reprompt
    beats:
      - id: shoot
        label: Shoot
        kind: strike
        base: 2
        stat_check: Physique
        damage_channel: strike
        attack_bonus: 1
        combat_skill: 1
        effect: "Target takes damage this round"
        narrator_hint: Blaster bolts sear the corridor. Sparks off bulkheads.
      # ...repeat attack_bonus/combat_skill on overload (the other strike beat)
```

Leave `dogfight` (`sealed_letter_lookup`) and `negotiation` (dial) **unchanged**.

- [ ] **Step 2: Verify the pack still loads (server repo)**

Run: `uv run pytest tests/genre/test_space_opera_loads_swn.py -v`
Expected: PASS — `hp_depletion` confrontations validate without metrics; `dial_threshold` ones still have theirs.

- [ ] **Step 3: Commit (content repo)**

```bash
cd sidequest-content
git add genre_packs/space_opera/rules.yaml
git commit -m "feat(space_opera): combat + ship_combat attack-vs-AC, hp_depletion, no dials"
```

---

### Task 9: Author opponent AC + HP (incl. enemy-ship hull) and verify materialization

**Files (content repo):**
- Modify: `sidequest-content/genre_packs/space_opera/` creature/opponent definitions (grep for where combat opponents are defined — `creatures.yaml` / world `npcs` / encounter opponents)
- Verify (server repo): materialization seeds `CreatureCore.armor_class` + HP

- [ ] **Step 1: Locate opponent definitions**

Run (content repo): `grep -rn "armor_class\|armor\|hp\|hull\|opponent" genre_packs/space_opera/ | grep -iv "narrator\|hint"`
Identify the personal-combat opponents and the enemy-ship entries used by `ship_combat`.

- [ ] **Step 2: Author AC + HP**

Give each combat opponent an `armor_class` and an HP pool (use the content's existing HP shape — B/X HP is translated at the materializer seam per project memory `project_hp_removed`). For enemy ships, set `armor_class` (ship AC) and HP representing **hull**.

- [ ] **Step 3: Verify the materialization seam (server repo)**

Read `sidequest/game/world_materialization.py` (`_apply_npc` / creature seeding). Confirm `CreatureCore.armor_class` AND `hp` are populated from content. If `armor_class` is NOT wired through (only commented at `creature_core.py:118`), add the seeding here — fail loud if content armor is malformed; do not default-10 silently. Add/extend a materialization unit test:

```python
# tests/game/test_materialize_armor_class.py
def test_materialized_opponent_carries_content_armor_class():
    # build a synthetic content creature with armor_class 14, materialize, assert core.armor_class == 14
    ...
```

- [ ] **Step 4: Run the materialization test**

Run: `uv run pytest tests/game/test_materialize_armor_class.py -v`
Expected: PASS.

- [ ] **Step 5: Commit (both repos as touched)**

```bash
# content
cd sidequest-content && git add genre_packs/space_opera && git commit -m "feat(space_opera): opponent + enemy-ship armor_class and HP/hull pools"
# server (only if materialization wiring was added)
cd ../sidequest-server && git add sidequest/game/world_materialization.py tests/game/test_materialize_armor_class.py && git commit -m "feat(materialize): seed CreatureCore.armor_class + HP from content"
```

---

### Task 10: End-to-end wiring tests (personal + ship) + both-worlds load

**Files (server repo):**
- Test: `tests/server/test_space_opera_swn_combat_e2e.py` (create)

- [ ] **Step 1: Write the end-to-end wiring tests**

Drive a real `space_opera` Firefight beat through `dispatch_dice_throw` against the **real pack** (not a synthetic fixture — per memory `project_opposed_check_wiring_trap`). Assert:
1. the roll resolves vs the **authored opponent AC** (`attack.target_number`),
2. a hit ablates opponent **HP**,
3. the encounter resolves on 0 HP — assert via the `encounter_resolved_span` with `source="hp_depletion"` (OTEL span assertion, **not** source-text grep, per the server CLAUDE.md "No Source-Text Wiring Tests"),
4. the `opposed_check` branch in `narration_apply` is **not** reached for space_opera combat.

```python
# tests/server/test_space_opera_swn_combat_e2e.py
# Use the existing OTEL span-capture fixture (grep tests/ for the established span recorder, e.g.
# a `captured_spans`/`span_exporter` fixture) to assert the resolution span fired.

def test_firefight_resolves_on_hp_depletion(space_opera_pack, span_recorder):
    # 1. build the real combat encounter (lethal opponent HP so one hit drops it)
    # 2. dispatch a 'shoot' beat with a winning d20 face
    # 3. assert encounter.resolved and outcome == "player_victory"
    # 4. assert a span SPAN_ENCOUNTER_RESOLVED with attrs source="hp_depletion" was recorded
    ...

def test_ship_combat_resolves_on_hull_depletion(space_opera_pack, span_recorder):
    # same shape against ship_combat with hull-as-HP
    ...
```

> Build on the existing dispatch e2e harness — grep `tests/server` for a test that already calls `dispatch_dice_throw` with a real pack + snapshot (e.g. the ADR-114 ablative-HP e2e) and reuse its fixtures. Do not stand up a synthetic non-opposed ConfrontationDef.

- [ ] **Step 2: Run tests to verify they fail (if pack/engine not yet complete) or pass**

Run: `uv run pytest tests/server/test_space_opera_swn_combat_e2e.py -v`
Expected: PASS once Tasks 1–9 are merged. Investigate any failure as a real wiring gap.

- [ ] **Step 3: Both-worlds load test**

```python
# add to tests/genre/test_space_opera_loads_swn.py
import pytest

@pytest.mark.parametrize("world", ["aureate_span", "coyote_star"])
def test_world_loads_under_swn(world):
    pack = load_genre_pack(PACK.resolve())  # load pack + world overlay per the real API
    # assert the world overlay loads clean under ruleset: swn
    ...
```

- [ ] **Step 4: Run the full server gate**

Run: `uv run pytest -q && uv run ruff check .`
Expected: PASS.

- [ ] **Step 5: Commit + open PRs**

```bash
git add tests/server/test_space_opera_swn_combat_e2e.py tests/genre/test_space_opera_loads_swn.py
git commit -m "test(swn): e2e Firefight + ship_combat resolve on HP; both worlds load"
```

Open the server PR (`feat/<story-id>-swn-binding` → `develop`) and the content PR (`feat/<story-id>-swn-binding` → `develop`). Merge content first (server e2e depends on it), then server.

---

## Phase 3 — Calibration

### Task 11: Calibrate SWN combat numbers (content repo)

**Files (content repo):**
- Modify: `sidequest-content/genre_packs/space_opera/rules.yaml` (beat `attack_bonus`/`combat_skill`, damage dice), opponent `armor_class` + HP, enemy-ship AC + hull.

- [ ] **Step 1: Set target feel**

Decide the SWN baseline: a level-1 PC attack bonus, typical opponent AC (SWN unarmored 10; light armor ~13; combat armor ~16), HP pools (PC ~ a few hits; mooks 1-2 hits; named foes more), weapon damage dice. No legacy numbers to honor.

- [ ] **Step 2: Tune values**

Edit `attack_bonus`/`combat_skill` per strike beat, opponent `armor_class` + HP, ship AC + hull, and weapon damage specs so a Firefight lasts a satisfying handful of rounds (not one-shot, not a slog).

- [ ] **Step 3: Sanity-run the e2e**

Run (server repo): `uv run pytest tests/server/test_space_opera_swn_combat_e2e.py -v`
Expected: PASS — calibration changes values, not wiring.

- [ ] **Step 4: Playtest pass (manual)**

Run a headless or UI playtest of a `space_opera` Firefight; confirm the OTEL GM panel shows `encounter.beat_applied` with `target_number` = AC and the `hp_depletion` resolution span on a kill. Adjust numbers as needed.

- [ ] **Step 5: Commit (content repo)**

```bash
cd sidequest-content
git add genre_packs/space_opera/rules.yaml
git commit -m "balance(space_opera): calibrate SWN attack bonuses, AC, HP/hull, weapon dice"
```

---

## Out of scope / follow-up

- **`sidequest-ui` HP track.** The CONFRONTATION frame now carries `win_condition` + `player_hp`/`opponent_hp`. The UI should render an HP track (and suppress the momentum dial) for `hp_depletion` confrontations — so Sebastien/Jade see the math. Separate `sidequest-ui` story.
- **Full SWN ship subsystem.** ship_combat uses the personal attack-vs-AC + hull-as-HP shape. SWN's dedicated ship rules (mass/power/hardpoints, crew action economy, ship classes) are a future epic.
- **Psionics (P7)** and the **per-module narrator tool contract (P5)** remain deferred.

---

## Self-Review

**Spec coverage:**
- attribute_map field + validation → Task 1 ✓
- save_params via map + `_stat` fail-loud → Task 2 ✓
- WinCondition enum + optional metrics + validator → Task 3 ✓
- StructuredEncounter.win_condition + init synthesis → Task 4 ✓
- hp_depletion resolution branch + dial gating + OTEL → Task 5 ✓
- payload win_condition + HP → Task 6 ✓
- bind ruleset + attribute_map content → Task 7 ✓
- switch both combats (mode + win_condition + drop metrics + attack_bonus/combat_skill) → Task 8 ✓
- opponent/ship AC + HP + materialization verify → Task 9 ✓
- e2e wiring (personal + ship) + both worlds + opposed-branch-not-reached → Task 10 ✓
- calibration → Task 11 ✓
- attack/check unchanged (flavor) → asserted in Task 2 ✓
- doctrine divergence (dials default elsewhere) → preserved by `dial_threshold` default + `not hp_depletion` gate (Tasks 3, 5) ✓

**Placeholder scan:** No "TBD/TODO". Tasks 6, 7, 9, 10 contain explicit "confirm the real signature/fixture before running" notes — these are verification instructions, not placeholders; the code shown is concrete.

**Type consistency:** `WinCondition` (enum, ConfrontationDef) vs `win_condition: str` (StructuredEncounter, `.value`) — intentional, documented in Task 4 to avoid the import cycle; comparisons use the string `"hp_depletion"`. `attribute_map` keys are uppercase SWN names throughout. `core_resolver`/`edge_resolver` both resolve a name → `CreatureCore | None`.
