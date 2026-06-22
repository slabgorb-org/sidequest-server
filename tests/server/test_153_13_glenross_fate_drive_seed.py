"""RED (story 153-13): glenross Fate chargen must set a REAL drive, not echo the
vocation/calling label into the seeded quest spine.

Repro (MEASURED 2026-06-22 against the shipped tea_and_murder / glenross content):
walking glenross chargen and picking a vocation produced ``character.drive == ""``
(glenross authors NO drive scene), so ``quest_seed.seed_quest_spine`` fell back to
``calling_label`` and seeded a degenerate spine —
``quest_log["seed_drive"].title == .objective == "Episcopal Rector"`` and
``active_stakes == "Episcopal Rector"``. The narrator has nothing to work from at
turn 0.

The server mechanism is CORRECT and is out of scope: ``builder.py`` sets
``character.drive`` from a "drive-shaped" chargen choice (one that touches
relationship/goals/emotional_state WITHOUT setting class/race), and
``quest_seed.py`` faithfully copies the drive (ADR-146 / story 77-2). glenross
simply ships no such scene (space_opera/annees_folles do). The fix is the Fate
chargen drive-assignment SURFACE for glenross — a real drive scene / per-calling
drives list (content) — NOT ``quest_seed.py``.

Tests:
- AC-1 (builder seam): the production ``CharacterBuilder`` on real glenross content
  assigns a real, non-vocation drive.
- AC-2 / AC-3 / AC-5 (handler seam): the full chargen-commit handler seeds a
  meaningful, non-degenerate quest whose title is the drive, NOT the calling label,
  and emits a non-warning ``quest.seeded_at_creation`` span (no silent calling-label
  echo).
- AC-4 (article agreement, already fixed by server PR #988): a vowel-leading
  vocation in the ``{class}`` prose slot reads "an Episcopal Rector", not
  "a Episcopal Rector".

Skips cleanly when sidequest-content is not on disk in this checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.protocol.messages import (
    CharacterCreationPayload,
    ErrorMessage,
)
from sidequest.server.session_handler import WebSocketSessionHandler
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path
from tests.server.conftest import mock_claude_client_factory as _mock_claude_client_factory
from tests.server.test_chargen_dispatch import _connect, _send_chargen, run

GENRE_SLUG = "tea_and_murder"
WORLD_SLUG = "glenross"
SEED_QUEST_ID = "seed_drive"
SEED_SPAN = "quest.seeded_at_creation"
# A vowel-leading vocation that exists in the shipped glenross vocation scene; the
# article-agreement repro (sq-playtest 150-6) used exactly this label.
VOWEL_VOCATION = "Episcopal Rector"


def _has_content() -> bool:
    return (GENRE_PACKS_DIR / GENRE_SLUG).is_dir()


pytestmark = pytest.mark.skipif(not _has_content(), reason="sidequest-content not on disk")


# ---------------------------------------------------------------------------
# AC-1 — builder seam: chargen assigns a real drive, not the vocation label
# ---------------------------------------------------------------------------


def _load_pack():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path(GENRE_SLUG))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _build_pc(world: str, name: str = "Eleanor Vance", prefer_vocation: str | None = None):
    """Walk the REAL chargen for ``world`` through the production ``CharacterBuilder``
    and return the built Character. When ``prefer_vocation`` matches a vocation
    choice label, pick it (so ``calling_label`` is deterministic); otherwise take the
    first choice in each scene.

    Mirrors the annees_folles narrative-walk harness
    (``tests/integration/test_126_24_annees_folles_chargen_seed.py``): accept the
    seeded Fate steps, author HC/Trouble, otherwise take the offered choice.
    """
    from sidequest.game.builder import CharacterBuilder
    from sidequest.server.dispatch.char_creation_resolve import resolve_char_creation_scenes

    pack = _load_pack()
    scenes = resolve_char_creation_scenes(pack, world_slug=world)
    assert scenes, f"no char_creation scenes resolved for world {world!r}"
    builder = CharacterBuilder(
        scenes=scenes, rules=pack.rules, backstory_tables=pack.backstory_tables
    ).with_lobby_name(name)
    if pack.equipment_tables is not None:
        builder = builder.with_equipment_tables(pack.equipment_tables)
    if pack.classes:
        builder = builder.with_classes(pack.classes)

    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 80, "chargen walk did not reach confirmation"
        if builder.is_awaiting_followup():
            builder.answer_followup(name)
            continue
        scene = builder.current_scene()
        eff = scene.mechanical_effects
        step = eff.fate_chargen_step if eff is not None else None
        if step == "aspects":
            payload = builder.to_scene_message("p1").payload
            slots = payload.fate_aspect_slots or []
            free = [
                s.value
                for s in slots
                if s.kind not in ("high_concept", "trouble") and (s.value or "").strip()
            ]
            builder.apply_fate_aspects(
                high_concept="The Rector Who Sees Too Much",
                trouble="I Cannot Let an Injustice Stand",
                free_aspects=free,
            )
            continue
        if step == "pyramid":
            payload = builder.to_scene_message("p1").payload
            builder.apply_fate_pyramid(dict(payload.fate_current_allocation or {}))
            continue
        if step == "stunts":
            builder.apply_fate_stunts([])
            continue
        if not scene.choices:
            try:
                builder.apply_auto_advance()
            except Exception:
                builder.apply_freeform(name)
            continue
        # Prefer the named vocation (so calling_label is deterministic when a world
        # is asked for one); else take the first choice.
        idx = None
        if prefer_vocation is not None:
            idx = next(
                (i for i, c in enumerate(scene.choices) if c.label == prefer_vocation),
                None,
            )
        builder.apply_choice(idx if idx is not None else 0)

    return builder.build(name)


class TestDriveBuilderSeam:
    # glenross is the story's named world (deterministic "Episcopal Rector" vocation);
    # blackthorn_moor is the sibling tea_and_murder world with the IDENTICAL missing-
    # drive gap, fixed in the same change (the "no half-wired features" principle).
    @pytest.mark.parametrize(
        "world,prefer_vocation,expected_calling",
        [
            ("glenross", VOWEL_VOCATION, VOWEL_VOCATION),
            ("blackthorn_moor", None, None),
        ],
    )
    def test_chargen_assigns_real_drive_not_vocation_label(
        self, world: str, prefer_vocation: str | None, expected_calling: str | None
    ) -> None:
        """AC-1: after Fate chargen, ``character.drive`` is a genuine aspiration — not
        the vocation/calling label, and not empty (an empty drive is what makes the
        downstream seed silently fall back to the calling)."""
        pc = _build_pc(world, prefer_vocation=prefer_vocation)

        calling = (pc.calling_label or "").strip()
        assert calling, f"{world}: harness picked no vocation/calling"
        if expected_calling is not None:
            assert pc.calling_label == expected_calling, (
                f"{world}: harness expected to pick {expected_calling!r}; got "
                f"calling_label={pc.calling_label!r}"
            )

        drive = (pc.drive or "").strip()
        assert drive, (
            f"{world} Fate chargen left character.drive EMPTY — the world authors no "
            "drive scene, so the seeded quest spine silently falls back to the calling "
            "label. Add a real drive surface (drive scene / per-calling drives list)."
        )
        assert drive != pc.calling_label, (
            f"{world}: character.drive echoes the vocation/calling label "
            f"{pc.calling_label!r} instead of a real aspiration"
        )


# ---------------------------------------------------------------------------
# AC-2 / AC-3 / AC-5 — handler seam: the production chargen-commit path seeds a
# meaningful quest whose title is the DRIVE, not the calling label.
# ---------------------------------------------------------------------------


@pytest.fixture
def _pg_isolation(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    """Bind the process pool to a per-worker throwaway PG db, truncated per test,
    so each commit is a first-commit (which is the only path that seeds). Pattern
    copied verbatim from ``test_chargen_quest_seed_wiring.py``."""
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


def _install_hermetic_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """glenross's chargen-commit fires an opening narration turn (caverns'
    flickering_reach does not). The narrator prose itself comes from the mock
    client, but the post-narration SIDECAR extractor (``build_sidecar_extractor_llm``
    → ``_call_haiku_sdk`` → ``llm_factory.query``) is the one collaborator that is
    NOT covered by the conftest's ``build_async_anthropic`` stub, so it reaches the
    agent-SDK ``query`` seam. The conftest autouse guard already REFUSES the real
    transport (no live call can happen), but feeding the sidecar an empty,
    schema-valid ``SidecarExtraction`` ({} — every field defaults) keeps this test
    hermetic-clean instead of leaning on the catch-loop to swallow a refusal.

    Installed from the TEST BODY (not a fixture) so it is the LAST ``setattr`` on
    the shared ``query`` symbol — the autouse guard (tests/server/conftest.py:517)
    re-binds ``query`` to ``_refuse`` after requested fixtures, so a fixture-level
    fake loses the ordering race."""
    from tests.agents.fakes.fake_agent_sdk import FakeQuery, structured_output_stream

    fake = FakeQuery(structured_output_stream({}))
    monkeypatch.setattr("sidequest.agents.llm_factory.query", fake, raising=False)
    monkeypatch.setattr("sidequest.agents.anthropic_sdk_client.query", fake, raising=False)


@pytest.fixture
def handler(tmp_path: Path) -> WebSocketSessionHandler:
    return WebSocketSessionHandler(
        claude_client_factory=_mock_claude_client_factory(),
        genre_pack_search_paths=[GENRE_PACKS_DIR],
        save_dir=tmp_path,
    )


async def _walk_handler_to_confirmation(
    handler: WebSocketSessionHandler, name: str
) -> None:
    """Fate-aware walk of the websocket chargen handler to Confirmation: choice
    scenes take the first choice, the three Fate steps submit their seeded
    defaults, display-only scenes ``continue``."""
    sd = handler._session_data  # type: ignore[attr-defined]
    builder = sd.builder
    assert builder is not None, "connect to glenross did not build a chargen builder"
    guard = 0
    while not builder.is_confirmation():
        guard += 1
        assert guard < 80, "handler chargen walk did not reach confirmation"
        scene = builder.current_scene()
        eff = scene.mechanical_effects
        step = eff.fate_chargen_step if eff is not None else None
        if step == "aspects":
            payload = builder.to_scene_message("p1").payload
            slots = payload.fate_aspect_slots or []
            free = [
                s.value
                for s in slots
                if s.kind not in ("high_concept", "trouble") and (s.value or "").strip()
            ]
            out = await _send_chargen(
                handler,
                CharacterCreationPayload(
                    phase="fate_aspects_confirm",
                    fate_high_concept="The Rector Who Sees Too Much",
                    fate_trouble="I Cannot Let an Injustice Stand",
                    fate_free_aspects=free,
                ),
            )
        elif step == "pyramid":
            payload = builder.to_scene_message("p1").payload
            out = await _send_chargen(
                handler,
                CharacterCreationPayload(
                    phase="fate_pyramid_confirm",
                    fate_allocation=dict(payload.fate_current_allocation or {}),
                ),
            )
        elif step == "stunts":
            out = await _send_chargen(
                handler,
                CharacterCreationPayload(phase="fate_stunts_confirm", fate_selected_stunts=[]),
            )
        elif scene.choices:
            out = await _send_chargen(handler, CharacterCreationPayload(phase="scene", choice="1"))
        elif scene.allows_freeform:
            out = await _send_chargen(
                handler, CharacterCreationPayload(phase="scene", choice=name)
            )
        else:
            out = await _send_chargen(handler, CharacterCreationPayload(phase="continue"))
        assert out and not isinstance(out[0], ErrorMessage), (
            f"chargen handler errored at scene {builder.current_scene_index()}: "
            f"{getattr(out[0].payload, 'message', out) if out else 'no output'}"
        )


class TestGlenrossSeedQuestHandlerSeam:
    def test_commit_seeds_meaningful_quest_not_the_calling(
        self,
        handler: WebSocketSessionHandler,
        _pg_isolation,
        otel_capture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC-2 / AC-3 / AC-5: drive the FULL Fate chargen path through the
        production commit handler (not ``quest_seed.py`` directly). The seeded
        ``seed_drive`` quest must carry the drive, never the calling label, and the
        seed span must be a real (non-warning) seed — no silent calling-label echo.
        """
        _install_hermetic_sdk(monkeypatch)

        async def body() -> None:
            await _connect(handler, genre=GENRE_SLUG, world=WORLD_SLUG, player_name="Eleanor")
            await _walk_handler_to_confirmation(handler, name="Eleanor Vance")
            out = await _send_chargen(handler, CharacterCreationPayload(phase="confirmation"))
            assert out, "confirmation must return at least the CHARACTER_CREATION{complete} message"

        run(body())

        sd = handler._session_data  # type: ignore[attr-defined]
        snap = sd.snapshot
        assert len(snap.characters) == 1, "PC must be materialized on the snapshot"
        pc = snap.characters[0]
        calling = (pc.calling_label or "").strip()
        assert calling, "harness sanity: a vocation/calling should have been chosen"

        # The seed must have produced a real spine entry (AC-2: non-degenerate).
        assert SEED_QUEST_ID in snap.quest_log, (
            "no seed_drive quest was seeded on commit — glenross chargen produced no "
            "drive for the spine to seed from"
        )
        seeded = snap.quest_log[SEED_QUEST_ID]
        assert (seeded.title or "").strip(), "seeded quest has an empty title"

        # AC-5 / the bug: the seeded quest must NOT silently echo the calling label.
        assert seeded.title != calling, (
            f"seeded quest title is the calling label {calling!r} — the drive was "
            "empty and the spine silently fell back to the vocation"
        )
        assert seeded.objective != calling, (
            f"seeded quest objective is the calling label {calling!r}, not a real drive"
        )
        assert snap.active_stakes.strip() != calling, (
            f"active_stakes is the calling label {calling!r}, not a real drive"
        )

        # AC-1 corroboration at the snapshot level: the committed PC has a real drive,
        # and the spine seeded from THAT drive (quest_seed copies character.drive).
        drive = (pc.drive or "").strip()
        assert drive and drive != calling, (
            f"committed character.drive is empty or echoes the calling {calling!r}"
        )
        assert seeded.title == drive, (
            "seeded quest title should be the character's drive (quest_seed copies "
            f"character.drive); got title={seeded.title!r} drive={drive!r}"
        )

        # The seed engaged as a REAL seed (severity != warning), proving it did not
        # take the loud-degrade (empty-source) path nor a silent calling fallback.
        seed_spans = [s for s in otel_capture.get_finished_spans() if s.name == SEED_SPAN]
        assert len(seed_spans) == 1, (
            f"exactly one {SEED_SPAN} span must fire on commit (got {len(seed_spans)})"
        )
        attrs = dict(seed_spans[0].attributes or {})
        assert attrs.get("severity") != "warning", (
            "seed fired a 'warning' (empty-drive) span — glenross still has no real "
            "drive to seed from"
        )
        assert attrs.get("has_stakes") is True


# ---------------------------------------------------------------------------
# AC-4 — article agreement (already fixed by server PR #988). Regression guard
# that the indefinite_article helper is exercised on the {class} vocation slot.
# ---------------------------------------------------------------------------


class TestVocationArticleAgreement:
    def test_indefinite_article_picks_an_for_vowel_vocation(self) -> None:
        from sidequest.game.builder import indefinite_article

        assert indefinite_article(VOWEL_VOCATION) == "an"
        assert indefinite_article("Country Doctor") == "a"

    def test_class_slot_renders_an_episcopal_rector(self) -> None:
        from sidequest.game.builder import substitute_token_with_article

        rendered = substitute_token_with_article(
            "Life as a {class} is quiet enough.", "{class}", VOWEL_VOCATION
        )
        assert "an Episcopal Rector" in rendered
        assert "a Episcopal Rector" not in rendered

    def test_sentence_leading_article_stays_capitalized(self) -> None:
        from sidequest.game.builder import substitute_token_with_article

        rendered = substitute_token_with_article(
            "A {class} keeps odd hours.", "{class}", VOWEL_VOCATION
        )
        assert rendered.startswith("An Episcopal Rector")
