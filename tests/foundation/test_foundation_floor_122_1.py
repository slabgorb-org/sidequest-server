"""Story 122-1 (RED) — Foundation floor: relocate asset_urls / slug_fold /
reference_anchors below the server tier (ADR-147 step 1).

The test IS the spec. ADR-147 establishes one layering law:

    Imports flow downward only:
        foundation <- {game, genre, orbital, magic, interior} <- server
    Domain and utility code MUST NOT import from ``sidequest.server``; the
    foundation floor imports only third-party libs, ``protocol/``, and the
    foundation-level watcher API (``sidequest.telemetry``, ADR-132).

Step 1 establishes a new ``sidequest/foundation/`` package and moves three pure
helpers into it, erasing the upward edges where ``game/`` and ``genre/`` reach
up into ``server/`` to call them:

    server.asset_urls       (resolve_asset_url, resolve_player_portrait_url,
                             rewrite_theme_css_asset_urls)
    server.slug_fold        (fold_to_ascii)
    server.reference_anchors (build_rules_url, build_lore_url,
                             reference_url_for_{class,ability,journal_entry,
                             location_entity,region})

This is a *relocation*, not a copy: the old ``sidequest.server.<module>`` paths
MUST cease to exist (No Silent Fallbacks / No Stubbing — no compatibility shim
left behind). Behaviour is preserved byte-for-byte; only import paths move.

ENFORCED INVARIANTS (each fails crisply until Dev lands the move):

  A. ``sidequest.foundation`` exists and re-exposes the three modules' full
     public API at the new path.
  B. The old ``sidequest.server.{asset_urls,slug_fold,reference_anchors}``
     module paths are GONE (relocation, no shim).
  C. Behaviour preserved — the relocated pure functions still produce their
     known outputs (this also proves ``reference_anchors``'s transitive
     ``slugify`` dependency travelled to the floor with it).
  D. No module under game/genre/orbital/magic/interior imports the three
     relocated modules from ``sidequest.server`` (the killed upward edges).
  E. Foundation-floor purity — nothing under ``sidequest/foundation/`` imports
     ``sidequest.server`` at all (this is what drags the pure ``reference_slug``
     helper down with ``reference_anchors``; leaving it in ``server/`` would
     mint a brand-new foundation->server edge).

The AST import-direction scan here is the layer guard ADR-147 §Enforcement
prescribes ("a ~20-line AST/grep test in tests/ is sufficient"); it is the
wiring test for layer direction (project rule: every suite needs a wiring test).
Story 122-5 generalises it to a full no-upward-import CI guard for all five
domain packages; this file is scoped to the three modules 122-1 relocates.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

# --- Package geography -------------------------------------------------------
import sidequest  # noqa: E402  (path discovery needs the import side effect)

SIDEQUEST_PKG: Path = Path(sidequest.__file__).resolve().parent
FOUNDATION_DIR: Path = SIDEQUEST_PKG / "foundation"

# Domain tiers that the layering law forbids from importing upward.
DOMAIN_TIERS: tuple[str, ...] = ("game", "genre", "orbital", "magic", "interior")

# The three modules Story 122-1 relocates to the foundation floor.
RELOCATING_MODULES: frozenset[str] = frozenset({"asset_urls", "slug_fold", "reference_anchors"})


# --- AST import scanning -----------------------------------------------------


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _import_targets(tree: ast.AST) -> list[str]:
    """Every dotted module path this AST imports, module-level OR nested/lazy.

    ``ast.walk`` recurses into function bodies, so the lazy in-method imports
    ADR-147 calls out are caught alongside top-level ones.
    """
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # ``from a.b import c`` contributes a.b plus a.b.c for each name,
            # so both "from sidequest.server.asset_urls import x" and
            # "from sidequest.server import asset_urls" are detectable.
            targets.append(mod)
            for alias in node.names:
                targets.append(f"{mod}.{alias.name}" if mod else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
    return targets


def _imports_relocated_from_server(targets: list[str]) -> list[str]:
    """Targets that pull one of the three relocating modules out of server."""
    hits: list[str] = []
    for t in targets:
        if not t.startswith("sidequest.server"):
            continue
        parts = t.split(".")
        # parts[:2] == ["sidequest", "server"]; the module name follows.
        if len(parts) >= 3 and parts[2] in RELOCATING_MODULES:
            hits.append(t)
    return hits


def _imports_any_server(targets: list[str]) -> list[str]:
    return [t for t in targets if t == "sidequest.server" or t.startswith("sidequest.server.")]


def _domain_violations() -> dict[str, list[str]]:
    """Map {file: [offending import targets]} for domain-tier upward edges."""
    violations: dict[str, list[str]] = {}
    for tier in DOMAIN_TIERS:
        tier_dir = SIDEQUEST_PKG / tier
        if not tier_dir.is_dir():
            continue
        for path in _iter_py_files(tier_dir):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            hits = _imports_relocated_from_server(_import_targets(tree))
            if hits:
                violations[str(path.relative_to(SIDEQUEST_PKG))] = sorted(set(hits))
    return violations


# ---------------------------------------------------------------------------
# A. Foundation package exists and re-exposes the relocated public API
# ---------------------------------------------------------------------------


def test_foundation_package_exists() -> None:
    assert (FOUNDATION_DIR / "__init__.py").is_file(), (
        "ADR-147 step 1: sidequest/foundation/ package must exist"
    )
    assert importlib.util.find_spec("sidequest.foundation") is not None


def test_foundation_exposes_asset_urls_api() -> None:
    from sidequest.foundation.asset_urls import (  # noqa: F401
        resolve_asset_url,
        resolve_player_portrait_url,
        rewrite_theme_css_asset_urls,
    )

    assert callable(resolve_asset_url)
    assert callable(resolve_player_portrait_url)
    assert callable(rewrite_theme_css_asset_urls)


def test_foundation_exposes_slug_fold_api() -> None:
    from sidequest.foundation.slug_fold import fold_to_ascii

    assert callable(fold_to_ascii)


def test_foundation_exposes_reference_anchors_api() -> None:
    from sidequest.foundation.reference_anchors import (  # noqa: F401
        build_lore_url,
        build_rules_url,
        reference_url_for_ability,
        reference_url_for_class,
        reference_url_for_journal_entry,
        reference_url_for_location_entity,
        reference_url_for_region,
    )

    for fn in (
        build_lore_url,
        build_rules_url,
        reference_url_for_ability,
        reference_url_for_class,
        reference_url_for_journal_entry,
        reference_url_for_location_entity,
        reference_url_for_region,
    ):
        assert callable(fn)


# ---------------------------------------------------------------------------
# B. Old server-tier module paths are GONE (relocation, not copy — no shim)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", sorted(RELOCATING_MODULES))
def test_old_server_module_path_removed(module: str) -> None:
    old_path = f"sidequest.server.{module}"
    try:
        spec = importlib.util.find_spec(old_path)
    except ModuleNotFoundError:
        spec = None  # parent stopped re-exporting it — also acceptable
    assert spec is None, (
        f"{old_path} still resolves — Story 122-1 relocates it to "
        f"sidequest.foundation.{module} and must leave no compatibility shim "
        "(No Silent Fallbacks / No Stubbing)."
    )


def test_old_server_module_file_removed() -> None:
    leftover = [m for m in RELOCATING_MODULES if (SIDEQUEST_PKG / "server" / f"{m}.py").exists()]
    assert not leftover, f"server/ still holds relocated module files: {leftover}"


# ---------------------------------------------------------------------------
# C. Behaviour preserved at the new path (pure functions, known outputs)
# ---------------------------------------------------------------------------


def test_asset_url_behaviour_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    from sidequest.foundation.asset_urls import resolve_asset_url

    monkeypatch.delenv("SIDEQUEST_ASSET_BASE_URL", raising=False)
    assert (
        resolve_asset_url("genre_packs/caverns_and_claudes/audio/music/combat.ogg")
        == "https://cdn.slabgorb.com/genre_packs/caverns_and_claudes/audio/music/combat.ogg"
    )

    monkeypatch.setenv("SIDEQUEST_ASSET_BASE_URL", "local")
    assert (
        resolve_asset_url("genre_packs/caverns_and_claudes/audio/music/combat.ogg")
        == "/genre/caverns_and_claudes/audio/music/combat.ogg"
    )
    with pytest.raises(ValueError, match="unknown asset prefix"):
        resolve_asset_url("randomthing/foo.ogg")


def test_fold_to_ascii_behaviour_preserved() -> None:
    from sidequest.foundation.slug_fold import fold_to_ascii

    # NFKD decomposition strips the combining mark but keeps the base letter
    # AND the original case (the helper does not lowercase).
    assert fold_to_ascii("Évropi") == "Evropi"
    assert fold_to_ascii("café") == "cafe"
    # Behavioural fingerprint of the *real* NFKD fold vs a naive
    # str.encode("ascii", "ignore"): the accented vowel folds to "e", it is
    # NOT dropped (which would yield "caf").
    assert fold_to_ascii("café") != "caf"


def test_reference_anchor_behaviour_preserved() -> None:
    from sidequest.foundation.reference_anchors import (
        build_lore_url,
        build_rules_url,
        reference_url_for_ability,
    )

    # Slug fragments are lowercased + hyphenated — this exercises the
    # transitive reference_slug.slugify dependency, proving it travelled to
    # the floor too (else this import/relocation breaks).
    assert (
        build_lore_url("p", "w", "legend", "The Sunken King")
        == "/reference/lore/p/w#legend-the-sunken-king"
    )
    assert (
        reference_url_for_ability(
            pack="p", source="Class", ability_name="Cosh", owning_class_name="Burglar"
        )
        == "/reference/rules/p#class-burglar-signature-cosh"
    )
    # Non-class sources do not mint a URL.
    assert (
        reference_url_for_ability(
            pack="p", source="Race", ability_name="Darkvision", owning_class_name=None
        )
        is None
    )
    with pytest.raises(ValueError, match="at least one key segment"):
        build_rules_url("p", "class")


# ---------------------------------------------------------------------------
# D. The killed upward edges — no domain-tier import of the three from server
# ---------------------------------------------------------------------------


def test_no_domain_tier_upward_edges_for_relocated_modules() -> None:
    violations = _domain_violations()
    assert violations == {}, (
        "Domain tiers must import the relocated helpers from "
        "sidequest.foundation, not sidequest.server. Remaining upward edges:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in sorted(violations.items()))
    )


@pytest.mark.parametrize(
    ("importer", "relocated_module"),
    [
        ("game/room_file_loader.py", "asset_urls"),
        ("genre/audio_paths.py", "asset_urls"),
        ("game/alias_resolution.py", "slug_fold"),
        ("game/builder.py", "reference_anchors"),
        ("game/cookbook/compose.py", "reference_anchors"),
    ],
)
def test_known_importer_no_longer_reaches_into_server(importer: str, relocated_module: str) -> None:
    """The five concrete edges ADR-147 names (its own table lists five importer
    files though its Decision text says "four" — the test pins the real set)."""
    path = SIDEQUEST_PKG / importer
    assert path.exists(), f"expected importer {importer} to exist"
    targets = _import_targets(ast.parse(path.read_text(encoding="utf-8")))
    offending = [
        t for t in _imports_relocated_from_server(targets) if t.split(".")[2] == relocated_module
    ]
    assert not offending, (
        f"{importer} still imports {relocated_module} from sidequest.server "
        f"({offending}); it must import from sidequest.foundation.{relocated_module}."
    )


# ---------------------------------------------------------------------------
# E. Foundation-floor purity — nothing in foundation/ imports server
# ---------------------------------------------------------------------------


def test_foundation_floor_imports_nothing_from_server() -> None:
    # Precondition: the floor must exist, or this scan would vacuously pass.
    assert FOUNDATION_DIR.is_dir(), (
        "sidequest/foundation/ does not exist yet — Story 122-1 has not landed."
    )
    impure: dict[str, list[str]] = {}
    for path in _iter_py_files(FOUNDATION_DIR):
        targets = _import_targets(ast.parse(path.read_text(encoding="utf-8")))
        hits = _imports_any_server(targets)
        if hits:
            impure[str(path.relative_to(SIDEQUEST_PKG))] = sorted(set(hits))
    assert impure == {}, (
        "foundation/ must not import sidequest.server (it may import third-party,"
        " protocol/, and the foundation-level telemetry watcher only). "
        "reference_anchors' slugify dependency (reference_slug) must be relocated"
        " to the floor too. Offenders:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in sorted(impure.items()))
    )
