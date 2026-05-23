"""RED tests for Story 50-18 — POST /dev/scene/{name} dev-gated endpoint.

Wire-level + watcher-level integration tests for the scene-harness HTTP
endpoint specified in ADR-092. The endpoint hydrates a fixture YAML into
a GameSnapshot, persists via SqliteStore, and returns the minted slug —
all gated behind ``DEV_SCENES=1`` so production builds carry zero surface.

Contract under test (ADR-092 §Decision):

1. ``POST /dev/scene/{name}`` is registered ONLY when ``DEV_SCENES=1`` is
   set when ``create_app()`` runs. With the env var unset, the route does
   not appear in ``app.routes`` and a POST to it returns 404 (FastAPI's
   default for an unmatched path).
2. On success, returns ``{"slug": "<game_slug>"}`` and the save file is
   present at ``db_path_for_slug(save_dir, slug)`` with the hydrated
   snapshot in ``game_state``.
3. Missing fixture → 404 with the missing path in the JSON body.
4. Hydration error → 422 with field-level detail.
5. OTEL: emits ``scene_harness.intent.load``, ``.hydrate.ok``, ``.persist.ok``
   spans on the success path; ``.hydrate.error`` on the failure path.
6. The route is wired in through the production ``create_app()`` factory,
   not via a hand-built test app — verifies real integration per CLAUDE.md
   "Every Test Suite Needs a Wiring Test".

All tests currently RED — the route, hydrator, and span helpers do not
exist yet (ADR-092 implementation-status: partial; ADR-087 P0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_FIXTURES_DIR = REPO_ROOT / "scenarios" / "fixtures"


# ── Fixtures: capture watcher events ────────────────────────────────────────


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, dict]]:
    """Patch ``publish_event`` on the watcher hub module so every emitted
    semantic span is captured for the assertion phase.

    Mirrors the pattern in ``tests/server/test_render_mounts.py``.
    """
    captured: list[tuple[str, dict, dict]] = []

    def fake_publish(
        event_type: str,
        fields: dict[str, Any],
        *,
        component: str = "",
        severity: str = "info",
    ) -> None:
        captured.append((event_type, dict(fields), {"component": component, "severity": severity}))

    import sidequest.telemetry.watcher_hub as _hub

    monkeypatch.setattr(_hub, "publish_event", fake_publish)
    return captured


def _build_dev_scenes_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    save_dir: Path,
    fixtures_dir: Path = CANONICAL_FIXTURES_DIR,
):
    """Construct a production-shaped FastAPI app with ``DEV_SCENES=1`` set.

    Uses the real ``create_app()`` factory — not a hand-built ``FastAPI()``
    — because the wiring-test rule (CLAUDE.md) requires every test suite
    to verify the component is reachable through production code paths.
    """
    monkeypatch.setenv("DEV_SCENES", "1")
    monkeypatch.setenv("SIDEQUEST_FIXTURES_DIR", str(fixtures_dir))

    # Importing inside the helper keeps the env mutation in scope for the
    # one-shot factory call — ``create_app()`` reads the env at construction
    # time per ADR-092 §Decision point 1.
    from sidequest.server.app import create_app

    return create_app(
        save_dir=save_dir,
        genre_pack_search_paths=[],
    )


# ── AC-2: route absent without DEV_SCENES=1 ─────────────────────────────────


def test_scene_route_absent_when_dev_scenes_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §Decision point 1: when ``DEV_SCENES`` is unset, the
    ``/dev/scene/{name}`` route MUST NOT be registered. POST returns 404
    (FastAPI's default for unmatched path) — production builds carry
    ZERO scene-harness surface."""
    monkeypatch.delenv("DEV_SCENES", raising=False)

    from sidequest.server.app import create_app

    app = create_app(save_dir=tmp_path, genre_pack_search_paths=[])

    # The route must not appear in ``app.routes`` at all — not present-but-403,
    # not present-but-redirected. Absent.
    paths = {getattr(r, "path", "") for r in app.routes}
    scene_routes = [p for p in paths if "/dev/scene" in p]
    assert scene_routes == [], (
        f"DEV_SCENES unset — /dev/scene/* must not be registered, found: {scene_routes!r}"
    )

    client = TestClient(app)
    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 404, (
        f"DEV_SCENES unset — POST /dev/scene/combat_brawl_wasteland must 404, got {r.status_code}"
    )


def test_scene_route_absent_when_dev_scenes_env_set_to_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the exact string ``"1"`` enables the route. ``"0"``, ``"false"``,
    ``""`` — all keep the production-safe default. This is a "fail-closed"
    test: ambiguity in the flag value must NOT silently enable dev surface."""
    monkeypatch.setenv("DEV_SCENES", "0")

    from sidequest.server.app import create_app

    app = create_app(save_dir=tmp_path, genre_pack_search_paths=[])

    client = TestClient(app)
    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 404, f"DEV_SCENES=0 must keep the route absent, got {r.status_code}"


# ── AC-1: route present + happy path ────────────────────────────────────────


def test_scene_route_registered_when_dev_scenes_env_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With ``DEV_SCENES=1`` and a real canonical fixture, the route is
    registered AND a POST succeeds. This is the wiring test."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)

    paths = {getattr(r, "path", "") for r in app.routes}
    scene_routes = [p for p in paths if "/dev/scene" in p]
    assert scene_routes, (
        f"DEV_SCENES=1 — /dev/scene/* must be registered. routes: {sorted(paths)!r}"
    )

    client = TestClient(app)
    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200, (
        f"POST /dev/scene/combat_brawl_wasteland (DEV_SCENES=1) must succeed; "
        f"got {r.status_code}, body: {r.text}"
    )


def test_scene_post_response_body_has_slug_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §Decision point 1: response is ``{"slug": "<game_slug>"}``.

    The UI side (App.tsx:1402) destructures ``{ slug }`` — the field name is
    a contract, not an implementation detail."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)
    assert "slug" in body, f"response missing 'slug' field: {body!r}"
    assert isinstance(body["slug"], str) and body["slug"], (
        f"slug must be a non-empty string, got {body['slug']!r}"
    )


def test_scene_post_persists_save_file_at_slug_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §Decision point 2: persist via the existing SqliteStore.

    After a successful POST, the save file MUST exist at
    ``db_path_for_slug(save_dir, slug)`` — the same path the slug-keyed
    connect handler (dispatch_connect) will look at."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200
    slug = r.json()["slug"]

    from sidequest.game.persistence import db_path_for_slug

    expected = db_path_for_slug(tmp_path, slug)
    assert expected.exists(), (
        f"save file missing at expected slug path {expected!s}; "
        f"scene-harness must persist via SqliteStore so dispatch_connect can find it"
    )


def test_scene_post_persisted_snapshot_carries_fixture_genre_and_world(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The persisted snapshot must round-trip the fixture's identity fields.

    Loading the saved game_state and parsing it back into GameSnapshot
    yields ``genre_slug=mutant_wasteland`` and ``world_slug=flickering_reach``
    (from combat_brawl_wasteland.yaml). If these are blank or wrong, every
    downstream genre-pack lookup will silently fall through."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200
    slug = r.json()["slug"]

    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(tmp_path, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None, "save file exists but SqliteStore.load returned None"

    # ``SavedSession.snapshot`` is the hydrated GameSnapshot.
    snapshot = saved.snapshot
    assert snapshot.genre_slug == "mutant_wasteland"
    assert snapshot.world_slug == "flickering_reach"


def test_scene_post_persisted_snapshot_carries_fixture_character(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The fixture's ``character:`` block must reach ``snapshot.characters[0]``.

    Without this, the slug-connect handler sees ``has_character=False`` and
    drops the player back into chargen — defeating the entire point of
    the scene harness (skip chargen for iteration)."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    slug = r.json()["slug"]

    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(tmp_path, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None
    snapshot = saved.snapshot

    assert len(snapshot.characters) >= 1, (
        "combat_brawl_wasteland.yaml has a character block — snapshot.characters[0] must be populated"
    )
    # Character nests CreatureCore under ``.core``; ``Character.name`` is a method.
    assert snapshot.characters[0].core.name == "Skar"


@pytest.mark.parametrize(
    "fixture_name",
    [
        "combat_brawl_wasteland",
        "combat_dogfight_space",
        "social_negotiation_tea",
        "social_poker_wasteland",
    ],
)
def test_every_canonical_fixture_can_be_loaded_via_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture_name: str,
) -> None:
    """ADR-092 §AC-4: all four canonical fixtures hydrate via the URL flow.

    The UI's ``?scene=NAME`` path will hit each of these; any single one
    failing breaks Keith's iteration loop for that mechanic."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post(f"/dev/scene/{fixture_name}")
    assert r.status_code == 200, (
        f"canonical fixture {fixture_name!r} must hydrate via the endpoint; "
        f"got {r.status_code}, body: {r.text}"
    )


# ── AC-3: failure is loud ───────────────────────────────────────────────────


def test_unknown_fixture_returns_404_with_path_in_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §Decision point 5: missing fixture YAML → 404 with the
    missing path in the body. Loud failure — never silent fall through."""
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/does_not_exist_anywhere")
    assert r.status_code == 404
    body = r.json()
    # Body must surface the missing fixture name (and ideally the path) so
    # the dev knows which file to create — not a generic "not found".
    body_text = repr(body).lower()
    assert "does_not_exist_anywhere" in body_text, (
        f"404 body must name the missing fixture; got {body!r}"
    )


def test_malformed_fixture_yaml_returns_422_with_field_detail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §Decision point 5: hydration error → 422 with field-level
    detail. A fixture missing ``genre:`` must surface a 422 that says
    ``genre`` somewhere — not 500, not a stack trace."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    bad = fixtures_dir / "no_genre.yaml"
    bad.write_text("world: default\n", encoding="utf-8")

    save_dir = tmp_path / "saves"
    save_dir.mkdir()

    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/no_genre")
    assert r.status_code == 422, (
        f"malformed fixture (missing genre) must surface 422, got {r.status_code}: {r.text}"
    )
    body_text = repr(r.json()).lower()
    assert "genre" in body_text, f"422 body must surface the failing field name; got {r.json()!r}"


# ── OTEL: watcher events on every subsystem decision (CLAUDE.md principle) ──


def test_scene_harness_emits_load_intent_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §OTEL: emit ``scene_harness.intent.load`` with
    ``fixture_name`` and (ultimately) ``game_slug`` so the GM panel can
    prove the harness was the source of this game's first turn."""
    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200

    intent_events = [e for e in captured if "scene_harness" in e[0] and "load" in e[0]]
    assert intent_events, (
        f"scene-harness must emit a load-intent span; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )
    # The intent span must carry the fixture name so the GM panel groups
    # events by fixture (Keith iterates many fixtures per playtest session).
    fields = intent_events[0][1]
    assert fields.get("fixture_name") == "combat_brawl_wasteland", (
        f"load-intent span must carry fixture_name='combat_brawl_wasteland'; got fields={fields!r}"
    )


def test_scene_harness_emits_hydrate_ok_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §OTEL: ``scene_harness.hydrate.ok`` on success path,
    carrying field counts (npcs, characters) so the dashboard can
    confirm the fixture wasn't silently empty-hydrated."""
    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200

    ok_events = [
        e for e in captured if "scene_harness" in e[0] and "hydrate" in e[0] and "ok" in e[0]
    ]
    assert ok_events, (
        f"scene-harness must emit hydrate.ok on success; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )


def test_scene_harness_emits_persist_ok_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §OTEL: ``scene_harness.persist.ok`` so the GM panel sees
    persistence committed (not just "we got to the end of the route")."""
    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(monkeypatch, save_dir=tmp_path)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_brawl_wasteland")
    assert r.status_code == 200

    persist_events = [e for e in captured if "scene_harness" in e[0] and "persist" in e[0]]
    assert persist_events, (
        f"scene-harness must emit persist.ok on success; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )
    # The persist span must carry the minted slug so the GM panel correlates
    # the harness-load to subsequent slug-connect events.
    slug = r.json()["slug"]
    fields = persist_events[0][1]
    assert fields.get("game_slug") == slug or fields.get("slug") == slug, (
        f"persist.ok span must carry the minted slug {slug!r}; got fields={fields!r}"
    )


def test_scene_harness_emits_hydrate_error_span_on_invalid_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ADR-092 §OTEL: ``scene_harness.hydrate.error`` on failure path
    so the GM panel surfaces the rejected fixture without a developer
    having to tail server logs."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "no_genre.yaml").write_text("world: default\n", encoding="utf-8")
    save_dir = tmp_path / "saves"
    save_dir.mkdir()

    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/no_genre")
    assert r.status_code == 422

    err_events = [e for e in captured if "scene_harness" in e[0] and "error" in e[0]]
    assert err_events, (
        f"scene-harness must emit a hydrate.error span on 422; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )


# ── Story 50-23 wiring: POST /dev/scene/{name} with multi-PC fixtures ───────
#
# AC#11 from the story session: load a multi-PC fixture end-to-end through
# the production route — POST returns slug, snapshot persists, and the
# saved snapshot carries every PC the fixture declared. Uses a tmp_path
# fixture (not a Wave 2 canonical file on disk) so the test's red/green
# status reflects ONLY the multi-PC hydrator change.


def test_dev_scene_route_persists_four_pc_party_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC#11 wiring: a 4-PC fixture loaded via POST /dev/scene/{name}
    persists a snapshot whose ``characters`` list carries every PC the
    fixture declared, in declared order.

    Production-path wiring (CLAUDE.md "Every Test Suite Needs a Wiring
    Test"): the hydrator change is reachable from real code paths —
    route registered by ``create_app()``, route calls ``hydrate_fixture``,
    result persisted via ``SqliteStore``, ``slug-connect`` can subsequently
    find N characters in the save.

    A unit-only suite would not catch a hydrator that returns the right
    object but a router that flattens the list back to ``characters[0]``
    before persisting; this test does.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "party_test.yaml").write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "characters:\n"
        "  - name: Wren\n    description: scout\n    personality: cautious\n"
        "    backstory: scouted these tunnels before\n    char_class: thief\n    race: human\n"
        "  - name: Borin\n    description: warrior\n    personality: hot-tempered\n"
        "    backstory: clan war veteran\n    char_class: fighter\n    race: dwarf\n"
        "  - name: Caia\n    description: cleric\n    personality: stoic\n"
        "    backstory: temple novitiate\n    char_class: cleric\n    race: human\n"
        "  - name: Dax\n    description: rogue\n    personality: sly\n"
        "    backstory: street thief\n    char_class: thief\n    race: halfling\n",
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)

    client = TestClient(app)
    r = client.post("/dev/scene/party_test")
    assert r.status_code == 200, (
        f"multi-PC fixture must hydrate via POST /dev/scene; got {r.status_code} body={r.text}"
    )
    slug = r.json()["slug"]

    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(save_dir, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None, (
        "save file exists but SqliteStore.load returned None — "
        "the route either didn't persist or wrote to the wrong path"
    )

    names = [pc.core.name for pc in saved.snapshot.characters]
    assert names == ["Wren", "Borin", "Caia", "Dax"], (
        f"multi-PC list must round-trip through the harness in fixture-declared order; "
        f"got {names!r}"
    )


def test_dev_scene_route_hydrate_ok_span_reports_full_character_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OTEL wiring (CLAUDE.md OTEL Observability Principle): the
    ``scene_harness.hydrate.ok`` span fired by the router reports the
    party size, not 1.

    Without this, the GM panel can't tell whether a multi-PC fixture
    actually hydrated all four PCs or whether the hydrator silently
    collapsed to one — which is the same lie-detector concern that
    drives every other span in this file.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "party_three.yaml").write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "characters:\n"
        "  - name: Alpha\n    description: alpha PC\n    personality: brave\n"
        "    backstory: first of three\n    char_class: fighter\n    race: human\n"
        "  - name: Beta\n    description: beta PC\n    personality: clever\n"
        "    backstory: second of three\n    char_class: thief\n    race: human\n"
        "  - name: Gamma\n    description: gamma PC\n    personality: wise\n"
        "    backstory: third of three\n    char_class: cleric\n    race: human\n",
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    captured = _capture_events(monkeypatch)
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/party_three")
    assert r.status_code == 200

    ok_events = [e for e in captured if e[0] == "scene_harness.hydrate.ok"]
    assert ok_events, (
        f"missing scene_harness.hydrate.ok span; got types: {sorted({e[0] for e in captured})!r}"
    )
    fields = ok_events[0][1]
    assert fields.get("character_count") == 3, (
        f"hydrate.ok must report the full party size; "
        f"got character_count={fields.get('character_count')!r} "
        f"(fields: {fields!r})"
    )


def test_dev_scene_route_rejects_both_character_and_characters_with_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC#4 + AC#10 through the HTTP boundary: a fixture with BOTH
    ``character:`` and ``characters:`` surfaces as HTTP 422 (not 500, not
    silently-pick-one-and-200).

    The FixtureValidationError → 422 mapping is the existing pattern in
    ``scene_harness_router``; this test extends it to the new conflict
    case so a future hydrator regression that swallows the conflict is
    visible at the wire.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    # Both blocks INDIVIDUALLY valid — the only thing that can fail
    # validation is the conflict check itself. See the sibling unit
    # test ``test_both_character_and_characters_blocks_raises_*`` for
    # the same discipline at the hydrator layer.
    (fixtures_dir / "both_blocks_route.yaml").write_text(
        "genre: caverns_and_claudes\n"
        "world: default\n"
        "character:\n"
        "  name: Solo\n  description: solo PC\n  personality: stoic\n"
        "  backstory: lone wanderer\n  char_class: ranger\n  race: elf\n"
        "characters:\n"
        "  - name: Party\n    description: a party member\n    personality: gregarious\n"
        "    backstory: tavern regular\n    char_class: bard\n    race: half-elf\n",
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/both_blocks_route")
    assert r.status_code == 422, (
        f"conflicting character+characters fixture must 422 at the wire; "
        f"got {r.status_code} body={r.text}"
    )


# ── Story 50-20: scenario_state hydration through the wire ──────────────────


def test_dev_scene_route_persists_scenario_state_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC#16: POST /dev/scene/{name} with a mystery fixture → snapshot
    persists with scenario_state populated → SqliteStore round-trip
    preserves clue_graph, discovered_clues, npc_roles, guilty_npc, tension.

    The integration probe that proves Wave 2 mystery fixtures will work:
    without this, an in-memory hydrator test could pass while the wire
    drops the scenario_state field on the floor.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "mystery_mid_tea_red.yaml").write_text(
        "genre: tea_and_murder\n"
        "world: victoria\n"
        "character:\n"
        "  name: Investigator\n  description: A keen-eyed sleuth\n"
        "  personality: observant\n  backstory: tea, biscuits, and bodies\n"
        "  char_class: detective\n  race: human\n"
        "npcs:\n"
        "  - name: Lady Ashworth\n    role: hostess\n    disposition: 0\n"
        "  - name: Mr. Pike\n    role: butler\n    disposition: 0\n"
        "  - name: Dr. Hartmoor\n    role: physician\n    disposition: 0\n"
        "scenario_state:\n"
        "  clue_graph:\n"
        "    nodes:\n"
        "      - id: cracked_teacup\n"
        "        type: physical_evidence\n"
        "        description: The teacup is cracked along the rim\n"
        "        discovery_method: observation\n"
        "        visibility: public\n"
        "        requires: []\n"
        "      - id: raised_voices\n"
        "        type: testimony\n"
        "        description: The butler heard raised voices\n"
        "        discovery_method: interrogation\n"
        "        visibility: public\n"
        "        requires: [cracked_teacup]\n"
        "      - id: insurance_motive\n"
        "        type: motive\n"
        "        description: Insurance policy named the victim\n"
        "        discovery_method: research\n"
        "        visibility: secret\n"
        "        requires: [raised_voices]\n"
        "  discovered_clues: [cracked_teacup]\n"
        "  npc_roles:\n"
        "    Lady Ashworth: guilty\n"
        "    Mr. Pike: witness\n"
        "    Dr. Hartmoor: innocent\n"
        "  guilty_npc: Lady Ashworth\n"
        "  tension: 0.5\n",
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/mystery_mid_tea_red")
    assert r.status_code == 200, (
        f"mystery fixture with scenario_state must 200 at the wire; "
        f"got {r.status_code} body={r.text}"
    )
    slug = r.json()["slug"]

    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(save_dir, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None, (
        "save file exists but SqliteStore.load returned None — persistence failed after hydration"
    )
    snapshot = saved.snapshot

    state = snapshot.scenario_state
    assert state is not None, (
        "scenario_state must round-trip through SqliteStore for slug-connect "
        "to inherit the pre-populated state"
    )
    assert [n.id for n in state.clue_graph.nodes] == [
        "cracked_teacup",
        "raised_voices",
        "insurance_motive",
    ], (
        f"clue_graph node ids drifted across persistence; got {[n.id for n in state.clue_graph.nodes]!r}"
    )
    assert state.discovered_clues == {"cracked_teacup"}, (
        f"discovered_clues drifted across persistence; got {state.discovered_clues!r}"
    )
    assert state.npc_roles == {
        "Lady Ashworth": "guilty",
        "Mr. Pike": "witness",
        "Dr. Hartmoor": "innocent",
    }, f"npc_roles drifted across persistence; got {state.npc_roles!r}"
    assert "Lady Ashworth" in state.guilty_npc, (
        f"guilty_npc identity drifted; got {state.guilty_npc!r}"
    )
    assert state.tension == pytest.approx(0.5)


def test_dev_scene_route_persists_encounter_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-6 (story 50-21): POST /dev/scene/{name} with an encounter fixture →
    snapshot persists with encounter populated → SqliteStore round-trip
    preserves encounter_type and the per-metric threshold override.

    The integration probe that proves Wave 2 pre-armed combat fixtures
    will work: without this, the in-memory hydrator tests could pass while
    the wire drops StructuredEncounter on the floor (mirrors the 50-20
    scenario_state end-to-end probe directly above).
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "combat_pretier_probe.yaml").write_text(
        "genre: mutant_wasteland\n"
        "world: flickering_reach\n"
        "character:\n"
        "  name: Skar\n  description: A scarred vault dweller\n"
        "  personality: cautious\n  backstory: emerged from a vault\n"
        "  char_class: Beastkin\n  race: Uplifted Animal\n"
        "encounter:\n"
        "  type: combat\n"
        "  player_metric:\n"
        "    threshold: 25\n",
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/combat_pretier_probe")
    assert r.status_code == 200, (
        f"combat fixture with encounter must 200 at the wire; got {r.status_code} body={r.text}"
    )
    slug = r.json()["slug"]

    from sidequest.game.encounter import StructuredEncounter
    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(save_dir, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None, (
        "save file exists but SqliteStore.load returned None — persistence failed after hydration"
    )
    snapshot = saved.snapshot

    enc = snapshot.encounter
    assert isinstance(enc, StructuredEncounter), (
        "encounter must round-trip through SqliteStore for slug-connect to "
        f"inherit the pre-armed combat state; got {type(enc).__name__}"
    )
    assert enc.encounter_type == "combat", (
        f"encounter_type drifted across persistence; got {enc.encounter_type!r}"
    )
    assert enc.player_metric.threshold == 25, (
        f"per-metric threshold override lost across persistence; got "
        f"{enc.player_metric.threshold} (expected 25)"
    )
    assert enc.opponent_metric.threshold == 10, (
        f"un-overridden opponent_metric default lost across persistence; got "
        f"{enc.opponent_metric.threshold} (expected 10)"
    )


def test_dev_scene_route_rejects_scenario_state_dag_violation_with_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC#4 + AC#16 through the HTTP boundary: a fixture pre-discovering a
    clue with unmet prerequisites surfaces as HTTP 422 (not 500, not silent
    discard of discovered_clues).

    The PrerequisiteNotSatisfiedError → FixtureValidationError → HTTP 422
    mapping is the existing FixtureValidationError pattern; this test
    extends it to the new DAG-validation case so a future regression that
    swallows the violation is visible at the wire.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "bad_dag_at_wire.yaml").write_text(
        "genre: tea_and_murder\n"
        "world: victoria\n"
        "character:\n"
        "  name: Investigator\n  description: A keen-eyed sleuth\n"
        "  personality: observant\n  backstory: tea, biscuits, and bodies\n"
        "  char_class: detective\n  race: human\n"
        "npcs:\n"
        "  - name: Lady Ashworth\n    role: hostess\n    disposition: 0\n"
        "scenario_state:\n"
        "  clue_graph:\n"
        "    nodes:\n"
        "      - id: clue_a\n"
        "        type: physical_evidence\n"
        "        description: First clue\n"
        "        discovery_method: observation\n"
        "        visibility: public\n"
        "        requires: []\n"
        "      - id: clue_b\n"
        "        type: testimony\n"
        "        description: Second clue\n"
        "        discovery_method: interrogation\n"
        "        visibility: public\n"
        "        requires: [clue_a]\n"
        "  discovered_clues: [clue_b]\n",  # missing clue_a
        encoding="utf-8",
    )

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/bad_dag_at_wire")
    assert r.status_code == 422, (
        f"scenario_state DAG violation must 422 at the wire; got {r.status_code} body={r.text}"
    )


# ── Story 50-22: magic_state + character.abilities through the HTTP path ─────
#
# AC-6 (wiring): a synthetic fixture declaring BOTH new blocks must hydrate
# via the real ``/dev/scene/{name}`` route, persist through SqliteStore, and
# load back with both fields intact. This is the CLAUDE.md-mandated
# integration test — it proves the 50-22 hydration branch is reachable from
# the production HTTP code path (router → hydrate_fixture → SqliteStore),
# not merely unit-correct.

_MAGIC_FIXTURE_50_22 = (
    "genre: space_opera\n"
    "world: coyote_star\n"
    "character:\n"
    "  name: Practitioner\n"
    "  description: A focused adept of the deep arts\n"
    "  personality: Focused and wary\n"
    "  backstory: Apprenticed in the classified registers\n"
    "  char_class: Mage\n"
    "  race: Human\n"
    "  abilities:\n"
    "    - name: Voidstep\n"
    "      genre_description: Slip a half-second sideways out of causality\n"
    "      mechanical_effect: Once per scene, negate one incoming consequence\n"
    "      source: Class\n"
    "      involuntary: false\n"
    "magic_state:\n"
    "  config:\n"
    "    world_slug: coyote_star\n"
    "    genre_slug: space_opera\n"
    "    allowed_sources: [innate]\n"
    "    active_plugins: [innate_v1]\n"
    "    intensity: 0.25\n"
    "    world_knowledge:\n"
    "      primary: classified\n"
    "      local_register: folkloric\n"
    "    visibility:\n"
    "      primary: feared\n"
    "      local_register: dismissed\n"
    "    hard_limits:\n"
    "      - id: psionics_never_decisive\n"
    "        description: psionics can never be the decisive factor\n"
    "    cost_types: [sanity]\n"
    "    ledger_bars: []\n"
    "    narrator_register: clinical\n"
    "  control_tier:\n"
    "    practitioner: 2\n"
)


def test_scene_post_persists_magic_state_and_abilities_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-6: the new blocks survive the full production round-trip.

    POST the synthetic fixture through ``/dev/scene/{name}``, then load the
    saved DB with ``SqliteStore`` and assert ``snapshot.magic_state`` is a
    non-None MagicState with the declared config + control_tier AND
    ``snapshot.characters[0].abilities`` carries the declared ability.

    Without persistence fidelity the scene harness can't stage a mid-ritual
    magic fixture for iteration — the whole point of 50-22.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "magic_50_22.yaml").write_text(_MAGIC_FIXTURE_50_22, encoding="utf-8")

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/magic_50_22")
    assert r.status_code == 200, (
        f"magic_state + abilities fixture must hydrate via the endpoint; "
        f"got {r.status_code} body={r.text}"
    )
    slug = r.json()["slug"]

    from sidequest.game.persistence import SqliteStore, db_path_for_slug

    store = SqliteStore(db_path_for_slug(save_dir, slug))
    store.initialize()
    saved = store.load()
    assert saved is not None, "save file exists but SqliteStore.load returned None"
    snapshot = saved.snapshot

    assert snapshot.magic_state is not None, (
        "magic_state must survive the SqliteStore round-trip, not be None"
    )
    assert snapshot.magic_state.config.world_slug == "coyote_star"
    assert snapshot.magic_state.control_tier == {"practitioner": 2}, (
        f"control_tier drifted across persistence; got {snapshot.magic_state.control_tier!r}"
    )

    assert len(snapshot.characters) >= 1
    abilities = snapshot.characters[0].abilities
    assert [a.name for a in abilities] == ["Voidstep"], (
        f"character abilities must survive the round-trip; got {abilities!r}"
    )
    assert str(abilities[0].source) == "Class"


def test_dev_scene_route_rejects_malformed_magic_config_with_422(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-2 + AC-3 through the HTTP boundary: a ``magic_state.config``
    missing a required WorldMagicConfig field must surface as HTTP 422
    (FixtureValidationError → 422), never a leaked 500 and never a silent
    200 with magic_state quietly dropped.
    """
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    broken = _MAGIC_FIXTURE_50_22.replace("    narrator_register: clinical\n", "")
    (fixtures_dir / "magic_badcfg_at_wire.yaml").write_text(broken, encoding="utf-8")

    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/magic_badcfg_at_wire")
    assert r.status_code == 422, (
        f"malformed magic_state.config must 422 at the wire; got {r.status_code} body={r.text}"
    )


def test_scene_harness_emits_magic_state_hydrated_span(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """50-22 OTEL wiring (CLAUDE.md observability principle + "Every Test
    Suite Needs a Wiring Test"): hydrating a ``magic_state:`` fixture must
    emit a ``magic.state_hydrated`` watcher event so the GM panel can
    confirm the fixture staged real magic state rather than the narrator
    improvising one.

    Found by simplify-quality during verify: the event was emitted but
    unasserted, and the original bound-import (`publish_event as
    _watcher_publish`) made it uncapturable by the standard
    ``_capture_events`` harness. The emitter was realigned to the
    ``scene_harness_router`` convention (`_hub.publish_event`) so this
    test exercises the real production path.
    """
    captured = _capture_events(monkeypatch)

    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    (fixtures_dir / "magic_otel.yaml").write_text(_MAGIC_FIXTURE_50_22, encoding="utf-8")
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    app = _build_dev_scenes_app(monkeypatch, save_dir=save_dir, fixtures_dir=fixtures_dir)
    client = TestClient(app)

    r = client.post("/dev/scene/magic_otel")
    assert r.status_code == 200, f"fixture must hydrate; got {r.status_code} body={r.text}"

    magic_events = [e for e in captured if e[0] == "magic.state_hydrated"]
    assert magic_events, (
        f"hydrating magic_state: must emit a 'magic.state_hydrated' watcher event; "
        f"captured event types: {sorted({e[0] for e in captured})!r}"
    )
    event_type, fields, meta = magic_events[0]
    # Field-level identity — the lie-detector needs real values, not a bare
    # truthy (a silently-empty-hydrated fixture would carry wrong slugs).
    assert fields["world_slug"] == "coyote_star", (
        f"event must report the hydrated world_slug; got {fields!r}"
    )
    assert fields["genre_slug"] == "space_opera"
    assert fields["control_tier_actors"] == 1, (
        f"event must report the 1 control_tier actor from the fixture; got {fields!r}"
    )
    assert meta["component"] == "magic", (
        f"event must be tagged component=magic for the Subsystems tab; got {meta!r}"
    )
