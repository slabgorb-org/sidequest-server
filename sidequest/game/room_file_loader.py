"""Load per-room YAML files (ADR-096) → TacticalGridPayload.

Worlds use `<world_dir>/rooms/<room_id>.yaml` plus sibling
`.cavern.png` and `.mask.txt` artifacts emitted by the cavern_renderer
authoring tool.

Story 52-4 adds the runtime side: `emit_runtime_cavern_png` takes a
persisted mask BLOB (the dict ``RegionMask.to_dict()`` produces and
``DungeonStore.load_masks`` returns) and writes an ADR-096-shaped
``.cavern.png`` sidecar at a caller-supplied path. The persisted mask
*is* the truth (ADR-096 §2); the PNG is a derived view the UI renders.
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageDraw

from sidequest.protocol.models import (
    CellularParams,
    DerivedRoomData,
    LocationEntity,
    TacticalGridPayload,
)
from sidequest.server.asset_urls import resolve_asset_url
from sidequest.telemetry.spans.cavern_room import cavern_room_load_span
from sidequest.telemetry.spans.dungeon_render import cavern_mask_to_png_span

logger = logging.getLogger(__name__)

# ADR-096 mask alphabet: '#' is wall, '.' is floor, '\n' separates rows.
_MASK_WALL = ord("#")
_MASK_FLOOR = ord(".")
_MASK_NEWLINE = ord("\n")

# Visual palette for the runtime renderer. Kept intentionally minimal vs
# the static cavern_renderer tool — full visual parity is not in scope
# for 52-4. Pixel dimensions and the wall/floor alphabet are
# load-bearing (UI cell-stepped math); fancy stippling and grain are not.
_FLOOR_RGB = (58, 58, 74)  # #3a3a4a (matches cavern_renderer.render._FLOOR_BASE)
_WALL_RGB = (14, 14, 24)  # #0e0e18 (matches cavern_renderer.render._WALL_BASE)


class RoomNotFoundError(Exception):
    """Raised when no <room_id>.yaml exists in the world's rooms/ dir."""


def load_room_payload(
    world_dir: Path,
    room_id: str,
    genre_slug: str = "caverns_and_claudes",
) -> TacticalGridPayload:
    """Load a room's metadata + mask, return a TacticalGridPayload.

    Tokens and initiative are not populated here — the dispatch layer
    fills them from game state.
    """
    yaml_path = world_dir / "rooms" / f"{room_id}.yaml"
    if not yaml_path.is_file():
        raise RoomNotFoundError(f"no room file at {yaml_path}")
    data = yaml.safe_load(yaml_path.read_text())
    room_type = data.get("room_type")
    if room_type not in ("cavern", "settlement"):
        raise ValueError(f"{yaml_path}: invalid room_type {room_type!r}")

    # Story 54-2 / ADR-109: typed location-entity manifest. Loaded
    # leniently — a malformed entry surfaces a ValidationError noisily
    # rather than silently dropping the row (no silent fallbacks per
    # CLAUDE.md). The pf validate locations validator (Story 54-3)
    # catches drift at author time.
    entities_raw = data.get("entities") or []
    entities = [LocationEntity.model_validate(e) for e in entities_raw]

    if room_type == "settlement":
        return TacticalGridPayload(
            room_id=room_id,
            room_name=data["name"],
            room_type="settlement",
            mask=None,
            cavern_image_url=None,
            cell_size=None,
            cellular=None,
            derived=None,
            tokens=[],
            initiative=None,
            settlement_description=data.get("description"),
            settlement_exits=data.get("exits"),
            entities=entities,
        )

    # Cavern path
    mask_path = world_dir / "rooms" / f"{room_id}.mask.txt"
    if not mask_path.is_file():
        raise FileNotFoundError(
            f"cavern room {yaml_path} missing sibling mask {mask_path}; "
            f"run `uv run cavern_renderer {yaml_path}` to regenerate"
        )
    mask = mask_path.read_text()

    cellular = data["cellular"]
    derived = data.get("derived")
    if derived is None:
        raise ValueError(
            f"{yaml_path}: cavern room missing 'derived:' block; run cavern_renderer to populate"
        )

    relative = f"genre_packs/{genre_slug}/worlds/{world_dir.name}/rooms/{room_id}.cavern.png"
    image_url = resolve_asset_url(relative)

    with cavern_room_load_span(
        room_id=room_id,
        seed=cellular["seed"],
        density=cellular.get("density", 0.55),
        floor_count=derived["floor_count"],
        mask=mask,
        cavern_image_url=image_url,
    ):
        pass

    return TacticalGridPayload(
        room_id=room_id,
        room_name=data["name"],
        room_type="cavern",
        mask=mask,
        cavern_image_url=image_url,
        cell_size=cellular.get("cell_size", 28),
        cellular=CellularParams(
            size=tuple(cellular["size"]),
            seed=cellular["seed"],
            density=cellular.get("density", 0.55),
            cutoff=cellular.get("cutoff", 5),
            passes=cellular.get("passes", 4),
        ),
        derived=DerivedRoomData(
            floor_count=derived["floor_count"],
            exits={k: (tuple(v) if v else None) for k, v in derived["exits"].items()},
            pois=[tuple(p) for p in derived["pois"]],
        ),
        tokens=[],
        initiative=None,
        entities=entities,
    )


# ---------------------------------------------------------------------------
# Story 52-4: Runtime cavern PNG emission
# ---------------------------------------------------------------------------


def emit_runtime_cavern_png(
    *,
    mask_dict: dict[str, Any],
    output_path: Path,
    region_id: str,
) -> None:
    """Convert a persisted mask BLOB into an ADR-096 ``.cavern.png`` sidecar.

    ``mask_dict`` is the shape ``RegionMask.to_dict()`` produces (Story
    52-2) and ``DungeonStore.load_masks`` returns (Story 52-3):

        {
            "mask_bytes_b64": "<base64 of ASCII mask bytes>",
            "mask_sha": "<sha256 hex of mask bytes>",
            "block": {
                "cell_width": int,    # ADR-096 (typically 28)
                "grid_width": int,    # mask columns
                "grid_height": int,   # mask rows
                "origin_x": int,
                "origin_y": int,
            },
        }

    The PNG is written at ``output_path`` (parent directories are
    created as needed; mirrors ``cavern_renderer.render_grid_to_png``).
    PNG dimensions = ``grid_width * cell_width`` by
    ``grid_height * cell_width`` — the cell-stepped contract per
    ADR-096 §2; the UI's pixel→cell math depends on this being exact.

    Emits the ``dungeon.render.cavern_mask_to_png`` OTEL span with the
    region id, mask SHA, grid dimensions, cell width, and output path
    so the GM panel can verify the runtime renderer engaged for this
    region (Illusionism detector per CLAUDE.md OTEL Observability
    Principle).

    No silent fallbacks:
      * Missing ``mask_bytes_b64`` / ``block`` keys raise ``KeyError``.
      * Invalid base64 raises ``ValueError`` (``binascii.Error`` is a
        ``ValueError`` subclass in Python 3).
      * Empty grid (zero rows or zero columns) raises ``ValueError``.
      * Non-positive ``cell_width`` raises ``ValueError``.

    Parameters
    ----------
    mask_dict
        Persisted mask BLOB dict, as returned by
        ``DungeonStore.load_masks()``.
    output_path
        Absolute filesystem path where the PNG sidecar is written.
        Parent directories are created if absent.
    region_id
        Logical region identifier (used for OTEL attribution, not for
        path computation — the caller owns the output_path layout).
    """
    grid = _decode_runtime_mask_grid(mask_dict)
    block = mask_dict["block"]
    cell_width = block["cell_width"]
    if not isinstance(cell_width, int) or cell_width <= 0:
        raise ValueError(
            f"emit_runtime_cavern_png: block.cell_width must be a positive int, "
            f"got {cell_width!r}. ADR-096 mandates cell-stepped dimensions; a "
            "non-positive width would produce a 0×0 PNG. No Silent Fallbacks."
        )

    grid_height = len(grid)
    grid_width = len(grid[0])
    mask_sha = mask_dict.get("mask_sha", "")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _render_runtime_grid_to_png(
        grid=grid,
        output_path=output_path,
        cell_width=cell_width,
    )

    with cavern_mask_to_png_span(
        region_id=region_id,
        mask_sha256=mask_sha,
        grid_width=grid_width,
        grid_height=grid_height,
        cell_width=cell_width,
        output_path=str(output_path),
    ):
        pass

    logger.debug(
        "emit_runtime_cavern_png region_id=%s sha=%s grid=%dx%d cell_width=%d out=%s",
        region_id,
        mask_sha[:16] if mask_sha else "",
        grid_width,
        grid_height,
        cell_width,
        output_path,
    )


def _decode_runtime_mask_grid(mask_dict: dict[str, Any]) -> list[list[int]]:
    """Decode the persisted mask BLOB into a 2D grid of WALL/FLOOR ints.

    Raises ``KeyError`` if required keys are absent (No Silent Fallbacks
    — never silently default to an empty grid). Raises ``ValueError``
    for invalid base64 or for an empty/degenerate grid.
    """
    # Required keys — KeyError on access is intentional and loud.
    b64 = mask_dict["mask_bytes_b64"]
    _block = mask_dict["block"]  # accessed here so missing block raises early
    del _block

    if not isinstance(b64, str):
        raise ValueError(
            f"emit_runtime_cavern_png: mask_bytes_b64 must be str, got "
            f"{type(b64).__name__}. The persisted mask BLOB is JSON-shaped "
            "per RegionMask.to_dict(); a non-string indicates corruption."
        )

    try:
        # ``validate=True`` forces strict base64 — a bogus payload raises
        # ``binascii.Error`` (subclass of ``ValueError``) instead of
        # silently decoding garbage and producing a wrong grid.
        mask_bytes = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"emit_runtime_cavern_png: mask_bytes_b64 is not valid base64: {exc}. "
            "No silent fallback to an empty mask — the corruption must be visible."
        ) from exc

    if not mask_bytes:
        raise ValueError(
            "emit_runtime_cavern_png: decoded mask is empty. ADR-096 requires "
            "real wall/floor topology; an empty mask is a silent lie. "
            "No Silent Fallbacks."
        )

    rows: list[list[int]] = []
    current: list[int] = []
    for byte in mask_bytes:
        if byte == _MASK_NEWLINE:
            rows.append(current)
            current = []
            continue
        if byte == _MASK_WALL:
            current.append(1)
        elif byte == _MASK_FLOOR:
            current.append(0)
        else:
            raise ValueError(
                f"emit_runtime_cavern_png: mask contains illegal byte "
                f"{byte!r} (only '#' wall, '.' floor, and newline are "
                "permitted per ADR-096 §2)."
            )
    if current:
        rows.append(current)

    if not rows or not rows[0]:
        raise ValueError(
            "emit_runtime_cavern_png: mask decoded to zero rows or zero "
            "cells. ADR-096 requires real wall/floor topology; an empty "
            "mask is a silent lie. No Silent Fallbacks."
        )

    width = len(rows[0])
    for i, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(
                f"emit_runtime_cavern_png: row {i} has {len(row)} cells, "
                f"expected {width} (matching row 0). Ragged grids are not "
                "valid ADR-096 masks."
            )

    return rows


def _render_runtime_grid_to_png(
    *,
    grid: list[list[int]],
    output_path: Path,
    cell_width: int,
) -> None:
    """Render a wall/floor grid to a PNG file.

    The visual is deliberately minimal: solid wall cells on a wall-color
    background, solid floor cells on a floor-color base. The static
    cavern_renderer tool (``sidequest-content/tools/cavern_renderer``)
    adds stipple, grain, and inked edges; matching that visual exactly
    is out-of-scope for 52-4 (the contract is dimensions + wall/floor
    legibility, not byte-identical visual parity).
    """
    height = len(grid)
    width = len(grid[0])
    img = Image.new("RGB", (width * cell_width, height * cell_width), _WALL_RGB)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        for x in range(width):
            if grid[y][x] != 0:
                continue  # wall — background already filled
            px, py = x * cell_width, y * cell_width
            draw.rectangle(
                (px, py, px + cell_width - 1, py + cell_width - 1),
                fill=_FLOOR_RGB,
            )
    img.save(output_path, "PNG", optimize=True)
