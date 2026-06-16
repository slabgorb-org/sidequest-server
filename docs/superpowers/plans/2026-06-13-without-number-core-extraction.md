# Without Number Core Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert an honest `WithoutNumberRulesetModule` shared chassis between the `RulesetModule` ABC and the four WN siblings (SWN/WWN/CWN/AWN), reparent all four onto it, migrate every `isinstance` capability gate, and then retune attribute generation + lethality so a fresh character is shaped and survivable.

**Architecture:** Today the WN hierarchy grew upside-down — `SwnRulesetModule` is the de-facto base and `Wwn(Swn)` / `Cwn(Swn)` / `Awn(Cwn)` inherit through it (a fantasy ruleset inherits starship combat). This plan extracts the genuinely-shared chassis (d20 conventions, core resolution, the Effort economy, the Shock/Trauma/Mortal-Injury/System-Strain lethality skeleton) up into a new `WithoutNumberRulesetModule(RulesetModule)`; the four siblings become clean siblings holding only their game-specific subsystems. Lethality spans are unified onto the existing slug-parameterized `spans/wn.py` emitter pattern (the Effort engine already did this in Story 102-6). Step 1 preserves mechanical output byte-for-byte; **two deliberate corrections ride along** (per user direction 2026-06-13): AWN's lethality spans stop being mislabeled `cwn.*` and become `awn.*`, and the lethality tools (`stabilize_mortal_injury`, `adjust_system_strain`, `commit_effort`) expand to every module whose hoisted chassis now supports them. Step 2 is an isolated, intended behavior change: attribute spread + first-session lethality tuning.

**Tech Stack:** Python 3 / pytest (`-n auto` xdist by default; `-n0` for OTEL span-count isolation per `project_server_test_otel_deadlock`), pydantic v2 configs, OpenTelemetry spans via `sidequest/telemetry/spans/`.

---

## Source-of-truth design

This plan implements `docs/superpowers/specs/2026-06-13-without-number-core-extraction-design.md`. Where code investigation refined the spec, the plan governs and the deviation is logged in **Design Decisions** below. Promote to **ADR-142** once this plan is approved.

### Design Decisions (refinements to the spec's extraction-boundary table)

These were under-specified in the spec and are resolved here:

- **DD-1 — The base ABC already holds the method surface.** `base.py`'s `RulesetModule` already defines concrete `NotImplementedError`-defaulting hooks (`ship_attack_params`, `adjudicate_jump`, `resolve_opponent_attack`) and no-op lethality defaults (`resolve_trauma`/`resolve_shock`/`resolve_downed`/`resolve_hacking`). The new `WithoutNumberRulesetModule` is inserted **between** the ABC and the siblings; it *overrides* the ABC's no-op lethality defaults with the real WN implementations. The ABC is untouched.

- **DD-2 — Config guard is a tuple, not a new config superclass.** The hoisted lethality methods guard `isinstance(cfg, (CwnConfig, WwnConfig))` — exactly the precedent already in `server/dispatch/downed_seam.py:75`. `AwnConfig(CwnConfig)` is covered transitively. **The config tree is NOT reparented** (only the module tree is), so every existing `isinstance(cfg, CwnConfig)` / `isinstance(cfg, (CwnConfig, WwnConfig))` gate keeps working unchanged. A shared `WithoutNumberConfig` carrying `trauma`/`system_strain` is **deferred** to a later cleanup.

- **DD-3 — Luck save hoists into the core `save_params`.** WWN and CWN have byte-identical `luck`-save overrides; AWN inherits CWN's. Hoisting the `luck` branch into the core `save_params` lets AWN reparent cleanly and removes the duplication. SWN inherits the luck-aware `save_params` but never authors a `luck` save category, so the branch is unreachable for SWN (previously SWN raised `ValueError` on `save="luck"`; now it would resolve one — an unreachable, non-contractual error path). Logged.

- **DD-4 — AWN span correction is in Step 1 (user-approved).** AWN currently inherits CWN's hardcoded `cwn_*` span emitters, so an AWN game emits `cwn.trauma.roll` / `cwn.system_strain.delta` — the exact mislabel `awn.py`'s docstring and the OTEL Observability Principle forbid. Unifying onto slug-parameterized emitters fixes this (`cwn.*` → `awn.*`). This is the **one intended mechanical-span change in Step 1**; WWN and CWN span output stays byte-identical.

- **DD-5 — Lethality tools expand to their full capability set (user-approved "expand latent gaps now").** With lethality hoisted to the core, `stabilize_mortal_injury` and `adjust_system_strain` (CWN+AWN-only today) extend to **WWN**, and `commit_effort` (WWN-only today) extends to every WN module (Effort is core). `use_mutation` and `_awn_mutation_module` are **narrowed** to `AwnRulesetModule` (mutations are AWN-specific; they only matched via `Awn IS-A Cwn`). See the **Gate Migration Table** in Task 7.

### Gate Migration Table (authoritative — Task 7 implements exactly this)

| # | Site | Current gate | Capability after hoist | New gate | Class |
|---|------|--------------|------------------------|----------|-------|
| 1 | `agents/tools/commit_effort.py:95,105` (+ decorator) | `ruleset != "wwn"` + `isinstance(module, WwnRulesetModule)` | Effort = WN-core | `isinstance(module, WithoutNumberRulesetModule)` | EXPAND |
| 2 | `agents/tools/stabilize_mortal_injury.py:116` | `isinstance(module, CwnRulesetModule)` | Mortal Injury = WN-core lethality | `isinstance(module, WithoutNumberRulesetModule)` | EXPAND→WWN |
| 3 | `agents/tools/adjust_system_strain.py:102` | `isinstance(module, CwnRulesetModule)` | System Strain = WN-core lethality | `isinstance(module, WithoutNumberRulesetModule)` | EXPAND→WWN |
| 4 | `agents/tools/use_mutation.py:60` | `isinstance(module, CwnRulesetModule)` | Mutations = AWN-only | `isinstance(module, AwnRulesetModule)` | NARROW |
| 5 | `agents/tools/veterans_luck.py:85` | `isinstance(module, WwnRulesetModule)` | Warrior = WWN-only | unchanged | KEEP |
| 6 | `agents/tools/long_rest.py:125` | `isinstance(module, WwnRulesetModule)` | WWN magic reprepare | unchanged | KEEP |
| 7 | `agents/subsystems/magic_working.py:84` `_wwn_cast_module` | `isinstance(module, WwnRulesetModule)` | WWN cast spine | unchanged | KEEP |
| 8 | `agents/subsystems/magic_working.py:101` `_psionic_module` | `isinstance(module, SwnRulesetModule)` | Psionics = SWN-only (stays on SWN) | unchanged (set narrows `{all}`→`{swn}`, correct) | KEEP* |
| 9 | `agents/subsystems/magic_working.py:128` `_awn_mutation_module` | `isinstance(module, CwnRulesetModule)` | AWN mutations | `isinstance(module, AwnRulesetModule)` | NARROW |
| 10 | `server/session.py:99` | `isinstance(module, SwnRulesetModule)` | Scene Effort reclaim = WN-core | `isinstance(module, WithoutNumberRulesetModule)` | PRESERVE |
| 11 | `server/narration_apply.py:439` | `get_ruleset_module("wwn")` + assert `WwnRulesetModule` | WWN spellcast downed seam | unchanged | KEEP |
| 12 | `server/narration_apply.py:537` | `isinstance(module, CwnRulesetModule)` | AWN mutation apply | `isinstance(module, AwnRulesetModule)` | NARROW |
| 13 | `server/dispatch/dice.py:637,644` | `isinstance(ruleset, SwnRulesetModule)` | WN sealed-round / hp_depletion = WN-core | `isinstance(ruleset, WithoutNumberRulesetModule)` | PRESERVE |
| 14 | `server/dispatch/dice.py:1175,1255` | assert `isinstance(ruleset, WwnRulesetModule)` | Killing Blow = WWN-only | unchanged | KEEP |

\* **#8 verification required** (Task 7 Step): confirm no shipped non-SWN pack authors a `psionic_discipline_catalog`. If one does, that pack regresses (loses inherited psionics) and the finding must be logged before proceeding. Run the grep in Task 7.

**Config gates — all UNCHANGED** (config tree not reparented): `downed_seam.py:75,140`, `encounter_lifecycle.py:1329`, `confrontation.py:378`, `dice.py:474`, `builder.py:96`. Task 7 includes a verification step that these still resolve correctly.

---

## File Structure

**Created:**
- `sidequest/game/ruleset/without_number.py` — the new `WithoutNumberRulesetModule(RulesetModule)` shared chassis. One responsibility: WN-family resolution every sibling inherits.
- `tests/game/ruleset/test_142_wn_core_extraction.py` — characterization (mechanical byte-identity) + MRO/wiring reflection test.
- `tests/game/ruleset/test_142_wn_lethality_spans.py` — span-parity characterization (WWN/CWN identical, AWN corrected `awn.*`).
- `tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py` — Step 2 intended-change assertions.

**Modified:**
- `sidequest/game/ruleset/swn.py` — strip the WN-core methods (now in the base); keep `activate_discipline`, `ship_attack_params`, `adjudicate_jump`, spike constants, `PSIONIC_EFFORT_SOURCE`. Reparent `class SwnRulesetModule(WithoutNumberRulesetModule)`.
- `sidequest/game/ruleset/wwn.py` — reparent onto the core; **delete** the inherited-`ship_attack_params` neutralizing override, the duplicated lethality (`resolve_shock`/`resolve_trauma`/`apply_system_strain`/`resolve_downed`), and the `luck` `save_params` override (now in core). Keep `resolve_spellcast`, `apply_killing_blow`, `veterans_luck`.
- `sidequest/game/ruleset/cwn.py` — reparent onto the core; **delete** the duplicated lethality + `luck` `save_params` override. Keep `resolve_hacking`.
- `sidequest/game/ruleset/awn.py` — reparent `class AwnRulesetModule(WithoutNumberRulesetModule)`; empty body (inherits lethality from core, drives `awn.*` spans via `self.slug`).
- `sidequest/telemetry/spans/wn.py` — add the five slug-parameterized lethality emitters + register routes for `{wwn, cwn, awn} × {system_strain.delta, trauma.roll, shock.applied, mortal_injury.declared, major_injury.roll}`.
- `sidequest/telemetry/spans/wwn.py`, `sidequest/telemetry/spans/cwn.py` — keep the `SPAN_*` name constants (routing-completeness + back-compat), but the five lethality emitter *functions* are superseded by `wn.py`'s. Leave the constants; delete the now-unused emitter functions only after confirming no other caller (Task 6 includes the grep).
- The 8 gate sites in the migration table (Tasks 7).
- Step 2: `sidequest/game/ruleset/without_number.py` (attribute/lethality defaults) + the relevant config defaults in `sidequest/genre/models/rules.py`.

---

## Step 1 — Extract the WN core (behavior-preserving + the two approved corrections)

### Task 1: Mechanical characterization net (pin byte-identity)

Captures each WN sibling's mechanical output for a representative matrix **against current code** so the refactor is provably output-preserving. These assertions pass before AND after the refactor (that is the point — a characterization test, not a red/green feature test).

**Files:**
- Create: `tests/game/ruleset/test_142_wn_core_extraction.py`

- [ ] **Step 1: Write the characterization tests using synthetic fixtures**

Use synthetic configs/cores (per the "no content in unit tests" rule — never load a real pack). Mirror the fixture shape already used in `tests/game/ruleset/test_cwn_trauma.py` / `test_cwn_system_strain.py` / `test_wwn_effort.py` (read those first for the exact `CreatureCore` / `WwnConfig` / `CwnConfig` construction helpers and reuse them).

```python
"""Characterization net for the WN core extraction (ADR-142).

Pins each WN sibling's MECHANICAL output byte-identical across the
extract-superclass refactor. Span-name parity lives in
test_142_wn_lethality_spans.py. Synthetic fixtures only — no pack load.
"""
from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset import get_ruleset_module
from sidequest.game.ruleset.resolution import AttackRollParams, CheckRollParams
# Reuse the existing fixture builders from the neighbouring ruleset tests.
# (Copy the minimal _core/_cfg helpers from test_cwn_trauma.py / test_wwn_effort.py
#  into a shared conftest if they are module-private — do NOT import test internals.)


def _attack_beat(stat_check="STRENGTH", attack_bonus=2, combat_skill=1):
    from sidequest.genre.models.rules import BeatDef
    return BeatDef(  # fill required fields per BeatDef's model — see test_swn_module.py
        ...
    )


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
def test_attack_params_unchanged(slug):
    module = get_ruleset_module(slug)
    beat = _attack_beat()
    stats = {"STRENGTH": 14, "DEXTERITY": 13}
    target = _synthetic_target_core(armor_class=15)
    params = module.attack_params(
        beat=beat, attacker_stats=stats, attacker_core=None, target_core=target
    )
    assert params == AttackRollParams(modifier=4, target_number=15)


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
def test_save_params_three_attribute_saves_unchanged(slug):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    params = module.save_params(
        stats={"STRENGTH": 14, "CONSTITUTION": 12},
        save="physical", level=1, label="Physical save", cfg=cfg,
    )
    assert params == CheckRollParams(sides=20, count=1, modifier=1, difficulty=15, label="Physical save")


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_luck_save_unchanged_for_luck_family(slug):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    params = module.save_params(stats={}, save="luck", level=3, label="Luck", cfg=cfg)
    assert params == CheckRollParams(sides=20, count=1, modifier=0, difficulty=13, label="Luck")


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_trauma_resolution_unchanged(slug):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    rng = random.Random(42)
    spec = _trauma_spec(trauma_die="1d8", trauma_rating=2, trauma_target=6)
    result = module.resolve_trauma(spec=spec, base_total=10, cfg=cfg, rng=rng, actor="Test")
    # Pin the EXACT (base, final, traumatic, roll, target) tuple captured from current code.
    assert (result.base_total, result.final_total, result.traumatic) == (10, _expected_final(slug), _expected_traumatic(slug))


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_system_strain_commit_unchanged(slug):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    core = _strain_core(current=0, max=6, permanent=0)
    res = module.apply_system_strain(core=core, kind="temporary", amount=2, source="test", cfg=cfg)
    assert (res.applied, res.current, res.max, res.delta) == (True, 2, 6, 2)


@pytest.mark.parametrize("slug", ["swn", "wwn", "cwn", "awn"])
def test_effort_commit_reclaim_cycle_unchanged(slug):
    module = get_ruleset_module(slug)
    core = _effort_core(source="psionic", available=2, max=2)
    commit = module.commit_effort(core=core, source="psionic", points=1, duration="scene", label="x")
    assert (commit.applied, commit.available) == (True, 1)
    reclaim = module.reclaim_effort(core=core, source="psionic", trigger="scene")
    assert (reclaim.applied, reclaim.available) == (True, 2)


@pytest.mark.parametrize("slug", ["wwn", "cwn", "awn"])
def test_resolve_downed_declares_mortal_injury_unchanged(slug):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    core = _strain_core(current=0, max=6, permanent=0)
    rng = random.Random(7)
    result = module.resolve_downed(core=core, save_target=10, scene_traumatic=False, cfg=cfg, rng=rng)
    assert result.mortal is True
    assert any("Mortal Injury" in s.text for s in core.statuses)
```

Replace each `_helper(...)` with the concrete fixture builders read from the neighbouring tests. Fill `_expected_*` by running the test once against current code and pinning the printed values (capture-then-assert — that is what makes it a characterization net).

- [ ] **Step 2: Run against CURRENT (unrefactored) code; pin the captured values**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_wn_core_extraction.py -v`
Expected: GREEN. Any value you had to fill from a first run is now the frozen baseline.

- [ ] **Step 3: Commit the net**

```bash
git add tests/game/ruleset/test_142_wn_core_extraction.py
git commit -m "test(ruleset): characterization net pinning WN mechanical output pre-extraction (ADR-142)"
```

### Task 2: Lethality span-parity characterization

Pins span NAMES + attributes. WWN/CWN assert identical names; AWN asserts the **corrected** `awn.*` names — so the AWN cases FAIL on current code (current emits `cwn.*`) and turn green only after Task 6. Mark them `xfail(strict=True)` until Task 6 flips them, so the suite stays green mid-refactor and the flip is explicit.

**Files:**
- Create: `tests/game/ruleset/test_142_wn_lethality_spans.py`

- [ ] **Step 1: Write the span-parity test using the in-memory span exporter**

Read `tests/server/test_neon_combat_lethality_dispatch.py` and `tests/game/ruleset/test_cwn_trauma.py` for the established in-memory OTEL exporter fixture (monkeypatch `sidequest.telemetry.spans.tracer`). Reuse it; do not invent a new harness.

```python
"""Span-name parity for the lethality hoist (ADR-142, DD-4).

WWN/CWN lethality spans stay byte-identical (wwn.* / cwn.*). AWN's lethality
spans are CORRECTED from the inherited cwn.* mislabel to awn.* — the one
intended span change in Step 1. The awn cases are xfail until Task 6 unifies
the emitters, then they flip green and the xfail markers are removed.
"""
from __future__ import annotations

import random

import pytest

from sidequest.game.ruleset import get_ruleset_module


@pytest.mark.parametrize(
    "slug,expected_prefix",
    [
        ("wwn", "wwn"),
        ("cwn", "cwn"),
        pytest.param("awn", "awn", marks=pytest.mark.xfail(
            reason="awn.* lethality spans land in Task 6 (DD-4); current code emits cwn.*",
            strict=True,
        )),
    ],
)
def test_trauma_span_namespaced_by_slug(slug, expected_prefix, span_exporter):
    module = get_ruleset_module(slug)
    cfg = _cfg_for(slug)
    module.resolve_trauma(
        spec=_trauma_spec(trauma_die="1d8", trauma_rating=2, trauma_target=6),
        base_total=10, cfg=cfg, rng=random.Random(1), actor="Test",
    )
    names = [s.name for s in span_exporter.get_finished_spans()]
    assert f"{expected_prefix}.trauma.roll" in names
    assert not any(n.endswith(".trauma.roll") and not n.startswith(expected_prefix) for n in names)
```

Repeat the parametrized test for `system_strain.delta` (drive `apply_system_strain`), `mortal_injury.declared` + `major_injury.roll` (drive `resolve_downed` with `scene_traumatic=True`), and `shock.applied` (drive `resolve_shock`).

- [ ] **Step 2: Run; confirm wwn/cwn green and awn xfail**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_wn_lethality_spans.py -v`
Expected: wwn/cwn PASS, awn XFAIL (strict). No XPASS.

- [ ] **Step 3: Commit**

```bash
git add tests/game/ruleset/test_142_wn_lethality_spans.py
git commit -m "test(ruleset): lethality span-parity net; awn.* correction pending (ADR-142 DD-4)"
```

### Task 3: Create `WithoutNumberRulesetModule` and reparent SWN

**Files:**
- Create: `sidequest/game/ruleset/without_number.py`
- Modify: `sidequest/game/ruleset/swn.py`

- [ ] **Step 1: Create the base module by moving the WN-core surface out of `swn.py`**

Create `without_number.py` containing `class WithoutNumberRulesetModule(RulesetModule)`. **Move verbatim** (cut from `swn.py`, paste into the new class) these members, preserving signatures and bodies exactly:

- Module-level helpers: `swn_attribute_modifier` (swn.py:56-69) and `_stat` (swn.py:72-83).
- `_SAVE_ATTRS` class attr (swn.py:89-94).
- `find_confrontation` (96-99), `stat_modifier` (101-102), `compute_dc` (104-107, the `NotImplementedError` raiser — shared across WN), `offer_difficulty` (109-114), `attack_params` (116-126), `resolve_opponent_attack` (128-152), `apply_beat` (218-229), `check_params` (231-254), `resolve_damage` (283-288), `roll_initiative` (290-310).
- `save_params` (256-281) — **but add the `luck` branch** per DD-3 (see Step 2 below).
- The entire Effort engine: `commit_effort` (322-360), `reclaim_effort` (362-401), `reclaim_scene_effort` (403-426), `reclaim_day_and_refresh` (428-479).

Set `slug` to be supplied by subclasses (the base declares `slug: str` only — it is abstract-by-convention, never registered directly). Keep the spans namespaced by `self.slug` exactly as the moved Effort code already does.

- [ ] **Step 2: Fold the `luck` save branch into the base `save_params` (DD-3)**

In the moved `save_params`, prepend the luck branch (lifted identically from `wwn.py:64-71` / `cwn.py:41-48`):

```python
def save_params(self, *, stats, save, level, label, cfg, character_core=None):
    if save == "luck":
        return CheckRollParams(
            sides=20, count=1,
            modifier=status_roll_modifier(character_core),
            difficulty=int(cfg.save_base) - (int(level) - 1),
            label=label,
        )
    if save not in self._SAVE_ATTRS:
        raise ValueError(f"unknown save category {save!r}, expected one of {list(self._SAVE_ATTRS)}")
    # ... rest of the three-attribute body, moved verbatim ...
```

- [ ] **Step 3: Add the lethality skeleton to the base (move from `wwn.py`, the canonical copy)**

Move `resolve_shock` (wwn.py:76-98), `resolve_trauma` (wwn.py:100-148), `apply_system_strain` (wwn.py:150-236), and `resolve_downed` (wwn.py:238-296) into the base — **with two edits each**:
  1. Broaden the config guard from `isinstance(cfg, WwnConfig)` to `isinstance(cfg, (CwnConfig, WwnConfig))` (DD-2).
  2. Replace the hardcoded `wwn_*_span(...)` calls with the slug-parameterized emitters from `wn.py` (created in Task 6): `system_strain_delta_span(ruleset=self.slug, ...)`, `trauma_roll_span(ruleset=self.slug, ...)`, etc. **This task wires the call; Task 6 defines the emitter.** To keep the tree compiling between tasks, import the new emitters from `wn.py` here and implement them in Task 6 — OR sequence Task 6 before this step. **Sequencing note:** do Task 6's emitter definitions first, then this step. (Subagent-driven execution: run Task 6 Step 1 before Task 3 Step 3.)

Imports the base now needs: `CreatureCore`, `random`, `trace`, `Status`/`StatusSeverity`/`status_roll_modifier`, `StrainResult`, `DownedResult`/`LethalityResult`/`major_injury_entry`, `DamageSpec`, `CwnConfig`/`WwnConfig`/`SwnConfig`, the Effort types, and the `wn.py` emitters. Copy the import block from `wwn.py` + `swn.py` and prune to what the base actually references.

- [ ] **Step 4: Reparent SWN onto the core**

In `swn.py`: change `class SwnRulesetModule(RulesetModule)` → `class SwnRulesetModule(WithoutNumberRulesetModule)`. Update the import (`from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule`). What REMAINS in `swn.py`: `ship_attack_params` (154-174), `adjudicate_jump` (176-216), `activate_discipline` (485-582), the spike constants (`SPIKE_TRANSIT_DAYS`/`SPIKE_FUEL_PER_JUMP`/`UNDERRATED_DRIVE_FUEL_PENALTY`), `PSIONIC_EFFORT_SOURCE`, and the `compute_dc` override is now inherited (delete the SWN copy — it is identical to the base raiser). `activate_discipline` calls `self.commit_effort` / `self.apply_system_strain` (both inherited from the core now) — no change needed.

- [ ] **Step 5: Run the SWN-touching tests**

Run: `uv run pytest -n0 tests/game/ruleset/test_swn_module.py tests/game/ruleset/test_swn_roll_initiative.py tests/game/ruleset/test_swn_psionics_effort_102_6.py tests/game/ruleset/test_142_wn_core_extraction.py -v`
Expected: GREEN (SWN cases of the characterization net included).

- [ ] **Step 6: Commit**

```bash
git add sidequest/game/ruleset/without_number.py sidequest/game/ruleset/swn.py
git commit -m "refactor(ruleset): extract WithoutNumberRulesetModule core; reparent SWN (ADR-142)"
```

### Task 4: Reparent WWN onto the core

**Files:**
- Modify: `sidequest/game/ruleset/wwn.py`

- [ ] **Step 1: Reparent and delete the now-inherited members**

Change `class WwnRulesetModule(SwnRulesetModule)` → `class WwnRulesetModule(WithoutNumberRulesetModule)`; update the import. **Delete** from `wwn.py`:
  - `ship_attack_params` override (54-58) — the entire reason it existed was to neutralize the inherited SWN dogfight; WWN no longer inherits it, so the base's `NotImplementedError` default applies directly. **Verify** the base default message still reads `"wwn ruleset has no ship-gunnery resolution"` shape via `self.slug` (the ABC default at `base.py:103` already uses `self.slug`).
  - `save_params` luck override (60-74) — now in core (DD-3).
  - `resolve_shock` (76-98), `resolve_trauma` (100-148), `apply_system_strain` (150-236), `resolve_downed` (238-296) — now in core (these were the canonical copy moved in Task 3 Step 3).

**Keep** in `wwn.py`: `resolve_spellcast` (310-428), `apply_killing_blow` (434-463), `veterans_luck` (465-511), and `slug = "wwn"`. Prune the import block to what remains.

- [ ] **Step 2: Run WWN tests**

Run: `uv run pytest -n0 tests/game/ruleset/test_wwn_spellcast.py tests/game/ruleset/test_wwn_effort.py tests/game/ruleset/test_wwn_downed_dispatch_seam.py tests/agents/tools/test_wwn_class_power_tools.py tests/server/test_wwn_cast_dispatch.py -v`
Expected: GREEN.

- [ ] **Step 3: Commit**

```bash
git add sidequest/game/ruleset/wwn.py
git commit -m "refactor(ruleset): reparent WWN onto WN core; drop neutralizing ship override + duplicated lethality (ADR-142)"
```

### Task 5: Reparent CWN and AWN onto the core

**Files:**
- Modify: `sidequest/game/ruleset/cwn.py`, `sidequest/game/ruleset/awn.py`

- [ ] **Step 1: Reparent CWN; keep only `resolve_hacking`**

Change `class CwnRulesetModule(SwnRulesetModule)` → `class CwnRulesetModule(WithoutNumberRulesetModule)`; update the import. **Delete** the now-inherited `save_params` luck override (37-51), `resolve_shock` (53-77), `resolve_trauma` (79-133), `apply_system_strain` (135-221), `resolve_downed` (223-280). **Keep** `resolve_hacking` (282-314) and `slug = "cwn"`. Prune imports.

- [ ] **Step 2: Reparent AWN directly onto the core**

In `awn.py`: change `from sidequest.game.ruleset.cwn import CwnRulesetModule` → `from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule`, and `class AwnRulesetModule(CwnRulesetModule)` → `class AwnRulesetModule(WithoutNumberRulesetModule)`. Body stays just `slug = "awn"`. Update the docstring: AWN now inherits the lethality skeleton from the **WN core** (not CWN), and emits **`awn.*`** spans via `self.slug` — the honest-slug guarantee the old docstring promised. Note that AWN no longer has `resolve_hacking` (it never used it — `AwnConfig.hacking` defaults `None`); it inherits the base no-op default, never invoked.

- [ ] **Step 3: Run CWN + AWN tests**

Run: `uv run pytest -n0 tests/game/ruleset/test_cwn_module.py tests/game/ruleset/test_cwn_shock.py tests/game/ruleset/test_cwn_trauma.py tests/game/ruleset/test_cwn_system_strain.py tests/game/ruleset/test_awn_downed_dispatch_seam.py tests/server/test_awn_combat_dispatch.py tests/server/test_mutant_wasteland_combat_dispatch.py -v`
Expected: GREEN for mechanical assertions. (Span-name assertions for AWN flip in Task 6.)

- [ ] **Step 4: Commit**

```bash
git add sidequest/game/ruleset/cwn.py sidequest/game/ruleset/awn.py
git commit -m "refactor(ruleset): reparent CWN+AWN onto WN core; AWN no longer Awn(Cwn) (ADR-142)"
```

### Task 6: Unify lethality span emitters onto `wn.py` (slug-parameterized)

> Per the sequencing note in Task 3 Step 3, **Step 1 of this task runs before Task 3 Step 3.** It is documented here for cohesion.

**Files:**
- Modify: `sidequest/telemetry/spans/wn.py`
- Modify: `sidequest/telemetry/spans/awn.py` (add lethality routes — file already exists for mutations)
- Modify: `sidequest/telemetry/spans/wwn.py`, `sidequest/telemetry/spans/cwn.py` (remove superseded emitter functions; keep `SPAN_*` constants)

- [ ] **Step 1: Add the five slug-parameterized lethality emitters to `wn.py`**

Follow the exact pattern already in `wn.py` (dynamic `{slug}.{event}` names, one `SpanRoute` registered per `(slug, event)` pair). Add to `_WN_EVENTS` (or a parallel lethality dict) the five events, then register routes for the lethality-bearing slugs `("wwn", "cwn", "awn")` only (SWN has no lethality config, never emits these). Mirror the attribute schema captured in the existing `wwn.py` `SPAN_ROUTES[...].extract` lambdas so the GM-panel projection is unchanged:

```python
_WN_LETHALITY_EVENTS = {
    "system_strain.delta": "system_strain",
    "trauma.roll": "trauma",
    "shock.applied": "shock",
    "mortal_injury.declared": "mortal_injury",
    "major_injury.roll": "major_injury",
}
_WN_LETHALITY_SLUGS = ("wwn", "cwn", "awn")

for _slug in _WN_LETHALITY_SLUGS:
    for _event, _field in _WN_LETHALITY_EVENTS.items():
        SPAN_ROUTES[f"{_slug}.{_event}"] = SpanRoute(
            event_type="state_transition",
            component=_slug,
            extract=(lambda field: (lambda span: {"field": field, **(span.attributes or {})}))(_field),
        )


def system_strain_delta_span(*, ruleset, actor, source, amount, new_total, max, applied,
                             _tracer=None, **attrs):
    attributes = {"field": "system_strain", "actor": actor, "source": source,
                  "amount": amount, "new_total": new_total, "max": max,
                  "applied": applied, **attrs}
    with Span.open(f"{ruleset}.system_strain.delta", attributes, tracer_override=_tracer):
        pass
```

Add `trauma_roll_span`, `shock_applied_span`, `mortal_injury_declared_span`, `major_injury_roll_span` the same way — copy each attribute set verbatim from the corresponding `wwn_*_span` body so WWN/CWN output is byte-identical. (Confirm the `extract` lambda shape matches what `wwn.py`/`cwn.py` registered; if their extracts pulled specific keys with defaults, replicate that exactly rather than the `**span.attributes` shorthand above.)

- [ ] **Step 2: Point the core lethality methods at the new emitters**

(This is Task 3 Step 3's wiring.) In `without_number.py`, the hoisted `resolve_trauma`/`resolve_shock`/`apply_system_strain`/`resolve_downed` call `trauma_roll_span(ruleset=self.slug, ...)` etc. Import them from `sidequest.telemetry.spans.wn`.

- [ ] **Step 3: Remove the superseded per-slug emitter functions; keep the constants**

Grep for remaining callers first:
Run: `grep -rn "wwn_trauma_roll_span\|wwn_shock_applied_span\|wwn_system_strain_delta_span\|wwn_mortal_injury_declared_span\|wwn_major_injury_roll_span\|cwn_trauma_roll_span\|cwn_shock_applied_span\|cwn_system_strain_delta_span\|cwn_mortal_injury_declared_span\|cwn_major_injury_roll_span" sidequest/ tests/`
Expected after the reparent: only the `spans/wwn.py` / `spans/cwn.py` definitions themselves (no production callers — the ruleset modules now use the `wn.py` emitters). If a test imports them directly, update that test to assert via the in-memory exporter on span NAME instead. Delete the five emitter *functions* from `wwn.py` and `cwn.py`; **keep** the `SPAN_WWN_*` / `SPAN_CWN_*` name constants and their `SPAN_ROUTES` registrations (back-compat + routing-completeness). The `awn.*` routes are newly added in Step 1.

- [ ] **Step 4: Flip the AWN span xfails to green**

Remove the `xfail` markers from the AWN parametrize cases in `test_142_wn_lethality_spans.py`.

Run: `uv run pytest -n0 tests/game/ruleset/test_142_wn_lethality_spans.py tests/telemetry/test_routing_completeness.py -v`
Expected: all GREEN (AWN now emits `awn.*`; routing-completeness passes with the new `awn.*` routes registered).

- [ ] **Step 5: Commit**

```bash
git add sidequest/telemetry/spans/wn.py sidequest/telemetry/spans/awn.py sidequest/telemetry/spans/wwn.py sidequest/telemetry/spans/cwn.py sidequest/game/ruleset/without_number.py tests/game/ruleset/test_142_wn_lethality_spans.py
git commit -m "refactor(telemetry): unify WN lethality spans slug-parameterized; correct AWN cwn.*->awn.* (ADR-142 DD-4)"
```

### Task 7: Migrate the `isinstance` capability gates

Implement the **Gate Migration Table** exactly. Each edit is small; do them as one task with a commit at the end.

**Files:** the 8 sites flagged EXPAND / NARROW / PRESERVE in the table.

- [ ] **Step 1: Verification grep for the `_psionic_module` narrowing (gate #8)**

Run: `grep -rln "psionic_discipline_catalog" genre_packs/ ../sidequest-content/ 2>/dev/null; grep -rn "psionic_discipline_catalog" sidequest/genre/`
Inspect: confirm no **non-SWN** pack (`ruleset:` ≠ `swn`) ships a `psionic_discipline_catalog`. If one does, STOP and log it — that pack regresses. If none, gate #8 needs no edit (the flatten makes `isinstance(module, SwnRulesetModule)` correctly match only SWN).

- [ ] **Step 2: EXPAND gates 1, 2, 3, 10, 13 → `WithoutNumberRulesetModule`**

For each, change the imported class and the `isinstance` target. Example for `agents/tools/stabilize_mortal_injury.py:116`:

```python
# was: from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
...
# was: if not isinstance(module, CwnRulesetModule):
if not isinstance(module, WithoutNumberRulesetModule):
    ruleset = getattr(getattr(pack, "rules", None), "ruleset", None)
    raise ValueError(
        f"stabilize_mortal_injury requires a Without Number ruleset (swn/wwn/cwn/awn); "
        f"loaded pack has ruleset={ruleset!r}"
    )
```

Apply the same shape to `adjust_system_strain.py:102`. For `commit_effort.py`: also delete the `pack.rules.ruleset != "wwn"` string check at line 95 (replaced by the capability gate) and update the decorator if it declares `ruleset="wwn"` (read the `@tool(...)` decorator above the function — if it restricts the tool to wwn packs, broaden it to the WN family or remove the restriction so all WN packs expose the tool; mirror how `use_mutation`'s `ruleset="awn"` decorator works). For `session.py:99` and `dice.py:637,644`: swap the import + `isinstance` target only (logic unchanged).

> **Note (DD-5):** gates 2 and 3 now match WWN too. The core methods they call (`resolve_downed` / `apply_system_strain`) guard `isinstance(cfg, (CwnConfig, WwnConfig))`, so a WWN pack passes; an SWN pack (no `trauma`/`system_strain` config) raises loudly inside the method — acceptable (SWN never authors a mortal-injury/strain surface). This widening is the approved capability expansion; the characterization net's existing WWN cases already cover the methods.

- [ ] **Step 3: NARROW gates 4, 9, 12 → `AwnRulesetModule`**

For `use_mutation.py:60`, `magic_working.py:128` (`_awn_mutation_module`), `narration_apply.py:537`: change the import to `AwnRulesetModule` and the `isinstance` target. Example for `magic_working.py:128`:

```python
# was: from sidequest.game.ruleset.cwn import CwnRulesetModule
from sidequest.game.ruleset.awn import AwnRulesetModule
...
# was: return module if isinstance(module, CwnRulesetModule) else None
return module if isinstance(module, AwnRulesetModule) else None
```

Update the adjacent error strings / comments that say "CWN-family" to "AWN" (mutations are AWN-specific).

- [ ] **Step 4: Config-gate verification (no edits expected)**

Run: `uv run pytest -n0 tests/server/test_reprisal_wn_downed_seam.py tests/server/test_neon_combat_lethality_dispatch.py tests/integration/test_mutation_wiring.py tests/mutation/ -v`
Expected: GREEN — confirms the unchanged config gates (`downed_seam`, `builder`, `confrontation`, `dice`, `encounter_lifecycle`) still resolve correctly through `AwnConfig(CwnConfig)`.

- [ ] **Step 5: Run the tool + dispatch suites**

Run: `uv run pytest -n0 tests/agents/tools/test_stabilize_mortal_injury_tool.py tests/agents/tools/test_adjust_system_strain_tool.py tests/agents/test_73_15_ruleset_tool_filter.py tests/server/test_psionics_dispatch_wiring_102_6.py tests/mutation/test_use_ops.py -v`
Expected: GREEN. If a tool test asserted CWN/AWN-only rejection of WWN, update it to reflect the approved expansion (WWN is now accepted) and note it in the session deviation log.

- [ ] **Step 6: Commit**

```bash
git add sidequest/agents sidequest/server
git commit -m "refactor(ruleset): migrate isinstance capability gates to WN core; expand lethality tools, narrow mutations to AWN (ADR-142 DD-5)"
```

### Task 8: MRO / wiring reflection test

Proves the hierarchy is genuinely flat — no WN sibling inherits from another WN sibling — via runtime reflection (not source grep).

**Files:**
- Modify: `tests/game/ruleset/test_142_wn_core_extraction.py`

- [ ] **Step 1: Add the MRO + registry reflection test**

```python
def test_all_wn_modules_reparented_onto_core_and_are_clean_siblings():
    from sidequest.game.ruleset import get_ruleset_module
    from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule
    from sidequest.game.ruleset.swn import SwnRulesetModule
    from sidequest.game.ruleset.wwn import WwnRulesetModule
    from sidequest.game.ruleset.cwn import CwnRulesetModule
    from sidequest.game.ruleset.awn import AwnRulesetModule

    siblings = {SwnRulesetModule, WwnRulesetModule, CwnRulesetModule, AwnRulesetModule}
    for slug in ("swn", "wwn", "cwn", "awn"):
        module = get_ruleset_module(slug)
        assert isinstance(module, WithoutNumberRulesetModule), f"{slug} not on the WN core"
        # No WN sibling inherits from another WN sibling.
        ancestors = set(type(module).__mro__) - {type(module)}
        assert not (ancestors & siblings), (
            f"{slug} inherits from another WN sibling: {ancestors & siblings}"
        )


def test_wwn_does_not_inherit_starship_combat():
    # The smoking gun: WWN must NOT resolve ship gunnery (it no longer inherits SWN).
    import pytest
    from sidequest.game.ruleset import get_ruleset_module
    wwn = get_ruleset_module("wwn")
    with pytest.raises(NotImplementedError):
        wwn.ship_attack_params(
            attacker_stats={}, pilot_skill=0, attack_bonus=0,
            geometry_modifier=0, target_ac=10, cfg=None,
        )
```

- [ ] **Step 2: Run**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_wn_core_extraction.py -v`
Expected: GREEN.

- [ ] **Step 3: Commit**

```bash
git add tests/game/ruleset/test_142_wn_core_extraction.py
git commit -m "test(ruleset): MRO reflection — WN siblings reparented, WWN has no starship combat (ADR-142)"
```

### Task 9: Full-suite gate for Step 1

- [ ] **Step 1: Lint + format the touched files**

Run: `uv run ruff check sidequest/game/ruleset/ sidequest/telemetry/spans/ sidequest/agents/ sidequest/server/ tests/game/ruleset/ && uv run ruff format sidequest/game/ruleset/without_number.py sidequest/game/ruleset/swn.py sidequest/game/ruleset/wwn.py sidequest/game/ruleset/cwn.py sidequest/game/ruleset/awn.py`
Expected: clean. (Format only branch-touched files per `project_server_ruff_format_drift`.)

- [ ] **Step 2: Type check**

Run: `uv run pyright sidequest/game/ruleset/ sidequest/telemetry/spans/wn.py`
Expected: no new errors.

- [ ] **Step 3: Full ruleset + integration suite (serial for OTEL span-count tests)**

Run: `uv run pytest -n0 tests/game/ruleset/ tests/integration/test_wwn_caverns_dispatch.py tests/integration/test_wwn_elemental_harmony_dispatch.py tests/integration/test_102_4_wn_sealed_round.py tests/server/test_awn_combat_dispatch.py tests/server/test_road_warrior_combat_dispatch.py -v`
Expected: GREEN. (Per `project_wwn_content_breaks_server_fixtures`, ~13 pre-existing content-vs-server failures may exist on develop — classify those as pre-existing, unrelated; this task's gate is that no NEW failure was introduced.)

- [ ] **Step 4: Commit (if formatting changed anything)**

```bash
git add -A && git commit -m "chore(ruleset): lint/format pass for WN core extraction (ADR-142)" || true
```

**Step 1 is now complete: the WN core is honest, all four siblings reparented, gates migrated, behavior preserved (AWN span correction logged).**

---

## Step 2 — Retune attributes + lethality (the isolated intended change)

Kept strictly separate from Step 1 so the characterization net cleanly distinguishes "refactor (no change)" from "tuning (intended change)." These are the ONLY tests expected to change between Step 1 and Step 2.

### Task 10: Attribute spread — shape, not flat 13s

**Files:**
- Create: `tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py`
- Modify: `sidequest/game/ruleset/without_number.py` (and/or the chargen attribute-gen surface it owns)

> **Prerequisite read:** before writing code, locate where attribute generation currently produces the flat 13/12 point-buy-27 array. Grep: `grep -rn "point.buy\|27\|standard_array\|attribute.*gen\|roll_attributes" sidequest/game/ sidequest/genre/`. The WWN SRD standard array is **14, 12, 11, 10, 9, 7** (assign-to-taste) and point-buy is an alternative; cite the WWN SRD chargen chapter (attributes + standard array). If attribute-gen lives outside the ruleset module today, this task wires a ruleset-owned default — confirm the seam with the Architect before implementing (this is the boundary the deferred "ruleset chargen seam" spec will formalize; here we only fix the DEFAULT values, not build the seam).

- [ ] **Step 1: Write the failing test for a shaped array**

```python
def test_default_attribute_array_has_shape_not_flat_13s():
    # A fresh WWN character should have a real prime and real dump stats,
    # matching the WWN SRD standard array (14,12,11,10,9,7), not flat 13/12.
    scores = sorted(_generate_default_attribute_array(ruleset="wwn"), reverse=True)
    assert scores[0] >= 14, "no prime stat — still flat"
    assert scores[-1] <= 9, "no dump stat — still flat"
    assert len(set(scores)) >= 4, "array has no spread"
    assert sum(scores) == 63  # WWN standard array total (14+12+11+10+9+7)
```

- [ ] **Step 2: Run; confirm RED**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py::test_default_attribute_array_has_shape_not_flat_13s -v`
Expected: FAIL (current default is flat).

- [ ] **Step 3: Implement the WWN standard-array default**

Set the ruleset-owned default attribute array to the WWN SRD standard array `[14, 12, 11, 10, 9, 7]` (cite SRD chargen in a comment). Keep point-buy as an alternative path if one exists, but fix its budget/array so it cannot collapse to flat 13s.

- [ ] **Step 4: Run; confirm GREEN**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py::test_default_attribute_array_has_shape_not_flat_13s -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py sidequest/game/ruleset/without_number.py
git commit -m "feat(ruleset): WWN standard-array attribute default — shaped scores, not flat 13s (ADR-142 Step 2)"
```

### Task 11: Lethality tuning — survivable first hit

**Files:**
- Modify: `tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py`
- Modify: `sidequest/genre/models/rules.py` (`TraumaConfig` / `SystemStrainConfig` defaults) and/or `without_number.py` lethality defaults.

> **Prerequisite read:** read the WWN SRD lethality chapter (Mortal Wounds → stabilization window) and compare to the current `TraumaConfig` defaults (`default_trauma_target=6`, `mortal_injury_rounds=6`). The goal is that a starting character dropped to 0 HP gets a stabilization window matching WWN's actual feel rather than instant death. Confirm with the Architect whether the lever is `mortal_injury_rounds`, starting HP/`base_max`, or the Trauma threshold — cite the SRD number for whichever you change.

- [ ] **Step 1: Write the failing first-hit-survivability test**

```python
def test_starting_character_survives_first_solid_hit():
    # A level-1 WWN PC at full HP taking one average weapon hit should not be
    # instantly dead: HP drops but the character is downed-with-stabilization-window,
    # not removed. Pin the WWN-feel: a Mortal Injury declares a stabilization
    # window of mortal_injury_rounds, not death-on-zero.
    core = _level1_wwn_pc()  # full HpPool seeded from the tuned default
    cfg = _cfg_for("wwn")
    module = get_ruleset_module("wwn")
    # ... apply one average strike via the strike channel ...
    assert core.hp.current > 0 or _has_stabilization_window(core, cfg)
```

Make the assertion concrete against the chosen lever (e.g. assert `cfg.trauma.mortal_injury_rounds >= N` AND that a 0-HP `resolve_downed` attaches a Mortal Injury status with a >0 round count rather than an immediate death). Pin the exact SRD-cited number.

- [ ] **Step 2: Run; confirm RED**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py::test_starting_character_survives_first_solid_hit -v`
Expected: FAIL.

- [ ] **Step 3: Tune the lethality defaults**

Adjust the WWN-feel lethality default (the lever confirmed in the prerequisite read), citing the SRD lethality chapter in a comment. Keep the change in the WN-core / config default so every WWN world inherits it (the whole point — owned once by the ruleset).

- [ ] **Step 4: Run; confirm GREEN + characterization net still green**

Run: `uv run pytest -n0 tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py tests/game/ruleset/test_142_wn_core_extraction.py -v`
Expected: Step 2 tests PASS; the Step 1 characterization net still PASSES (tuning changed defaults, not the resolution math the net pins — if a net assertion moved, that is a signal you tuned resolution logic instead of a default; revert and re-scope).

- [ ] **Step 5: Commit**

```bash
git add tests/game/ruleset/test_142_step2_attribute_lethality_tuning.py sidequest/genre/models/rules.py
git commit -m "feat(ruleset): WWN-feel first-session lethality default — stabilization window, not instant death (ADR-142 Step 2)"
```

### Task 12: Final aggregate gate

- [ ] **Step 1: Full server check**

Run: `uv run ruff check . && uv run pytest -n0 tests/game/ruleset/ tests/server/test_awn_combat_dispatch.py tests/server/test_neon_combat_lethality_dispatch.py tests/server/test_road_warrior_combat_dispatch.py tests/integration/test_wwn_caverns_dispatch.py -v`
Expected: GREEN (modulo pre-existing content-fixture failures per `project_wwn_content_breaks_server_fixtures` — classify, do not block).

- [ ] **Step 2: Update the design doc status + promote to ADR-142**

Move/annotate `docs/superpowers/specs/2026-06-13-without-number-core-extraction-design.md` to reflect "implemented" and create `docs/adr/ADR-142-without-number-core-extraction.md` capturing DD-1…DD-5. (Hand to Tech Writer / Architect per workflow.)

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "docs(adr): promote WN core extraction to ADR-142"
```

---

## Self-Review (completed by the plan author)

**Spec coverage:** Step 1 (extract core + reparent four) → Tasks 3–5; the gate migration the spec omitted → Task 7 (with the authoritative table); span unification (spec's "lethality skeleton moves up") → Task 6; characterization + OTEL parity + wiring tests (spec's Test strategy) → Tasks 1, 2, 8. Step 2 (attributes + lethality) → Tasks 10–11. Deferred items (chargen seam, per-sibling chargen libraries, Fate Core) are out of scope by the spec and untouched here.

**Placeholder scan:** The characterization/Step-2 tests carry intentional `_helper(...)` / `_expected_*` markers because their concrete values must be *captured from a first run against current code* (that is the characterization method, not a placeholder) and their fixture builders must be *read from the neighbouring existing tests* rather than re-invented (avoids drift from the real `CreatureCore`/`*Config` constructors). Every refactor step references exact `file:line` ranges for verbatim moves. No "add appropriate error handling" / "similar to Task N" placeholders.

**Type consistency:** `WithoutNumberRulesetModule` is the single new type, imported identically everywhere (`from sidequest.game.ruleset.without_number import WithoutNumberRulesetModule`). Span emitters named `system_strain_delta_span` / `trauma_roll_span` / `shock_applied_span` / `mortal_injury_declared_span` / `major_injury_roll_span` consistently across Task 3/6. Config guard `(CwnConfig, WwnConfig)` consistent across the hoisted methods (DD-2).

**Known risk — the sequencing coupling between Task 3 Step 3 and Task 6 Step 1** (the core methods call emitters defined in Task 6). The plan flags this in both places; under subagent-driven execution, run Task 6 Step 1 before Task 3 Step 3, or implement the `wn.py` emitters and the core methods in a single working session.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-13-without-number-core-extraction.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Best fit here because the refactor has a hard regression net (Tasks 1–2) that each later task must keep green.
2. **Inline Execution** — execute tasks in this session with checkpoints.

Which approach?
