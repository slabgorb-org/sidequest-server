"""Story 153-2 (RED) — emit authored ``establishing_narration`` verbatim at cold-open.

Finding ``[SWN-OPENING-ESTABLISHING-NARRATION-DROPPED]`` (epic-153, the
2026-06-20/21 full-stack /sq-playtest sweep): in the SWN ``space_opera`` worlds
(``aureate_span`` / ``coyote_star`` / ``perseus_cloud``) the world's authored
``establishing_narration`` is DROPPED at cold-open — the player starts the game
without the authored scene-setting prose.

Root cause (server-side, ``_run_opening_turn_narration``): today the cold-open
emits ONLY ``sd.opening_seed`` (the ``first_turn_invitation``) verbatim as a
player-facing ``NARRATION`` frame (websocket_session_handler.py ~2999). The
authored ``establishing_narration`` is handed to the *narrator LLM* as an
"ESTABLISHING NARRATION (play this scene):" directive (``dispatch/opening.py:82``
and ``:310``) and is never emitted verbatim — so the LLM is free to paraphrase
or drop it. The SWN narrator dropped it. The fix: emit the authored
``establishing_narration`` to the player VERBATIM at cold-open, the same way
``opening_seed`` already is.

These tests drive the REAL chargen → cold-open path (no faked
``_run_opening_turn_narration``) against the hermetic ``test_genre/flickering_reach``
fixture pack, forcing the world to resolve a synthetic location-anchored
``Opening`` (the ``aureate_span`` shape) whose ``establishing_narration`` is a
unique marker string. The narrator mock returns fixed, unrelated prose, so the
marker can ONLY appear in the emitted narration if the server emits it verbatim.

The bug is architectural (no world emits ``establishing_narration`` verbatim
today), so a synthetic opening on the fixture world reproduces it faithfully.

All ACs FAIL today (RED). SKIP ≠ RED — these run and FAIL.

AC coverage
-----------
AC1 (verbatim, e2e wiring) — test_establishing_narration_emitted_verbatim_at_cold_open
AC2 (reading order)        — test_establishing_narration_precedes_first_turn_invitation
AC3 (GM-panel observable)  — test_establishing_emit_publishes_info_watcher_event
AC4 (no-opening no-leak)   — test_no_establishing_event_when_world_has_no_opening

Requires Postgres (cold-open emits through emit_event → EventLog, ADR-115).
Run with::

    SIDEQUEST_TEST_DATABASE_URL=postgresql://$USER@localhost:5432/sidequest_test \\
    uv run pytest -n0 tests/server/test_153_2_establishing_narration_verbatim.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.genre.models.narrative import (
    Opening,
    OpeningSetting,
    OpeningTrigger,
)
from sidequest.protocol.messages import (
    CharacterCreationMessage,
    CharacterCreationPayload,
    ErrorMessage,
    NarrationMessage,
    SessionEventMessage,
    SessionEventPayload,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests.server.conftest import (
    mock_claude_client_factory as _mock_claude_client_factory,
)

_FIXTURE_PACKS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "packs"

# ---------------------------------------------------------------------------
# Distinctive markers — present ONLY if the server emits the authored prose
# verbatim. The narrator mock returns unrelated fixed prose, so any appearance
# of these markers in player-facing narration is the server's verbatim emit.
# ---------------------------------------------------------------------------

ESTABLISHING_MARKER = (
    "AUREATE-ESTABLISH-7f3a9c — Amber light pours through the concourse glass, "
    "gilding the slow dust of the long fall."
)
# first_turn_invitation must not contain '?' (Opening validator).
INVITATION_MARKER = "AUREATE-SEED-7f3a9c — You stand at the rail, the drop yawning below you."


# ---------------------------------------------------------------------------
# Hermetic harness — fixture pack (test_genre/flickering_reach) + throwaway PG.
# Mirrors test_lore_seeding_dispatch: fixture packs are minimal and dungeon-free,
# so the full chargen → cold-open path runs without hitting a real LLM.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, truncated per test
    so each run is a clean first-commit (cold-open fires)."""
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
            conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")  # pyright: ignore[reportCallIssue, reportArgumentType]
    monkeypatch.setenv("SIDEQUEST_DATABASE_URL", plain)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[_FIXTURE_PACKS_DIR],
        save_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


async def _connect(handler: WebSocketSessionHandler) -> None:
    from tests.server.conftest import attach_default_room_context, seed_slug_for_test

    slug = seed_slug_for_test(handler._save_dir, genre="test_genre", world="flickering_reach")
    attach_default_room_context(handler)
    await handler.handle_message(
        SessionEventMessage(  # pyright: ignore[reportArgumentType]
            payload=SessionEventPayload(
                event="connect",
                player_name="Tester",
                game_slug=slug,
            ),
            player_id="",
        )
    )


async def _walk_and_confirm(handler: WebSocketSessionHandler) -> list[object]:
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None
    while not builder.is_confirmation():
        scene = builder.current_scene()
        if scene.choices:
            payload = CharacterCreationPayload(phase="scene", choice="1")
        elif scene.allows_freeform:
            payload = CharacterCreationPayload(phase="scene", choice="Rux")
        else:
            payload = CharacterCreationPayload(phase="continue")
        out = await handler.handle_message(
            CharacterCreationMessage(payload=payload, player_id="pid")  # pyright: ignore[reportArgumentType]
        )
        if out and isinstance(out[0], ErrorMessage):
            raise AssertionError(f"walk error: {out[0].payload.message}")
    out = await handler.handle_message(
        CharacterCreationMessage(  # pyright: ignore[reportArgumentType]
            payload=CharacterCreationPayload(phase="confirmation"),
            player_id="pid",
        )
    )
    return list(out)


def _synthetic_aureate_opening() -> Opening:
    """A location-anchored Opening modelled on the SWN aureate_span shape.

    ``mode='either'`` + ``backgrounds=[]`` so it always resolves for the solo,
    single-PC chargen the harness drives. Carries a distinctive
    ``establishing_narration`` and a *different* distinctive
    ``first_turn_invitation`` so the two emissions are discriminable.
    """
    return Opening(
        id="aureate_concourse_test_153_2",
        name="The Amber Concourse",
        triggers=OpeningTrigger(mode="either", min_players=1, max_players=6, backgrounds=[]),
        setting=OpeningSetting(
            location_label="The Amber Concourse",
            situation="The long fall has ended; the Span holds its breath.",
        ),
        establishing_narration=ESTABLISHING_MARKER,
        first_turn_invitation=INVITATION_MARKER,
    )


def _force_opening(handler: WebSocketSessionHandler, opening: Opening) -> None:
    """Overwrite the resolved world's opening bank so ``opening`` is the sole
    candidate the chargen-complete populator can resolve."""
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    world = sd.genre_pack.worlds.get(sd.world_slug)
    assert world is not None, "fixture world flickering_reach must be loaded after connect"
    world.openings = [opening]


def _narration_texts(messages: list[object]) -> list[str]:
    return [str(m.payload.text) for m in messages if isinstance(m, NarrationMessage)]


def _spy_watcher(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, dict]]:
    """Record every cold-open watcher publish as ``(event_type, fields, kwargs)``.

    Patches ``_watcher_publish`` where it is USED — the module global in
    ``websocket_session_handler`` that ``cold_open_emitted`` (and the
    establishing-emit event under test) publish through (lang-review #6: patch
    where used, not where defined). Record-only, like the existing
    test_opening_loud_fail ``captured_events`` spy.
    """
    events: list[tuple[str, dict, dict]] = []

    def _record(
        event_type: str,
        fields: object,
        *,
        component: str = "",
        severity: str = "info",
    ) -> None:
        events.append(
            (
                event_type,
                dict(fields) if isinstance(fields, dict) else {},
                {"component": component, "severity": severity},
            )
        )

    monkeypatch.setattr("sidequest.server.websocket_session_handler._watcher_publish", _record)
    return events


# ---------------------------------------------------------------------------
# AC1 — verbatim emission (end-to-end wiring test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_establishing_narration_emitted_verbatim_at_cold_open(
    handler: WebSocketSessionHandler,
) -> None:
    """RED: the authored ``establishing_narration`` must reach the player as a
    verbatim ``NARRATION`` frame at cold-open.

    Today only the ``first_turn_invitation`` (``opening_seed``) is emitted
    verbatim; ``establishing_narration`` is only fed to the narrator as a
    directive. The marker therefore never appears in any player-facing
    narration → this assertion FAILS until Dev emits it verbatim.

    Wiring test (CLAUDE.md "Verify Wiring, Not Just Existence"): it drives the
    REAL chargen-complete → ``_populate_opening_directive_on_chargen_complete``
    → ``_run_opening_turn_narration`` path, not a synthetic call.
    """
    await _connect(handler)
    _force_opening(handler, _synthetic_aureate_opening())
    out = await _walk_and_confirm(handler)

    texts = _narration_texts(out)
    assert any(ESTABLISHING_MARKER in t for t in texts), (
        "Authored establishing_narration must be emitted VERBATIM to the player at "
        "cold-open — not merely handed to the narrator as a 'play this scene' "
        "directive (the SWN narrator dropped it). No emitted NARRATION contained the "
        f"authored prose. Emitted narration texts: {texts!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — reading order: scene-set before the closing invitation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_establishing_narration_precedes_first_turn_invitation(
    handler: WebSocketSessionHandler,
) -> None:
    """RED: in the cold-open the player reads, the scene-setting
    ``establishing_narration`` must come BEFORE the closing
    ``first_turn_invitation`` (which "closes the scene").

    Order is asserted over the concatenated player-facing narration, so it
    holds whether Dev emits one combined frame or two separate frames.
    """
    await _connect(handler)
    _force_opening(handler, _synthetic_aureate_opening())
    out = await _walk_and_confirm(handler)

    joined = "\n".join(_narration_texts(out))
    assert ESTABLISHING_MARKER in joined, (
        f"establishing_narration was not emitted verbatim at all: {joined!r}"
    )
    assert INVITATION_MARKER in joined, (
        f"first_turn_invitation (the existing cold-open seed) was not emitted: {joined!r}"
    )
    assert joined.index(ESTABLISHING_MARKER) < joined.index(INVITATION_MARKER), (
        "Scene-setting establishing_narration must be read BEFORE the closing "
        "first_turn_invitation. The authored prose sets the scene; the invitation "
        f"closes it. Got establishing@{joined.index(ESTABLISHING_MARKER)} vs "
        f"invitation@{joined.index(INVITATION_MARKER)}."
    )


# ---------------------------------------------------------------------------
# AC3 — GM-panel observability (CLAUDE.md OTEL Observability Principle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_establishing_emit_publishes_info_watcher_event(
    handler: WebSocketSessionHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the verbatim establishing emission must fire a GM-panel watcher
    event so the lie-detector can confirm the authored scene reached the player
    (and that the narrator did not silently swallow it).

    Contract (AC, mirror of the existing ``cold_open_emitted`` event):
    a ``establishing_narration_emitted`` watcher event carrying a positive
    ``establishing_len``, at ``severity="info"`` (lang-review #4 — a successful
    subsystem decision is info, not error).
    """
    events = _spy_watcher(monkeypatch)

    await _connect(handler)
    _force_opening(handler, _synthetic_aureate_opening())
    await _walk_and_confirm(handler)

    establishing = [
        (fields, meta)
        for (etype, fields, meta) in events
        if etype == "establishing_narration_emitted"
    ]
    assert establishing, (
        "Cold-open verbatim establishing emission must publish a "
        "'establishing_narration_emitted' GM-panel watcher event (the lie-detector "
        "mirror of 'cold_open_emitted'). Watcher events seen: "
        f"{sorted({etype for etype, _f, _m in events})!r}"
    )
    fields, meta = establishing[0]
    assert int(fields.get("establishing_len", 0)) > 0, (
        "establishing_narration_emitted must carry a positive establishing_len so the "
        f"GM panel sees how much authored prose was emitted; got fields={fields!r}"
    )
    assert meta.get("severity") == "info", (
        "A successful establishing emission is an info-level decision, not an error "
        f"(lang-review #4 log-level classification); got severity={meta.get('severity')!r}"
    )


# ---------------------------------------------------------------------------
# AC4 — no-leak guard: no opening resolved ⇒ no establishing emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_establishing_event_when_world_has_no_opening(
    handler: WebSocketSessionHandler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: when the populator resolves no opening (empty bank),
    the fix must NOT emit a stray establishing frame or event.

    Pins the gate so a careless fix can't emit an empty/garbage establishing
    narration when there is no authored prose to emit.
    """
    events = _spy_watcher(monkeypatch)

    await _connect(handler)
    sd = handler._session_data  # type: ignore[attr-defined]
    assert sd is not None
    world = sd.genre_pack.worlds.get(sd.world_slug)
    assert world is not None
    world.openings = []  # populator bails with world_or_openings_missing

    out = await _walk_and_confirm(handler)

    establishing_events = [
        etype for (etype, _f, _m) in events if etype == "establishing_narration_emitted"
    ]
    assert not establishing_events, (
        "No opening resolved → the establishing emission must not fire; got "
        f"{establishing_events!r}"
    )
    assert not any(ESTABLISHING_MARKER in t for t in _narration_texts(out)), (
        "No authored establishing_narration exists when the opening bank is empty; "
        "no marker prose may appear in player-facing narration."
    )
