"""Wire-first boundary tests for Story 45-6: chargen archetype-resolution gate.

Playtest 3 (2026-04-19, evropi) shipped a character (``pumblestone_sweedlewit``)
with ``resolved_archetype=NULL``: the snapshot looked valid, the persist
span fired, the state flipped to ``Playing`` — but the narrator had no
archetype anchor, character voice drifted, and the GM panel showed a
class with no archetype to back it.

Three silent-skip branches in
``WebSocketSessionHandler._resolve_character_archetype``
(``websocket_session_handler.py:548-630``) let chargen complete without
binding an archetype:

1. Builder produced no axis pair (``raw is None or "/" not in raw``).
2. Pack lacks ``base_archetypes`` / ``archetype_constraints``.
3. Resolver raised ``GenreValidationError`` (caught and swallowed).

Story 45-6 wires a gate at the chargen-confirmation seam (after
``_resolve_character_archetype``, before ``apply_starting_loadout``)
that distinguishes:

- ``OK_RESOLVED`` — pass (resolver wrote a display name).
- ``OK_NO_AXES`` — pass (pack opted out of the archetype system).
- ``BLOCKED_PARTIAL`` — fail with a typed ERROR frame
  ``code="chargen_archetype_unresolved"``.

Two OTEL spans wrap the gate so Sebastien's GM panel sees every
chargen-confirm decision:

- ``chargen.archetype_gate_evaluated`` — fires on every confirm with a
  ``state`` attribute (``"ok_resolved"`` | ``"ok_no_axes"`` |
  ``"blocked_partial"``).
- ``chargen.archetype_gate_blocked`` — fires only on the blocked branch
  with a ``block_reason`` attribute (``"raw_pair_unresolved"`` |
  ``"missing_axes_with_pack_axes"`` | ``"resolver_raised"``).

Wire-first discipline: tests drive the WS dispatch layer
(``handler.handle_message(CharacterCreationMessage(phase="confirmation"))``)
end-to-end. The gate must be reachable from the production seam, not a
unit-tested helper bolted on the side.

Boundary contract: a regression that removes the gate, the error code,
or either span will trip at least one of these tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.builder import AccumulatedChoices, CharacterBuilder
from sidequest.genre.error import GenreValidationError
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler, _State
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def save_dir(tmp_path: Path) -> Path:
    """Per-test save directory so persist assertions are isolated."""
    return tmp_path


@pytest.fixture
def handler_factory(save_dir: Path):
    """Build a WebSocketSessionHandler bound to caverns_and_claudes content.

    caverns_and_claudes is the chosen substrate because:
    - It has ``base_archetypes`` (loaded from
      ``sidequest-content/archetypes_base.yaml``) and
      ``archetype_constraints`` (loaded from
      ``caverns_and_claudes/archetype_constraints.yaml``) — i.e. it
      declares axes, so the BLOCKED_PARTIAL case (pack-axes-set,
      hints-unset) is reachable.
    - As of Story 45-6 the pack's ``char_creation.yaml`` scene 1 sets
      ``jungian_hint=hero`` / ``rpg_role_hint=jack_of_all_trades`` (a
      ``common`` pairing). A default-1 walk now produces a
      fully-resolved archetype, so the OK_RESOLVED-on-default tests
      need no hint injection. The BLOCKED_PARTIAL and OK_NO_AXES tests
      explicitly null the hints via ``_inject_hints(..., None, None)``
      to recreate the pumblestone failure case.
    """
    # CONTENT_ROOT lives inside the fixture so the conftest import above
    # can stay at the top of the module (drops a noqa: E402 and matches
    # the pattern used by the adjacent test_chargen_*.py files).
    content_root = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
    if not (content_root / "caverns_and_claudes").is_dir():
        pytest.skip("caverns_and_claudes content not found")

    def make() -> WebSocketSessionHandler:
        return WebSocketSessionHandler(
            claude_client_factory=_mock_claude_client_factory(),
            genre_pack_search_paths=[content_root],
            save_dir=save_dir,
        )

    return make


@pytest.fixture
def otel_capture():
    """Install an in-memory OTEL exporter and yield it for assertions."""
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


# ---------------------------------------------------------------------------
# WS-driven helpers
# ---------------------------------------------------------------------------


async def _connect(
    handler: WebSocketSessionHandler,
    *,
    player_name: str = "Pumblestone",
    world: str = "grimvault",
) -> SessionEventMessage:
    """Send SESSION_EVENT.connect and confirm the handler entered Creating."""
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre="caverns_and_claudes", world=world)
    attach_default_room_context(handler)
    payload = SessionEventPayload(
        event="connect",
        player_name=player_name,
        game_slug=slug,
    )
    out = await handler.handle_message(SessionEventMessage(payload=payload, player_id=""))
    assert isinstance(out[0], SessionEventMessage)
    return out[0]


async def _walk_to_confirmation(handler: WebSocketSessionHandler) -> None:
    """Walk chargen scenes via CHARACTER_CREATION until the builder is at
    ``is_confirmation()``. Does NOT send the confirmation message — the
    test drives that itself so each AC can configure its pre-condition.
    """
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None, "connect must construct a chargen builder"

    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Pumblestone")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")


async def _send_confirmation(handler: WebSocketSessionHandler) -> list:
    """Send the phase=confirmation message — the load-bearing one."""
    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("chargen_confirmation"):
        return await handler.handle_message(
            CharacterCreationMessage(
                payload=CharacterCreationPayload(phase="confirmation"),
                player_id="pid",
            )
        )


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list:
    """Return all finished spans with the given name."""
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _inject_hints(
    monkeypatch: pytest.MonkeyPatch,
    *,
    jungian: str | None,
    rpg_role: str | None,
) -> None:
    """Override ``CharacterBuilder.accumulated`` to force the given hint
    pair onto the builder's accumulated state.

    The accumulator is recomputed from scene results on every call, so a
    direct attribute mutation wouldn't survive ``builder.build()``'s next
    ``self.accumulated()`` call. Wrapping the method is the durable seam.
    """
    real = CharacterBuilder.accumulated

    def fake(self: CharacterBuilder) -> AccumulatedChoices:
        acc = real(self)
        acc.jungian_hint = jungian
        acc.rpg_role_hint = rpg_role
        return acc

    monkeypatch.setattr(CharacterBuilder, "accumulated", fake)


# ---------------------------------------------------------------------------
# AC1 — OK_RESOLVED: axes-set pack + valid hints succeed
# ---------------------------------------------------------------------------


class TestArchetypeGateOkResolved:
    """AC1: a pack with ``base_archetypes`` and ``archetype_constraints``,
    chargen scenes that set both hints (``hero`` / ``tank`` is a
    ``common`` pairing in caverns_and_claudes), drives confirmation
    cleanly. Character is persisted, state flips to Playing,
    ``resolved_archetype`` is the resolved display name (NOT the raw
    ``"hero/tank"`` form)."""

    def test_resolved_archetype_is_display_name_not_raw_pair(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # ``hero`` / ``tank`` is a ``common`` pairing in
            # caverns_and_claudes/archetype_constraints.yaml. The resolver
            # must produce a display name (not the raw ``"hero/tank"``).
            _inject_hints(monkeypatch, jungian="hero", rpg_role="tank")

            out = await _send_confirmation(handler)
            assert out, "confirmation must produce at least one frame"
            # No ERROR frame — the gate must pass.
            for msg in out:
                assert not isinstance(msg, ErrorMessage), (
                    f"OK_RESOLVED branch should not error; got {msg!r}"
                )

            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd.snapshot.characters, "OK_RESOLVED must persist the character to the snapshot"
            character = sd.snapshot.characters[0]
            assert character.resolved_archetype is not None, (
                "OK_RESOLVED must leave a non-None resolved_archetype"
            )
            assert "/" not in character.resolved_archetype, (
                "OK_RESOLVED must replace the raw 'j/r' pair with a "
                "display name; got: "
                f"{character.resolved_archetype!r}"
            )
            # Lockstep contract with apply_archetype_resolved
            # (sidequest/game/archetype_apply.py): resolved_archetype and
            # archetype_provenance MUST be set together. The gate's
            # discriminator currently checks "/" in resolved_archetype,
            # but the more durable invariant is "archetype_provenance is
            # not None" — Reviewer flagged that no test asserts this
            # contract, so a future refactor that splits the two writes
            # could silently break the gate's correctness. This assertion
            # locks in the lockstep guarantee.
            assert character.archetype_provenance is not None, (
                "OK_RESOLVED must set archetype_provenance in lockstep "
                "with resolved_archetype (apply_archetype_resolved "
                "writes both atomically — if a future refactor splits "
                "them, the gate's discriminator silently breaks)"
            )
            # The handler must transition to Playing on success.
            assert handler._state == _State.Playing  # type: ignore[attr-defined]

            # The gate evaluator span MUST fire even on the success
            # branch — that's the negative-confirmation Sebastien needs
            # to know the gate ran (not just absent because the path
            # bypassed it). Without this assertion, the test passes
            # today with NO gate present (the existing resolver already
            # produces the right state) — which would be vacuous.
            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, (
                "chargen.archetype_gate_evaluated must fire on the "
                "OK_RESOLVED branch — without this span, the test "
                "would pass even if the gate did not exist"
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# AC2 — BLOCKED_PARTIAL: axes-set pack + missing hints fails
#
# This is the ``pumblestone`` regression: pack declares axes, chargen
# scene didn't set both hints, the silent-skip branch shipped a null-
# archetype character. The gate must block, the typed-ERROR frame must
# carry the documented code, the character must NOT be persisted, and
# the handler state must stay Creating.
# ---------------------------------------------------------------------------


class TestArchetypeGateBlockedPartial:
    """AC2: the ``pumblestone`` negative-to-positive regression.

    caverns_and_claudes declares axes (base_archetypes +
    archetype_constraints) but its chargen scenes set neither
    ``jungian_hint`` nor ``rpg_role_hint``. A default-1 walk produces a
    character with ``resolved_archetype=None``. The gate must reject
    confirmation."""

    def test_confirmation_returns_typed_error_with_documented_code(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Strip both hints to simulate the pumblestone path
            # (caverns_and_claudes scene 1 sets default hints, so we
            # null them out to recreate the malformed-scene case the
            # gate is supposed to catch).
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            out = await _send_confirmation(handler)
            assert out, "confirmation must produce a frame"
            assert isinstance(out[0], ErrorMessage), (
                f"BLOCKED_PARTIAL must return an ERROR frame, got {out[0]!r}"
            )
            err = out[0].payload
            assert err.code == "chargen_archetype_unresolved", (
                "BLOCKED_PARTIAL ERROR must carry the documented code "
                "'chargen_archetype_unresolved' so the UI can branch "
                f"without keyword-matching; got code={err.code!r}"
            )
            # Human-readable message must exist (NonBlankString
            # constructor enforces non-blank — we just confirm the field
            # is populated and unwraps to a non-empty string).
            assert err.message is not None, "BLOCKED_PARTIAL must include a message field"
            assert str(err.message).strip(), (
                "BLOCKED_PARTIAL message must unwrap to a non-empty string"
            )

            # Negative regression (Reviewer-flagged): the legacy
            # ``character_creation.archetype_resolution_failed`` event
            # is the inner-resolver event — it MUST fire only on the
            # resolver-raised branch, not on missing_axes_with_pack_axes.
            # Without this assertion, a future change that accidentally
            # emits the legacy event on the pumblestone path would
            # silently degrade Sebastien's GM-panel signal (the panel
            # would conflate scene-malformed with resolver-error).
            legacy_events = [
                e
                for span in otel_capture.get_finished_spans()
                for e in span.events
                if e.name == "character_creation.archetype_resolution_failed"
            ]
            assert not legacy_events, (
                "On the missing_axes_with_pack_axes path the resolver "
                "short-circuits at line 574 (raw is None / no '/') "
                "BEFORE entering the try/except — the legacy "
                "archetype_resolution_failed event must NOT fire. "
                f"Found {len(legacy_events)} legacy event(s); the "
                "gate's blocked span is the correct signal here."
            )

        asyncio.run(body())

    def test_blocked_chargen_does_not_persist_character(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Recreate the pumblestone case: pack has axes, hints unset.
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            sd_before = handler._session_data  # type: ignore[attr-defined]
            assert not sd_before.snapshot.characters, (
                "Pre-confirmation snapshot must have no characters"
            )

            await _send_confirmation(handler)

            sd_after = handler._session_data  # type: ignore[attr-defined]
            assert not sd_after.snapshot.characters, (
                "BLOCKED_PARTIAL must NOT append the character to "
                "sd.snapshot.characters — pumblestone shipped because "
                "the silent-skip path persisted anyway"
            )
            # Handler state must not flip — chargen is not done.
            assert handler._state == _State.Creating, (  # type: ignore[attr-defined]
                f"BLOCKED_PARTIAL must keep the handler in Creating; got {handler._state!r}"
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# AC2b — BLOCKED_PARTIAL with block_reason="raw_pair_unresolved"
#
# Reviewer-flagged gap: the gate has three documented `block_reason` values
# (raw_pair_unresolved, missing_axes_with_pack_axes, resolver_raised) but
# the original test suite exercised only two of them. This branch fires when
# the builder writes a raw "jungian/rpg_role" pair onto the character (both
# hints set) AND the pack has no axes — the resolver short-circuits at
# `_resolve_character_archetype` line 579, leaving the raw pair in place.
# The gate then sees `pack_has_axes=False` + `"/"` in `resolved_archetype`
# and routes to BLOCKED_PARTIAL with `block_reason="raw_pair_unresolved"`.
#
# Real-world surface: a content author who declared chargen scenes with
# hints (correct shape) but forgot to ship `archetype_constraints.yaml`
# (or shipped `base_archetypes` only). Today no production pack hits this,
# but the discriminator is part of the gate's public contract and a
# regression in the routing would silently misclassify the failure mode.
# ---------------------------------------------------------------------------


class TestArchetypeGateRawPairUnresolved:
    """AC2b: pack-axes-absent + scene-set-hints → BLOCKED_PARTIAL with
    `block_reason="raw_pair_unresolved"`.

    The builder writes `f"{jungian}/{rpg_role}"` onto
    `character.resolved_archetype`. The resolver at
    `_resolve_character_archetype` short-circuits at line 579 because the
    pack lacks axes, leaving the raw pair. The gate observes
    `pack_has_axes=False` AND `"/"` in `resolved_archetype` and routes
    to the `raw_pair_unresolved` branch — distinct from
    `missing_axes_with_pack_axes` (where `resolved_archetype is None`)
    and `resolver_raised` (where pack HAS axes and the resolver was
    called and raised)."""

    def test_pack_axisless_with_set_hints_blocks_with_raw_pair_unresolved(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Both hints set — builder will produce raw "hero/tank".
            _inject_hints(monkeypatch, jungian="hero", rpg_role="tank")

            # Strip pack axes — resolver short-circuits at line 579,
            # raw pair stays on the character.
            sd = handler._session_data  # type: ignore[attr-defined]
            sd.genre_pack.base_archetypes = None
            sd.genre_pack.archetype_constraints = None

            out = await _send_confirmation(handler)
            assert out, "confirmation must produce a frame"
            assert isinstance(out[0], ErrorMessage), (
                f"raw_pair_unresolved branch must return an ERROR frame, got {out[0]!r}"
            )
            assert out[0].payload.code == "chargen_archetype_unresolved", (
                "raw_pair_unresolved ERROR must carry the documented "
                "code; got "
                f"{out[0].payload.code!r}"
            )

            # Character must NOT be persisted.
            assert not sd.snapshot.characters, (
                "raw_pair_unresolved must NOT append the character to sd.snapshot.characters"
            )

            # The blocked span fires with block_reason='raw_pair_unresolved'
            # — the load-bearing assertion. This is the only way to
            # distinguish this branch from missing_axes_with_pack_axes
            # at the OTEL surface; if the discriminator regresses, this
            # test catches it.
            blocked = _spans_named(otel_capture, "chargen.archetype_gate_blocked")
            assert len(blocked) >= 1, (
                "chargen.archetype_gate_blocked must fire on the raw_pair_unresolved branch"
            )
            bl_attrs = dict(blocked[-1].attributes or {})
            assert bl_attrs.get("block_reason") == "raw_pair_unresolved", (
                "Pack-axisless + raw pair must produce "
                "block_reason='raw_pair_unresolved' (NOT "
                "'missing_axes_with_pack_axes', NOT 'resolver_raised'); "
                f"got {bl_attrs.get('block_reason')!r}"
            )

            # Negative regression: the legacy resolver-failed event
            # must NOT fire on this branch (resolver short-circuited
            # before the try/except).
            legacy_events = [
                e
                for span in otel_capture.get_finished_spans()
                for e in span.events
                if e.name == "character_creation.archetype_resolution_failed"
            ]
            assert not legacy_events, (
                "raw_pair_unresolved is the pack-lacks-axes short-circuit "
                "at _resolve_character_archetype:579 — it returns BEFORE "
                "the try/except, so the legacy resolver-failed event "
                "must NOT fire. The gate's blocked span is the only "
                "signal."
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# AC3 — OK_NO_AXES: pack with no axes succeeds with resolved_archetype=None
# ---------------------------------------------------------------------------


class TestArchetypeGateOkNoAxes:
    """AC3: a pack that opts out of the archetype system
    (``base_archetypes is None and archetype_constraints is None``)
    must chargen cleanly even when the character has
    ``resolved_archetype=None``. The gate distinguishes "pack opted out"
    from "pack opted in but scene malformed"."""

    def test_axisless_pack_chargen_succeeds_with_null_archetype(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Strip axes from the loaded pack AND null out hints —
            # simulating a pack that legitimately doesn't use the
            # archetype system. Without nulling the hints, the
            # caverns_and_claudes default chargen scenes set them
            # (post-Story 45-6 content fix), the resolver runs against
            # the now-None pack axes via the early-return at
            # ``websocket_session_handler.py:579``, leaves the raw "j/r"
            # pair on the character, and the gate would correctly block
            # it as ``raw_pair_unresolved``.
            sd = handler._session_data  # type: ignore[attr-defined]
            sd.genre_pack.base_archetypes = None
            sd.genre_pack.archetype_constraints = None
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            out = await _send_confirmation(handler)
            assert out, "confirmation must produce a frame"
            for msg in out:
                assert not isinstance(msg, ErrorMessage), (
                    f"OK_NO_AXES must not error; pack opted out — got {msg!r}"
                )

            assert sd.snapshot.characters, "OK_NO_AXES must persist the character"
            assert sd.snapshot.characters[0].resolved_archetype is None, (
                "OK_NO_AXES is allowed to leave resolved_archetype=None"
            )
            assert handler._state == _State.Playing  # type: ignore[attr-defined]

            # The gate evaluator span MUST fire on the OK_NO_AXES
            # branch too — the lie-detector confirms the gate ran and
            # chose the pack-opted-out branch deliberately, vs. the
            # silent skip that shipped pumblestone. Without this
            # assertion the test passes today with no gate present.
            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, (
                "chargen.archetype_gate_evaluated must fire on the "
                "OK_NO_AXES branch — without this span the test would "
                "pass even if the gate did not exist"
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# AC4 — OTEL: chargen.archetype_gate_evaluated fires every confirm with the
# right state attribute; chargen.archetype_gate_blocked fires only on
# blocked branch.
# ---------------------------------------------------------------------------


class TestArchetypeGateOtel:
    """AC4: the gate emits two OTEL spans so Sebastien's GM panel can
    see every chargen-confirm decision (Sebastien's lie-detector per
    CLAUDE.md OTEL Observability Principle)."""

    def test_evaluated_span_fires_on_ok_resolved_with_state_attr(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)
            _inject_hints(monkeypatch, jungian="hero", rpg_role="tank")
            await _send_confirmation(handler)

            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, (
                "chargen.archetype_gate_evaluated must fire on every "
                "confirm — Sebastien's GM panel needs the negative "
                "confirmation that the gate ran"
            )
            attrs = dict(evaluated[-1].attributes or {})
            assert attrs.get("state") == "ok_resolved", (
                f"OK_RESOLVED span state must be 'ok_resolved'; got {attrs.get('state')!r}"
            )
            # No blocked span on the success path.
            blocked = _spans_named(otel_capture, "chargen.archetype_gate_blocked")
            assert not blocked, (
                "chargen.archetype_gate_blocked must NOT fire on the OK_RESOLVED branch"
            )

        asyncio.run(body())

    def test_evaluated_span_fires_on_ok_no_axes_with_state_attr(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            sd = handler._session_data  # type: ignore[attr-defined]
            sd.genre_pack.base_archetypes = None
            sd.genre_pack.archetype_constraints = None
            # See test_axisless_pack_chargen_succeeds_with_null_archetype
            # for the rationale — pack-axisless + caverns default hints
            # would land in the gate's raw_pair_unresolved branch.
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            await _send_confirmation(handler)

            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, (
                "chargen.archetype_gate_evaluated must fire on the "
                "OK_NO_AXES branch — every confirm path emits"
            )
            attrs = dict(evaluated[-1].attributes or {})
            assert attrs.get("state") == "ok_no_axes", (
                f"OK_NO_AXES span state must be 'ok_no_axes'; got {attrs.get('state')!r}"
            )
            blocked = _spans_named(otel_capture, "chargen.archetype_gate_blocked")
            assert not blocked, (
                "chargen.archetype_gate_blocked must NOT fire on the OK_NO_AXES branch"
            )

        asyncio.run(body())

    def test_blocked_span_fires_on_blocked_partial_with_block_reason(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Recreate pumblestone: pack has axes, hints unset.
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            await _send_confirmation(handler)

            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, (
                "chargen.archetype_gate_evaluated must fire on the "
                "BLOCKED_PARTIAL branch too — Sebastien needs the "
                "evaluator span every time"
            )
            ev_attrs = dict(evaluated[-1].attributes or {})
            assert ev_attrs.get("state") == "blocked_partial", (
                f"BLOCKED_PARTIAL evaluator span state must be "
                f"'blocked_partial'; got {ev_attrs.get('state')!r}"
            )

            blocked = _spans_named(otel_capture, "chargen.archetype_gate_blocked")
            assert len(blocked) >= 1, (
                "chargen.archetype_gate_blocked is the explicit "
                "lie-detector entry — it must fire when chargen would "
                "have shipped broken"
            )
            bl_attrs = dict(blocked[-1].attributes or {})
            # In the AC2 setup, hints are unset and pack has axes →
            # block_reason is missing_axes_with_pack_axes (chargen scene
            # malformed).
            assert bl_attrs.get("block_reason") == "missing_axes_with_pack_axes", (
                f"BLOCKED_PARTIAL with hints=None and pack-axes-set must "
                f"have block_reason='missing_axes_with_pack_axes'; got "
                f"{bl_attrs.get('block_reason')!r}"
            )

        asyncio.run(body())

    def test_span_routes_register_both_gate_spans(self) -> None:
        """SPAN_ROUTES registration is what makes the GM panel see these
        spans — without a route entry, the dashboard's typed tabs miss
        the new subsystem (per ``test_routing_completeness.py`` the
        constant must be in SPAN_ROUTES *or* FLAT_ONLY_SPANS, but for
        Sebastien's lie-detector the ``state_transition`` route is
        load-bearing)."""
        from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_ROUTES

        for name in (
            "chargen.archetype_gate_evaluated",
            "chargen.archetype_gate_blocked",
        ):
            assert name in SPAN_ROUTES, (
                f"{name!r} must be registered in SPAN_ROUTES so the GM "
                f"panel emits a typed event when the span closes"
            )
            assert name not in FLAT_ONLY_SPANS, (
                f"{name!r} must NOT be in FLAT_ONLY_SPANS — the GM "
                f"panel needs the typed event, not just agent_span_close"
            )
            route = SPAN_ROUTES[name]
            # Per story context, both spans drive a state_transition
            # event tagged to a chargen / character_creation component.
            assert route.event_type == "state_transition", (
                f"{name} route must emit state_transition events; got {route.event_type!r}"
            )


# ---------------------------------------------------------------------------
# AC5 — Resolver-raised path routes through the gate as BLOCKED_PARTIAL
# with block_reason="resolver_raised"
# ---------------------------------------------------------------------------


class TestArchetypeGateResolverRaised:
    """AC5: when the resolver raises ``GenreValidationError`` (forbidden
    pairing, unknown axis), the legacy ``archetype_resolution_failed``
    event still fires (no regression there) but the new gate observes
    that the raw pair is still on the character and blocks with
    ``block_reason="resolver_raised"``. The character is NOT
    persisted."""

    def test_resolver_raise_blocks_and_does_not_persist(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Inject both hints so builder.build() produces a raw "j/r"
            # pair on the character — required for the resolver to be
            # called at all.
            _inject_hints(monkeypatch, jungian="hero", rpg_role="tank")

            # Stub the resolver to raise. This is the third silent-skip
            # branch (websocket_session_handler.py:595-612): the catch
            # logs and emits archetype_resolution_failed, then returns
            # — the raw pair stays on the character.
            def _raise(*args, **kwargs):
                raise GenreValidationError(message="test-forced resolver raise")

            monkeypatch.setattr(
                "sidequest.server.websocket_session_handler.resolve_archetype",
                _raise,
            )

            out = await _send_confirmation(handler)
            assert out and isinstance(out[0], ErrorMessage), (
                "Resolver-raised path must return a typed ERROR frame "
                "via the gate, not silently ship a partial character"
            )
            assert out[0].payload.code == "chargen_archetype_unresolved", (
                "Resolver-raised ERROR must carry the documented code"
            )

            sd = handler._session_data  # type: ignore[attr-defined]
            assert not sd.snapshot.characters, "Resolver-raised path must not persist the character"

            # Legacy resolver-failed event still fires (no regression).
            legacy_events = [
                e
                for span in otel_capture.get_finished_spans()
                for e in span.events
                if e.name == "character_creation.archetype_resolution_failed"
            ]
            assert legacy_events, (
                "The pre-existing archetype_resolution_failed event "
                "must still fire — gate wraps the resolver, doesn't "
                "replace it"
            )

            # New blocked span fires with block_reason='resolver_raised'.
            blocked = _spans_named(otel_capture, "chargen.archetype_gate_blocked")
            assert len(blocked) >= 1, (
                "chargen.archetype_gate_blocked must fire on the resolver-raised branch"
            )
            bl_attrs = dict(blocked[-1].attributes or {})
            assert bl_attrs.get("block_reason") == "resolver_raised", (
                "Resolver-raised gate-blocked span must distinguish "
                "this branch via block_reason='resolver_raised' so "
                "Sebastien can see WHY the gate blocked; got "
                f"{bl_attrs.get('block_reason')!r}"
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# Wire test — verify the gate is actually called from production code
#
# Wire-first principle (CLAUDE.md): "Verify it's actually connected
# end-to-end. Tests passing and files existing means nothing if the
# component isn't imported, the hook isn't called, or the endpoint isn't
# hit in production code." — i.e. the gate helper, if extracted, must
# have a non-test consumer.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule-enforcement: python.md rule 4 — error paths MUST log
#
# Reviewer flagged that the BLOCKED_PARTIAL gate path emits OTEL spans but
# does NOT call ``logger.warning()`` or ``logger.error()``. Per python.md
# rule 4 ("Error paths MUST have ``logger.error()`` or ``logger.warning()``"),
# the OTEL span is not a substitute for the structured server log: the
# server-log surface is independent (journald / CloudWatch / file), and ops
# debugging without the entry is invisible.
#
# The reviewer's HIGH finding maps to this test. Until Dev adds a
# WARNING-level log entry on the blocked branch, this test fails.
# ---------------------------------------------------------------------------


class TestArchetypeGateLogging:
    """Rule 4 (python.md): error paths must log to the server log surface,
    not just OTEL. Sebastien sees OTEL, but the on-call ops engineer
    greps server logs — both surfaces need the entry."""

    def test_blocked_partial_emits_warning_log(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the gate blocks, the structured server log MUST carry a
        WARNING (or ERROR) entry — typically tagged
        ``chargen.archetype_gate_blocked`` so it's grep-able alongside
        the existing ``chargen.archetype_resolution_failed`` log line
        already present in ``_resolve_character_archetype``. This is
        not just style: per CLAUDE.md "No Silent Fallbacks", a
        rejection that fires only an OTEL span is half-loud at best —
        ops dashboards that don't speak OTEL miss it entirely."""
        import logging

        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)
            # Recreate pumblestone — pack has axes, hints unset.
            _inject_hints(monkeypatch, jungian=None, rpg_role=None)

            with caplog.at_level(logging.WARNING):
                await _send_confirmation(handler)

            # Look for a WARNING (or ERROR) record from the
            # session-handler module that mentions the gate block.
            # Acceptable shapes: a dedicated chargen.archetype_gate_blocked
            # logger line, OR any WARNING record that contains both
            # "archetype_gate" and the block_reason. This gives Dev
            # flexibility on the exact log message format while still
            # enforcing the rule-4 contract.
            relevant = [
                rec
                for rec in caplog.records
                if rec.levelno >= logging.WARNING
                and ("archetype_gate" in rec.getMessage() or "archetype_gate" in (rec.name or ""))
            ]
            assert relevant, (
                "BLOCKED_PARTIAL gate path must emit a WARNING-level "
                "log entry (python.md rule 4 — error paths MUST log "
                "to the server log surface). Found 0 matching records "
                "in caplog. The OTEL span fires correctly, but the "
                "server log is independent: ops debugging without "
                "this entry is invisible."
            )

        asyncio.run(body())


# ---------------------------------------------------------------------------
# Robustness: gate discriminator survives a "/" in resolved display name
#
# Reviewer flagged that the gate's discriminator (`"/"` in
# ``character.resolved_archetype``) is brittle — ``ArchetypeResolved.name``
# is a free-form ``str`` with no validator forbidding "/". A funnel could
# legitimately define a name like "Sage/Healer" today, and the gate would
# misclassify the success as ``resolver_raised``. Reviewer recommended
# switching the discriminator to ``character.archetype_provenance is not
# None`` (set in lockstep by ``apply_archetype_resolved``), which is a
# more durable signal.
#
# This test simulates a name with "/" and asserts the gate correctly
# routes to OK_RESOLVED. With the current shape-based discriminator the
# test fails; with Dev's fix using ``archetype_provenance`` it passes.
# ---------------------------------------------------------------------------


class TestArchetypeGateDiscriminatorRobustness:
    """The gate must distinguish OK_RESOLVED from BLOCKED_PARTIAL using a
    signal that survives ``ArchetypeResolved.name`` containing a "/".
    The post-resolve invariant is: ``apply_archetype_resolved`` writes
    BOTH ``resolved_archetype`` AND ``archetype_provenance`` together —
    so ``archetype_provenance is not None`` is the durable
    OK_RESOLVED marker, regardless of name shape."""

    def test_resolved_name_with_slash_routes_to_ok_resolved(
        self,
        handler_factory,
        monkeypatch: pytest.MonkeyPatch,
        otel_capture: InMemorySpanExporter,
    ) -> None:
        """Inject a successful resolution whose display name contains
        "/". The current shape-based discriminator misclassifies this
        as ``resolver_raised`` (raw pair); a discriminator that keys
        on ``archetype_provenance is not None`` correctly routes to
        OK_RESOLVED."""
        from sidequest.protocol.provenance import Tier

        async def body() -> None:
            handler = handler_factory()
            await _connect(handler)
            await _walk_to_confirmation(handler)

            # Patch apply_archetype_resolved (where the handler module
            # imports it) to write a synthetic display name that contains
            # "/". This is the latent risk Reviewer flagged: no validator
            # currently forbids "/" in ArchetypeResolved.name.
            def _apply_with_slash(character, resolution):
                # Reproduce the lockstep contract — both fields together.
                character.resolved_archetype = "Sage/Healer"
                character.archetype_provenance = {
                    "tier": Tier.world.value,
                    "file": "synthetic",
                    "merge_steps": [],
                }

            monkeypatch.setattr(
                "sidequest.server.websocket_session_handler.apply_archetype_resolved",
                _apply_with_slash,
            )
            # Both hints set — so the resolver is reached and the patched
            # apply runs.
            _inject_hints(monkeypatch, jungian="sage", rpg_role="healer")

            out = await _send_confirmation(handler)
            assert out, "confirmation must produce a frame"
            for msg in out:
                assert not isinstance(msg, ErrorMessage), (
                    "A successful resolution whose display name contains "
                    "'/' must route to OK_RESOLVED, not BLOCKED_PARTIAL. "
                    "Today the gate's syntactic discriminator misclassifies "
                    "this as resolver_raised. Fix: discriminate on "
                    "archetype_provenance is not None (set by "
                    "apply_archetype_resolved in lockstep with "
                    f"resolved_archetype). Got: {msg!r}"
                )

            sd = handler._session_data  # type: ignore[attr-defined]
            assert sd.snapshot.characters, (
                "OK_RESOLVED with slash-in-name must persist the character"
            )
            character = sd.snapshot.characters[0]
            assert character.resolved_archetype == "Sage/Healer"
            assert character.archetype_provenance is not None

            # Evaluator span MUST report state='ok_resolved' (not
            # 'blocked_partial').
            evaluated = _spans_named(otel_capture, "chargen.archetype_gate_evaluated")
            assert len(evaluated) >= 1, "Evaluator span must fire even on slash-in-name path"
            attrs = dict(evaluated[-1].attributes or {})
            assert attrs.get("state") == "ok_resolved", (
                f"Slash-in-display-name must produce state='ok_resolved'; "
                f"got {attrs.get('state')!r}. The gate must use "
                f"archetype_provenance as the OK_RESOLVED signal, not the "
                f"presence of '/' in the name string."
            )

        asyncio.run(body())


class TestArchetypeGateWiring:
    def test_gate_helper_has_production_consumer(self) -> None:
        """The gate MUST be called from inside
        ``_chargen_confirmation``. The original wire-check did a
        whole-file source-scan that matched the def line itself — Dev
        could keep the def and remove the call site and the test would
        still pass. Reviewer flagged that as too weak.

        This stronger version uses ``inspect.getsource`` to read just
        the body of ``_chargen_confirmation`` and asserts that at
        least one of the gate's invocation patterns (the helper call,
        or the constant import as used in an inline gate, or the OTEL
        span constant name) appears INSIDE the method body — not
        merely somewhere in the file. A future refactor that drops the
        call site fails immediately, regardless of whether the def
        survives.
        """
        import inspect

        from sidequest.server.session_handler import WebSocketSessionHandler

        body = inspect.getsource(WebSocketSessionHandler._chargen_confirmation)
        # Acceptable invocation patterns — at least ONE must appear
        # inside the method body. The first is the canonical extracted
        # helper; the other two cover an inline-gate alternative where
        # Dev calls ``tracer.start_as_current_span`` directly with the
        # imported constant.
        invocation_patterns = [
            "self._gate_archetype_resolution(",
            "SPAN_CHARGEN_ARCHETYPE_GATE_EVALUATED",
            "chargen.archetype_gate_evaluated",
        ]
        matches = [p for p in invocation_patterns if p in body]
        assert matches, (
            "The chargen-confirmation seam must INVOKE the archetype "
            "gate. Acceptable invocation patterns inside "
            "_chargen_confirmation's body:\n  "
            + "\n  ".join(f"- {p!r}" for p in invocation_patterns)
            + "\nNone found. If Dev kept the gate definition but "
            "removed the call site (or inlined and removed the span "
            "emission), production silently bypasses the gate — this "
            "test is the wire-check that catches that regression."
        )

    def test_gate_definition_exists_in_handler_module(self) -> None:
        """Companion to the call-site wire-check: confirm the gate
        method actually exists on the handler class. Together the two
        tests prove (a) the gate is defined, AND (b) the gate is
        called — covering both halves of the wiring contract."""
        from sidequest.server.session_handler import WebSocketSessionHandler

        assert hasattr(WebSocketSessionHandler, "_gate_archetype_resolution"), (
            "WebSocketSessionHandler must define _gate_archetype_resolution. "
            "If Dev renamed the method, update this assertion AND the "
            "call-site test above to match."
        )
