"""RED (story 90-5): pin the *failure-path* contract of ruleset-module
encounter seeding that 90-1 shipped but left untested + under-observed.

90-1 made ``seed_manual`` fail LOUD for a ``ruleset: wwn|cwn|swn|awn`` pack
whose encounter generation produces nothing (``raise EncounterSeedError`` instead
of the old silent ``None`` skip). The Reviewer APPROVED-with-findings: the
*success* path is delivered + CI-guarded, but the *failure* path has no
regression test and degraded observability — the raise fires **before** the
``pregen.seed_manual`` span opens, so the seeding-failure decision emits no
seeding span at all (the old code at least emitted ``encounters_after=0``). The
contract is "one revert away from silently un-happening": revert the raise to a
warning and the entire 90-1 suite still passes.

These tests close that gap:

* **Item 2 (coverage lock — passes today):** a ruleset-module pack with failing
  encounter generation RAISES ``EncounterSeedError``; a dial pack does NOT
  (keeps the legacy warning-only skip — ADR-006). A revert-to-silent breaks the
  ruleset lock.
* **Item 3 (RED — fails today):** the ``pregen.seed_manual`` span must fire WITH
  a ``seed_error`` attribute (and ``encounters_after=0``) *before* the raise, so
  the GM panel records the seeding-failure decision (OTEL Observability
  Principle). Today the span never fires on the failure path.
* **Item 4 (folded in):** that failure span must carry the ``ruleset`` attribute
  (the "which path fired" proof the 90-1 code comment claims) — asserted on the
  failure span here; the success span already carries it.

All hermetic — ``seed_manual``'s ``load_genre_pack`` + the two tool CLIs are
monkeypatched, mirroring the established ``test_pregen.py`` stub pattern. No
shipped content, no subprocesses.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from sidequest.game.monster_manual import MonsterManual
from sidequest.server.dispatch import pregen
from sidequest.server.dispatch.pregen import EncounterSeedError, seed_manual
from sidequest.telemetry.spans.pregen import SPAN_PREGEN_SEED_MANUAL


def _stub_pack(*, ruleset: str, combat_encounters: bool = True) -> Any:
    """Minimal stand-in pack exposing exactly what ``seed_manual`` reads, with a
    ``rules`` block carrying the bound ``ruleset`` + ``combat_encounters`` flag.

    Mirrors ``tests/server/dispatch/test_pregen.py::_stub_pack`` but adds the
    ``rules`` namespace the failure branch keys on (``getattr(pack.rules,
    "ruleset", "dial")``). No cultures → the ``DEFAULT_NPC_FALLBACK_COUNT``
    namegen loop (monkeypatched), so the test exercises the *encounter* branch.
    """
    pack = SimpleNamespace(
        cultures=[],
        archetype_constraints=None,
        rules=SimpleNamespace(ruleset=ruleset, combat_encounters=combat_encounters),
    )
    pack.effective_cultures = lambda _world: ([], "stub")
    spawnable = SimpleNamespace(name="Drifter", named_individual=False)
    pack.effective_archetypes = lambda _world: ([spawnable], "stub")
    # epic-157 faction/zone work: seed_manual resolves the world bestiary once
    # for faction tagging. These tests drive the empty-encounter path, so a
    # None bestiary (encounters seed untagged) is the right stand-in.
    pack.effective_bestiary = lambda _world: (None, "stub")
    return pack


def _fake_namegen(argv: list[str]) -> int:
    """A namegen that always succeeds — keeps the NPC loop out of the way so the
    encounter branch is what's under test."""
    print(json.dumps({"name": "Stub NPC", "role": "drifter", "culture": ""}))
    return 0


def _failing_encountergen(_argv: list[str]) -> int:
    """encountergen that produces no usable output (nonzero exit) — drives
    ``_generate_encounter`` to ``None``, the precondition for the loud raise."""
    return 1


# ---------------------------------------------------------------------------
# Item 2 — the EncounterSeedError raise is pinned (revert-to-silent breaks it)
# ---------------------------------------------------------------------------


def test_seed_manual_raises_for_ruleset_pack_when_encounters_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ruleset-module pack whose encounter generation yields nothing must
    ``raise EncounterSeedError`` — never a silent empty pool. Coverage lock: this
    passes on current ``develop`` and FAILS if the raise is reverted to a warning
    (the 'one revert away' hole the Reviewer flagged)."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack(ruleset="wwn"))
    monkeypatch.setattr(pregen, "namegen_main", _fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", _failing_encountergen)

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="heavy_metal", world="evropi")
        with pytest.raises(EncounterSeedError) as exc:
            seed_manual(
                genre_packs_path=tmp_path / "packs",
                genre="heavy_metal",
                world="evropi",
                manual=manual,
                rng=random.Random(905),
            )
    # The loud message must name the ruleset + the bestiary contract so an
    # operator can act on it (not a bare RuntimeError).
    msg = str(exc.value)
    assert "wwn" in msg and "bestiary" in msg.lower(), (
        f"EncounterSeedError must name the ruleset + bestiary contract; got {msg!r}"
    )


def test_seed_manual_native_pack_does_not_raise_on_empty_encounters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard (ADR-006): a DIAL pack keeps the legacy warning-only
    skip — failing encounter generation must NOT raise, so dial sessions still
    bind with whatever the Manual had. The loud-fail is ruleset-module only."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack(ruleset="dial"))
    monkeypatch.setattr(pregen, "namegen_main", _fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", _failing_encountergen)

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="caverns_and_claudes", world="beneath_sunden")
        # Must complete without raising.
        seed_manual(
            genre_packs_path=tmp_path / "packs",
            genre="caverns_and_claudes",
            world="beneath_sunden",
            manual=manual,
            rng=random.Random(905),
        )
    assert len(manual.encounters) == 0, "dial empty-encounter path stays warning-only (no pool)"


# ---------------------------------------------------------------------------
# Item 3 (+4) — the seeding-failure decision is OTEL-visible BEFORE the raise
# ---------------------------------------------------------------------------


def test_seed_manual_emits_span_with_error_attr_before_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, otel_capture
) -> None:
    """RED: when ``seed_manual`` fails loud for a ruleset pack, the
    ``pregen.seed_manual`` span must STILL fire — carrying a ``seed_error``
    attribute, ``encounters_after=0``, and the ``ruleset`` attribute — *before*
    the ``EncounterSeedError`` propagates, so the GM panel records the failure
    as a first-class seeding-layer signal (OTEL Observability Principle).

    Fails on current ``develop``: the raise (pregen.py:~338) executes inside the
    tier loop, BEFORE ``Span.open(SPAN_PREGEN_SEED_MANUAL)`` (pregen.py:~366), so
    no seeding span is emitted on the failure path at all."""
    monkeypatch.setattr(pregen, "load_genre_pack", lambda _dir: _stub_pack(ruleset="wwn"))
    monkeypatch.setattr(pregen, "namegen_main", _fake_namegen)
    monkeypatch.setattr(pregen, "encountergen_main", _failing_encountergen)

    with mock.patch.object(Path, "home", return_value=tmp_path):
        manual = MonsterManual(genre="heavy_metal", world="evropi")
        with pytest.raises(EncounterSeedError):
            seed_manual(
                genre_packs_path=tmp_path / "packs",
                genre="heavy_metal",
                world="evropi",
                manual=manual,
                rng=random.Random(905),
            )

    spans = [s for s in otel_capture.get_finished_spans() if s.name == SPAN_PREGEN_SEED_MANUAL]
    assert spans, (
        "pregen.seed_manual span must fire even on the failure path — the seeding "
        "failure is a GM-panel decision and must not be invisible (90-5 item 3)"
    )
    attrs = spans[-1].attributes
    # Item 4: which path fired — the failure span names its ruleset.
    assert attrs.get("ruleset") == "wwn", (
        f"failure span must carry ruleset='wwn'; got {attrs.get('ruleset')!r}"
    )
    # The pool really is empty — the span must not over-report.
    assert attrs.get("encounters_after") == 0, (
        f"failure span must report encounters_after=0; got {attrs.get('encounters_after')!r}"
    )
    # Item 3: a truthy error attribute marks this span as the failure decision.
    seed_error = attrs.get("seed_error")
    assert isinstance(seed_error, str) and seed_error, (
        "failure span must carry a non-empty 'seed_error' attribute so the GM panel "
        f"can distinguish a loud seeding failure from a healthy seed; got {seed_error!r}"
    )
