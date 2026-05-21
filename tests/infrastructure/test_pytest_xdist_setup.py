"""RED-phase tests for Story 50-27: pytest-xdist parallel-suite wiring.

These tests assert that the unit suite is configured to run in parallel by
default. They cover three wiring surfaces — the server's own pyproject.toml,
the orchestrator's justfile recipe, and the orchestrator's pf-check repo
config — plus a regression guard for the existing tmp_save_dir isolation
contract.

They fail today (pytest-xdist is not installed, `-n` flag is not in addopts
or any recipe) and should turn green once Dev wires the dep + `-n auto`
through the canonical addopts surface.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # sidequest-server/
ORCHESTRATOR_ROOT = REPO_ROOT.parent  # oq-1/

PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
JUSTFILE_PATH = ORCHESTRATOR_ROOT / "justfile"
REPOS_YAML_PATH = ORCHESTRATOR_ROOT / ".pennyfarthing" / "repos.yaml"

# Matches `-n auto`, `-n2`, `-n 4`, or `--numprocesses=auto`/`--numprocesses 4`.
# Used to assert that an invocation surface engages pytest-xdist parallel mode.
_PARALLEL_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-n\s*(?:auto|\d+)|--numprocesses[\s=](?:auto|\d+))(?:\s|$)"
)


def _load_pyproject() -> dict:
    with PYPROJECT_PATH.open("rb") as f:
        return tomllib.load(f)


def _pytest_addopts() -> str:
    config = _load_pyproject()
    return config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")


# --- AC-1 / wiring: dependency surface ----------------------------------


def test_pytest_xdist_in_dev_dependencies() -> None:
    """pytest-xdist must be declared as a dev dependency in pyproject.toml.

    Without this, `uv sync` won't install xdist and the rest of the parallel
    wiring is dead.
    """
    config = _load_pyproject()
    dev_deps = config.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    has_xdist = any(re.match(r"^pytest-xdist(\s|>=|==|<|>|;|$)", dep) for dep in dev_deps)
    assert has_xdist, (
        f"pytest-xdist not found in [project.optional-dependencies].dev. "
        f"Current dev deps: {dev_deps}"
    )


def test_pytest_xdist_module_importable() -> None:
    """The xdist plugin module must actually be importable.

    Uses find_spec rather than `import xdist` so this test file still collects
    when xdist isn't installed (otherwise an ImportError at module-load time
    would mask every test in this file).
    """
    spec = importlib.util.find_spec("xdist")
    assert spec is not None, (
        "pytest-xdist is declared but not installed — run `uv sync` in "
        "sidequest-server/ to install dev dependencies."
    )


# --- AC-3: parallel-by-default surfaces ---------------------------------


def test_pytest_addopts_engages_parallel_mode() -> None:
    """pyproject.toml [tool.pytest.ini_options].addopts must include `-n`.

    `-n auto` (or `-n N` with N>=2) is the canonical way to make `pytest` —
    and therefore `uv run pytest`, `just server-test`, and `pf check` — fan
    out across workers without each recipe needing to re-specify the flag.
    """
    addopts = _pytest_addopts()
    assert addopts, "pyproject.toml [tool.pytest.ini_options].addopts is empty"

    assert _PARALLEL_FLAG_RE.search(addopts), (
        f"addopts does not engage pytest-xdist parallel mode. "
        f"Expected `-n auto` (or numeric/--numprocesses variant) in addopts. "
        f"Current addopts: {addopts!r}"
    )


def test_orchestrator_justfile_server_test_recipe_uses_parallel() -> None:
    """The `server-test` recipe in the orchestrator justfile must run pytest
    in a way that engages xdist.

    Acceptable wirings:
      (a) recipe contains an explicit `-n` flag, OR
      (b) recipe is a bare `pytest`/`uv run pytest` call that picks up
          `-n auto` from pyproject's addopts.

    Either way, the effective invocation must produce a parallel run.
    """
    assert JUSTFILE_PATH.exists(), f"justfile not found at {JUSTFILE_PATH}"
    lines = JUSTFILE_PATH.read_text(encoding="utf-8").splitlines()

    # Locate the `server-test:` recipe header.
    recipe_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^server-test\s*:", line):
            recipe_idx = i
            break
    assert recipe_idx is not None, (
        "justfile has no `server-test:` recipe — AC-3 wiring surface is missing"
    )

    # Collect recipe body (indented lines after the header until next recipe
    # or blank-then-unindented).
    body: list[str] = []
    for raw in lines[recipe_idx + 1 :]:
        if raw.startswith((" ", "\t")):
            body.append(raw.strip())
        elif raw.strip() == "":
            # Blank line ends the recipe.
            break
        else:
            # Next recipe header.
            break
    recipe_text = " ".join(body)
    assert "pytest" in recipe_text, (
        f"server-test recipe does not invoke pytest. Recipe: {recipe_text!r}"
    )

    addopts = _pytest_addopts()
    assert _PARALLEL_FLAG_RE.search(recipe_text) or _PARALLEL_FLAG_RE.search(addopts), (
        f"server-test recipe relies on addopts for `-n` but addopts is "
        f"missing it too. Recipe: {recipe_text!r}, addopts: {addopts!r}"
    )


def test_pf_check_server_invokes_parallel_unit_suite() -> None:
    """`pf check` for sidequest-server must run pytest in parallel.

    `pf check` resolves the test command in this order (see
    `.pennyfarthing/scripts/workflow/check.py:run_tests`):
      1. `repos.yaml` `server.test_command` (highest)
      2. `just test` recipe (not present for server)
      3. language-default
    The repos.yaml `test_command` is `pytest` (bare). For parallel mode to
    be the default, either:
      (a) `repos.yaml` server.test_command contains `-n`, OR
      (b) pyproject's addopts contains `-n` (inherited by every pytest call).
    """
    assert REPOS_YAML_PATH.exists(), f"repos.yaml not found at {REPOS_YAML_PATH}"
    with REPOS_YAML_PATH.open("r", encoding="utf-8") as f:
        repos = yaml.safe_load(f)
    server_cmd = repos.get("repos", {}).get("server", {}).get("test_command", "")

    addopts = _pytest_addopts()
    assert _PARALLEL_FLAG_RE.search(server_cmd) or _PARALLEL_FLAG_RE.search(addopts), (
        f"pf check for server would not engage parallel mode. "
        f"repos.yaml server.test_command={server_cmd!r}, "
        f"pyproject addopts={addopts!r}. One must contain `-n auto`."
    )


# --- AC-4 regression guard (passes today, fails if isolation breaks) ---


def test_tmp_save_dir_fixture_isolated_from_real_home(
    tmp_save_dir: Path,
) -> None:
    """The `tmp_save_dir` fixture must NOT return a path under ~/.sidequest.

    Documents the existing isolation contract that protects real player saves
    when the suite runs in parallel under xdist (each worker is a separate
    process with its own pytest tmp_path tree). A future refactor that
    points this fixture at Path.home() would clobber real saves and silently
    cross-contaminate workers — this test catches that.
    """
    real_save_root = (Path.home() / ".sidequest").resolve()
    fixture_path = tmp_save_dir.resolve()

    assert fixture_path.exists() and fixture_path.is_dir(), (
        f"tmp_save_dir should return an existing directory, got {fixture_path}"
    )
    assert real_save_root not in fixture_path.parents, (
        f"tmp_save_dir resolved to {fixture_path}, which is inside the real "
        f"~/.sidequest save root ({real_save_root}). Parallel workers would "
        f"corrupt real player saves."
    )


# --- Wiring smoke: ensure this test file is discoverable -----------------


def test_infrastructure_suite_collects() -> None:
    """Trivial collection smoke. The TEA red phase needs the new
    `tests/infrastructure/` package to be wired up — if pytest can't even
    collect this file, the rest of the suite is meaningless.
    """
    # This test runs, therefore the package collected. No mock — the act of
    # passing IS the assertion.
    assert __package__ == "tests.infrastructure", (
        f"Expected this file to be collected as tests.infrastructure, "
        f"got {__package__}. Check tests/infrastructure/__init__.py exists."
    )
