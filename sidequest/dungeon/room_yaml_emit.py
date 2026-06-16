"""Writes a per-region YAML at ``<world>/rooms/<room_id>.yaml``.

Story 55-1 / ADR-109 §5.2. Called by the materializer from
``_stage_emit_room_yamls`` after the ADR-096 mask emit + Plan 5
``conn.commit()``. Idempotent: existing YAMLs are not overwritten
(freeze invariant — a re-materialization of a frozen region must not
rewrite content). The production caller passes ``overwrite=False``;
tests can opt into ``overwrite=True`` to verify replacement behaviour.

**On-disk shape.** The YAML carries:

* ``room_type: settlement`` — placeholder shape so 54-2's
  ``room_file_loader.load_room_payload`` accepts the file without
  requiring sibling cavern artefacts (``cellular``/``derived``/mask
  sidecar). The procedural rooms 55-1 emits ARE caverns whose mask
  lives in the Plan 5 SQLite store (52-2/52-3) and whose ``.cavern.png``
  is rendered at runtime (52-4); the YAML's job is to carry the
  cookbook-composed prose + manifest, not duplicate the cavern visual
  pipeline. See Delivery Findings on the session for the deviation log.
* ``name: <room_id>`` — placeholder satisfying the loader's required
  field.
* ``description: <prose>`` — the top-level prose the spec §4.2
  contract calls for.
* ``entities: [LocationEntity.model_dump(mode="json"), ...]`` — the
  manifest list 54-2's loader consumes verbatim.

This shape round-trips through ``room_file_loader.load_room_payload``
without ad-hoc shape massaging by the caller — the AC-8 contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from sidequest.protocol.models import LocationEntity


def write_room_yaml(
    *,
    world_dir: Path,
    room_id: str,
    description: str,
    entities: Iterable[LocationEntity],
    overwrite: bool = False,
) -> Path:
    """Write one ``<world_dir>/rooms/<room_id>.yaml`` and return its path.

    Raises ``FileExistsError`` when the target file is already present
    and ``overwrite`` is False — this is the freeze invariant. Creates
    ``<world_dir>/rooms/`` if missing.
    """
    rooms_dir = Path(world_dir) / "rooms"
    rooms_dir.mkdir(parents=True, exist_ok=True)
    target = rooms_dir / f"{room_id}.yaml"

    if target.exists() and not overwrite:
        raise FileExistsError(
            f"write_room_yaml: {target!s} already exists and overwrite=False. "
            "Re-materialization of a frozen region must not rewrite content "
            "(freeze invariant — ADR-106 §7)."
        )

    payload: dict = {
        "room_type": "settlement",
        "name": room_id,
        "description": description,
        "entities": [e.model_dump(mode="json") for e in entities],
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False))
    return target
