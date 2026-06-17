"""Failing tests for Story 93-1: Haiku archetype inference unblocks all-freeform chargen.

[BAR-1] pingpong playtest: a player who answers hint-bearing chargen scenes
via the "describe it in your own words" box accumulates ZERO archetype hints
(``builder.apply_freeform`` records the text but sets neither ``jungian_hint``
nor ``rpg_role_hint``), so axis-bearing packs (heavy_metal, elemental_harmony)
dead-end at confirm with ``missing_axes_with_pack_axes``. The fail-loud gate
is correct; freeform just offers no path to resolution.

Story 93-1 intercepts at the gate: when the archetype gate would BLOCK with
``missing_axes_with_pack_axes`` AND the player supplied freeform answers, a
single Haiku call (intent-router ``emit_tool`` pattern, ADR-102/ADR-113)
infers the missing axis value(s) from the accumulated freeform text,
constrained to the pack's valid axis ids. Contract pinned here:

- ``sidequest.agents.llm_factory.infer_archetype_from_freeform`` — async,
  keyword-only: ``freeform_text``, ``base`` (BaseArchetypes), ``constraints``
  (ArchetypeConstraints), ``existing_hints``, ``session_id``. Returns
  ``dict[str, str]`` with ONLY the newly inferred axes, or ``None`` when
  Haiku produced an out-of-enum value or there was nothing to infer from.
  (Param shape mirrors ``resolve_archetype(base=, constraints=)`` — the
  valid axis ids live on ``BaseArchetypes``, not on the constraints model.)
- The transport is the module-level ``llm_factory.query`` agent-SDK seam
  (Story 119-3, the successor to ``build_async_anthropic``) — these tests
  fake THAT seam with ``FakeQuery(structured_output_stream({...}))`` and
  read ``ResultMessage.structured_output``, the same monkeypatch doctrine
  the other 119-3 Haiku-site tests use.
- Fill strategy: only missing axes are filled; a preset-set hint is NEVER
  overridden, even if Haiku returns a value for it.
- Fail-safe (No Silent Fallbacks): out-of-enum → loud chargen error, no
  coercion, no pack-default. No freeform to infer from → the existing
  ``missing_axes_with_pack_axes`` block stands and Haiku is never called.
  (Design note for worlds WITH a name-entry scene: the name answer must
  not count as inference fodder — every player types a name, so counting
  it would make the fail-loud path unreachable. barsoom has no name
  scene, so AC4 here exercises the zero-freeform case directly.)
- Observability: ``chargen.archetype_inferred`` span
  (``SPAN_CHARGEN_ARCHETYPE_INFERRED``) on every successful inference with
  ``inferred_axes`` / ``jungian_hint`` / ``rpg_role_hint`` /
  ``source="freeform"`` attrs, routed to the GM panel via ``SPAN_ROUTES``
  (OTEL Observability Principle — the lie detector must distinguish
  "inference fired" from "preset accumulation").
- Cost (ADR-134): the Haiku call records to the session cost ledger and
  respects the pre-flight hard ceiling.

Wire-first discipline (CLAUDE.md "Every Test Suite Needs a Wiring Test"):
the integration tests drive the WS dispatch layer end-to-end —
``handler.handle_message(CharacterCreationMessage(phase="confirmation"))``
against real heavy_metal/barsoom content — so the inference must be
reachable from the production chargen-confirm seam, not a unit-tested
helper bolted on the side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.builder import AccumulatedChoices, CharacterBuilder
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler, _State
from tests.agents.fakes.fake_agent_sdk import FakeQuery, structured_output_stream
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

_INFERRED_SPAN_NAME = "chargen.archetype_inferred"

_CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _load_heavy_metal_pack():
    """Load the real heavy_metal pack with an explicit content root — the
    bare ``GenreLoader()`` default search paths don't resolve from the test
    process CWD in every environment."""
    from sidequest.genre.loader import GenreLoader

    if not (_CONTENT_ROOT / "heavy_metal").is_dir():
        pytest.skip("heavy_metal content not found")
    pack = GenreLoader(search_paths=[_CONTENT_ROOT]).load("heavy_metal")
    assert pack.base_archetypes is not None, "heavy_metal must be axis-bearing"
    assert pack.archetype_constraints is not None
    return pack


# A common pairing in heavy_metal/archetype_constraints.yaml — the canonical
# valid inference result for these tests.
_VALID_JUNGIAN = "hero"
_VALID_RPG_ROLE = "tank"

# Definitely not a Jungian archetype id in archetypes_base.yaml.
_OUT_OF_ENUM = "starlord"

_FREEFORM_ORIGIN = (
    "I was born in the slag-quarters under the foundry stacks, third child "
    "of a debt-bonded smith. When the collectors came for my sister I broke "
    "the foreman's arm with his own ledger-rod and carried her across the "
    "ash flats. I protect what is mine, and I stand in front when it counts."
)
_FREEFORM_NAME = "Dejah Voss"


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/server/test_45_6_chargen_archetype_gate.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Story 119-3: the archetype-inference Haiku call drives the agent-SDK
    ``query`` seam through ``build_agent_sdk_options``, which asserts the Max
    subscription invariant — both PAYG credentials UNSET (a set key re-routes
    to PAYG and raises ``AgentSdkAuthUnavailable``). Pin the subscription world
    so the fake transport is reachable regardless of the ambient environment."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Point the process-global pool at a per-worker throwaway PG database.

    Same isolation doctrine as the 45-6 gate tests: TRUNCATE per-test so a
    fixed-slug row from a sibling test can't leak a pre-built character into
    this test's connect, which would skip the chargen branch entirely.
    """
    import psycopg

    from sidequest.game import db_pool

    plain = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"
        ).fetchall()
        if rows:
            names = ", ".join(f'"{r[0]}"' for r in rows)
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture
def save_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def handler_factory(save_dir: Path):
    """Build a WebSocketSessionHandler bound to real content.

    heavy_metal/barsoom is the AC1 substrate: the pack declares
    ``base_archetypes`` + ``archetype_constraints`` (axis-bearing, so the
    BLOCKED_PARTIAL case is reachable) and every hint-bearing barsoom
    chargen scene sets ``allows_freeform: true`` — the all-freeform walk
    accumulates zero hints, which is exactly the [BAR-1] dead-end.
    """
    content_root = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if not (content_root / "heavy_metal" / "worlds" / "barsoom").is_dir():
        pytest.skip("heavy_metal/barsoom content not found")

    def make() -> WebSocketSessionHandler:
        return WebSocketSessionHandler(
            claude_client_factory=_mock_claude_client_factory(),
            genre_pack_search_paths=[content_root],
            save_dir=save_dir,
        )

    return make


@pytest.fixture
def otel_capture():
    """Install an in-memory OTEL exporter on the REAL tracer provider.

    AC6 requires the span assertion to ride a real OTEL context, not a
    mocked emit — same exporter doctrine as the 45-6 gate tests.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


@pytest.fixture
def fresh_ledger():
    """Reset the ADR-134 session cost ledger around the test (AC7)."""
    from sidequest.agents import cost_safety

    cost_safety.ledger().reset_for_tests()
    yield cost_safety.ledger()
    cost_safety.ledger().reset_for_tests()


# ---------------------------------------------------------------------------
# Fake Haiku transport (patched at the agent-SDK ``query`` seam, story 119-3)
# ---------------------------------------------------------------------------


def _fake_inference_sdk(
    monkeypatch: pytest.MonkeyPatch, *, tool_input: dict[str, Any]
) -> FakeQuery:
    """Patch ``llm_factory.query`` (the agent-SDK transport seam, Story 119-3)
    with a :class:`FakeQuery` that yields a terminal ``ResultMessage`` whose
    ``structured_output`` carries ``tool_input`` — the forced-extraction
    Path A surface that replaced the raw ``tool_use``-block read.

    Returns the ``FakeQuery`` so tests can assert on ``.calls`` (call count)
    and ``.calls[-1].prompt`` (the user message that reached the transport).
    The default ``fake_usage()`` rides the stream so cost accounting bills a
    non-zero Haiku call (the ADR-134 ledger assertions depend on it).
    """
    from sidequest.agents import llm_factory

    fake = FakeQuery(structured_output_stream(dict(tool_input)))
    monkeypatch.setattr(llm_factory, "query", fake, raising=False)
    return fake


# The archetype-inference user message always opens with this header
# (``infer_archetype_from_freeform`` builds it). Story 119-3 routes ALL Haiku
# single-shots through the shared ``llm_factory.query`` seam, so a single
# ``FakeQuery`` also records the post-narration objective classifier that fires
# during the opening turn. Count ONLY the archetype-inference calls so the
# assertion's original intent ("one inference per confirm") survives the
# now-shared seam.
def _inference_calls(fake: FakeQuery) -> list:
    return [c for c in fake.calls if str(c.prompt).startswith("Missing axes to infer:")]


# ---------------------------------------------------------------------------
# WS-driven helpers
# ---------------------------------------------------------------------------


async def _connect(
    handler: WebSocketSessionHandler,
    *,
    player_name: str = "Dejah",
    genre: str = "heavy_metal",
    world: str = "barsoom",
) -> SessionEventMessage:
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre=genre, world=world)
    attach_default_room_context(handler)
    payload = SessionEventPayload(
        event="connect",
        player_name=player_name,
        game_slug=slug,
    )
    out = await handler.handle_message(SessionEventMessage(payload=payload, player_id=""))
    assert isinstance(out[0], SessionEventMessage)
    return out[0]


async def _walk_all_freeform(handler: WebSocketSessionHandler) -> None:
    """Walk every chargen scene answering via freeform — zero preset picks.

    Hint-bearing barsoom scenes all set ``allows_freeform: true``, so this
    walk accumulates NO jungian/rpg_role hints: the exact [BAR-1] player
    behavior. The terminal name-entry scene (no choices) gets a name.
    """
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None, "connect must construct a chargen builder"

    while not builder.is_confirmation():
        scene = builder.current_scene()
        if not scene.choices:
            if scene.allows_freeform or scene.allows_freeform is None:
                payload = CharacterCreationPayload(phase="scene", choice=_FREEFORM_NAME)
            else:
                payload = CharacterCreationPayload(phase="continue")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice=_FREEFORM_ORIGIN)
        else:
            # Scene refuses freeform — nothing hint-bearing on barsoom does,
            # but stay walkable rather than asserting content shape here.
            payload = CharacterCreationPayload(phase="scene", choice="1")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


async def _walk_presets_only(handler: WebSocketSessionHandler) -> None:
    """Walk every chargen scene via canned choice 1 — zero freeform
    answers accumulate (barsoom has no name-entry scene; the character
    name comes from the lobby player_name). The freeform branch below is
    defensive for content drift."""
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None

    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform or scene.allows_freeform is None:
            payload = CharacterCreationPayload(phase="scene", choice=_FREEFORM_NAME)
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


async def _send_confirmation(handler: WebSocketSessionHandler) -> list:
    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("chargen_confirmation"):
        return await handler.handle_message(
            CharacterCreationMessage(
                payload=CharacterCreationPayload(phase="confirmation"),
                player_id="pid",
            )
        )


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _inject_hints(
    monkeypatch: pytest.MonkeyPatch,
    *,
    jungian: str | None,
    rpg_role: str | None,
) -> None:
    """Force the given hint pair onto the builder's accumulated state —
    the durable seam (the accumulator is recomputed on every call)."""
    real = CharacterBuilder.accumulated

    def fake(self: CharacterBuilder) -> AccumulatedChoices:
        acc = real(self)
        acc.jungian_hint = jungian
        acc.rpg_role_hint = rpg_role
        return acc

    monkeypatch.setattr(CharacterBuilder, "accumulated", fake)


def _disable_default_hint_stamping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the GENUINE ``CharacterBuilder.accumulated``.

    The autouse ``_default_archetype_hints`` fixture (tests/server/
    conftest.py) stamps ``hero``/``tank`` onto any builder whose hints are
    both None — it exists to defeat the 45-6 gate for server tests that
    don't care about chargen. These tests are EXACTLY about the both-None
    state ([BAR-1]: all-freeform accumulates zero hints), so the stamping
    must come off or the gate never blocks and the inference path is
    unreachable. The original function is recovered from the fake's
    closure (the fake is marked with ``_is_default_archetype_hints_fake``
    for exactly this kind of detection — see the conftest's own
    ``fake_gate``). ``StopIteration`` here means the conftest fixture
    changed shape — update this helper alongside it."""
    current = CharacterBuilder.accumulated
    if getattr(current, "_is_default_archetype_hints_fake", False):
        real = next(
            cell.cell_contents
            for cell in (current.__closure__ or ())
            if getattr(cell.cell_contents, "__name__", "") == "accumulated"
        )
        monkeypatch.setattr(CharacterBuilder, "accumulated", real)


# ---------------------------------------------------------------------------
# AC1 + AC6 — all-freeform chargen succeeds via inference, wired end-to-end
# ---------------------------------------------------------------------------


class TestAllFreeformInferenceUnblocks:
    """AC1/AC6: heavy_metal/barsoom, every scene answered freeform. Today
    this dead-ends with ``missing_axes_with_pack_axes``; with 93-1 the
    confirm seam must run the Haiku inference and ship the character."""

    async def test_all_freeform_chargen_succeeds_end_to_end(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
        fresh_ledger,
    ) -> None:
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        _disable_default_hint_stamping(monkeypatch)
        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)

        out = await _send_confirmation(handler)
        assert out, "confirmation must produce at least one frame"
        for msg in out:
            assert not isinstance(msg, ErrorMessage), (
                "all-freeform chargen on an axis-bearing pack must succeed "
                f"via archetype inference (93-1); got ERROR: {msg.payload.message!r}"
            )

        # The inference is REACHABLE from the production confirm seam — the
        # wiring assertion. A stubbed/mocked gate never touches the agent-SDK
        # query seam.
        inference_calls = _inference_calls(fake_query)
        assert len(inference_calls) >= 1, (
            "the Haiku inference call must be reachable from the production "
            "chargen-confirm flow (the agent-SDK query seam was never reached)"
        )
        # Single per-chargen call — cost scales with drama, not with retries.
        assert len(inference_calls) == 1, (
            f"expected exactly one inference call per chargen confirm, got {len(inference_calls)}"
        )

        sd = handler._session_data  # type: ignore[attr-defined]
        assert sd.snapshot.characters, "inference-unblocked chargen must persist the character"
        character = sd.snapshot.characters[0]
        assert character.resolved_archetype is not None
        assert "/" not in character.resolved_archetype, (
            "the inferred pair must run through the resolver to a display "
            f"name, not ship raw; got {character.resolved_archetype!r}"
        )
        assert character.archetype_provenance is not None, (
            "inference must end in apply_archetype_resolved (provenance "
            "stamped in lockstep) — the gate's OK_RESOLVED signal"
        )
        assert handler._state == _State.Playing  # type: ignore[attr-defined]

        # AC5: the lie-detector span fired on the real OTEL context.
        spans = _spans_named(otel_capture, _INFERRED_SPAN_NAME)
        assert spans, (
            f"successful inference must emit the {_INFERRED_SPAN_NAME!r} span "
            "(GM panel must distinguish inference from preset accumulation)"
        )
        attrs = spans[0].attributes or {}
        assert attrs.get("source") == "freeform"
        assert set(attrs.get("inferred_axes", ())) == {"jungian_hint", "rpg_role_hint"}, (
            "all-freeform chargen infers BOTH axes; inferred_axes must say so"
        )
        assert attrs.get("jungian_hint") == _VALID_JUNGIAN
        assert attrs.get("rpg_role_hint") == _VALID_RPG_ROLE

        # AC7 (session delta): the inference call billed the ADR-134 ledger.
        assert fresh_ledger.instrumented_total_usd() > 0.0, (
            "the chargen inference Haiku call must record to the session "
            "cost ledger (ADR-134) — no dark spend"
        )

    async def test_archetype_matches_inferred_pair_not_pack_default(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        """AC6: the shipped archetype must derive from the INFERRED pair.
        Two different inferred pairs must not collapse onto one pack
        default — that would be a silent fallback wearing a success face."""
        from sidequest.genre.archetype.shim import resolve_archetype

        pack = _load_heavy_metal_pack()
        expected = resolve_archetype(
            jungian=_VALID_JUNGIAN,
            rpg_role=_VALID_RPG_ROLE,
            base=pack.base_archetypes,
            constraints=pack.archetype_constraints,
            funnels=None,
            genre="heavy_metal",
            world=None,
        )

        _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        _disable_default_hint_stamping(monkeypatch)
        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)
        out = await _send_confirmation(handler)
        for msg in out:
            assert not isinstance(msg, ErrorMessage), msg.payload.message

        sd = handler._session_data  # type: ignore[attr-defined]
        character = sd.snapshot.characters[0]
        assert character.resolved_archetype == expected.resolved.name, (
            f"character must carry the archetype resolved from the inferred "
            f"{_VALID_JUNGIAN}/{_VALID_RPG_ROLE} pair "
            f"({expected.resolved.name!r}); got {character.resolved_archetype!r}"
        )


# ---------------------------------------------------------------------------
# AC2 — out-of-enum Haiku output is rejected loudly (no coercion)
# ---------------------------------------------------------------------------


class TestOutOfEnumRejected:
    async def test_out_of_enum_inference_fails_loud(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _OUT_OF_ENUM,  # not a valid Jungian id
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        _disable_default_hint_stamping(monkeypatch)
        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)

        import logging

        with caplog.at_level(logging.WARNING):
            out = await _send_confirmation(handler)

        errors = [m for m in out if isinstance(m, ErrorMessage)]
        assert errors, (
            "out-of-enum Haiku output must fail chargen loudly — no "
            "coercion, no pack-default fallback (No Silent Fallbacks)"
        )
        assert "inference" in str(errors[0].payload.message).lower(), (
            "the error must name inference as the failure so the player/"
            "operator can distinguish it from the plain missing-axes block; "
            f"got: {errors[0].payload.message!r}"
        )

        sd = handler._session_data  # type: ignore[attr-defined]
        assert not sd.snapshot.characters, "a rejected inference must not persist a character"
        assert handler._state != _State.Playing  # type: ignore[attr-defined]

        # Nothing was inferred — the success span must NOT fire (it would
        # tell the GM panel an inference landed when it was rejected).
        assert not _spans_named(otel_capture, _INFERRED_SPAN_NAME), (
            f"{_INFERRED_SPAN_NAME!r} must not fire for a rejected inference"
        )

        # python.md rule 4: the error path logs to the structured server
        # log surface, independent of the OTEL pipeline.
        assert any("infer" in rec.getMessage().lower() for rec in caplog.records), (
            "rejected inference must log a WARNING naming the inference failure"
        )


# ---------------------------------------------------------------------------
# AC3 — partial preset + freeform: fill only the missing axis, never override
# ---------------------------------------------------------------------------


class TestPartialPresetInference:
    async def test_inference_fills_only_missing_axis_never_overrides_preset(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        """Preset sets jungian='ruler'; freeform must supply rpg_role. The
        fake Haiku ADVERSARIALLY returns both axes — the preset-set jungian
        must survive untouched ('ruler'/'support' is a common heavy_metal
        pairing, so the override would also have resolved: only the
        provenance of the hint distinguishes right from wrong here)."""
        from sidequest.genre.archetype.shim import resolve_archetype

        _fake_inference_sdk(
            monkeypatch,
            tool_input={
                # Adversarial: tries to override the preset jungian too.
                "jungian_hint": _VALID_JUNGIAN,  # hero ≠ preset ruler
                "rpg_role_hint": "support",
            },
        )
        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)
        # Preset path set one axis; the other is missing.
        _inject_hints(monkeypatch, jungian="ruler", rpg_role=None)

        out = await _send_confirmation(handler)
        for msg in out:
            assert not isinstance(msg, ErrorMessage), (
                f"partial-preset inference must succeed; got {msg.payload.message!r}"
            )

        spans = _spans_named(otel_capture, _INFERRED_SPAN_NAME)
        assert spans, "successful inference must emit the inferred span"
        attrs = spans[0].attributes or {}
        assert set(attrs.get("inferred_axes", ())) == {"rpg_role_hint"}, (
            "only the MISSING axis is inferred — the preset-set jungian "
            f"must not appear in inferred_axes; got {attrs.get('inferred_axes')!r}"
        )
        assert attrs.get("jungian_hint") == "ruler", (
            "the preset-set jungian_hint must never be overridden by the "
            f"inference output; got {attrs.get('jungian_hint')!r}"
        )
        assert attrs.get("rpg_role_hint") == "support"

        pack = _load_heavy_metal_pack()
        expected = resolve_archetype(
            jungian="ruler",
            rpg_role="support",
            base=pack.base_archetypes,
            constraints=pack.archetype_constraints,
            funnels=None,
            genre="heavy_metal",
            world=None,
        )
        sd = handler._session_data  # type: ignore[attr-defined]
        character = sd.snapshot.characters[0]
        assert character.resolved_archetype == expected.resolved.name, (
            "the shipped archetype must resolve from preset-jungian + "
            "inferred-rpg_role (ruler/support), proving the preset half "
            f"won; got {character.resolved_archetype!r}"
        )


# ---------------------------------------------------------------------------
# AC4 — no freeform to infer from: the fail-loud block stands, Haiku unused
# ---------------------------------------------------------------------------


class TestNoFreeformStillFailsLoud:
    async def test_preset_only_missing_axes_still_blocks_without_haiku_call(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        """Preset-only chargen (zero freeform answers on barsoom) that
        still lacks axes must keep the existing loud block, and the Haiku
        SDK must never be touched (no spend on nothing to infer from)."""
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        handler = handler_factory()
        await _connect(handler)
        await _walk_presets_only(handler)
        # Recreate the pumblestone state: pack has axes, hints absent.
        _inject_hints(monkeypatch, jungian=None, rpg_role=None)

        out = await _send_confirmation(handler)
        errors = [m for m in out if isinstance(m, ErrorMessage)]
        assert errors, "missing axes with no freeform must still fail loud"
        assert "missing_axes_with_pack_axes" in str(errors[0].payload.message), (
            "the original block reason must be preserved on the no-freeform "
            f"path; got: {errors[0].payload.message!r}"
        )

        assert len(_inference_calls(fake_query)) == 0, (
            "with no freeform answers to infer from, the Haiku inference "
            "must not be called at all (name-scene text is not fodder)"
        )

        sd = handler._session_data  # type: ignore[attr-defined]
        assert not sd.snapshot.characters
        assert handler._state != _State.Playing  # type: ignore[attr-defined]
        assert not _spans_named(otel_capture, _INFERRED_SPAN_NAME)


# ---------------------------------------------------------------------------
# AC5 — span constant + GM-panel routing
# ---------------------------------------------------------------------------


class TestInferredSpanRegistration:
    def test_span_constant_and_gm_panel_route_exist(self) -> None:
        """``chargen.archetype_inferred`` must be a registered span constant
        with a ``SPAN_ROUTES`` entry — that routing IS the 'visible on the
        GM panel' guarantee (OTEL Observability Principle)."""
        from sidequest.telemetry.spans._core import SPAN_ROUTES
        from sidequest.telemetry.spans.chargen import (
            SPAN_CHARGEN_ARCHETYPE_INFERRED,
        )

        assert SPAN_CHARGEN_ARCHETYPE_INFERRED == _INFERRED_SPAN_NAME
        assert SPAN_CHARGEN_ARCHETYPE_INFERRED in SPAN_ROUTES, (
            "the inferred span must be routed to the GM panel via "
            "SPAN_ROUTES — an unrouted span is invisible to the lie detector"
        )
        route = SPAN_ROUTES[SPAN_CHARGEN_ARCHETYPE_INFERRED]
        assert route.component == "character_creation"


# ---------------------------------------------------------------------------
# Unit contract — infer_archetype_from_freeform (llm_factory)
# ---------------------------------------------------------------------------


def _load_heavy_metal_axes():
    pack = _load_heavy_metal_pack()
    return pack.base_archetypes, pack.archetype_constraints


class TestInferenceFunctionContract:
    async def test_returns_only_missing_axes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """jungian already set → the result contains rpg_role_hint ONLY,
        even when Haiku adversarially returns both axes."""
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": "outlaw", "rpg_role_hint": _VALID_RPG_ROLE},
        )
        result = await infer_archetype_from_freeform(
            freeform_text=_FREEFORM_ORIGIN,
            base=base,
            constraints=constraints,
            existing_hints={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": None},
            session_id="93-1-unit",
        )
        assert result == {"rpg_role_hint": _VALID_RPG_ROLE}, (
            "only the missing axis may be filled; an existing hint must "
            f"never be echoed or overridden — got {result!r}"
        )

    async def test_out_of_enum_returns_none_no_coercion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _OUT_OF_ENUM, "rpg_role_hint": _VALID_RPG_ROLE},
        )
        result = await infer_archetype_from_freeform(
            freeform_text=_FREEFORM_ORIGIN,
            base=base,
            constraints=constraints,
            existing_hints={"jungian_hint": None, "rpg_role_hint": None},
            session_id="93-1-unit",
        )
        assert result is None, (
            "an out-of-enum value invalidates the whole inference — None, "
            f"never a coerced/partial result; got {result!r}"
        )

    async def test_empty_freeform_returns_none_without_sdk_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to infer from → None, and NO Haiku spend."""
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE},
        )
        for empty in ("", "   \n\t  "):
            result = await infer_archetype_from_freeform(
                freeform_text=empty,
                base=base,
                constraints=constraints,
                existing_hints={"jungian_hint": None, "rpg_role_hint": None},
                session_id="93-1-unit",
            )
            assert result is None, (
                f"freeform_text={empty!r} has nothing to infer from; got {result!r}"
            )
        assert len(fake_query.calls) == 0, (
            "empty freeform must short-circuit BEFORE the SDK — no spend "
            "on an inference that cannot succeed"
        )

    async def test_nothing_missing_returns_empty_without_sdk_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both axes already set → {} (nothing inferred) and no SDK call.
        Distinct from None: this is a no-op, not a failure."""
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE},
        )
        result = await infer_archetype_from_freeform(
            freeform_text=_FREEFORM_ORIGIN,
            base=base,
            constraints=constraints,
            existing_hints={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
            session_id="93-1-unit",
        )
        assert result == {}, f"nothing missing → empty no-op result; got {result!r}"
        assert len(fake_query.calls) == 0

    async def test_null_axes_from_haiku_yield_empty_not_invented_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Haiku declines to infer (nulls) → empty result, no invented axis.
        The caller's gate then keeps blocking — fail loud downstream."""
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": None, "rpg_role_hint": None},
        )
        result = await infer_archetype_from_freeform(
            freeform_text=_FREEFORM_ORIGIN,
            base=base,
            constraints=constraints,
            existing_hints={"jungian_hint": None, "rpg_role_hint": None},
            session_id="93-1-unit",
        )
        assert (
            result is not None
            and not result.get("jungian_hint")
            and not result.get("rpg_role_hint")
        ), (
            "a declined inference must not invent axis values; got "
            f"{result!r} (empty dict expected — the gate fails loud later)"
        )


# ---------------------------------------------------------------------------
# AC7 — ADR-134 cost accounting and hard ceiling
# ---------------------------------------------------------------------------


class TestInferenceCostAccounting:
    async def test_inference_records_to_session_ledger(
        self, monkeypatch: pytest.MonkeyPatch, fresh_ledger
    ) -> None:
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE},
        )
        before = fresh_ledger.instrumented_total_usd()
        result = await infer_archetype_from_freeform(
            freeform_text=_FREEFORM_ORIGIN,
            base=base,
            constraints=constraints,
            existing_hints={"jungian_hint": None, "rpg_role_hint": None},
            session_id="93-1-cost-room",
        )
        assert result == {
            "jungian_hint": _VALID_JUNGIAN,
            "rpg_role_hint": _VALID_RPG_ROLE,
        }
        assert fresh_ledger.instrumented_total_usd() > before, (
            "the inference Haiku call must record to the ADR-134 ledger — "
            "an unmetered chargen call is exactly the dark-spend class the "
            "ledger exists to catch"
        )

    async def test_inference_respects_hard_ceiling_preflight(
        self, monkeypatch: pytest.MonkeyPatch, fresh_ledger
    ) -> None:
        """A session already over its ceiling must be refused BEFORE the
        SDK call — terminal refusal, not one more billed token (ADR-134)."""
        from sidequest.agents.anthropic_sdk_client import (
            AnthropicSdkCostCeilingExceeded,
        )
        from sidequest.agents.llm_factory import infer_archetype_from_freeform

        base, constraints = _load_heavy_metal_axes()
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE},
        )
        monkeypatch.setenv("SIDEQUEST_SESSION_COST_CEILING_USD", "0.01")
        session_id = "93-1-killed-room"
        # Push the session over its ceiling through the ledger's own API.
        # The crossing call itself raises (update_cumulative kills the
        # session on crossing) — that IS the simulated narrator kill; the
        # cumulative is recorded before the raise.
        with pytest.raises(AnthropicSdkCostCeilingExceeded):
            fresh_ledger.record_call(
                session_id=session_id,
                caller="narrator",
                model="claude-sonnet-4-6",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.02,
                ceiling_usd=0.01,
            )

        with pytest.raises(AnthropicSdkCostCeilingExceeded):
            await infer_archetype_from_freeform(
                freeform_text=_FREEFORM_ORIGIN,
                base=base,
                constraints=constraints,
                existing_hints={"jungian_hint": None, "rpg_role_hint": None},
                session_id=session_id,
            )
        assert len(fake_query.calls) == 0, (
            "the ceiling check is a PRE-flight refusal — the SDK must not "
            "be called for a killed session"
        )


# ---------------------------------------------------------------------------
# Review rework (Thought Police rejection, 2026-06-10) — two findings
# ---------------------------------------------------------------------------


class TestSdkFailureDegradesLoudly:
    """[HIGH] [EDGE] review finding: the real SDK raises ``anthropic.*``
    exceptions (transport blips, 429s, 529s, context-length 400s) that are
    NOT in our ``LlmClientError`` family. Those must degrade to the same
    loud inference-failed chargen block as every other failure mode — never
    propagate out of the confirm dispatch (websocket.py's outer catch would
    tear the player's WS session down mid-chargen)."""

    async def test_transport_error_yields_inference_failed_block_not_crash(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sidequest.agents import llm_factory
        from tests.agents.fakes.fake_agent_sdk import RaisingFakeQuery

        class _FakeTransportError(Exception):
            """Stands in for claude_agent_sdk transport errors — deliberately
            outside the LlmClientError family (the agent SDK raises its own
            ``CLINotFoundError`` / ``ProcessError`` / ``CLIJSONDecodeError``
            family, none of which subclass our ``LlmClientError``)."""

        # Story 119-3: the transport seam is the module-level ``query``; a
        # RaisingFakeQuery makes consuming its stream raise — the agent-SDK
        # analog of ``messages.create`` blowing up mid-call.
        fake_query = RaisingFakeQuery(_FakeTransportError("simulated agent-SDK transport failure"))
        monkeypatch.setattr(llm_factory, "query", fake_query, raising=False)
        _disable_default_hint_stamping(monkeypatch)

        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)

        import logging

        with caplog.at_level(logging.WARNING):
            # The load-bearing assertion is that this call RETURNS instead
            # of raising _FakeTransportError up through the dispatch.
            out = await _send_confirmation(handler)

        errors = [m for m in out if isinstance(m, ErrorMessage)]
        assert errors, (
            "an SDK transport failure during inference must surface as the "
            "loud chargen block, not crash the confirm dispatch"
        )
        assert "inference" in str(errors[0].payload.message).lower()

        sd = handler._session_data  # type: ignore[attr-defined]
        assert not sd.snapshot.characters, "a failed inference must not persist a character"
        assert handler._state != _State.Playing  # type: ignore[attr-defined]
        assert not _spans_named(otel_capture, _INFERRED_SPAN_NAME), (
            "no success span for a failed inference"
        )
        assert any("infer" in rec.getMessage().lower() for rec in caplog.records), (
            "the failure must be logged at WARNING on the structured log surface"
        )


class TestOversizedFodderBounded:
    """[MEDIUM] [SEC] review finding (python.md #11): player freeform fodder
    must be length-bounded before it reaches the SDK — otherwise a hostile
    player grinds per-call cost and an oversized context draws an API 400."""

    async def test_fodder_constant_exists_and_is_sane(self) -> None:
        from sidequest.agents.llm_factory import (
            _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS,
        )

        assert 1_000 <= _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS <= 16_000, (
            "the fodder bound must be generous enough for real backstories "
            "(>=1k chars) and small enough to bound cost (<=16k chars); got "
            f"{_ARCHETYPE_INFERENCE_MAX_FODDER_CHARS}"
        )

    async def test_oversized_freeform_is_truncated_before_sdk_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sidequest.agents.llm_factory import (
            _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS,
            infer_archetype_from_freeform,
        )

        base, constraints = _load_heavy_metal_axes()
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={"jungian_hint": _VALID_JUNGIAN, "rpg_role_hint": _VALID_RPG_ROLE},
        )

        oversized = ("I protect what is mine. " * 20_000).strip()  # ~480k chars
        assert len(oversized) > _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS * 10

        import logging

        with caplog.at_level(logging.WARNING):
            result = await infer_archetype_from_freeform(
                freeform_text=oversized,
                base=base,
                constraints=constraints,
                existing_hints={"jungian_hint": None, "rpg_role_hint": None},
                session_id="93-1-oversize",
            )

        assert result == {
            "jungian_hint": _VALID_JUNGIAN,
            "rpg_role_hint": _VALID_RPG_ROLE,
        }, "truncation must not break a legitimate (if verbose) inference"
        assert len(fake_query.calls) == 1

        # The payload the transport actually received must be bounded: the
        # agent-SDK ``query`` carries the user message as its ``prompt`` (the
        # headers + fodder + pairing hints), so allow a small fixed overhead
        # beyond the fodder bound.
        sent = fake_query.calls[-1].prompt
        assert isinstance(sent, str)
        assert len(sent) <= _ARCHETYPE_INFERENCE_MAX_FODDER_CHARS + 2_000, (
            f"prompt is {len(sent)} chars — the joined fodder must be "
            f"truncated to ~{_ARCHETYPE_INFERENCE_MAX_FODDER_CHARS} before the call"
        )
        assert any("truncat" in rec.getMessage().lower() for rec in caplog.records), (
            "truncation must be logged loudly (No Silent Fallbacks: the "
            "player's words were cut — the operator should be able to see it)"
        )


# ---------------------------------------------------------------------------
# Story 93-2 — creation_answers provenance marks the inference-fed scenes
# ---------------------------------------------------------------------------


class TestCreationAnswersInferenceMarking:
    """Story 93-2 AC5: the scene(s) whose freeform fed the 93-1 archetype
    inference carry ``archetype_inferred=True`` on the Character's
    ``creation_answers`` provenance — and ONLY those scenes. The marking
    contract is pinned against the builder's own fodder definition
    (``freeform_answer_texts``), so the name-scene exclusion doctrine is
    inherited rather than re-stated: an answer is marked iff its text was
    inference fodder AND the inference actually fired.
    """

    async def test_inference_marks_fodder_scenes_on_creation_answers(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
        fresh_ledger,
    ) -> None:
        _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        _disable_default_hint_stamping(monkeypatch)
        handler = handler_factory()
        await _connect(handler)
        await _walk_all_freeform(handler)

        # Ground truth captured BEFORE confirm consumes the builder: the
        # exact texts the inference will be fed (name-entry answers already
        # excluded by the 93-1 fodder doctrine).
        sd = handler._session_data  # type: ignore[attr-defined]
        fodder_texts = set(sd.builder.freeform_answer_texts())
        assert fodder_texts, "all-freeform walk must accumulate inference fodder"

        out = await _send_confirmation(handler)
        for msg in out:
            assert not isinstance(msg, ErrorMessage), msg.payload.message

        character = sd.snapshot.characters[0]
        answers = character.creation_answers
        assert answers, (
            "an all-freeform chargen must ship creation_answers provenance "
            "(93-2) — the History section has nothing to render otherwise"
        )

        marked = [a for a in answers if a.archetype_inferred]
        unmarked = [a for a in answers if not a.archetype_inferred]
        assert marked, (
            "the inference fired (gate unblocked) — the scenes whose words "
            "fed it must carry archetype_inferred=True for the UI badge"
        )
        for entry in marked:
            assert entry.kind == "freeform", (
                f"{entry.scene_id}: only freeform answers can feed the "
                f"inference; a {entry.kind!r} entry must never be marked"
            )
            assert entry.value in fodder_texts, (
                f"{entry.scene_id}: marked entry's text was not in the "
                "inference fodder — marking must track what the Haiku call "
                "actually consumed, not blanket-flag freeform scenes"
            )
        for entry in unmarked:
            assert entry.value not in fodder_texts or entry.kind != "freeform", (
                f"{entry.scene_id}: this freeform answer WAS inference "
                "fodder but is not marked — the UI badge would lie by "
                "omission"
            )

    async def test_preset_build_marks_nothing(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        """Preset accumulation never marks: the badge means 'the engine read
        your words', and on a preset walk it never did. The fake transport is
        installed purely to prove no inference call sneaks in."""
        fake_query = _fake_inference_sdk(
            monkeypatch,
            tool_input={
                "jungian_hint": _VALID_JUNGIAN,
                "rpg_role_hint": _VALID_RPG_ROLE,
            },
        )
        handler = handler_factory()
        await _connect(handler)
        await _walk_presets_only(handler)
        out = await _send_confirmation(handler)
        for msg in out:
            assert not isinstance(msg, ErrorMessage), msg.payload.message

        assert len(_inference_calls(fake_query)) == 0, (
            "preset walk with stamped hints must not reach the inference transport"
        )
        sd = handler._session_data  # type: ignore[attr-defined]
        character = sd.snapshot.characters[0]
        assert character.creation_answers, (
            "preset chargen must also record creation_answers (AC3) — "
            "provenance is not a freeform-only feature"
        )
        assert all(not a.archetype_inferred for a in character.creation_answers), (
            "no inference fired — no entry may carry archetype_inferred=True"
        )
