"""Single-vs-cluster detection for space-opera worlds (Story 104-1 / M-A).

Spec: docs/superpowers/specs/2026-06-11-space-opera-map-playtest-addendum.md §5,
as amended live by the operator (2026-06-11): the single-vs-cluster signal is a
**system COUNT**, not the mere presence of a ``systems/`` dir. Under the unified
model every space-opera world declares its systems; a world with one system
collapses to its single orrery, a world with more than one is a cluster.

    is_cluster := (system_count > 1)

Count precedence (No Silent Fallbacks — the decision is always explicit and
observable on the GM panel):

  1. a sector graph (``*.sector.json`` with a ``system`` node dict) → count its
     system nodes. This is perseus_cloud's authoritative source: its ``systems/``
     dir is intentionally sparse (only ``yula.yaml`` authored; the rest are
     Diamonds-and-Coal, regenerable from the sector graph), so counting
     ``systems/*.yaml`` files would falsely read perseus as single. The sector
     graph wins when present.
  2. else a ``systems/`` dir → count its ``*.yaml`` entries. The one-entry file
     coyote_star / aureate_span carry is count == 1 → single; add a second entry
     and the same code flips the world to a cluster with no code change.
  3. else (no sector graph, no ``systems/`` dir) → a definite single system
     (count 1). This is an explicit classification, NOT a guess / "unknown".

This module lives in ``genre`` (the lower layer) so BOTH callers can reach it:
the genre loader (caches ``World.is_cluster`` for the in-game MAP_UPDATE path)
and the session-free reference projection in ``server`` (detects on disk via the
world dir it already holds).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sidequest.telemetry.spans import cluster_detected_span

logger = logging.getLogger(__name__)

# Signal-source labels recorded on the decision span so the GM panel can see
# which input drove the count.
_SIGNAL_SECTOR_GRAPH = "sector_graph"
_SIGNAL_SYSTEMS_DIR = "systems_dir"
_SIGNAL_NONE = "none"


def _sector_graph_system_count(world_dir: Path) -> int | None:
    """Count ``system`` nodes in a ``*.sector.json`` sector graph, or None.

    Returns None when no sector graph is present (the caller falls through to
    the next signal). A present-but-malformed sector graph fails loud rather
    than silently reporting zero — a corrupt graph is a content bug, not a
    single-system world.
    """
    sector_files = sorted(world_dir.glob("*.sector.json"))
    if not sector_files:
        return None
    # A world authors one sector graph; if several exist, the first by name is
    # the canonical one (deterministic, not arbitrary).
    sector_path = sector_files[0]
    data = json.loads(sector_path.read_text(encoding="utf-8"))
    systems = data.get("system")
    if not isinstance(systems, dict):
        raise ValueError(
            f"sector graph {sector_path} has no 'system' node dict "
            f"(got {type(systems).__name__}); cannot derive system count"
        )
    return len(systems)


def _systems_dir_entry_count(world_dir: Path) -> int | None:
    """Count ``*.yaml`` entries in the world's ``systems/`` dir, or None.

    Returns None when there is no ``systems/`` dir (the caller falls through to
    the definite-single classification).
    """
    systems_dir = world_dir / "systems"
    if not systems_dir.is_dir():
        return None
    return len(list(systems_dir.glob("*.yaml")))


def detect_system_count(world_dir: Path) -> tuple[int, str]:
    """Resolve ``(system_count, signal_source)`` for a world (no span).

    Pure detection split out from :func:`detect_is_cluster` so the loader and
    the reference projection share one definition of the count precedence.
    """
    sector_count = _sector_graph_system_count(world_dir)
    if sector_count is not None:
        return sector_count, _SIGNAL_SECTOR_GRAPH

    systems_count = _systems_dir_entry_count(world_dir)
    if systems_count is not None:
        return systems_count, _SIGNAL_SYSTEMS_DIR

    # No sector graph and no systems/ dir: a definite single-system world
    # (count 1), never an "unknown". (spec AC4)
    return 1, _SIGNAL_NONE


def detect_is_cluster(world_dir: Path) -> bool:
    """Return whether ``world_dir`` is a multi-system cluster (count > 1).

    Emits the ``sidequest.cartography.cluster_detected`` decision span so the GM
    panel can verify the flag (spec AC2) on both the cluster and single cases.
    """
    system_count, signal_source = detect_system_count(world_dir)
    is_cluster = system_count > 1
    with cluster_detected_span(
        world=world_dir.name,
        signal_source=signal_source,
        system_count=system_count,
        is_cluster=is_cluster,
    ):
        pass
    logger.info(
        "cluster_detected world=%s signal=%s system_count=%d is_cluster=%s",
        world_dir.name,
        signal_source,
        system_count,
        is_cluster,
    )
    return is_cluster
