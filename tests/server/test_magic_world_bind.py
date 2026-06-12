"""Story 90-2 — WWN/ADR-126 magic plugin not instantiated at session-bind.

The defect (now fixed by this change): ``snapshot.magic_state`` was populated
only at chargen confirmation (``chargen_mixin._chargen_confirmation`` →
``init_magic_state_for_session``) and on resume-backfill
(``connect._backfill_magic_state_on_resume``) — NEVER at world-bind. So for the
window between slug-connect and the first chargen commit — and for any narrator
cast path that fired before a PC committed — ``snapshot.magic_state`` was None
and the magic_working pipeline silently gated. The world ships a perfectly good
magic.yaml; the engine just never instantiated it until ``init_world_magic_state``
(added in this change) wired it into ``SessionRoom.bind_world``.

The fix (per context-story-90-2.md, Option A): a world-scope initializer
``init_world_magic_state`` that builds ``MagicState.from_config`` WITHOUT a
character (world-scope bars only, no per-character ledger), assigns it to
``snapshot.magic_state``, and emits a ``magic.world_bound`` watcher event for
GM-panel visibility (CLAUDE.md OTEL Observability Principle). Chargen
confirmation then REUSES that state (adding character bars) rather than
rebuilding it.

Test vehicle: **space_opera / coyote_star**. It ships a complete, loadable
magic config pair (genre ``magic.yaml`` + world ``magic.yaml``) with one
world-scope bar (``hegemony_heat``) and three character-scope bars
(``sanity`` / ``notice`` / ``vitality``). It is the established fixture in
``test_magic_init.py`` / ``test_magic_state_resume_backfill.py``. The bug is
world-agnostic — coyote_star exhibits the same None-at-bind defect long_foundry
does. (See the Delivery Findings note: the named subject *long_foundry* cannot
reach GREEN on its own AC because heavy_metal ships no genre-level magic.yaml —
a content-repo gap, tracked separately.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sidequest.game.character import Character, CreatureCore
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.server.magic_init import init_world_magic_state
from sidequest.server.session_room import SessionRoom

CONTENT_ROOT = Path(__file__).resolve().parents[2].parent / "sidequest-content" / "genre_packs"
GENRE_SLUG = "space_opera"
WORLD_SLUG = "coyote_star"


def _coyote_star_pack_dir() -> Path:
    """Return the space_opera pack dir, asserting the magic.yaml pair ships.

    Fail loud (AssertionError, not skip) — these tests exist to prove the
    world-bind wiring; silently skipping when content is missing would let
    a broken pipeline masquerade as green.
    """
    pack_dir = CONTENT_ROOT / GENRE_SLUG
    if not (pack_dir / "magic.yaml").is_file():
        raise AssertionError(f"space_opera genre magic.yaml missing at {pack_dir}")
    if not (pack_dir / "worlds" / WORLD_SLUG / "magic.yaml").is_file():
        raise AssertionError(f"coyote_star world magic.yaml missing under {pack_dir / 'worlds'}")
    return pack_dir


@pytest.fixture
def captured_magic_init_events(monkeypatch):
    """Capture ``_watcher_publish`` calls emitted from inside magic_init.

    Mirrors the fixture in ``test_magic_init.py``. ``raising=False`` so the
    fixture installs cleanly in RED even before the symbol is referenced on
    any new code path; the per-test assertions still fail in RED because the
    expected events are never emitted.
    """
    captured: list[dict] = []

    def _capture(event_type, fields, *, component="sidequest-server", severity="info"):
        captured.append(
            {
                "event_type": event_type,
                "fields": fields,
                "component": component,
                "severity": severity,
            }
        )

    from sidequest.server import magic_init as magic_init_module

    monkeypatch.setattr(magic_init_module, "_watcher_publish", _capture, raising=False)
    return captured


# ---------------------------------------------------------------------------
# AC1 — World-scope magic_state at bind time (no character yet)
# ---------------------------------------------------------------------------


def test_init_world_magic_state_populates_without_character() -> None:
    """A fresh world-bind (no PC committed) lands ``snapshot.magic_state``
    populated with the world config and world-scope bars ONLY — zero
    character-scope bars, because no character exists at bind time.
    """
    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)
    assert snap.magic_state is None

    ok = init_world_magic_state(
        snapshot=snap,
        genre_pack_source_dir=pack_dir,
        world_slug=WORLD_SLUG,
    )

    assert ok is True
    assert snap.magic_state is not None
    assert snap.magic_state.config.world_slug == WORLD_SLUG
    assert snap.magic_state.config.genre_slug == GENRE_SLUG

    # Load-bearing invariant: world-bind precedes any PC, so there must be
    # NO character-scope bars. (coyote_star's character bars are
    # sanity / notice / vitality — none may appear yet.)
    char_keys = [k for k in snap.magic_state.ledger if k.startswith("character|")]
    assert char_keys == [], (
        f"world-bind must not instantiate character bars before chargen; "
        f"found {char_keys}"
    )

    # The world-scope bar (coyote_star ships `hegemony_heat`, scope: world)
    # IS eagerly instantiated by MagicState.from_config — proving the world
    # config actually loaded, not just an empty shell.
    world_keys = [k for k in snap.magic_state.ledger if k.startswith("world|")]
    assert world_keys, (
        f"expected world-scope bars from the loaded config; ledger: "
        f"{list(snap.magic_state.ledger.keys())}"
    )


def test_init_world_magic_state_emits_world_bound_event(captured_magic_init_events) -> None:
    """AC1/AC4 — the OTEL lie-detector. World-bind must emit exactly one
    ``magic.world_bound`` watcher event (component=magic) carrying the
    world_slug, the active plugin list, and a bar count, so the GM panel can
    confirm the magic subsystem engaged at bind time.
    """
    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)

    init_world_magic_state(
        snapshot=snap,
        genre_pack_source_dir=pack_dir,
        world_slug=WORLD_SLUG,
    )

    bound = [
        e
        for e in captured_magic_init_events
        if e["event_type"] == "magic.world_bound" and e["component"] == "magic"
    ]
    assert len(bound) == 1, (
        f"expected exactly one magic.world_bound event; captured: "
        f"{[e['event_type'] for e in captured_magic_init_events]}"
    )
    fields = bound[0]["fields"]
    assert fields["world_slug"] == WORLD_SLUG
    # active_plugins must be a non-empty list — coyote_star activates at
    # least one plugin (innate_v1). A bare-truthy check would pass on a
    # stray string; assert list shape + non-empty explicitly.
    assert isinstance(fields["active_plugins"], list)
    assert len(fields["active_plugins"]) > 0
    # bar_count reflects the eagerly-instantiated world-scope bars (>= 1 for
    # coyote_star's hegemony_heat); never the character bars (none yet).
    assert isinstance(fields["bar_count"], int)
    assert fields["bar_count"] >= 1


# ---------------------------------------------------------------------------
# AC5 — No silent failures (skip / loader-error degrade loudly, never crash)
# ---------------------------------------------------------------------------


def test_init_world_magic_state_skips_world_without_magic_yaml(
    tmp_path: Path,
    captured_magic_init_events,
) -> None:
    """The common case: a world that ships no magic.yaml. World-bind is a
    clean no-op — returns False, leaves magic_state None, emits a
    ``magic.init_skipped`` event so the GM panel sees justified
    non-engagement (not silence), and does NOT raise.
    """
    fake_pack = tmp_path / "fake_pack"
    (fake_pack / "worlds" / "fake_world").mkdir(parents=True)

    snap = GameSnapshot(genre_slug="fake", world_slug="fake_world")
    ok = init_world_magic_state(
        snapshot=snap,
        genre_pack_source_dir=fake_pack,
        world_slug="fake_world",
    )

    assert ok is False
    assert snap.magic_state is None
    skipped = [e for e in captured_magic_init_events if e["event_type"] == "magic.init_skipped"]
    assert skipped, (
        "absence of magic.yaml must surface a magic.init_skipped watcher event "
        "(OTEL Observability Principle: invisible != broken)"
    )


def test_init_world_magic_state_skips_when_source_dir_none(
    captured_magic_init_events,
) -> None:
    """Packs loaded from a non-disk source carry source_dir=None. The magic
    loader needs file paths, so world-bind skips rather than guessing — and
    leaves magic_state None without raising, surfacing a ``magic.init_skipped``
    event so the skip is GM-panel-visible (OTEL Observability Principle).
    """
    snap = GameSnapshot()
    ok = init_world_magic_state(
        snapshot=snap,
        genre_pack_source_dir=None,
        world_slug="any",
    )
    assert ok is False
    assert snap.magic_state is None
    skipped = [e for e in captured_magic_init_events if e["event_type"] == "magic.init_skipped"]
    assert skipped, "source_dir=None must surface a magic.init_skipped watcher event"
    assert skipped[0]["fields"]["reason"] == "no_genre_pack_source_dir"


def test_init_world_magic_state_logs_loader_error_without_raising(
    tmp_path: Path,
    caplog,
    captured_magic_init_events,
) -> None:
    """Malformed magic.yaml must degrade loudly, never crash the bind:
    log at ERROR, emit ``magic.init_failed``, return False, leave
    magic_state None. A session-bind that raised on authoring drift would
    take down every connect to that world.
    """
    fake_pack = tmp_path / "fake_pack"
    world_dir = fake_pack / "worlds" / "broken_world"
    world_dir.mkdir(parents=True)
    # Unparseable YAML → yaml.safe_load raises → load_world_magic wraps it as
    # LoaderError. The initializer must catch it.
    (fake_pack / "magic.yaml").write_text("magic: [unterminated\n", encoding="utf-8")
    (world_dir / "magic.yaml").write_text("magic: [unterminated\n", encoding="utf-8")

    snap = GameSnapshot(genre_slug="fake", world_slug="broken_world")
    with caplog.at_level(logging.ERROR, logger="sidequest.server.magic_init"):
        ok = init_world_magic_state(
            snapshot=snap,
            genre_pack_source_dir=fake_pack,
            world_slug="broken_world",
        )

    assert ok is False
    assert snap.magic_state is None
    assert any(
        "magic.init_failed" in rec.message and rec.levelname == "ERROR"
        for rec in caplog.records
    ), "LoaderError must be logged at ERROR level (CLAUDE.md No Silent Fallbacks)"
    failed = [e for e in captured_magic_init_events if e["event_type"] == "magic.init_failed"]
    assert failed, "malformed config must emit a magic.init_failed watcher event"


# ---------------------------------------------------------------------------
# Idempotence — world-bind happens exactly once; never clobber existing state
# ---------------------------------------------------------------------------


def test_init_world_magic_state_idempotent_does_not_clobber() -> None:
    """World-bind is idempotent (the room may re-enter the bind path). A
    second call must REUSE the existing state object, never replace it —
    replacing would drop any character bars / bar debits a peer already
    committed against the canonical snapshot.
    """
    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)

    first_ok = init_world_magic_state(
        snapshot=snap, genre_pack_source_dir=pack_dir, world_slug=WORLD_SLUG
    )
    assert first_ok is True
    first = snap.magic_state
    assert first is not None

    second_ok = init_world_magic_state(
        snapshot=snap, genre_pack_source_dir=pack_dir, world_slug=WORLD_SLUG
    )
    assert second_ok is False, (
        "second world-bind must return False (already-populated state, no fresh build)"
    )
    assert snap.magic_state is first, (
        "second world-bind must reuse the existing magic_state, not rebuild it"
    )


# ---------------------------------------------------------------------------
# AC2 — Chargen REUSES the world-bound state (adds character bars, no rebuild)
# ---------------------------------------------------------------------------


def test_chargen_reuses_world_bound_magic_state() -> None:
    """After world-bind, the chargen confirmation hook
    (``init_magic_state_for_session``) must REUSE the bound state and add
    the character's bars — not build a fresh state that discards the
    world-scope ledger.
    """
    from sidequest.server.magic_init import init_magic_state_for_session

    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)

    init_world_magic_state(snapshot=snap, genre_pack_source_dir=pack_dir, world_slug=WORLD_SLUG)
    bound_state = snap.magic_state
    assert bound_state is not None

    init_magic_state_for_session(
        snapshot=snap,
        genre_pack_source_dir=pack_dir,
        world_slug=WORLD_SLUG,
        character_id="Sira Mendes",
    )

    # Same object — reuse, not rebuild. If identity changed, any reference a
    # peer/orchestrator held to the world-bound state would silently dangle.
    assert snap.magic_state is bound_state, (
        "chargen confirmation must reuse the world-bound magic_state object"
    )
    # Character bars now present alongside the still-intact world bar.
    char_keys = [k for k in snap.magic_state.ledger if k.startswith("character|Sira Mendes|")]
    assert char_keys, (
        f"chargen must add character bars to the reused state; ledger: "
        f"{list(snap.magic_state.ledger.keys())}"
    )
    world_keys = [k for k in snap.magic_state.ledger if k.startswith("world|")]
    assert world_keys, "world-scope bars must survive character registration"


def test_chargen_after_world_bind_is_a_reuse_commit(captured_magic_init_events) -> None:
    """AC2/AC4 OTEL — once world-bind has run, the chargen commit is a
    REUSE, not a first commit. ``init_magic_state_for_session`` emits its
    ``magic.init`` event with ``first_commit=False`` on the reuse path. This
    proves, via the lie-detector, that the world-bound state was actually
    threaded into chargen (before the fix, world-bind never ran so chargen
    was always the first commit).
    """
    from sidequest.server.magic_init import init_magic_state_for_session

    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)

    init_world_magic_state(snapshot=snap, genre_pack_source_dir=pack_dir, world_slug=WORLD_SLUG)
    init_magic_state_for_session(
        snapshot=snap,
        genre_pack_source_dir=pack_dir,
        world_slug=WORLD_SLUG,
        character_id="Sira Mendes",
    )

    inits = [e for e in captured_magic_init_events if e["event_type"] == "magic.init"]
    assert inits, "chargen registration must still emit a magic.init event"
    assert any(e["fields"].get("first_commit") is False for e in inits), (
        "after world-bind, the chargen commit must report first_commit=False "
        "(proving it reused the world-bound state rather than building a fresh one)"
    )


# ---------------------------------------------------------------------------
# AC3 — Resume backfill is a no-op once world-bind has populated magic_state
# ---------------------------------------------------------------------------


def test_backfill_no_op_after_world_bind() -> None:
    """Post-fix saves resume with magic_state already populated (world-bind
    ran). ``_backfill_magic_state_on_resume`` must early-return and leave the
    state object untouched — re-initializing would clobber tracked debits.
    """
    from types import SimpleNamespace

    from sidequest.handlers.connect import _backfill_magic_state_on_resume

    pack_dir = _coyote_star_pack_dir()
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)
    snap.characters.append(
        Character(
            core=CreatureCore(name="Hokulea", description="Engineer.", personality="Quiet."),
            backstory="Voidborn.",
            char_class="engineer",
            race="human",
            pronouns="they/them",
        )
    )

    init_world_magic_state(snapshot=snap, genre_pack_source_dir=pack_dir, world_slug=WORLD_SLUG)
    sentinel = snap.magic_state
    assert sentinel is not None

    _backfill_magic_state_on_resume(
        snapshot=snap,
        genre_pack=SimpleNamespace(source_dir=pack_dir),
        world_slug=WORLD_SLUG,
    )

    assert snap.magic_state is sentinel, (
        "resume backfill must NOT replace a world-bound magic_state"
    )


# ---------------------------------------------------------------------------
# WIRING — the seam. bind_world must actually instantiate magic_state.
# (CLAUDE.md "Every Test Suite Needs a Wiring Test" — behavior-driven, not a
#  source grep, which the server CLAUDE.md explicitly forbids.)
# ---------------------------------------------------------------------------


def _room() -> SessionRoom:
    return SessionRoom(slug=WORLD_SLUG, mode=GameMode.SOLO)


def test_bind_world_instantiates_magic_state(captured_magic_init_events) -> None:
    """The load-bearing wiring assertion: binding a magic world through the
    real ``SessionRoom.bind_world`` seam (the world-bind entry point, fed the
    resolved ``world_dir``) must leave ``room.snapshot.magic_state``
    populated and emit ``magic.world_bound``. This is the production path the
    connect handler drives; today it leaves magic_state None.
    """
    pack_dir = _coyote_star_pack_dir()
    world_dir = pack_dir / "worlds" / WORLD_SLUG
    snap = GameSnapshot(genre_slug=GENRE_SLUG, world_slug=WORLD_SLUG)

    room = _room()
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=world_dir,
        ruleset=None,
    )

    assert room.snapshot is not None
    assert room.snapshot.magic_state is not None, (
        "bind_world must instantiate world magic_state for a magic world — "
        "this is the wiring the story exists to add"
    )
    bound = [e for e in captured_magic_init_events if e["event_type"] == "magic.world_bound"]
    assert bound, "bind_world's magic init must emit magic.world_bound"


def test_bind_world_leaves_nonmagic_world_state_none(
    tmp_path: Path,
    captured_magic_init_events,
) -> None:
    """Binding a world with no magic.yaml must NOT crash and must leave
    magic_state None — the common case for non-magic settings. Guards
    against the world-bind hook failing loud on every non-magic connect.
    The skip is GM-panel-visible via ``magic.init_skipped`` (justified
    non-engagement, not silence).
    """
    world_dir = tmp_path / "plain_pack" / "worlds" / "plain_world"
    world_dir.mkdir(parents=True)
    snap = GameSnapshot(genre_slug="plain_pack", world_slug="plain_world")

    room = _room()
    room.bind_world(
        snapshot=snap,
        store=MagicMock(spec=SaveRepository),
        world_dir=world_dir,
        ruleset=None,
    )

    assert room.snapshot is not None
    assert room.snapshot.magic_state is None
    skipped = [e for e in captured_magic_init_events if e["event_type"] == "magic.init_skipped"]
    assert skipped, "non-magic world bind must surface a magic.init_skipped watcher event"
