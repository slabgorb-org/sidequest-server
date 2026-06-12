"""RED tests for Story 98-1 — ADR-141 two-scale spatial model, content slice C1.

Splits the monolithic ``perseus_cloud/orbits.yaml`` (one fabricated
``perseus_cloud`` star with 34 real systems hung off it as children) into
per-system files under ``perseus_cloud/systems/<id>.yaml``. This story authors
``yula`` as the first per-system orrery file and deletes the fabricated root.

These tests validate the **authored content** against the *production* parse
path — ``yaml.safe_load`` + ``OrbitsConfig.model_validate`` (the exact pair
``sidequest.orbital.loader.load_orbital_content`` runs at loader.py:50-52) —
plus the renderer's system-root resolver (``_resolve_scope_center``). That is
the wiring assertion required by CLAUDE.md "Every Test Suite Needs a Wiring
Test": the file must be consumable by the code that will load and render it,
not merely be valid YAML.

They fail RED until ``systems/yula.yaml`` is authored and the fabricated
``perseus_cloud`` primary is removed (Dev's GREEN task).

Exact source values re-homed from orbits.yaml (lines 309-315, 679-685, 834-840,
876-882, 981-987):

    yula           star    semi_major_au=8.343 period_days=8801.8 epoch_phase_deg=209.7
    yula_2         habitat parent=yula   0.45   110.3   344
    lisbon         habitat parent=yula_2 0.004  0.1     306
    mclaughlin_13  habitat parent=yula_2 0.008  0.3     66
    thule_7        habitat parent=yula_2 0.013  0.5     186
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sidequest.orbital.loader import load_orbital_content
from sidequest.orbital.models import BodyType, OrbitsConfig
from sidequest.orbital.render import Scope, _resolve_scope_center

# --- exact source values, the single source of truth for these tests ---------

EXPECTED_BODIES: dict[str, dict[str, object]] = {
    "yula": {
        "type": "star",
        "parent": None,
        "semi_major_au": 8.343,
        "period_days": 8801.8,
        "epoch_phase_deg": 209.7,
        "label": "YULA",
    },
    "yula_2": {
        "type": "habitat",
        "parent": "yula",
        "semi_major_au": 0.45,
        "period_days": 110.3,
        "epoch_phase_deg": 344,
        "label": "YULA",
    },
    "lisbon": {
        "type": "habitat",
        "parent": "yula_2",
        "semi_major_au": 0.004,
        "period_days": 0.1,
        "epoch_phase_deg": 306,
        "label": "LISBON",
    },
    "mclaughlin_13": {
        "type": "habitat",
        "parent": "yula_2",
        "semi_major_au": 0.008,
        "period_days": 0.3,
        "epoch_phase_deg": 66,
        "label": "MCLAUGHLIN 13",
    },
    "thule_7": {
        "type": "habitat",
        "parent": "yula_2",
        "semi_major_au": 0.013,
        "period_days": 0.5,
        "epoch_phase_deg": 186,
        "label": "THULE 7",
    },
}


# --- path helpers ------------------------------------------------------------


def _perseus_cloud_dir() -> Path:
    server_root = Path(__file__).resolve().parents[2]  # sidequest-server/
    repo_root = server_root.parent
    return (
        repo_root / "sidequest-content" / "genre_packs" / "space_opera" / "worlds" / "perseus_cloud"
    )


def _systems_dir() -> Path:
    return _perseus_cloud_dir() / "systems"


def _yula_path() -> Path:
    return _systems_dir() / "yula.yaml"


@pytest.fixture(scope="module")
def world_dir() -> Path:
    path = _perseus_cloud_dir()
    if not path.exists():
        pytest.skip(f"perseus_cloud world not present at {path} (content repo not checked out)")
    return path


@pytest.fixture
def yula_raw(world_dir: Path) -> dict:
    """Raw parsed YAML of systems/yula.yaml — fails RED if the file is absent."""
    path = _yula_path()
    assert path.exists(), (
        f"systems/yula.yaml not authored yet at {path} — "
        "Dev must create the per-system file (Story 98-1 GREEN)"
    )
    with path.open() as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"yula.yaml did not parse to a mapping: {type(data)!r}"
    return data


@pytest.fixture
def yula_orbits(yula_raw: dict) -> OrbitsConfig:
    """yula.yaml validated through the production OrbitsConfig model.

    This is the loader's parse path (loader.py:52). If the file references a
    missing parent (e.g. ``parent: perseus_cloud`` after the root is deleted),
    ``_validate_parent_refs`` raises here — so a clean parse is itself an AC2
    guarantee.
    """
    return OrbitsConfig.model_validate(yula_raw)


# --- AC1: yula.yaml created with correct body hierarchy ----------------------


def test_yula_system_file_exists(world_dir: Path) -> None:
    assert _yula_path().exists(), (
        f"expected per-system file at {_yula_path()}; "
        "Story 98-1 authors yula as the first systems/<id>.yaml"
    )


def test_yula_validates_as_production_orbits_config(yula_orbits: OrbitsConfig) -> None:
    # Reaching here means yaml.safe_load + OrbitsConfig.model_validate both
    # succeeded — extra="forbid" rejects stray keys, parent-ref validation
    # rejects dangling parents. A clean parse is a meaningful assertion.
    assert isinstance(yula_orbits, OrbitsConfig)
    assert yula_orbits.version, "yula.yaml must declare a version"


def test_yula_has_exactly_five_bodies(yula_orbits: OrbitsConfig) -> None:
    assert set(yula_orbits.bodies.keys()) == set(EXPECTED_BODIES.keys()), (
        f"expected exactly the yula subtree {sorted(EXPECTED_BODIES)}, "
        f"got {sorted(yula_orbits.bodies)}"
    )


def test_yula_is_the_sole_parentless_root(yula_orbits: OrbitsConfig) -> None:
    roots = [bid for bid, b in yula_orbits.bodies.items() if b.parent is None]
    assert roots == ["yula"], (
        f"a per-system file must have exactly one parent-less root; got {roots!r}"
    )


def test_yula_root_is_a_star(yula_orbits: OrbitsConfig) -> None:
    assert yula_orbits.bodies["yula"].type == BodyType.STAR


@pytest.mark.parametrize(
    ("body_id", "expected_parent"),
    [
        ("yula", None),
        ("yula_2", "yula"),
        ("lisbon", "yula_2"),
        ("mclaughlin_13", "yula_2"),
        ("thule_7", "yula_2"),
    ],
)
def test_yula_parent_linkages(
    yula_orbits: OrbitsConfig, body_id: str, expected_parent: str | None
) -> None:
    assert body_id in yula_orbits.bodies, f"missing body {body_id!r}"
    assert yula_orbits.bodies[body_id].parent == expected_parent, (
        f"{body_id!r} should parent to {expected_parent!r}, "
        f"got {yula_orbits.bodies[body_id].parent!r}"
    )


@pytest.mark.parametrize("body_id", list(EXPECTED_BODIES.keys()))
def test_yula_orbital_mechanics_exact(yula_orbits: OrbitsConfig, body_id: str) -> None:
    """Orbital + label fields must be copied verbatim from the source monolith.

    Paranoid against transcription drift (8801.8 fat-fingered to 8801.0 would
    silently shift the body's Kepler period and ADR-130 clock position).
    """
    body = yula_orbits.bodies[body_id]
    expected = EXPECTED_BODIES[body_id]
    assert body.type.value == expected["type"]
    assert body.semi_major_au == expected["semi_major_au"]
    assert body.period_days == expected["period_days"]
    assert body.epoch_phase_deg == expected["epoch_phase_deg"]
    assert body.label == expected["label"]


# --- AC2: fabricated perseus_cloud primary deleted --------------------------


def test_yula_has_no_perseus_cloud_body(yula_orbits: OrbitsConfig) -> None:
    assert "perseus_cloud" not in yula_orbits.bodies, (
        "the fabricated 'perseus_cloud' primary must not appear in a per-system file"
    )


def test_no_body_in_yula_parents_to_perseus_cloud(yula_orbits: OrbitsConfig) -> None:
    offenders = [bid for bid, b in yula_orbits.bodies.items() if b.parent == "perseus_cloud"]
    assert offenders == [], f"bodies still parented to deleted root perseus_cloud: {offenders!r}"


def test_perseus_cloud_world_has_no_parent_perseus_cloud_anywhere(world_dir: Path) -> None:
    """AC2 grep check: zero ``parent: perseus_cloud`` across the world's YAML.

    Robust to the AC4 delete-vs-stub decision: whether orbits.yaml is removed or
    stubbed, and across every authored systems/<id>.yaml, no body may still hang
    off the deleted fabricated root.
    """
    offenders: list[str] = []
    for yaml_path in world_dir.rglob("*.yaml"):
        text = yaml_path.read_text()
        if "parent: perseus_cloud" in text:
            offenders.append(str(yaml_path.relative_to(world_dir)))
    assert offenders == [], (
        f"files still reference the deleted fabricated root via "
        f"'parent: perseus_cloud': {offenders}"
    )


# --- AC3: calendar linkage preserved (ADR-130) ------------------------------


def test_yula_clock_epoch_days_preserved(yula_orbits: OrbitsConfig) -> None:
    assert yula_orbits.clock.epoch_days == 0.0, (
        "global campaign clock (epoch_days) must be carried into the per-system file"
    )


@pytest.mark.parametrize("body_id", ["yula_2", "lisbon", "mclaughlin_13", "thule_7"])
def test_orbiting_bodies_carry_calendar_fields(yula_orbits: OrbitsConfig, body_id: str) -> None:
    """Every orbiting (non-root) body needs period_days + epoch_phase_deg for
    ADR-130 clock math. The model enforces this on validate; assert explicitly
    so the AC is pinned even if the model's invariant ever loosens."""
    body = yula_orbits.bodies[body_id]
    assert body.period_days is not None, f"{body_id!r} missing period_days"
    assert body.epoch_phase_deg is not None, f"{body_id!r} missing epoch_phase_deg"


# --- AC4: monolithic orbits.yaml disposal (preferred: deleted) --------------


def test_monolithic_orbits_yaml_no_longer_holds_fabricated_cluster(world_dir: Path) -> None:
    """AC4 preferred outcome: the monolithic orbits.yaml is deleted. Tolerates
    Option B (retirement stub) as long as the fabricated 34-system cluster is
    gone — i.e. if orbits.yaml survives, it must not still define a parent-less
    'perseus_cloud' star with the real systems hung off it."""
    orbits_path = world_dir / "orbits.yaml"
    if not orbits_path.exists():
        return  # preferred path — file removed entirely
    raw = yaml.safe_load(orbits_path.read_text()) or {}
    bodies = raw.get("bodies", {}) if isinstance(raw, dict) else {}
    assert "perseus_cloud" not in bodies, (
        "orbits.yaml still defines the fabricated 'perseus_cloud' root cluster; "
        "delete the file (preferred) or strip the fake root to a stub"
    )


# --- AC5: other systems remain unwritten (no stubbing) ----------------------


def test_only_yula_system_is_authored(world_dir: Path) -> None:
    """Diamonds and Coal (ADR-014) + No Stubbing: this story authors yula only.
    No skeleton files for akkad/amanta/etc. — an unscripted system is a valid
    (unauthored) jump destination, not an empty shell."""
    systems = _systems_dir()
    assert systems.exists(), f"expected systems/ directory at {systems}"
    authored = sorted(p.name for p in systems.glob("*.yaml"))
    assert authored == ["yula.yaml"], (
        f"only yula.yaml should be authored this story; found {authored}"
    )


# --- Wiring: the file is consumable by the production loader + renderer ------


def test_yula_consumable_by_production_loader(yula_raw: dict, tmp_path: Path) -> None:
    """End-to-end wiring against the real loader.

    ``load_orbital_content`` reads ``<dir>/orbits.yaml`` (per-system resolution
    is S1/98-2's job, not yet landed). Stage the authored file as that input and
    prove the production loader+model pair consumes it: 5 bodies, clean parse,
    no dangling parents."""
    staged = tmp_path / "orbits.yaml"
    staged.write_text(yaml.safe_dump(yula_raw))
    content = load_orbital_content(tmp_path)
    assert set(content.orbits.bodies.keys()) == set(EXPECTED_BODIES.keys())


def test_renderer_root_resolver_picks_yula(yula_orbits: OrbitsConfig) -> None:
    """The renderer's system-root resolver (_resolve_scope_center) requires
    exactly one parent-less body and returns it as the chart center. After the
    split the root must resolve to 'yula', not the deleted 'perseus_cloud'."""
    center = _resolve_scope_center(yula_orbits, Scope.system_root())
    assert center == "yula", f"system-root scope should center on 'yula', got {center!r}"
