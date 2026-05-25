"""Story 59-7: Wire three LocalDM subsystems through Intent Router dispatch.

These three subsystems (npc_agency, distinctive_detail_hint, reflect_absence)
have been "DORMANT" since 2026-04-28 — handlers exist, are registered in the
dispatch bank, but the live router never emits dispatches for them and the
watcher ignores them. This story makes them first-class live-path subsystems.

AC coverage:
  AC1 — npc_agency dispatch engages handler via bank + OTEL span
  AC2 — distinctive_detail_hint dispatch engages handler via bank + OTEL span
  AC3 — reflect_absence dispatch engages handler via bank + OTEL span
  AC4 — redact_dispatch_package filters visibility-tagged dispatches for all three
  AC5 — Lie-detector watcher has engagement witnesses for all three

Project rule coverage:
  - "Every Test Suite Needs a Wiring Test" — watcher witness registration,
    router vocabulary, bank pipeline integration
  - "No Source-Text Wiring Tests" — uses runtime imports, dict membership,
    and behavioral assertions; never greps source
  - "No Silent Fallbacks" — watcher must raise on malformed params for these
    subsystems (same discipline as confrontation/magic/scenario)
  - "OTEL Observability Principle" — every subsystem dispatch must emit
    intent_router.subsystem span; every mismatch must emit
    dispatch_engagement.{subsystem}.mismatch span
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.subsystems import (
    SubsystemOutput,
    run_dispatch_bank,
)
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.protocol.dispatch import (
    DispatchPackage,
    NarratorDirective,
    PlayerDispatch,
    SubsystemDispatch,
    VisibilityTag,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _tag_all() -> VisibilityTag:
    return VisibilityTag(
        visible_to="all",
        perception_fidelity={},
        secrets_for=[],
        redact_from_narrator_canonical=False,
    )


def _tag_redacted(who: str) -> VisibilityTag:
    return VisibilityTag(
        visible_to=[who],
        perception_fidelity={},
        secrets_for=[who],
        redact_from_narrator_canonical=True,
    )


def _make_dispatch(
    subsystem: str,
    key: str,
    *,
    params: dict[str, Any] | None = None,
    visibility: VisibilityTag | None = None,
) -> SubsystemDispatch:
    return SubsystemDispatch(
        subsystem=subsystem,
        params=params or {},
        depends_on=[],
        idempotency_key=key,
        visibility=visibility or _tag_all(),
    )


def _make_package(dispatches: list[SubsystemDispatch]) -> DispatchPackage:
    return DispatchPackage(
        turn_id="t-59-7",
        per_player=[
            PlayerDispatch(
                player_id="player:Alice",
                raw_action="(synthetic for 59-7 wiring test)",
                resolved=[],
                dispatch=dispatches,
                lethality=[],
                narrator_instructions=[],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )


def _minimal_pool() -> list[NpcPoolMember]:
    return [
        NpcPoolMember(
            name="Harlan",
            role="innkeeper",
            pronouns="he/him",
            appearance="grey beard, apron",
            drawn_from="world_authored",
        ),
    ]


# ---------------------------------------------------------------------------
# AC1 — npc_agency dispatch engages handler via bank + OTEL span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_npc_agency_dispatch_engages_handler_through_bank(otel_capture):
    """AC1: Router dispatches npc_agency with NPC params. Handler is invoked
    via run_dispatch_bank. OTEL span intent_router.subsystem fires with
    subsystem=npc_agency and produced_directives >= 1.

    This proves the bank→handler→OTEL pipeline is live for npc_agency,
    not just registered for symmetry with the offline corpus runner.
    """
    d = _make_dispatch(
        "npc_agency",
        "ac1-npc",
        params={"npc_name": "Harlan", "situation": "player enters the inn"},
    )
    pkg = _make_package([d])
    res = await run_dispatch_bank(pkg, context={"npc_pool": _minimal_pool()})

    assert res.errors == [], f"npc_agency handler must not raise: {res.errors}"
    assert "ac1-npc" in res.outputs_by_key, "npc_agency output must be in bank result"
    out = res.outputs_by_key["ac1-npc"]
    assert len(out.directives) >= 1, "npc_agency must produce at least one directive"
    assert out.directives[0].kind == "must_narrate"

    spans = otel_capture.get_finished_spans()
    sub_spans = [s for s in spans if s.name == "intent_router.subsystem"]
    npc_spans = [
        s for s in sub_spans if dict(s.attributes or {}).get("subsystem") == "npc_agency"
    ]
    assert len(npc_spans) == 1, (
        f"expected exactly 1 intent_router.subsystem span for npc_agency; "
        f"got {len(npc_spans)} (all sub spans: "
        f"{[(s.name, dict(s.attributes or {})) for s in sub_spans]})"
    )
    attrs = dict(npc_spans[0].attributes or {})
    assert attrs["produced_directives"] >= 1


# ---------------------------------------------------------------------------
# AC2 — distinctive_detail_hint dispatch engages handler via bank + OTEL span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_distinctive_detail_hint_dispatch_engages_handler_through_bank(otel_capture):
    """AC2: Router dispatches distinctive_detail_hint with target+hint params.
    Handler is invoked. OTEL span fires with subsystem=distinctive_detail_hint
    and produced_directives == 1.
    """
    d = _make_dispatch(
        "distinctive_detail_hint",
        "ac2-dd",
        params={"target": "npc:goblin_chief", "hint": "scarred left eye"},
    )
    pkg = _make_package([d])
    res = await run_dispatch_bank(pkg)

    assert res.errors == []
    assert "ac2-dd" in res.outputs_by_key
    out = res.outputs_by_key["ac2-dd"]
    assert len(out.directives) == 1
    assert out.directives[0].kind == "distinctive_detail_for_referent"
    assert "scarred left eye" in out.directives[0].payload

    spans = otel_capture.get_finished_spans()
    sub_spans = [s for s in spans if s.name == "intent_router.subsystem"]
    dd_spans = [
        s
        for s in sub_spans
        if dict(s.attributes or {}).get("subsystem") == "distinctive_detail_hint"
    ]
    assert len(dd_spans) == 1
    assert dict(dd_spans[0].attributes or {})["produced_directives"] == 1


# ---------------------------------------------------------------------------
# AC3 — reflect_absence dispatch engages handler via bank + OTEL span
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_reflect_absence_dispatch_engages_handler_through_bank(otel_capture):
    """AC3: Router dispatches reflect_absence. Handler produces must_not_narrate
    and must_narrate directives. OTEL span fires with subsystem=reflect_absence
    and produced_directives == 2.
    """
    d = _make_dispatch(
        "reflect_absence",
        "ac3-ra",
        params={"addressee_hint": "nobody present"},
    )
    pkg = _make_package([d])
    res = await run_dispatch_bank(pkg)

    assert res.errors == []
    assert "ac3-ra" in res.outputs_by_key
    out = res.outputs_by_key["ac3-ra"]
    kinds = {d.kind for d in out.directives}
    assert "must_not_narrate" in kinds
    assert "must_narrate" in kinds

    spans = otel_capture.get_finished_spans()
    sub_spans = [s for s in spans if s.name == "intent_router.subsystem"]
    ra_spans = [
        s
        for s in sub_spans
        if dict(s.attributes or {}).get("subsystem") == "reflect_absence"
    ]
    assert len(ra_spans) == 1
    assert dict(ra_spans[0].attributes or {})["produced_directives"] == 2


# ---------------------------------------------------------------------------
# AC4 — redact_dispatch_package filters visibility-tagged dispatches
# ---------------------------------------------------------------------------


def test_ac4_redaction_strips_npc_agency_dispatch_with_redact_flag():
    """AC4: An npc_agency dispatch with redact_from_narrator_canonical=True
    is stripped by redact_dispatch_package. The narrator never sees it."""
    from sidequest.agents.prompt_redaction import redact_dispatch_package

    d_redacted = _make_dispatch(
        "npc_agency",
        "k-redacted",
        params={"npc_name": "Harlan", "situation": "secret meeting"},
        visibility=_tag_redacted("player:Alice"),
    )
    d_open = _make_dispatch(
        "reflect_absence",
        "k-open",
        params={"addressee_hint": "nobody"},
    )
    pkg = _make_package([d_redacted, d_open])
    redacted, removed = redact_dispatch_package(pkg)

    assert len(removed) == 1
    assert removed[0].idempotency_key == "k-redacted"
    assert len(redacted.per_player[0].dispatch) == 1
    assert redacted.per_player[0].dispatch[0].idempotency_key == "k-open"


def test_ac4_redaction_strips_distinctive_detail_directive_with_redact_flag():
    """AC4: A distinctive_detail_hint directive tagged for redaction is
    stripped from narrator_instructions. Proves the directive visibility
    tag (inherited from the dispatch) is honored by redaction."""
    from sidequest.agents.prompt_redaction import redact_dispatch_package

    pkg = DispatchPackage(
        turn_id="t-ac4-dd",
        per_player=[
            PlayerDispatch(
                player_id="player:Bob",
                raw_action="look at the merchant",
                resolved=[],
                dispatch=[],
                lethality=[],
                narrator_instructions=[
                    NarratorDirective(
                        kind="distinctive_detail_for_referent",
                        payload="name the merchant by her silver brooch",
                        visibility=_tag_redacted("player:Bob"),
                    ),
                    NarratorDirective(
                        kind="must_narrate",
                        payload="The market is busy.",
                        visibility=_tag_all(),
                    ),
                ],
            )
        ],
        cross_player=[],
        confidence_global=1.0,
    )
    redacted, removed = redact_dispatch_package(pkg)

    assert len(removed) == 1
    assert len(redacted.per_player[0].narrator_instructions) == 1
    assert redacted.per_player[0].narrator_instructions[0].payload == "The market is busy."


def test_ac4_redaction_strips_reflect_absence_dispatch_with_redact_flag():
    """AC4: A reflect_absence dispatch tagged for narrator-canonical redaction
    is stripped. Proves redaction applies to all three subsystem types, not
    just the subsystem names hard-coded pre-59-7."""
    from sidequest.agents.prompt_redaction import redact_dispatch_package

    d = _make_dispatch(
        "reflect_absence",
        "k-ra-secret",
        params={"addressee_hint": "the missing NPC"},
        visibility=_tag_redacted("player:Alice"),
    )
    pkg = _make_package([d])
    redacted, removed = redact_dispatch_package(pkg)

    assert len(removed) == 1
    assert removed[0].idempotency_key == "k-ra-secret"
    assert redacted.per_player[0].dispatch == []


# ---------------------------------------------------------------------------
# AC5 — Lie-detector watcher has engagement witnesses for all three
# ---------------------------------------------------------------------------


def test_ac5_watcher_witnesses_include_npc_agency():
    """AC5: The watcher's _WITNESSES dict must include npc_agency.

    Without this, the watcher silently skips npc_agency dispatches —
    the router could dispatch npc_agency, the narrator could wing the
    NPC response without the engine engaging, and the GM panel would
    never know. The lie detector must cover all live-path subsystems.
    """
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "npc_agency" in _WITNESSES, (
        "watcher _WITNESSES must include npc_agency. Without an engagement "
        "witness, router→npc_agency dispatches are invisible to the lie detector."
    )


def test_ac5_watcher_witnesses_include_distinctive_detail_hint():
    """AC5: _WITNESSES must include distinctive_detail_hint."""
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "distinctive_detail_hint" in _WITNESSES, (
        "watcher _WITNESSES must include distinctive_detail_hint."
    )


def test_ac5_watcher_witnesses_include_reflect_absence():
    """AC5: _WITNESSES must include reflect_absence."""
    from sidequest.agents.dispatch_engagement_watcher import _WITNESSES

    assert "reflect_absence" in _WITNESSES, (
        "watcher _WITNESSES must include reflect_absence."
    )


def test_ac5_dispatched_type_key_includes_all_three():
    """AC5: _DISPATCHED_TYPE_KEY must have entries for all three subsystems
    so the mismatch record can reference the dispatched value."""
    from sidequest.agents.dispatch_engagement_watcher import _DISPATCHED_TYPE_KEY

    for name in ("npc_agency", "distinctive_detail_hint", "reflect_absence"):
        assert name in _DISPATCHED_TYPE_KEY, (
            f"_DISPATCHED_TYPE_KEY must include {name!r} so mismatch records "
            f"can reference the dispatched param value."
        )


def test_ac5_npc_agency_dispatched_with_npc_not_in_pool_emits_mismatch():
    """AC5: Router dispatched npc_agency for NPC 'Stranger' but the NPC is
    not in snapshot.npc_pool → mismatch span fires.

    This is the npc_agency engagement witness: the subsystem returns
    data['error'] = 'npc_not_registered' when the NPC isn't found.
    The watcher should detect this as a mismatch — the router dispatched
    but the engine couldn't engage on the snapshot's actual NPC state.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.game.session import GameSnapshot

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    d = _make_dispatch(
        "npc_agency",
        "ac5-npc-miss",
        params={"npc_name": "Stranger", "situation": "spotted"},
    )
    pkg = _make_package([d])
    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
    )

    run_dispatch_engagement_watcher(package=pkg, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1, (
        f"expected 1 mismatch span for npc_agency; got {len(spans)}: "
        f"{[s.name for s in spans]}"
    )
    assert spans[0].name == "dispatch_engagement.npc_agency.mismatch"
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("subsystem") == "npc_agency"


def test_ac5_npc_agency_dispatched_with_npc_in_pool_emits_no_mismatch():
    """AC5 happy path: npc_agency dispatched for an NPC that IS in the pool →
    no mismatch span. The engine engaged (or at least could have)."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.game.session import GameSnapshot

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    d = _make_dispatch(
        "npc_agency",
        "ac5-npc-hit",
        params={"npc_name": "Harlan", "situation": "spotted"},
    )
    pkg = _make_package([d])
    snap = GameSnapshot(
        genre_slug="test_genre",
        world_slug="test_world",
        npc_pool=_minimal_pool(),
    )

    run_dispatch_engagement_watcher(package=pkg, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == [], f"expected zero mismatch spans; got: {[s.name for s in spans]}"


def test_ac5_distinctive_detail_dispatched_emits_no_mismatch():
    """AC5: distinctive_detail_hint is a directive-only subsystem — it always
    produces output when params are valid. No snapshot dependency means
    the witness should always pass (no mismatch) for well-formed dispatches.
    """
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.game.session import GameSnapshot

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    d = _make_dispatch(
        "distinctive_detail_hint",
        "ac5-dd-ok",
        params={"target": "npc:merchant", "hint": "silver brooch"},
    )
    pkg = _make_package([d])
    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")

    run_dispatch_engagement_watcher(package=pkg, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == [], f"expected zero mismatch spans; got: {[s.name for s in spans]}"


def test_ac5_reflect_absence_dispatched_emits_no_mismatch():
    """AC5: reflect_absence is directive-only — always produces directives.
    No snapshot dependency, so the witness always passes."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.game.session import GameSnapshot

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    d = _make_dispatch(
        "reflect_absence",
        "ac5-ra-ok",
        params={"addressee_hint": "nobody"},
    )
    pkg = _make_package([d])
    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")

    run_dispatch_engagement_watcher(package=pkg, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert spans == [], f"expected zero mismatch spans; got: {[s.name for s in spans]}"


def test_ac5_multiple_59_7_subsystems_in_one_turn_watched_independently():
    """AC5 integration: A turn with all three 59-7 subsystems dispatched.
    npc_agency with a missing NPC → 1 mismatch. distinctive_detail_hint
    and reflect_absence → 0 mismatches each. Total: exactly 1 span."""
    from sidequest.agents.dispatch_engagement_watcher import (
        run_dispatch_engagement_watcher,
    )
    from sidequest.game.session import GameSnapshot

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    dispatches = [
        _make_dispatch(
            "npc_agency",
            "k-npc",
            params={"npc_name": "Nobody", "situation": "test"},
        ),
        _make_dispatch(
            "distinctive_detail_hint",
            "k-dd",
            params={"target": "npc:goblin", "hint": "red hat"},
        ),
        _make_dispatch(
            "reflect_absence",
            "k-ra",
            params={"addressee_hint": "empty room"},
        ),
    ]
    pkg = _make_package(dispatches)
    snap = GameSnapshot(genre_slug="test_genre", world_slug="test_world")

    run_dispatch_engagement_watcher(package=pkg, snapshot=snap, tracer=tracer)

    spans = [s for s in exporter.get_finished_spans() if "dispatch_engagement" in s.name]
    assert len(spans) == 1, (
        f"expected 1 mismatch (npc_agency only); got {len(spans)}: "
        f"{[s.name for s in spans]}"
    )
    assert spans[0].name == "dispatch_engagement.npc_agency.mismatch"


# ---------------------------------------------------------------------------
# Router vocabulary — the system prompt must mention these subsystems
# ---------------------------------------------------------------------------


def test_intent_router_prompt_includes_npc_agency_in_vocabulary():
    """The router's system prompt must explicitly mention npc_agency so
    Haiku knows to emit dispatches for NPC-interaction intents. Without
    this, the subsystem is registered in the bank but the router never
    produces dispatches for it — the handler stays dormant on the live path.
    """
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert "npc_agency" in _SYSTEM_PROMPT, (
        "_SYSTEM_PROMPT must mention npc_agency in the subsystem dispatch "
        "vocabulary. Without it, Haiku never emits npc_agency dispatches "
        "and the handler stays dormant."
    )


def test_intent_router_prompt_includes_distinctive_detail_hint_in_vocabulary():
    """The router's system prompt must mention distinctive_detail_hint."""
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert "distinctive_detail_hint" in _SYSTEM_PROMPT, (
        "_SYSTEM_PROMPT must mention distinctive_detail_hint."
    )


def test_intent_router_prompt_includes_reflect_absence_in_vocabulary():
    """The router's system prompt must mention reflect_absence."""
    from sidequest.agents.intent_router import _SYSTEM_PROMPT

    assert "reflect_absence" in _SYSTEM_PROMPT, (
        "_SYSTEM_PROMPT must mention reflect_absence."
    )


# ---------------------------------------------------------------------------
# DORMANT marker removal verification
# ---------------------------------------------------------------------------


def test_npc_agency_module_no_longer_marked_dormant():
    """The npc_agency module docstring must not contain 'DORMANT' after
    59-7 wires it onto the live path. A leftover DORMANT marker is
    misleading — it tells future readers the module is dead code when it's
    actually live."""
    import sidequest.agents.subsystems.npc_agency as mod

    assert "DORMANT" not in (mod.__doc__ or ""), (
        "npc_agency.__doc__ still says DORMANT — update it now that "
        "59-7 wires the module onto the live path."
    )


def test_distinctive_detail_module_no_longer_marked_dormant():
    """distinctive_detail module docstring must not contain 'DORMANT'."""
    import sidequest.agents.subsystems.distinctive_detail as mod

    assert "DORMANT" not in (mod.__doc__ or ""), (
        "distinctive_detail.__doc__ still says DORMANT."
    )


def test_reflect_absence_module_no_longer_marked_dormant():
    """reflect_absence module docstring must not contain 'DORMANT'."""
    import sidequest.agents.subsystems.reflect_absence as mod

    assert "DORMANT" not in (mod.__doc__ or ""), (
        "reflect_absence.__doc__ still says DORMANT."
    )


def test_prompt_redaction_module_no_longer_marked_dormant():
    """prompt_redaction module docstring must not contain 'DORMANT' — it's
    live on the narrator prompt assembly path (orchestrator.py line ~1600).
    """
    import sidequest.agents.prompt_redaction as mod

    assert "DORMANT" not in (mod.__doc__ or ""), (
        "prompt_redaction.__doc__ still says DORMANT but redact_dispatch_package "
        "is called on the live narrator prompt path."
    )
