"""RED-phase contract tests — Story 100-7.

Phase 1, **Theme tokens** slice (concern C3) of the reference-pages → React
migration (spec:
``docs/superpowers/specs/2026-06-08-reference-pages-react-migration-design.md``).

The app's ``useGenreTheme`` is **session-coupled**: it injects genre CSS variables
from a ``theme_css`` event the server pushes over the WebSocket on connect. A
session-free reference route (``/reference/lore/:pack/:world``,
``/reference/rules/:pack``) has no such event. C3's resolution: reuse the
*mechanism* (CSS-variable injection) but feed it from the reference projection
JSON keyed by the URL ``:pack``, not the WS event. So Phase 1 (this story, server)
must make the projection JSON carry a **CSS-var token set** derived from the
pack's ``theme.yaml`` (palette/fonts/dinkus). Phase 2 (story 100-9, UI) is the
session-free injector that consumes it.

----------------------------------------------------------------------------
TEA decisions pinning this RED phase (logged as deviations/findings in the session
file — read them before changing a test):

  * **No keeper firewall here.** Unlike every prior 100-* slice (lore/rules),
    ``theme.yaml`` is STYLING — public CSS tokens, NOT keeper-firewalled content.
    Confirmed two ways: ``GenreTheme`` (``genre/models/theme.py``) is
    ``extra="forbid"`` over a fixed set of *styling-only* fields (palette colours,
    fonts, archetype, dinkus glyphs, session_opener toggle) — no GM-only/narrator
    field exists; and ``reference_visibility.py`` carries **no** ``theme`` carve.
    So there is NO ``classify()`` dimension and NO keeper-absence assertion.

  * **The firewall analog is ALLOWLIST discipline.** ``theme.yaml`` *does* carry
    non-CSS internal config that must NOT bleed into a CSS-var set: the dinkus
    ``cooldown``/``enabled``/``default_weight`` knobs, ``border_style``, the
    ``session_opener`` toggle, and any future authored field. A naive raw-splat
    (``yaml.safe_load`` → ``{stem: value}``) would dump all of it as bogus
    "tokens". So the load-bearing security test is a **positive control**: prove an
    internal field IS in the raw theme.yaml, then prove it is ABSENT from the token
    set, while the curated CSS vars survive. Same shape as 100-6's
    ``test_raw_splat_would_leak_*`` — just guarding allowlist instead of keeper.
    Per the firewall-test discipline lesson (100-5/100-6), all presence/absence
    assertions serialize with ``json.dumps(ensure_ascii=False)`` so the non-ASCII
    dinkus glyphs (✦ ⬡ †) appear VERBATIM and the substring checks are non-vacuous.

  * **The CSS-var contract is the SHARED ``useGenreTheme`` contract.** The six
    palette vars (``--primary --secondary --accent --background --surface --text``)
    are proven shared: ``useGenreTheme`` injects them and the reference bundle's
    ``theme.css`` defines them. They are NON-NEGOTIABLE. Fonts map
    ``web_font_family→--font-body`` / ``display_font_family→--font-display`` and
    dinkus glyphs map ``light/medium/heavy→--dinkus-{light,medium,heavy}`` — these
    names are pinned here; if Architect/Dev rename them, update the ``[shape]``
    assertions and log a deviation, but keep the security + the six palette vars.

  * **No live-pack assertions.** AC parity with 100-6: the end-to-end tests point
    the real FastAPI router at a SYNTHETIC tmp pack whose ``theme.yaml`` mirrors
    space_opera's real shape (project rule: no assertions against live
    genre_packs).

----------------------------------------------------------------------------
Contract this RED phase pins (Dev implements — ``build_theme_tokens`` does not
exist yet, so the import fails and every test below is RED):

  - ``build_theme_tokens(pack: str, *, pack_dir: Path) -> dict[str, str]``
    Reads ``<pack_dir>/theme.yaml`` and returns a FLAT CSS-var token dict
    (custom-property name → value), curated to a styling allowlist:
        {"--primary": "#…", "--secondary": "#…", "--accent": "#…",
         "--background": "#…", "--surface": "#…", "--text": "#…",
         "--font-body": "<web_font_family>", "--font-display": "<display_font_family>",
         "--dinkus-light": "…", "--dinkus-medium": "…", "--dinkus-heavy": "…"}
    Flat-dict shape is chosen so the Phase-2 injector iterates
    ``for [k,v] of entries: root.style.setProperty(k, v)``. Loud failure on a
    missing required theme field — raises ``MissingThemeFieldError`` (No Silent
    Fallbacks; reuse ``load_reference_theme`` semantics), so a pack that can't be
    themed 500s rather than rendering with collapsed defaults.

  - The projection JSON returned by ``GET /reference/api/rules/{pack}`` AND
    ``GET /reference/api/lore/{pack}/{world}`` carries the token set at the
    top-level ``"theme"`` key (``doc["theme"]["--primary"] == "#…"``). The
    endpoint surfaces a missing/malformed theme.yaml as **HTTP 500** (the route
    already catches ``MissingThemeFieldError``). Attaching at the route layer vs.
    inside ``build_*_projection`` is a Dev call — these tests assert the OBSERVABLE
    HTTP contract, not the internal seam (see the Delivery Finding about the
    empty-pack sibling test in test_reference_rules_projection.py).

Shape-only assertions are flagged ``[shape]``. The ALLOWLIST security assertions
are the non-negotiable ones — do not weaken them to fit a shape change.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

# RED: this import fails until Dev adds the theme-token projection builder.
from sidequest.server.reference_projection import build_theme_tokens
from sidequest.server.reference_routes import create_reference_router
from sidequest.server.reference_theme import MissingThemeFieldError

# ---------------------------------------------------------------------------
# Synthetic theme.yaml mirroring space_opera's real shape. Carries BOTH the
# curated styling fields (which must project) AND the internal non-CSS config
# (which must NOT leak into the token set). Distinctive sentinel values make the
# allowlist absence checks unambiguous.
# ---------------------------------------------------------------------------
_PRIMARY = "#4A90D9"
_SECONDARY = "#2C5F8A"
_ACCENT = "#E8A838"
_BACKGROUND = "#0D1117"
_SURFACE = "#161B22"
_TEXT = "#C9D1D9"
_WEB_FONT = "Rajdhani"
_DISPLAY_FONT = "Orbitron"
# Non-ASCII dinkus glyphs — verbatim-only under ensure_ascii=False.
_DINKUS_LIGHT = "✦"
_DINKUS_MEDIUM = "✦ ⬡ ✦"
_DINKUS_HEAVY = "⬡ ✦ ⬡ ✦ ⬡"

# Internal, non-CSS config that a raw splat would leak as bogus tokens. Each is a
# distinctive sentinel so its absence from the token set is unambiguous.
_INTERNAL_BORDER_STYLE = "ZZBORDERSTYLEZZ"
_INTERNAL_DEFAULT_WEIGHT = "ZZDEFAULTWEIGHTZZ"
_INTERNAL_AUTHORED_NOTE = "ZZINTERNALNOTEZZ"

_THEME_YAML = (
    f"primary: '{_PRIMARY}'\n"
    f"secondary: '{_SECONDARY}'\n"
    f"accent: '{_ACCENT}'\n"
    f"background: '{_BACKGROUND}'\n"
    f"surface: '{_SURFACE}'\n"
    f"text: '{_TEXT}'\n"
    f"border_style: {_INTERNAL_BORDER_STYLE}\n"
    f"web_font_family: {_WEB_FONT}\n"
    f"display_font_family: {_DISPLAY_FONT}\n"
    "archetype: terminal\n"
    # An authored field outside the known schema — proves the projector is an
    # allowlist, not a tolerant splat.
    f"_authored_note: {_INTERNAL_AUTHORED_NOTE}\n"
    "dinkus:\n"
    "  enabled: true\n"
    "  cooldown: 42\n"
    f"  default_weight: {_INTERNAL_DEFAULT_WEIGHT}\n"
    "  glyph:\n"
    f'    light: "{_DINKUS_LIGHT}"\n'
    f'    medium: "{_DINKUS_MEDIUM}"\n'
    f'    heavy: "{_DINKUS_HEAVY}"\n'
    "session_opener:\n"
    "  enabled: true\n"
)

# Curated CSS-var token keys the contract requires.
_REQUIRED_PALETTE_VARS = {
    "--primary": _PRIMARY,
    "--secondary": _SECONDARY,
    "--accent": _ACCENT,
    "--background": _BACKGROUND,
    "--surface": _SURFACE,
    "--text": _TEXT,
}
_REQUIRED_FONT_VARS = {
    "--font-body": _WEB_FONT,
    "--font-display": _DISPLAY_FONT,
}
_REQUIRED_DINKUS_VARS = {
    "--dinkus-light": _DINKUS_LIGHT,
    "--dinkus-medium": _DINKUS_MEDIUM,
    "--dinkus-heavy": _DINKUS_HEAVY,
}

# Internal values that must NEVER appear in the token set (allowlist firewall).
_INTERNAL_SENTINELS = (
    _INTERNAL_BORDER_STYLE,
    _INTERNAL_DEFAULT_WEIGHT,
    _INTERNAL_AUTHORED_NOTE,
)


def _blob(obj: object) -> str:
    """Serialize with ``ensure_ascii=False`` so non-ASCII dinkus glyphs appear
    VERBATIM (default ``ensure_ascii=True`` escapes ✦→\\u2726, making substring
    presence/absence checks vacuous). Firewall-test-discipline carry-forward from
    100-5/100-6."""
    return _json.dumps(obj, ensure_ascii=False)


def _seed_pack(root: Path, pack: str = "space_opera", *, theme: str | None = _THEME_YAML) -> Path:
    """Write a synthetic pack dir with a theme.yaml (and a trivial rules.yaml so
    the rules endpoint also has a section to project). Returns the pack dir."""
    pack_dir = root / pack
    pack_dir.mkdir(parents=True)
    if theme is not None:
        (pack_dir / "theme.yaml").write_text(theme, encoding="utf-8")
    (pack_dir / "rules.yaml").write_text(
        "confrontations:\n  - type: negotiation\n    label: Talk\n", encoding="utf-8"
    )
    return pack_dir


def _client(root: Path) -> TestClient:
    """Real FastAPI app with the production reference router, pointed at a tmp
    pack root."""
    app = FastAPI()
    app.state.genre_pack_search_paths = [str(root)]
    app.include_router(create_reference_router())
    return TestClient(app)


# ===========================================================================
# Group 1 — build_theme_tokens unit: the flat CSS-var token dict.
# ===========================================================================


def test_theme_tokens_includes_palette_vars(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    tokens = build_theme_tokens("space_opera", pack_dir=pack_dir)
    # The six palette vars are the SHARED useGenreTheme contract — non-negotiable.
    for var, value in _REQUIRED_PALETTE_VARS.items():
        assert tokens.get(var) == value, f"palette token {var} must equal theme.yaml value"


def test_theme_tokens_includes_font_vars(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    tokens = build_theme_tokens("space_opera", pack_dir=pack_dir)
    # [shape] font mapping web_font_family→--font-body, display_font_family→--font-display.
    for var, value in _REQUIRED_FONT_VARS.items():
        assert tokens.get(var) == value, f"font token {var} must map from theme.yaml"


def test_theme_tokens_includes_dinkus_vars(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    tokens = build_theme_tokens("space_opera", pack_dir=pack_dir)
    # [shape] dinkus glyph mapping. Non-ASCII; the dict comparison is exact.
    for var, value in _REQUIRED_DINKUS_VARS.items():
        assert tokens.get(var) == value, f"dinkus token {var} must map from theme.yaml glyphs"


def test_theme_tokens_keys_are_all_css_custom_properties(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    tokens = build_theme_tokens("space_opera", pack_dir=pack_dir)
    # [shape] flat dict suitable for root.style.setProperty(name, value): EVERY
    # key is a CSS custom property name. Catches a structured/nested return shape
    # and bogus internal keys (e.g. "cooldown", "session_opener") in one assertion.
    assert tokens, "token set must be non-empty"
    for key in tokens:
        assert isinstance(key, str) and key.startswith("--"), (
            f"token key {key!r} is not a CSS custom property — the set must be a flat "
            "{'--var': value} dict the injector can splat via setProperty"
        )
    for value in tokens.values():
        assert isinstance(value, str), "token values must be CSS-string primitives"


# ===========================================================================
# Group 2 — Allowlist firewall: internal non-CSS config must NOT become tokens.
#           This is the security spine for THIS story (no keeper dimension here).
#           Load-bearing. Do not weaken.
# ===========================================================================


def test_theme_tokens_omit_internal_config(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    tokens = build_theme_tokens("space_opera", pack_dir=pack_dir)
    blob = _blob(tokens)
    # border_style / dinkus.default_weight / dinkus.cooldown / session_opener /
    # the authored _note are internal knobs, not CSS vars. None may appear as a
    # token key or value.
    for sentinel in _INTERNAL_SENTINELS:
        assert sentinel not in blob, (
            f"internal config value {sentinel!r} leaked into the CSS-var token set — "
            "the projector must be a curated allowlist, not a raw theme.yaml splat"
        )
    assert "cooldown" not in tokens, "dinkus.cooldown is an internal knob, not a CSS var"
    assert "session_opener" not in tokens
    assert "--cooldown" not in tokens


def test_raw_splat_would_leak_internal_but_tokens_do_not(tmp_path: Path):
    # Non-vacuity positive control (100-6 shape, allowlist variant). A naive
    # splat WOULD surface the internal config: prove it IS in the raw theme.yaml,
    # then prove the curated token set scrubs it while keeping the real CSS vars.
    pack_dir = _seed_pack(tmp_path)
    raw_blob = _blob(yaml.safe_load((pack_dir / "theme.yaml").read_text()))
    assert _INTERNAL_BORDER_STYLE in raw_blob, (
        "sanity: the internal field IS in the raw theme.yaml, so a raw splat would leak it"
    )
    token_blob = _blob(build_theme_tokens("space_opera", pack_dir=pack_dir))
    assert _INTERNAL_BORDER_STYLE not in token_blob, (
        "the curated token set must scrub what a raw splat would leak"
    )
    # …and the curated CSS vars still cross (the set is not just emptied).
    assert _PRIMARY in token_blob
    assert _DINKUS_MEDIUM in token_blob, "non-ASCII dinkus glyph must survive verbatim"


# ===========================================================================
# Group 3 — Loud failure: a pack that can't be themed fails, never silently
#           collapses to defaults (No Silent Fallbacks; spec: malformed/absent
#           theme.yaml → 500).
# ===========================================================================


def test_theme_tokens_missing_required_field_raises(tmp_path: Path):
    # theme.yaml present but missing a required field (primary) → loud.
    bad = _THEME_YAML.replace(f"primary: '{_PRIMARY}'\n", "")
    pack_dir = _seed_pack(tmp_path, theme=bad)
    with pytest.raises(MissingThemeFieldError):
        build_theme_tokens("space_opera", pack_dir=pack_dir)


def test_theme_tokens_missing_file_raises(tmp_path: Path):
    # No theme.yaml at all → loud (a pack with no theme cannot be themed).
    pack_dir = _seed_pack(tmp_path, theme=None)
    with pytest.raises(MissingThemeFieldError):
        build_theme_tokens("space_opera", pack_dir=pack_dir)


# ===========================================================================
# Group 4 — Wiring: the token set is reachable end-to-end through BOTH
#           production reference endpoints (CLAUDE.md: every suite needs a wiring
#           test that hits a production code path, not just the unit builder).
# ===========================================================================


def test_rules_api_carries_theme_tokens(tmp_path: Path):
    _seed_pack(tmp_path)
    resp = _client(tmp_path).get("/reference/api/rules/space_opera")
    assert resp.status_code == 200
    doc = resp.json()
    assert "theme" in doc, "the rules projection must carry a top-level theme token set"
    theme = doc["theme"]
    assert theme.get("--primary") == _PRIMARY
    assert theme.get("--font-body") == _WEB_FONT
    assert theme.get("--dinkus-medium") == _DINKUS_MEDIUM


def test_lore_api_carries_theme_tokens(tmp_path: Path):
    pack_dir = _seed_pack(tmp_path)
    # Minimal world dir — empty is fine (lore sections are purely additive).
    (pack_dir / "worlds" / "perseus_cloud").mkdir(parents=True)
    resp = _client(tmp_path).get("/reference/api/lore/space_opera/perseus_cloud")
    assert resp.status_code == 200
    doc = resp.json()
    assert "theme" in doc, "the lore projection must carry the same top-level theme token set"
    assert doc["theme"].get("--primary") == _PRIMARY
    assert doc["theme"].get("--background") == _BACKGROUND


def test_rules_api_theme_has_no_internal_leak(tmp_path: Path):
    # The allowlist firewall holds across the live HTTP boundary too.
    _seed_pack(tmp_path)
    resp = _client(tmp_path).get("/reference/api/rules/space_opera")
    assert resp.status_code == 200
    blob = _blob(resp.json()["theme"])
    for sentinel in _INTERNAL_SENTINELS:
        assert sentinel not in blob, (
            f"internal config {sentinel!r} leaked through the live HTTP path"
        )


def test_rules_api_500_on_malformed_theme(tmp_path: Path):
    # Spec error-handling: a theme.yaml missing a required field surfaces as a loud
    # 500 from the endpoint, not a silently un-themed payload.
    bad = _THEME_YAML.replace(f"primary: '{_PRIMARY}'\n", "")
    _seed_pack(tmp_path, theme=bad)
    resp = _client(tmp_path).get("/reference/api/rules/space_opera")
    assert resp.status_code == 500, (
        "a pack whose theme.yaml is missing a required field must 500, never return "
        "a theme-less projection (No Silent Fallbacks)"
    )
