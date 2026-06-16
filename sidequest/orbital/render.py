"""Server-side SVG renderer for the orbital chart.

Per spec §6 of the original visual-restoration design and the orrery-v2
spec (docs/superpowers/specs/2026-05-04-orrery-v2-visual-restoration.md):
renderer produces a complete SVG document per (world, scope, t_hours,
party_at). Layers: engraved (bearing rose + orbits + bodies + scale +
moon band), flavor (chart.yaml annotations including textPath labels),
party (current location).

Position math is Kepler-correct via `sidequest.orbital.position`.
For e=0 bodies the output is bit-identical to the prior circular formula,
so `eccentricity=0` fixtures don't drift.

Orrery v2 (Story 45-42) adds:
  - Bearing rose at chart center (system_root only).
  - Star-as-reticle for STAR-typed centered bodies.
  - Register-driven orbit + label styling (engraved/chalk/prose).
  - Moons rendered at system-root scope in a fixed-radius band around
    their parent (with auto-allocated or pinned `moon_display_radius_px`).
  - `curve_along` honored on engraved_label annotations via SVG textPath.
  - Radial-out label anchor + bearing-rose clearance + peer-collision tier.
  - Non-color hazard signal (dashed glyph stroke).
  - OTEL chart.render attrs for register / moon / label-density counts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import svgwrite
import svgwrite.base
import svgwrite.container
import svgwrite.path
import svgwrite.shapes
import svgwrite.text
import svgwrite.validator2

from sidequest.orbital import palette
from sidequest.orbital.label_strategy import (
    CalloutBlock,
    GutterLayout,
    LabelDecision,
    LabelStrategy,
    Register,
    _StrategyInput,
    estimate_text_width_px,
    lay_out_gutter,
    select_label_strategies,
)
from sidequest.orbital.models import (
    Annotation,
    BodyDef,
    BodyType,
    ChartConfig,
    OrbitsConfig,
)
from sidequest.orbital.position import ellipse_geometry, kepler_position
from sidequest.telemetry.spans.chart import (
    emit_chart_label_distribution,
    emit_chart_label_strategy,
    emit_chart_render,
)

# svgwrite's built-in validators reject attributes that aren't in their
# (somewhat outdated) allowlists. We need:
#   - `data-*` for click-routing on rendered bodies
#   - `paint-order` for the haloed-text trick
#   - `class` so we can mark drillable groups for client-side hover styling
#   - `href` and `xlink:href` for textPath references
#   - `letter-spacing` for register-styled labels
#   - `font-weight` for register-styled labels
#   - `startOffset` for textPath positioning
#   - `fill-opacity`, `stroke-opacity`, `opacity` for register dimming
_PASSTHROUGH_ATTRS = frozenset(
    {
        "paint-order",
        "class",
        "href",
        "xlink:href",
        "letter-spacing",
        "font-weight",
        "startOffset",
    }
)

_orig_is_valid_attribute = svgwrite.validator2.Full11Validator.is_valid_svg_attribute


def _is_valid_or_passthrough(self, elementname, attributename):
    if attributename.startswith("data-"):
        return True
    if attributename in _PASSTHROUGH_ATTRS:
        return True
    return _orig_is_valid_attribute(self, elementname, attributename)


svgwrite.validator2.Full11Validator.is_valid_svg_attribute = _is_valid_or_passthrough
svgwrite.validator2.Tiny12Validator.is_valid_svg_attribute = _is_valid_or_passthrough


def _check_svg_attribute_value_passthrough(self, elementname, attributename, value):
    if attributename.startswith("data-"):
        return
    if attributename in _PASSTHROUGH_ATTRS:
        return
    return _orig_check_value(self, elementname, attributename, value)


_orig_check_value = svgwrite.validator2.Full11Validator.check_svg_attribute_value
svgwrite.validator2.Full11Validator.check_svg_attribute_value = (
    _check_svg_attribute_value_passthrough
)
svgwrite.validator2.Tiny12Validator.check_svg_attribute_value = (
    _check_svg_attribute_value_passthrough
)


# ---------------------------------------------------------------------------
# Public API: Scope, render_chart
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """Render scope — which body is centered."""

    center_body_id: str

    @classmethod
    def system_root(cls) -> Scope:
        return cls(center_body_id="<root>")

    @property
    def is_system_root(self) -> bool:
        return self.center_body_id == "<root>"


@dataclass
class _RenderStats:
    """Accumulator for OTEL chart.render attribute counts.

    Populated by `_render_engraved_layer` as it walks bodies; passed back
    to `render_chart` so the chart.render span has accurate per-register
    body counts and label-density signals.
    """

    body_count_engraved: int = 0
    body_count_chalk: int = 0
    body_count_prose: int = 0
    body_count_moons_rendered: int = 0
    label_collision_tier_max: int = 0


# ---------------------------------------------------------------------------
# Position math (thin shims onto sidequest.orbital.position)
# ---------------------------------------------------------------------------


def _body_position_au_polar(body: BodyDef, t_hours: float) -> tuple[float, float]:
    """Return (au, theta_deg) of a body relative to its parent at story-time t."""
    return kepler_position(body, t_hours)


def _polar_to_cartesian(au: float, theta_deg: float, scale: float) -> tuple[float, float]:
    """Convert polar (AU, deg) to SVG cartesian pixels.

    SVG y-axis grows downward; we flip so 0° is "right" (3 o'clock) and 90°
    is "up" (12 o'clock) per orrery convention.
    """
    rad = math.radians(theta_deg)
    x = au * scale * math.cos(rad)
    y = -au * scale * math.sin(rad)
    return (x, y)


# ---------------------------------------------------------------------------
# Text helper — haloed text with optional register styling
# ---------------------------------------------------------------------------


def _haloed_text(
    content: str,
    *,
    insert: tuple[float, float],
    fill: str,
    font_family: str = palette.FONT_DISPLAY,
    font_size: int = 10,
    text_anchor: str = "start",
    font_style: str | None = None,
    font_weight: int | None = None,
    letter_spacing: int | None = None,
    opacity: float | None = None,
) -> svgwrite.text.Text:
    """Text with a dark halo via paint-order=stroke. Tiny-profile-safe."""
    text = svgwrite.text.Text(
        content,
        insert=insert,
        fill=fill,
        font_family=font_family,
        font_size=font_size,
        text_anchor=text_anchor,
    )
    text["stroke"] = palette.BG
    text["stroke-width"] = 3
    text["stroke-linejoin"] = "round"
    text["paint-order"] = "stroke"
    if font_style is not None:
        text["font-style"] = font_style
    if font_weight is not None:
        text["font-weight"] = font_weight
    if letter_spacing is not None:
        text["letter-spacing"] = letter_spacing
    if opacity is not None:
        text["opacity"] = opacity
    return text


# ---------------------------------------------------------------------------
# Body glyphs
# ---------------------------------------------------------------------------


def _star_reticle(x: float, y: float) -> svgwrite.container.Group:
    """Star: red dashed outer ring + red solid inner ring + crosshair ticks
    + brass core. Replaces the legacy concentric corona disks per §4.3.

    Conveys "the players are *inside* this thing" — a target reticle on
    the central authority, not a glowing sphere off to the side.
    """
    g = svgwrite.container.Group()
    # Outer dashed ring
    outer = svgwrite.shapes.Circle(
        center=(x, y),
        r=palette.STAR_RETICLE_OUTER_R,
        fill="none",
        stroke=palette.RED,
        stroke_width=palette.STAR_RETICLE_OUTER_STROKE,
    )
    outer["stroke-dasharray"] = palette.RETICLE_DASH_PATTERN
    g.add(outer)
    # Inner solid ring
    g.add(
        svgwrite.shapes.Circle(
            center=(x, y),
            r=palette.STAR_RETICLE_INNER_R,
            fill="none",
            stroke=palette.RED,
            stroke_width=palette.STAR_RETICLE_INNER_STROKE,
        )
    )
    # Crosshair ticks at N/E/S/W
    tick_in = palette.STAR_RETICLE_TICK_INNER
    tick_out = palette.STAR_RETICLE_TICK_OUTER
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        g.add(
            svgwrite.shapes.Line(
                start=(x + dx * tick_in, y + dy * tick_in),
                end=(x + dx * tick_out, y + dy * tick_out),
                stroke=palette.RED,
                stroke_width=1.4,
            )
        )
    # Brass core
    g.add(
        svgwrite.shapes.Circle(
            center=(x, y),
            r=palette.STAR_RETICLE_CORE_R,
            fill=palette.BRASS,
            stroke=palette.BRASS,
        )
    )
    return g


def _habitat_glyph(
    x: float, y: float, *, fill: str, hazard: bool = False
) -> svgwrite.shapes.Polygon:
    """Habitat: brass diamond. With hazard=True, adds dashed-stroke signal
    (AC #11 — non-color cue for color-blind players)."""
    pts = [(x, y - 5), (x + 5, y), (x, y + 5), (x - 5, y)]
    poly = svgwrite.shapes.Polygon(points=pts, fill=fill, stroke=palette.BRASS, stroke_width=1)
    if hazard:
        poly["stroke-dasharray"] = palette.HAZARD_GLYPH_DASH
        poly["stroke-width"] = palette.HAZARD_GLYPH_STROKE_WIDTH
    return poly


def _gate_glyph(x: float, y: float, *, fill: str, hazard: bool = False) -> svgwrite.shapes.Polygon:
    """Gate: hexagon outline."""
    r = 6
    pts = [
        (x, y - r),
        (x + r * 0.866, y - r * 0.5),
        (x + r * 0.866, y + r * 0.5),
        (x, y + r),
        (x - r * 0.866, y + r * 0.5),
        (x - r * 0.866, y - r * 0.5),
    ]
    poly = svgwrite.shapes.Polygon(points=pts, fill=fill, stroke=palette.BRASS, stroke_width=1)
    if hazard:
        poly["stroke-dasharray"] = palette.HAZARD_GLYPH_DASH
        poly["stroke-width"] = palette.HAZARD_GLYPH_STROKE_WIDTH
    return poly


def _wreck_glyph(x: float, y: float) -> svgwrite.container.Group:
    """Wreck: jagged 5-point asterisk in DIM brass."""
    g = svgwrite.container.Group()
    r = 5
    for i in range(5):
        theta = math.radians(90 + i * 72)
        x2 = x + r * math.cos(theta)
        y2 = y - r * math.sin(theta)
        g.add(svgwrite.shapes.Line(start=(x, y), end=(x2, y2), stroke=palette.DIM, stroke_width=1))
    return g


def _gas_giant_overlay(x: float, y: float, body_radius: float) -> svgwrite.container.Group:
    """Three horizontal banding lines for gas-giant subtype."""
    g = svgwrite.container.Group()
    for dy in (-body_radius * 0.4, 0.0, body_radius * 0.4):
        g.add(
            svgwrite.shapes.Line(
                start=(x - body_radius * 0.9, y + dy),
                end=(x + body_radius * 0.9, y + dy),
                stroke=palette.BRASS,
                stroke_width=0.6,
                stroke_opacity=0.6,
            )
        )
    return g


def _dotted_arc(
    *,
    cx: float,
    cy: float,
    radius: float,
    from_deg: float,
    extent_deg: float,
    color: str,
    dot_spacing_px: float = 4.0,
) -> svgwrite.container.Group:
    """Arc rendered as a series of small dots. Tiny-profile-safe."""
    g = svgwrite.container.Group()
    arc_len_px = abs(extent_deg) * math.pi / 180.0 * radius
    n = max(2, int(arc_len_px / dot_spacing_px))
    for i in range(n):
        theta_deg = from_deg + extent_deg * (i / max(1, n - 1))
        rad = math.radians(theta_deg)
        x = cx + radius * math.cos(rad)
        y = cy - radius * math.sin(rad)
        g.add(svgwrite.shapes.Circle(center=(x, y), r=0.8, fill=color))
    return g


# ---------------------------------------------------------------------------
# Bearing rose (§4.2)
# ---------------------------------------------------------------------------


def _render_bearing_rose() -> svgwrite.container.Group:
    """Engraved bearing dial at chart center. System-root scope only.

    Two thin engraved rings + 36 tick marks (longer at every 30°, longest
    at every 90°) + cardinal numerals (000/090/180/270) + intermediate
    numerals (030/060/120/150/210/240/300/330).
    """
    g = svgwrite.container.Group(id="bearing-rose")
    inner_r = palette.BEARING_ROSE_INNER_PX
    outer_r = palette.BEARING_ROSE_OUTER_PX
    # Two thin rings
    g.add(
        svgwrite.shapes.Circle(
            center=(0, 0), r=inner_r, fill="none", stroke=palette.BRASS, stroke_width=0.5
        )
    )
    g.add(
        svgwrite.shapes.Circle(
            center=(0, 0), r=outer_r, fill="none", stroke=palette.BRASS, stroke_width=0.5
        )
    )
    # 36 tick marks at every 10°
    for theta_deg in range(0, 360, 10):
        if theta_deg % 90 == 0:
            tick_len = palette.BEARING_ROSE_TICK_LEN_90
        elif theta_deg % 30 == 0:
            tick_len = palette.BEARING_ROSE_TICK_LEN_30
        else:
            tick_len = palette.BEARING_ROSE_TICK_LEN_10
        rad = math.radians(theta_deg)
        x_in = outer_r * math.cos(rad)
        y_in = -outer_r * math.sin(rad)
        x_out = (outer_r + tick_len) * math.cos(rad)
        y_out = -(outer_r + tick_len) * math.sin(rad)
        g.add(
            svgwrite.shapes.Line(
                start=(x_in, y_in), end=(x_out, y_out), stroke=palette.BRASS, stroke_width=0.6
            )
        )
    # Cardinal numerals at 000/090/180/270 (font 9, brass)
    cardinal_radius = outer_r + palette.BEARING_ROSE_TICK_LEN_90 + 8
    for theta_deg in (0, 90, 180, 270):
        rad = math.radians(theta_deg)
        x = cardinal_radius * math.cos(rad)
        y = -cardinal_radius * math.sin(rad)
        g.add(
            _haloed_text(
                f"{theta_deg:03d}",
                insert=(x, y + 3),
                fill=palette.BRASS,
                font_size=palette.BEARING_ROSE_CARDINAL_FONT_SIZE,
                text_anchor="middle",
            )
        )
    # Intermediate numerals (font 7, dim brass)
    intermediate_radius = outer_r + palette.BEARING_ROSE_TICK_LEN_30 + 6
    for theta_deg in (30, 60, 120, 150, 210, 240, 300, 330):
        rad = math.radians(theta_deg)
        x = intermediate_radius * math.cos(rad)
        y = -intermediate_radius * math.sin(rad)
        g.add(
            _haloed_text(
                f"{theta_deg:03d}",
                insert=(x, y + 2),
                fill=palette.DIM,
                font_size=palette.BEARING_ROSE_INTERMEDIATE_FONT_SIZE,
                text_anchor="middle",
            )
        )
    return g


# ---------------------------------------------------------------------------
# Curve-along resolution for engraved_label (§4.1)
# ---------------------------------------------------------------------------


class _CurveScopeMismatch(ValueError):
    """Raised when curve_along references a body that exists in the orbits
    config but isn't visible at the current render scope.

    Distinct from a true unknown-body error so `_render_annotation` can
    silently skip annotations that don't apply at this scope (e.g. the
    chart.yaml has `curve_along: orbit_grand_gate` but the renderer is
    drilled into Red Prospect — there's no orbit to attach to). Silent
    skip is correct here because the annotation has nowhere to render;
    raising would crash legitimate drill-in scopes.
    """


def _ellipse_perimeter_px(rx: float, ry: float) -> float:
    """Ramanujan II approximation of an ellipse perimeter (high accuracy)."""
    h = ((rx - ry) ** 2) / ((rx + ry) ** 2) if (rx + ry) > 0 else 0.0
    return math.pi * (rx + ry) * (1.0 + (3.0 * h) / (10.0 + math.sqrt(4.0 - 3.0 * h)))


def _resolve_curve_along(
    value: str,
    orbits: OrbitsConfig,
    center_id: str,
    vp: _Viewport,
) -> tuple[str, str, str, float]:
    """Resolve a `curve_along` reference to (path_id, path_d, resolved_body_id, circumference_px).

    Per spec §4.1:
      - `orbit_outermost` → outermost direct child's orbit ellipse
      - `orbit_<body_id>` → that body's orbit ellipse
      - `body:<body_id>` → that arc_belt body's own arc (raises for non-belt)

    The third return value is the body id whose orbit/arc was resolved.
    Callers use it to (a) inherit that body's `label_register` for
    textPath styling per AC #6 and (b) suppress that body's own
    radial-out label when the annotation duplicates it (§4.1: "If both
    exist, prefer the annotation").

    The path starts at the top of the ring and sweeps clockwise (sweep-flag=1)
    so textPath letters stay upright in the player's reading direction.

    Raises:
      ValueError: unknown reference scheme, body doesn't exist anywhere,
        or wrong body type.
      _CurveScopeMismatch: body exists but isn't visible at this scope.
        Caller should treat as "skip annotation at this scope".
    """
    if value.startswith("orbit_"):
        target = value[len("orbit_") :]
        if target == "outermost":
            # Outermost direct child of the current center
            children = [
                (bid, b)
                for bid, b in orbits.bodies.items()
                if b.parent == center_id and b.semi_major_au is not None
            ]
            if not children:
                raise ValueError(
                    f"curve_along='orbit_outermost' but center {center_id!r} has no children"
                )
            children.sort(key=lambda kv: kv[1].semi_major_au or 0.0, reverse=True)
            body_id, body = children[0]
        else:
            body_id = target
            if body_id not in orbits.bodies:
                raise ValueError(
                    f"curve_along={value!r}: orbit_{body_id!r} references nonexistent body"
                )
            body = orbits.bodies[body_id]
            if body.parent != center_id:
                raise _CurveScopeMismatch(
                    f"curve_along={value!r}: body {body_id!r} is not a direct child of "
                    f"center {center_id!r}"
                )
        ell = ellipse_geometry(body, vp.au_to_px)
        cx, cy = ell.center_x_px, ell.center_y_px
        rx, ry = ell.semi_major_px, ell.semi_minor_px
        path_id = f"curve_orbit_{body_id}"
        # Start at top of the ellipse (cx, cy - ry); sweep clockwise (sweep-flag=1)
        # in two arcs to wrap the full ring. SVG: M ax,ay A rx,ry rot lf sf bx,by
        path_d = (
            f"M {cx} {cy - ry} A {rx} {ry} 0 0 1 {cx} {cy + ry} A {rx} {ry} 0 0 1 {cx} {cy - ry}"
        )
        circumference = _ellipse_perimeter_px(rx, ry)
        return path_id, path_d, body_id, circumference

    if value.startswith("body:"):
        body_id = value[len("body:") :]
        if body_id not in orbits.bodies:
            raise ValueError(f"curve_along={value!r}: body:{body_id!r} references nonexistent body")
        body = orbits.bodies[body_id]
        if body.type != BodyType.ARC_BELT:
            raise ValueError(
                f"curve_along={value!r}: body:{body_id!r} is type={body.type.value!r}, "
                f"only arc_belt bodies have a meaningful body arc"
            )
        if (
            body.semi_major_au is None
            or body.epoch_phase_deg is None
            or body.arc_extent_deg is None
        ):
            raise ValueError(
                f"curve_along={value!r}: body:{body_id!r} missing arc geometry "
                f"(semi_major_au / epoch_phase_deg / arc_extent_deg)"
            )
        radius_px = body.semi_major_au * vp.au_to_px
        from_deg = body.epoch_phase_deg
        to_deg = from_deg + body.arc_extent_deg
        x1, y1 = _polar_to_cartesian(body.semi_major_au, from_deg, vp.au_to_px)
        x2, y2 = _polar_to_cartesian(body.semi_major_au, to_deg, vp.au_to_px)
        # Clockwise sweep along the arc (sweep-flag=1).
        large_arc = 1 if abs(body.arc_extent_deg) > 180 else 0
        path_id = f"curve_body_{body_id}"
        path_d = f"M {x1} {y1} A {radius_px} {radius_px} 0 {large_arc} 1 {x2} {y2}"
        # Arc length = r * θ (radians). For arc_belt the path is a single arc.
        circumference = abs(math.radians(body.arc_extent_deg)) * radius_px
        return path_id, path_d, body_id, circumference

    raise ValueError(
        f"curve_along={value!r}: unknown reference scheme; "
        f"expected 'orbit_outermost', 'orbit_<body_id>', or 'body:<body_id>'"
    )


def _engraved_label_textpath(
    text: str,
    *,
    path_id: str,
    register: _RegisterValue = "engraved",
    fill: str = palette.BRASS,
) -> svgwrite.text.Text:
    """Engraved label rendered along a curve via textPath.

    Per spec §4.1: em-dashes baked by renderer (`— text —`); content
    authors store plain strings.

    Per spec AC #6 + §4.4: the textPath inherits the resolved body's
    effective label_register. `prose` produces VT323 italic at opacity
    0.85 (matching the design's lowercase prose treatment for
    last_drift); `chalk` is Orbitron weight-600 with chalk
    letter-spacing; `engraved` is Orbitron weight-700 italic, the
    default cartographic register.
    """
    decorated = f"— {text} —"
    if register == "prose":
        font_family = palette.LABEL_PROSE_FONT
        font_size = palette.LABEL_PROSE_FONT_SIZE
        font_style = "italic"
        font_weight: int | None = None
        opacity: float | None = palette.LABEL_PROSE_OPACITY
        letter_spacing: int | None = None
    elif register == "chalk":
        font_family = palette.LABEL_CHALK_FONT
        font_size = 11
        font_style = None
        font_weight = palette.LABEL_CHALK_WEIGHT
        opacity = palette.ORBIT_OPACITY_CHALK
        letter_spacing = palette.LABEL_CHALK_LETTER_SPACING
    else:  # engraved (default)
        font_family = palette.LABEL_ENGRAVED_FONT
        font_size = 12
        font_style = "italic"
        font_weight = palette.LABEL_ENGRAVED_WEIGHT
        opacity = None
        letter_spacing = palette.LABEL_ENGRAVED_LETTER_SPACING

    elem = svgwrite.text.Text(
        "",
        fill=fill,
        font_family=font_family,
        font_size=font_size,
        text_anchor="middle",
    )
    elem["stroke"] = palette.BG
    elem["stroke-width"] = 3
    elem["stroke-linejoin"] = "round"
    elem["paint-order"] = "stroke"
    if font_style is not None:
        elem["font-style"] = font_style
    if font_weight is not None:
        elem["font-weight"] = font_weight
    if letter_spacing is not None:
        elem["letter-spacing"] = letter_spacing
    if opacity is not None:
        elem["opacity"] = opacity
    # textPath child references the path id.
    tp = svgwrite.text.TextPath(path=f"#{path_id}", text=decorated)
    tp["startOffset"] = "50%"
    elem.add(tp)
    return elem


# ---------------------------------------------------------------------------
# Body-label suppression — §4.1 last paragraph
# ---------------------------------------------------------------------------


def _arc_belt_bodies_with_textpath_annotation(
    chart: ChartConfig,
    orbits: OrbitsConfig,
    center_id: str,
    vp: _Viewport,
) -> set[str]:
    """Set of arc_belt body ids whose orbit is referenced by an
    `engraved_label` annotation in chart.yaml at this scope.

    Per spec §4.1: "If both exist, prefer the annotation." When an
    arc_belt body has both a `body.label` and a chart.yaml engraved_label
    that wraps its orbit, we suppress the body's own radial-out label —
    the textPath IS the label. (Restricted to arc_belt bodies because
    point-bodies can legitimately carry both a textPath wrapping the
    ring and a radial-out label at the body's current position.)
    """
    suppressed: set[str] = set()
    for annot in chart.annotations:
        if annot.kind != "engraved_label" or annot.curve_along is None:
            continue
        try:
            _, _, resolved_body_id, _ = _resolve_curve_along(
                annot.curve_along, orbits, center_id, vp
            )
        except (_CurveScopeMismatch, ValueError):
            # Off-scope or malformed — annotation won't render, so no
            # suppression needed. Malformed annotations are caught by
            # the flavor-layer render path's own error handling.
            continue
        body = orbits.bodies.get(resolved_body_id)
        if body is None:
            continue
        if body.type == BodyType.ARC_BELT:
            suppressed.add(resolved_body_id)
    return suppressed


# ---------------------------------------------------------------------------
# render_chart — top-level entry
# ---------------------------------------------------------------------------


def render_chart(
    *,
    orbits: OrbitsConfig,
    chart: ChartConfig,
    scope: Scope,
    t_hours: float,
    party_at: str | None,
) -> str:
    """Compose the full chart SVG. Returns a UTF-8 string."""
    center_id = _resolve_scope_center(orbits, scope)
    viewport = _viewport_for_scope(orbits, center_id)

    dwg = svgwrite.Drawing(
        size=(viewport.size_px, viewport.size_px),
        viewBox=(f"{-viewport.half} {-viewport.half} {viewport.size_px} {viewport.size_px}"),
        profile="tiny",
        debug=False,
    )
    dwg.add(
        dwg.rect(
            insert=(-viewport.half, -viewport.half),
            size=(viewport.size_px, viewport.size_px),
            fill=palette.BG,
        )
    )
    suppressed_label_ids = _arc_belt_bodies_with_textpath_annotation(
        chart, orbits, center_id, viewport
    )
    viewport_g = svgwrite.container.Group(id="viewport")
    engraved_layer, stats, gutter, decisions = _render_engraved_layer(
        orbits, chart, center_id, scope, viewport, t_hours, suppressed_label_ids
    )
    viewport_g.add(engraved_layer)
    viewport_g.add(_render_flavor_layer(chart, orbits, center_id, viewport, t_hours))
    viewport_g.add(_render_party_layer(orbits, center_id, viewport, t_hours, party_at))
    dwg.add(viewport_g)
    output = dwg.tostring()
    emit_chart_render(
        scope_center=center_id,
        t_hours=t_hours,
        party_at=party_at,
        body_count=len(orbits.bodies),
        output_size_bytes=len(output.encode("utf-8")),
        body_count_engraved=stats.body_count_engraved,
        body_count_chalk=stats.body_count_chalk,
        body_count_prose=stats.body_count_prose,
        body_count_moons_rendered=stats.body_count_moons_rendered,
        label_collision_tier_max=stats.label_collision_tier_max,
    )
    # ADR-094 distribution span.
    counts = {
        "textpath": sum(1 for d in decisions if d.strategy == LabelStrategy.TEXTPATH),
        "radial": sum(1 for d in decisions if d.strategy == LabelStrategy.RADIAL),
        "callout": sum(1 for d in decisions if d.strategy == LabelStrategy.CALLOUT),
    }
    bodies_unlabeled = sum(
        1
        for b in orbits.bodies.values()
        if (b.label is None or not b.label.strip()) and b.show_at_system_scope
    )
    # AC-O2 sum invariant: textpath + radial + callout + unlabeled == total.
    # `total` counts every visible-at-scope body, not just labeled ones.
    emit_chart_label_distribution(
        bodies_total=len(decisions) + bodies_unlabeled,
        bodies_textpath=counts["textpath"],
        bodies_radial=counts["radial"],
        bodies_callout=counts["callout"],
        bodies_unlabeled=bodies_unlabeled,
        gutter_inset_fallbacks=gutter.inset_fallback_count,
        cross_group_crossings=gutter.cross_group_crossing_count,
    )
    return output


# ---------------------------------------------------------------------------
# Viewport + scope resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Viewport:
    size_px: int
    half: int
    au_to_px: float

    # ADR-094 callout gutter zone — chart-area bbox is the inscribed square
    # bounded by ±max_au * au_to_px (the orbit envelope), which sits inside
    # the SVG canvas (svg_min_x / svg_max_x at -half / +half). Gutter blocks
    # live between chart edge and svg edge.
    @property
    def chart_min_x(self) -> float:
        # Chart area inset by GUTTER_WIDTH + INNER_MARGIN on each side.
        return -self.half + palette.GUTTER_WIDTH_PX + palette.GUTTER_INNER_MARGIN_PX

    @property
    def chart_max_x(self) -> float:
        return self.half - palette.GUTTER_WIDTH_PX - palette.GUTTER_INNER_MARGIN_PX

    @property
    def chart_top_y(self) -> float:
        return -self.half + palette.GUTTER_INNER_MARGIN_PX

    @property
    def chart_bottom_y(self) -> float:
        return self.half - palette.GUTTER_INNER_MARGIN_PX

    @property
    def svg_min_x(self) -> float:
        return -self.half

    @property
    def svg_max_x(self) -> float:
        return self.half


def _resolve_scope_center(orbits: OrbitsConfig, scope: Scope) -> str:
    if scope.center_body_id == "<root>":
        roots = [bid for bid, b in orbits.bodies.items() if b.parent is None]
        if len(roots) != 1:
            raise ValueError(
                f"system_root scope requires exactly one parent-less body; got {roots!r}"
            )
        return roots[0]
    if scope.center_body_id not in orbits.bodies:
        raise ValueError(f"scope center {scope.center_body_id!r} not in bodies")
    return scope.center_body_id


def _viewport_for_scope(orbits: OrbitsConfig, center_id: str) -> _Viewport:
    children = [b for b in orbits.bodies.values() if b.parent == center_id]
    max_au = max((c.semi_major_au or 0.0 for c in children), default=1.0) or 1.0
    size_px = 800
    half = size_px // 2
    pad = 1.2
    au_to_px = (half / pad) / max_au
    return _Viewport(size_px=size_px, half=half, au_to_px=au_to_px)


def _attach_body_id(elem: svgwrite.base.BaseElement, body_id: str) -> svgwrite.base.BaseElement:
    elem.attribs["data-body-id"] = body_id
    return elem


def _drillable_body_ids(orbits: OrbitsConfig) -> set[str]:
    """Bodies with at least one child."""
    return {bid for bid in orbits.bodies if any(b.parent == bid for b in orbits.bodies.values())}


# ---------------------------------------------------------------------------
# Register classification
# ---------------------------------------------------------------------------


_RegisterValue = Literal["engraved", "chalk", "prose"]


def _effective_label_register(body: BodyDef) -> _RegisterValue:
    """Effective label register: explicit override, else fall back to body register."""
    if body.label_register is not None:
        return body.label_register
    return body.register


def _orbit_stroke_attrs(register: _RegisterValue) -> dict[str, object]:
    """SVG attribute kwargs for an orbit ellipse stroke given its register."""
    if register == "chalk":
        return {
            "stroke": palette.BRASS,
            "stroke_width": palette.ORBIT_STROKE_CHALK,
            "stroke_opacity": palette.ORBIT_OPACITY_CHALK,
        }
    # engraved (default) and prose-on-orbit (no chalk override) — solid
    return {
        "stroke": palette.BRASS,
        "stroke_width": palette.ORBIT_STROKE_ENGRAVED,
    }


def _label_text(
    body: BodyDef,
    *,
    insert: tuple[float, float],
    fill: str,
    text_anchor: str,
) -> svgwrite.text.Text:
    """Render a body label per its effective register."""
    if body.label is None:
        raise ValueError("_label_text called on body with no label")
    register = _effective_label_register(body)
    if register == "prose":
        return _haloed_text(
            body.label,
            insert=insert,
            fill=fill,
            text_anchor=text_anchor,
            font_family=palette.LABEL_PROSE_FONT,
            font_size=palette.LABEL_PROSE_FONT_SIZE,
            font_style="italic",
            opacity=palette.LABEL_PROSE_OPACITY,
        )
    if register == "chalk":
        return _haloed_text(
            body.label,
            insert=insert,
            fill=fill,
            text_anchor=text_anchor,
            font_family=palette.LABEL_CHALK_FONT,
            font_size=10,
            font_weight=palette.LABEL_CHALK_WEIGHT,
            letter_spacing=palette.LABEL_CHALK_LETTER_SPACING,
        )
    # engraved (default)
    return _haloed_text(
        body.label,
        insert=insert,
        fill=fill,
        text_anchor=text_anchor,
        font_family=palette.LABEL_ENGRAVED_FONT,
        font_size=10,
        font_weight=palette.LABEL_ENGRAVED_WEIGHT,
        letter_spacing=palette.LABEL_ENGRAVED_LETTER_SPACING,
    )


# ---------------------------------------------------------------------------
# Label de-collision (§5)
# ---------------------------------------------------------------------------


@dataclass
class _BodyPlacement:
    """Resolved label-anchor placement for a body in the engraved layer."""

    body_id: str
    body: BodyDef
    body_x: float
    body_y: float
    glyph_radius: float
    bearing_deg: float  # bearing from chart center to body
    tier: int
    anchor_x: float
    anchor_y: float
    text_anchor: str  # "start" / "middle" / "end"


def _bearing_from_center(x: float, y: float) -> float:
    """Return bearing in degrees (0..360) from chart center to (x, y).
    0° = right (3 o'clock), 90° = up (12 o'clock) — matches _polar_to_cartesian."""
    return math.degrees(math.atan2(-y, x)) % 360


def _text_anchor_for_bearing(bearing_deg: float) -> str:
    """text-anchor per radial-out anchor logic (§5.1)."""
    # Within ±15° of straight up (90°) or straight down (270°): center
    if abs(bearing_deg - 90) <= 15 or abs(bearing_deg - 270) <= 15:
        return "middle"
    # Right half: bearings in (-90°, 90°) i.e. (270°,360] U [0°,90°): start
    if bearing_deg <= 90 or bearing_deg >= 270:
        return "start"
    # Left half: bearings in (90°, 270°): end
    return "end"


def _angular_distance(a: float, b: float) -> float:
    """Smallest angular distance in degrees between two bearings (0..180)."""
    diff = abs((a - b) % 360)
    return min(diff, 360 - diff)


def _assign_collision_tiers(
    placements: list[_BodyPlacement],
) -> int:
    """Walk bodies sorted by bearing; bump tier when neighbors are within
    MIN_ANGULAR_SEPARATION_DEG. Returns the maximum tier assigned.

    Cap tier at LABEL_TIER_MAX; beyond that, accept collision and warn.
    """
    if not placements:
        return 0
    by_bearing = sorted(placements, key=lambda p: p.bearing_deg)
    max_tier = 0
    for i, p in enumerate(by_bearing):
        if i == 0:
            p.tier = 0
            continue
        prev = by_bearing[i - 1]
        if _angular_distance(p.bearing_deg, prev.bearing_deg) < palette.MIN_ANGULAR_SEPARATION_DEG:
            p.tier = min(prev.tier + 1, palette.LABEL_TIER_MAX)
        else:
            p.tier = 0
        max_tier = max(max_tier, p.tier)
    return max_tier


def _arc_to_neighbor_for_placement(p: _BodyPlacement, peers: list[_BodyPlacement]) -> float:
    """Smallest arc length (px) at p's body radius to a peer's bearing.

    Used by ADR-094 strategy selection to know whether a radial label fits
    without crossing into a peer's space at the same radial distance.
    """
    body_radial = math.hypot(p.body_x, p.body_y)
    best_arc = float("inf")
    for other in peers:
        if other.body_id == p.body_id:
            continue
        delta_deg = _angular_distance(p.bearing_deg, other.bearing_deg)
        if delta_deg <= 0:
            continue
        arc_px = 2 * math.pi * body_radial * (delta_deg / 360.0)
        if arc_px < best_arc:
            best_arc = arc_px
    return best_arc if best_arc != float("inf") else 1e9


def _resolve_anchor(
    p: _BodyPlacement,
    *,
    apply_rose_clearance: bool,
) -> None:
    """Compute and set the radial-out anchor + tier offset + rose clearance
    for a placement. Mutates p.anchor_x / anchor_y / text_anchor.
    """
    bearing_rad = math.radians(p.bearing_deg)
    body_radial = math.hypot(p.body_x, p.body_y)
    base_radial = body_radial + p.glyph_radius + palette.LABEL_RADIAL_PADDING_PX
    tier_offset = p.tier * palette.LABEL_TIER_RADIAL_OFFSET_PX
    target_radial = base_radial + tier_offset
    if apply_rose_clearance:
        min_radial = palette.BEARING_ROSE_OUTER_PX + palette.LABEL_BEARING_ROSE_CLEARANCE
        target_radial = max(target_radial, min_radial + tier_offset)
    p.anchor_x = target_radial * math.cos(bearing_rad)
    p.anchor_y = -target_radial * math.sin(bearing_rad)
    p.text_anchor = _text_anchor_for_bearing(p.bearing_deg)


# ---------------------------------------------------------------------------
# ADR-094 strategy dispatch — SVG handlers (textpath / radial / callout)
# ---------------------------------------------------------------------------


def _emit_textpath_label(d: LabelDecision, viewport: _Viewport) -> svgwrite.base.BaseElement:
    """Emit a textPath label per ADR-094 textpath strategy.

    Mirrors `_engraved_label_textpath`'s register-driven styling but
    consumes a `LabelDecision` (so the strategy dispatch can call it
    without re-resolving the path).
    """
    assert d.textpath_path_id is not None
    decorated = f"— {d.text} —"
    register = d.register
    if register == "prose":
        font_family = palette.LABEL_PROSE_FONT
        font_size: int = palette.LABEL_PROSE_FONT_SIZE
        font_style: str | None = "italic"
        font_weight: int | None = None
        opacity: float | None = palette.LABEL_PROSE_OPACITY
        letter_spacing: int | None = None
    elif register == "chalk":
        font_family = palette.LABEL_CHALK_FONT
        font_size = 11
        font_style = None
        font_weight = palette.LABEL_CHALK_WEIGHT
        opacity = palette.ORBIT_OPACITY_CHALK
        letter_spacing = palette.LABEL_CHALK_LETTER_SPACING
    else:
        font_family = palette.LABEL_ENGRAVED_FONT
        font_size = 12
        font_style = "italic"
        font_weight = palette.LABEL_ENGRAVED_WEIGHT
        opacity = None
        letter_spacing = palette.LABEL_ENGRAVED_LETTER_SPACING

    elem = svgwrite.text.Text(
        "",
        fill=palette.BRASS,
        font_family=font_family,
        font_size=font_size,
        text_anchor="middle",
    )
    elem["stroke"] = palette.BG
    elem["stroke-width"] = 3
    elem["stroke-linejoin"] = "round"
    elem["paint-order"] = "stroke"
    if font_style is not None:
        elem["font-style"] = font_style
    if font_weight is not None:
        elem["font-weight"] = font_weight
    if letter_spacing is not None:
        elem["letter-spacing"] = letter_spacing
    if opacity is not None:
        elem["opacity"] = opacity
    tp = svgwrite.text.TextPath(path=f"#{d.textpath_path_id}", text=decorated)
    tp["startOffset"] = "50%"
    elem.add(tp)
    return elem


def _emit_radial_label(
    d: LabelDecision, p: _BodyPlacement, viewport: _Viewport
) -> svgwrite.base.BaseElement:
    """Emit a radial-out body label at p.anchor_x/y.

    Mirrors `_label_text` styling but driven by the strategy decision so
    the dispatch loop is the single emission path.
    """
    register = d.register
    if register == "prose":
        font_family = palette.LABEL_PROSE_FONT
        font_size: int = palette.LABEL_PROSE_FONT_SIZE
        font_weight: int | None = None
        font_style: str | None = "italic"
        letter_spacing: int | None = None
        opacity: float | None = palette.LABEL_PROSE_OPACITY
    elif register == "chalk":
        font_family = palette.LABEL_CHALK_FONT
        font_size = 10
        font_weight = palette.LABEL_CHALK_WEIGHT
        font_style = None
        letter_spacing = palette.LABEL_CHALK_LETTER_SPACING
        opacity = None
    else:
        font_family = palette.LABEL_ENGRAVED_FONT
        font_size = 10
        font_weight = palette.LABEL_ENGRAVED_WEIGHT
        font_style = None
        letter_spacing = palette.LABEL_ENGRAVED_LETTER_SPACING
        opacity = None

    fill = p.body.label_color or palette.BRASS
    elem = svgwrite.text.Text(
        d.text,
        x=[p.anchor_x],
        y=[p.anchor_y],
        fill=fill,
        font_family=font_family,
        font_size=font_size,
        text_anchor=p.text_anchor,
    )
    elem["stroke"] = palette.BG
    elem["stroke-width"] = 3
    elem["stroke-linejoin"] = "round"
    elem["paint-order"] = "stroke"
    if font_weight is not None:
        elem["font-weight"] = font_weight
    if font_style is not None:
        elem["font-style"] = font_style
    if letter_spacing is not None:
        elem["letter-spacing"] = letter_spacing
    if opacity is not None:
        elem["opacity"] = opacity
    elem["class"] = "radial-label"
    return elem


def _emit_callout_block(
    block: CalloutBlock, orbits: OrbitsConfig, viewport: _Viewport
) -> svgwrite.base.BaseElement:
    """Emit a callout block: leader line(s) + label rect/border + text lines.

    Singleton: title + optional tag.
    Grouped:   '<PARENT_LABEL> SYSTEM' title + bordered rect + per-member lines.
    """
    g = svgwrite.container.Group()
    g["class"] = "callout-block"
    if block.side == "inset":
        g["data-inset"] = "true"

    is_grouped = len(block.members) >= palette.CALLOUT_GROUP_MIN_MEMBERS

    # Leader stroke color: derive from first member's register.
    register = block.members[0].register
    if register == "prose":
        leader_color = palette.DIM
    elif register == "chalk":
        leader_color = palette.PARTY
    else:
        leader_color = palette.BRASS

    # --- Leader line(s) ---
    # Block edge nearest the bend.
    if block.anchor_x < block.block_x:
        edge_x = block.block_x
    else:
        edge_x = block.block_x + block.block_width_px
    edge_y = block.block_y + block.block_height_px / 2.0

    leader_origin = (block.anchor_x, block.anchor_y)
    bend_x = edge_x
    bend_y = leader_origin[1]
    path_d = f"M {leader_origin[0]} {leader_origin[1]} L {bend_x} {bend_y} L {edge_x} {edge_y}"
    leader = svgwrite.path.Path(d=path_d, fill="none", stroke=leader_color)
    leader["stroke-width"] = palette.LEADER_STROKE_WIDTH_PX
    leader["class"] = "callout-leader"
    g.add(leader)

    ts = palette.LEADER_TERMINATOR_SIZE_PX
    term = svgwrite.shapes.Rect(
        insert=(edge_x - ts / 2.0, edge_y - ts / 2.0),
        size=(ts, ts),
        fill=leader_color,
    )
    term["class"] = "callout-terminator"
    g.add(term)

    # --- Label block content ---
    pad = palette.CALLOUT_BLOCK_PADDING_PX
    text_x = block.block_x + pad

    if is_grouped:
        # Border rect.
        border = svgwrite.shapes.Rect(
            insert=(block.block_x, block.block_y),
            size=(block.block_width_px, block.block_height_px),
            fill="none",
            stroke=leader_color,
        )
        border["stroke-width"] = palette.CALLOUT_GROUP_BORDER_PX
        border["class"] = "callout-group-border"
        g.add(border)

        # Title: "<PARENT_LABEL> SYSTEM"
        parent_id = block.parent_label  # parent_id stashed on block.parent_label
        parent_body = orbits.bodies.get(parent_id) if parent_id else None
        parent_label_text = (
            (parent_body.label if parent_body and parent_body.label else (parent_id or ""))
            .strip()
            .upper()
        )
        title = svgwrite.text.Text(
            f"{parent_label_text} SYSTEM",
            x=[text_x],
            y=[block.block_y + pad + palette.CALLOUT_GROUP_TITLE_HEIGHT_PX * 0.75],
            fill=leader_color,
            font_family=palette.LABEL_ENGRAVED_FONT,
            font_size=11,
        )
        title["class"] = "callout-group-title"
        g.add(title)

        # One line per member.
        line_y = block.block_y + pad + palette.CALLOUT_GROUP_TITLE_HEIGHT_PX
        for m in block.members:
            line_y += palette.CALLOUT_BLOCK_LINE_HEIGHT_PX
            body = orbits.bodies.get(m.body_id)
            distance_label = ""
            if body and body.semi_major_au is not None:
                if body.semi_major_au >= 0.01:
                    distance_label = f"{body.semi_major_au:.2f} AU"
                else:
                    km = body.semi_major_au * 1.496e8
                    distance_label = f"{km / 1e6:.2f}M km"
            line_text = f"{m.text} · {distance_label}" if distance_label else m.text
            line = svgwrite.text.Text(
                line_text,
                x=[text_x],
                y=[line_y],
                fill=leader_color,
                font_family=palette.LABEL_ENGRAVED_FONT,
                font_size=10,
            )
            line["class"] = "callout-group-member"
            g.add(line)
    else:
        # Singleton: title + optional tag.
        m = block.members[0]
        title = svgwrite.text.Text(
            m.text,
            x=[text_x],
            y=[block.block_y + pad + palette.CALLOUT_BLOCK_LINE_HEIGHT_PX * 0.75],
            fill=leader_color,
            font_family=palette.LABEL_ENGRAVED_FONT,
            font_size=11,
        )
        title["class"] = "callout-singleton-title"
        g.add(title)
        if m.callout_tag:
            tag_y = (
                block.block_y
                + pad
                + palette.CALLOUT_BLOCK_LINE_HEIGHT_PX
                + palette.CALLOUT_BLOCK_TAG_LINE_HEIGHT_PX * 0.75
            )
            tag = svgwrite.text.Text(
                m.callout_tag,
                x=[text_x],
                y=[tag_y],
                fill=palette.DIM,
                font_family=palette.LABEL_PROSE_FONT,
                font_size=palette.LABEL_PROSE_FONT_SIZE,
            )
            tag["font-style"] = "italic"
            tag["class"] = "callout-singleton-tag"
            g.add(tag)

    return g


# ---------------------------------------------------------------------------
# Engraved layer rendering — orbits, glyphs, moon band, labels
# ---------------------------------------------------------------------------


def _glyph_default_fill(body_type: BodyType) -> str:
    """Default (non-hazard) fill color for a body type."""
    if body_type in (BodyType.STAR, BodyType.COMPANION):
        return palette.RED
    if body_type == BodyType.WRECK:
        return palette.DIM
    return palette.BRASS


def _glyph_visual_radius(body: BodyDef) -> float:
    """Approximate radial extent of the body's glyph (for label padding)."""
    if body.type == BodyType.STAR:
        return palette.STAR_RETICLE_OUTER_R
    if body.type == BodyType.COMPANION:
        return 6.0
    if body.type == BodyType.HABITAT and body.subtype == "gas_giant":
        return 10.0
    if body.type == BodyType.HABITAT:
        return 5.0
    if body.type == BodyType.GATE:
        return 6.0
    if body.type == BodyType.WRECK:
        return 5.0
    if body.type == BodyType.ARC_BELT:
        return 1.0
    return 5.0


def _body_glyph(body: BodyDef, *, x: float, y: float, body_id: str) -> svgwrite.base.BaseElement:
    """Pick the glyph for a body type. Honors hazard + subtype.

    Hazard semantic: red fill PLUS dashed glyph outline (AC #11 non-color signal).
    """
    fill = palette.RED if body.hazard else _glyph_default_fill(body.type)

    if body.type == BodyType.STAR:
        elem: svgwrite.base.BaseElement = _star_reticle(x, y)
    elif body.type == BodyType.COMPANION:
        circ = svgwrite.shapes.Circle(center=(x, y), r=6, fill=palette.RED)
        if body.hazard:
            circ["stroke"] = palette.BRASS
            circ["stroke-dasharray"] = palette.HAZARD_GLYPH_DASH
            circ["stroke-width"] = palette.HAZARD_GLYPH_STROKE_WIDTH
        elem = circ
    elif body.type == BodyType.HABITAT:
        if body.subtype == "gas_giant":
            group = svgwrite.container.Group()
            r = 10
            disk = svgwrite.shapes.Circle(center=(x, y), r=r, fill=fill)
            if body.hazard:
                disk["stroke"] = palette.BRASS
                disk["stroke-dasharray"] = palette.HAZARD_GLYPH_DASH
                disk["stroke-width"] = palette.HAZARD_GLYPH_STROKE_WIDTH
            group.add(disk)
            group.add(_gas_giant_overlay(x, y, body_radius=r))
            elem = group
        else:
            elem = _habitat_glyph(x, y, fill=fill, hazard=body.hazard)
    elif body.type == BodyType.ARC_BELT:
        # Belt-as-glyph fallback (engraved layer special-cases ARC_BELT to
        # render as a dotted arc on the orbit).
        elem = svgwrite.shapes.Circle(center=(x, y), r=2, fill=palette.BRASS)
    elif body.type == BodyType.GATE:
        elem = _gate_glyph(x, y, fill=fill, hazard=body.hazard)
    elif body.type == BodyType.WRECK:
        elem = _wreck_glyph(x, y)
    else:
        raise ValueError(f"unknown BodyType for body {body_id!r}: {body.type!r}")

    return _attach_body_id(elem, body_id)


def _moon_dot_glyph(body: BodyDef, x: float, y: float, body_id: str) -> svgwrite.base.BaseElement:
    """A small moon dot for system-scope moon-band rendering. Hazard moons
    get red color + dashed-outline non-color signal (AC #11)."""
    fill = palette.RED if body.hazard else palette.BRASS
    dot = svgwrite.shapes.Circle(center=(x, y), r=palette.MOON_DOT_R, fill=fill)
    if body.hazard:
        dot["stroke"] = palette.BRASS
        dot["stroke-dasharray"] = palette.HAZARD_GLYPH_DASH
        dot["stroke-width"] = palette.HAZARD_GLYPH_STROKE_WIDTH
    return _attach_body_id(dot, body_id)


def _allocated_moon_radii(
    moons: list[tuple[str, BodyDef]],
) -> dict[str, float]:
    """Per-moon system-scope band radii. Authorial overrides via
    `moon_display_radius_px` win; the rest auto-allocate by ascending
    `semi_major_au` starting at MOON_BAND_INNER_PX with MOON_BAND_STEP_PX
    spacing."""
    pinned: dict[str, float] = {
        bid: float(b.moon_display_radius_px)
        for bid, b in moons
        if b.moon_display_radius_px is not None
    }
    auto: list[tuple[str, BodyDef]] = [
        (bid, b) for bid, b in moons if b.moon_display_radius_px is None
    ]
    # Auto-allocate by ascending real semi_major_au
    auto.sort(key=lambda kv: kv[1].semi_major_au or 0.0)
    radii = dict(pinned)
    # `used` is a membership set — we only check `next_auto in used` to
    # skip slots already pinned. Sort order is irrelevant.
    used: set[float] = set(pinned.values())
    next_auto = palette.MOON_BAND_INNER_PX
    for bid, _b in auto:
        # Skip past any pinned radii that overlap
        while next_auto in used:
            next_auto += palette.MOON_BAND_STEP_PX
        radii[bid] = float(next_auto)
        used.add(float(next_auto))
        next_auto += palette.MOON_BAND_STEP_PX
    return radii


def _render_moon_band(
    parent_id: str,
    parent_x: float,
    parent_y: float,
    orbits: OrbitsConfig,
    t_hours: float,
    stats: _RenderStats,
) -> tuple[svgwrite.container.Group | None, list[_BodyPlacement]]:
    """Render moons of `parent_id` as a moon band at system-scope.

    Returns the moon-band group (or None if no visible moons / overflow
    beyond MOON_BAND_MAX) and a list of placements for forced-callout
    strategy candidates (ADR-094 §9 — any moon-band child with a
    non-empty `label:` is forced into the callout strategy because its
    sub-pixel render position has no radial space).
    """
    moons: list[tuple[str, BodyDef]] = [
        (bid, b)
        for bid, b in orbits.bodies.items()
        if b.parent == parent_id and b.show_at_system_scope
    ]
    if not moons:
        return None, []
    if len(moons) > palette.MOON_BAND_MAX:
        # Overflow: fall back to +N glyph upstream.
        return None, []

    radii = _allocated_moon_radii(moons)
    g = svgwrite.container.Group()
    g.attribs["class"] = "moon-band"
    g.attribs["data-parent-id"] = parent_id

    placements: list[_BodyPlacement] = []
    for body_id, body in moons:
        r_px = radii[body_id]
        # Concentric dashed orbit ellipse around the parent
        ring = svgwrite.shapes.Circle(
            center=(parent_x, parent_y),
            r=r_px,
            fill="none",
            stroke=palette.BRASS,
            stroke_width=0.4,
        )
        ring["stroke-dasharray"] = "2 3"
        ring["stroke-opacity"] = 0.6
        g.add(ring)
        # Moon dot at the moon's own epoch_phase_deg
        phase = body.epoch_phase_deg or 0.0
        rad = math.radians(phase)
        mx = parent_x + r_px * math.cos(rad)
        my = parent_y - r_px * math.sin(rad)
        g.add(_moon_dot_glyph(body, mx, my, body_id))
        # Forced-callout surfacing: any moon-band child with a non-empty
        # `label:` becomes a candidate for the strategy pass per ADR-094 §9.
        # Children without `label:` remain unlabeled (existing behavior).
        bearing = _bearing_from_center(mx, my)
        placements.append(
            _BodyPlacement(
                body_id=body_id,
                body=body,
                body_x=mx,
                body_y=my,
                glyph_radius=palette.MOON_DOT_R,
                bearing_deg=bearing,
                tier=0,
                anchor_x=0.0,
                anchor_y=0.0,
                text_anchor="start",
            )
        )
        stats.body_count_moons_rendered += 1
    return g, placements


def _accumulate_register(stats: _RenderStats, body: BodyDef, *, is_moon: bool) -> None:
    """Bump the appropriate register count. Moons count as prose
    (per §4.6: 'register: prose auto-applied' on system-scope moons).
    """
    if is_moon:
        stats.body_count_prose += 1
        return
    if body.register == "chalk":
        stats.body_count_chalk += 1
    elif body.register == "prose":
        stats.body_count_prose += 1
    else:
        stats.body_count_engraved += 1


def _render_engraved_layer(
    orbits: OrbitsConfig,
    chart: ChartConfig,
    center_id: str,
    scope: Scope,
    vp: _Viewport,
    t_hours: float,
    suppressed_label_ids: set[str] | None = None,
) -> tuple[svgwrite.container.Group, _RenderStats, GutterLayout, list[LabelDecision]]:
    g = svgwrite.container.Group(id="layer-engraved")
    stats = _RenderStats()
    center = orbits.bodies[center_id]
    scope_is_root = scope.is_system_root

    # Bearing rose at chart center (system_root only)
    if scope_is_root:
        g.add(_render_bearing_rose())

    # Drill-out edge label (when centered on a non-root)
    if center.parent is not None:
        if center.parent not in orbits.bodies:
            raise ValueError(f"center {center_id!r} has parent {center.parent!r} not in bodies")
        parent = orbits.bodies[center.parent]
        parent_label = parent.label or center.parent.upper()
        edge = svgwrite.container.Group()
        edge.attribs["data-action"] = "drill_out"
        edge.attribs["data-parent-id"] = center.parent
        edge.add(
            _haloed_text(
                f"← {parent_label} SYSTEM",
                insert=(-vp.half + 20, 0),
                fill=palette.BRASS,
                font_size=10,
            )
        )
        g.add(edge)

    # Center body
    g.add(_body_glyph(center, x=0, y=0, body_id=center_id))
    if center.label:
        # Star center: label inside reticle (per spec §4.3)
        if center.type == BodyType.STAR:
            g.add(
                _haloed_text(
                    center.label,
                    insert=(0, -palette.STAR_RETICLE_OUTER_R - 8),
                    fill=palette.RED,
                    text_anchor="middle",
                    font_size=11,
                    font_weight=700,
                )
            )
        else:
            g.add(
                _haloed_text(
                    center.label,
                    insert=(0, -22),
                    fill=center.label_color or palette.BRASS,
                    text_anchor="middle",
                    font_size=14,
                )
            )
    # Note: the center body is intentionally NOT counted in the register
    # body counts. Register drives orbit + label styling — the center has no
    # orbit at this scope, so its register is meaningless. The OTEL counts
    # describe the bodies *visible as orbiting children*, which is what the
    # GM panel cares about ("how many chalk bodies in this view?").

    # Phase 1: collect direct children + their positions
    direct_children: list[tuple[str, BodyDef]] = [
        (bid, b) for bid, b in orbits.bodies.items() if b.parent == center_id
    ]
    drillable_ids = _drillable_body_ids(orbits)

    placements: list[_BodyPlacement] = []
    arc_belt_placements: list[_BodyPlacement] = []  # arc-belts handled separately for orbits

    for body_id, body in direct_children:
        if body.type == BodyType.ARC_BELT:
            assert body.semi_major_au is not None
            assert body.epoch_phase_deg is not None
            assert body.arc_extent_deg is not None
            mid_deg = body.epoch_phase_deg + body.arc_extent_deg / 2
            mx, my = _polar_to_cartesian(body.semi_major_au, mid_deg, vp.au_to_px)
            arc_belt_placements.append(
                _BodyPlacement(
                    body_id=body_id,
                    body=body,
                    body_x=mx,
                    body_y=my,
                    glyph_radius=1.0,
                    bearing_deg=_bearing_from_center(mx, my),
                    tier=0,
                    anchor_x=0.0,
                    anchor_y=0.0,
                    text_anchor="start",
                )
            )
        else:
            au, theta = _body_position_au_polar(body, t_hours)
            x, y = _polar_to_cartesian(au, theta, vp.au_to_px)
            placements.append(
                _BodyPlacement(
                    body_id=body_id,
                    body=body,
                    body_x=x,
                    body_y=y,
                    glyph_radius=_glyph_visual_radius(body),
                    bearing_deg=_bearing_from_center(x, y),
                    tier=0,
                    anchor_x=0.0,
                    anchor_y=0.0,
                    text_anchor="start",
                )
            )

    # Phase 2: render arc-belt orbits/labels
    for p in arc_belt_placements:
        body = p.body
        radius_px = (body.semi_major_au or 0.0) * vp.au_to_px
        arc = _dotted_arc(
            cx=0,
            cy=0,
            radius=radius_px,
            from_deg=body.epoch_phase_deg or 0.0,
            extent_deg=body.arc_extent_deg or 0.0,
            color=palette.RED if body.hazard else palette.BRASS,
        )
        arc.attribs["data-body-id"] = p.body_id
        g.add(arc)
        _accumulate_register(stats, body, is_moon=False)

    # Phase 3: render direct-child orbits + glyphs (and possibly moon bands)
    moon_band_placements: list[_BodyPlacement] = []
    # ADR-094: labeled moon-band children are forced into the callout strategy
    # (sub-pixel render position has no radial space).
    forced_callout_placements: list[_BodyPlacement] = []
    parents_with_visible_moons: set[str] = {
        p.body_id
        for p in placements
        if p.body_id in drillable_ids
        and any(b.parent == p.body_id and b.show_at_system_scope for b in orbits.bodies.values())
        and sum(
            1 for b in orbits.bodies.values() if b.parent == p.body_id and b.show_at_system_scope
        )
        <= palette.MOON_BAND_MAX
    }

    for p in placements:
        body = p.body
        ell = ellipse_geometry(body, vp.au_to_px)
        orbit_attrs = _orbit_stroke_attrs(body.register)
        ellipse = svgwrite.shapes.Ellipse(
            center=(ell.center_x_px, ell.center_y_px),
            r=(ell.semi_major_px, ell.semi_minor_px),
            fill="none",
            **orbit_attrs,
        )
        if body.register == "chalk":
            ellipse["stroke-dasharray"] = palette.ORBIT_DASH_CHALK
        g.add(_attach_body_id(ellipse, p.body_id))

        renders_moon_band = scope_is_root and p.body_id in parents_with_visible_moons

        if p.body_id in drillable_ids and not renders_moon_band:
            # Legacy +N drillable cluster — used only when moons aren't being
            # rendered directly (overflow, drill-in scope, or no children
            # visible at system scope).
            cluster = svgwrite.container.Group()
            cluster.attribs["data-action"] = f"drill_in:{p.body_id}"
            cluster.attribs["data-body-id"] = p.body_id
            cluster.attribs["class"] = "drillable"
            cluster.add(
                svgwrite.shapes.Circle(
                    center=(p.body_x, p.body_y),
                    r=12,
                    fill="none",
                    stroke=palette.BRASS,
                    stroke_dasharray="2,2",
                    stroke_width=0.6,
                )
            )
            cluster.add(_body_glyph(body, x=p.body_x, y=p.body_y, body_id=p.body_id))
            child_count = sum(1 for c in orbits.bodies.values() if c.parent == p.body_id)
            cluster.add(
                _haloed_text(
                    f"+{child_count}",
                    insert=(p.body_x + 16, p.body_y + 4),
                    fill=palette.BRASS,
                    font_size=8,
                )
            )
            g.add(cluster)
        else:
            # Direct body glyph (+ moon band if applicable). For drillable
            # bodies, the body+band group carries the drill_in affordance
            # so the click target replaces the legacy +N chip per spec §4.6
            # ("the affordance becomes 'click anywhere in the moon system'").
            if p.body_id in drillable_ids:
                wrapper = svgwrite.container.Group()
                wrapper.attribs["data-action"] = f"drill_in:{p.body_id}"
                wrapper.attribs["data-body-id"] = p.body_id
                wrapper.attribs["class"] = "drillable"
                wrapper.add(_body_glyph(body, x=p.body_x, y=p.body_y, body_id=p.body_id))
                if renders_moon_band:
                    moon_band, child_placements = _render_moon_band(
                        p.body_id, p.body_x, p.body_y, orbits, t_hours, stats
                    )
                    if moon_band is not None:
                        wrapper.add(moon_band)
                        moon_band_placements.extend(child_placements)
                        # ADR-094 §9: surface labeled moon-band children to
                        # the forced-callout list for the strategy pass.
                        for cp in child_placements:
                            if cp.body.label and cp.body.label.strip():
                                forced_callout_placements.append(cp)
                g.add(wrapper)
            else:
                g.add(_body_glyph(body, x=p.body_x, y=p.body_y, body_id=p.body_id))
        _accumulate_register(stats, body, is_moon=False)

    # Phase 4: collision-tier assignment (direct children + arc-belts only).
    # Moon-band children are forced into callouts (ADR-094 §9), so they don't
    # need radial-out anchor resolution.
    radial_candidates = placements + arc_belt_placements
    max_tier = _assign_collision_tiers(radial_candidates)
    stats.label_collision_tier_max = max_tier

    # Phase 5: anchor resolution for radial candidates.
    # Bearing-rose clearance applies only at system_root scope.
    for p in radial_candidates:
        _resolve_anchor(p, apply_rose_clearance=scope_is_root)

    suppressed_ids = suppressed_label_ids or set()

    # Account moon-band register stats — moons render as prose at system scope.
    for p in moon_band_placements:
        _accumulate_register(stats, p.body, is_moon=True)

    # Phase 6: ADR-094 strategy dispatch.
    # Build per-body inputs from chart annotations + placements.
    callout_label_by_body: dict[str, Annotation] = {}
    for annot in chart.annotations:
        if annot.kind == "callout_label" and annot.body_ref:
            callout_label_by_body[annot.body_ref] = annot

    textpath_by_body: dict[str, tuple[str, float]] = {}
    for annot in chart.annotations:
        if annot.kind != "engraved_label" or annot.curve_along is None:
            continue
        try:
            path_id, _path_d, resolved_body_id, circumference = _resolve_curve_along(
                annot.curve_along, orbits, center_id, vp
            )
        except (_CurveScopeMismatch, ValueError):
            continue
        textpath_by_body[resolved_body_id] = (path_id, circumference)

    strategy_inputs: list[_StrategyInput] = []
    anchor_by_id: dict[str, tuple[float, float, float]] = {}
    semi_major_by_id: dict[str, float] = {}
    placement_by_body: dict[str, _BodyPlacement] = {}

    def _build_input(p: _BodyPlacement, *, is_moon_band_child: bool) -> _StrategyInput | None:
        if p.body.label is None or not p.body.label.strip():
            return None
        # Suppression: arc_belt + textpath annotation pairs already handled
        # via flavor layer; skip dispatch so we don't double-render.
        if p.body_id in suppressed_ids:
            return None
        body = p.body
        if is_moon_band_child and body.label_register is None:
            register: Register = "prose"
        else:
            register = body.label_register or body.register
        text = body.label.strip()
        text_w = estimate_text_width_px(text, register)
        cl = callout_label_by_body.get(p.body_id)
        tp = textpath_by_body.get(p.body_id)
        arc_to_neighbor = (
            None if is_moon_band_child else _arc_to_neighbor_for_placement(p, radial_candidates)
        )
        return _StrategyInput(
            body_id=p.body_id,
            parent_id=body.parent,
            parent_type=(orbits.bodies[body.parent].type.value if body.parent else None),
            text=text,
            register=register,
            text_width_px=text_w,
            is_moon_band_child=is_moon_band_child,
            callout_label_annotation=cl,
            textpath_path_id=tp[0] if tp else None,
            path_circumference_px=tp[1] if tp else None,
            arc_to_neighbor_px=arc_to_neighbor,
            radial_tier=p.tier,
            anchor_x=p.anchor_x,
            anchor_y=p.anchor_y,
            anchor_bearing_deg=p.bearing_deg,
            callout_tag=cl.tag if cl else None,
        )

    for p in radial_candidates:
        inp = _build_input(p, is_moon_band_child=False)
        if inp is None:
            continue
        strategy_inputs.append(inp)
        anchor_by_id[p.body_id] = (p.anchor_x, p.anchor_y, p.bearing_deg)
        placement_by_body[p.body_id] = p
        if p.body.semi_major_au is not None:
            semi_major_by_id[p.body_id] = p.body.semi_major_au

    for p in forced_callout_placements:
        inp = _build_input(p, is_moon_band_child=True)
        if inp is None:
            continue
        strategy_inputs.append(inp)
        # Anchor for moon-band child is the moon's dot position.
        anchor_by_id[p.body_id] = (p.body_x, p.body_y, p.bearing_deg)
        placement_by_body[p.body_id] = p
        if p.body.semi_major_au is not None:
            semi_major_by_id[p.body_id] = p.body.semi_major_au

    decisions = select_label_strategies(inputs=strategy_inputs)

    # Per-body OTEL spans.
    for d in decisions:
        emit_chart_label_strategy(
            body_id=d.body_id,
            parent_id=d.parent_id,
            parent_type=d.parent_type,
            strategy_chosen=d.strategy.value,
            selection_reason=d.reason.value,
            tier=d.radial_tier,
            arc_available_px=d.arc_available_px,
            text_width_px=d.text_width_px,
            path_circumference_px=d.path_circumference_px,
        )

    # Gutter layout for callout decisions.
    gutter = lay_out_gutter(
        decisions=list(decisions),
        anchor_by_id=anchor_by_id,
        semi_major_by_id=semi_major_by_id,
        viewport=vp,
    )

    gutter_blocks_by_body: dict[str, CalloutBlock] = {}
    for blk in gutter.blocks:
        for m in blk.members:
            gutter_blocks_by_body[m.body_id] = blk

    emitted_blocks: set[int] = set()
    for d in decisions:
        if d.strategy == LabelStrategy.TEXTPATH:
            g.add(_emit_textpath_label(d, vp))
        elif d.strategy == LabelStrategy.RADIAL:
            p = placement_by_body[d.body_id]
            g.add(_emit_radial_label(d, p, vp))
        elif d.strategy == LabelStrategy.CALLOUT:
            block = gutter_blocks_by_body.get(d.body_id)
            if block is None:
                continue
            block_key = id(block)
            if block_key in emitted_blocks:
                continue
            emitted_blocks.add(block_key)
            g.add(_emit_callout_block(block, orbits, vp))

    return g, stats, gutter, list(decisions)


# ---------------------------------------------------------------------------
# Flavor layer (chart.yaml annotations)
# ---------------------------------------------------------------------------


def _render_flavor_layer(
    chart: ChartConfig,
    orbits: OrbitsConfig,
    center_id: str,
    vp: _Viewport,
    t_hours: float,
) -> svgwrite.container.Group:
    g = svgwrite.container.Group(id="layer-flavor")
    # Collect path defs for textPath references.
    defs = svgwrite.container.Defs()
    defs_used = False
    for annot in chart.annotations:
        result = _render_annotation(annot, orbits, center_id, vp, t_hours)
        if result is None:
            continue
        elem, path_def = result
        if path_def is not None:
            path_id, path_d = path_def
            path = svgwrite.path.Path(d=path_d, id=path_id, fill="none", stroke="none")
            defs.add(path)
            defs_used = True
        g.add(elem)
    if defs_used:
        # Insert defs at the front of the flavor group so textPath refs resolve.
        g.elements.insert(0, defs)
    return g


def _render_annotation(
    annot: Annotation,
    orbits: OrbitsConfig,
    center_id: str,
    vp: _Viewport,
    t_hours: float,
) -> tuple[svgwrite.base.BaseElement, tuple[str, str] | None] | None:
    """Render a single annotation. Returns (element, optional path-def for textPath)."""
    if annot.kind == "callout_label":
        # ADR-094 — callout_label annotations are consumed by the engraved
        # layer's strategy dispatch (force-callout override). Skip here so we
        # don't duplicate the SVG output; the strategy dispatch is the single
        # rendering path.
        return None
    if annot.kind == "engraved_label":
        if annot.text is None:
            return None
        if annot.curve_along is not None:
            try:
                path_id, path_d, body_id, _ = _resolve_curve_along(
                    annot.curve_along, orbits, center_id, vp
                )
            except _CurveScopeMismatch:
                # Annotation references a body not visible at this scope.
                # Silent skip is correct — there is no orbit to attach the
                # textPath to. (Truly unknown bodies still raise upstream.)
                return None
            # Inherit the resolved body's effective label_register so the
            # textPath styling matches the body it represents (AC #6 — last
            # drift's textPath renders in prose register, not Orbitron).
            register = _effective_label_register(orbits.bodies[body_id])
            return (
                _engraved_label_textpath(annot.text, path_id=path_id, register=register),
                (path_id, path_d),
            )
        # Fallback: fixed top-center placement (backward compat).
        return (
            _haloed_text(
                annot.text,
                insert=(0, -vp.half + 30),
                fill=palette.BRASS,
                text_anchor="middle",
                font_size=12,
                font_style="italic",
            ),
            None,
        )
    if annot.kind == "glyph":
        if annot.text is None or annot.at is None:
            return None
        ra = float(annot.at.get("ra_deg", 0))
        au = float(annot.at.get("au", 0))
        x, y = _polar_to_cartesian(au, ra, vp.au_to_px)
        group = svgwrite.container.Group()
        group.add(
            _haloed_text(
                annot.text,
                insert=(x, y),
                fill=palette.BRASS,
                text_anchor="middle",
                font_size=20,
            )
        )
        if annot.caption:
            group.add(
                _haloed_text(
                    annot.caption,
                    insert=(x, y + 14),
                    fill=palette.BRASS,
                    text_anchor="middle",
                    font_size=9,
                    font_style="italic",
                )
            )
        return (group, None)
    if annot.kind == "scale_ruler":
        if annot.label is None:
            return None
        return (
            _haloed_text(
                annot.label,
                insert=(0, vp.half - 20),
                fill=palette.BRASS,
                text_anchor="middle",
                font_size=9,
            ),
            None,
        )
    if annot.kind == "bearing_marks":
        bearings = annot.bearings or [0.0, 90.0, 180.0, 270.0]
        group = svgwrite.container.Group()
        for theta_deg in bearings:
            x, y = _polar_to_cartesian(au=0.05, theta_deg=theta_deg, scale=vp.au_to_px)
            group.add(
                _haloed_text(
                    f"{int(theta_deg):03d}°",
                    insert=(x, y),
                    fill=palette.DIM,
                    text_anchor="middle",
                    font_family=palette.FONT_NUMERIC,
                    font_size=7,
                )
            )
        return (group, None)
    if annot.kind == "anomaly_marker":
        if annot.at is None:
            return None
        ra = float(annot.at.get("ra_deg", 0))
        au = float(annot.at.get("au", 0))
        x, y = _polar_to_cartesian(au, ra, vp.au_to_px)
        r = 8
        pts = [
            (x, y - r),
            (x + r * 0.866, y - r * 0.5),
            (x + r * 0.866, y + r * 0.5),
            (x, y + r),
            (x - r * 0.866, y + r * 0.5),
            (x - r * 0.866, y - r * 0.5),
        ]
        group = svgwrite.container.Group()
        group.add(
            svgwrite.shapes.Polygon(
                points=pts, fill=palette.BG, stroke=palette.RED, stroke_width=1.2
            )
        )
        if annot.text:
            group.add(
                _haloed_text(
                    annot.text,
                    insert=(x, y + 4),
                    fill=palette.RED,
                    text_anchor="middle",
                    font_size=10,
                )
            )
        if annot.caption:
            group.add(
                _haloed_text(
                    annot.caption,
                    insert=(x, y + r + 12),
                    fill=palette.RED,
                    text_anchor="middle",
                    font_size=9,
                    font_style="italic",
                )
            )
        return (group, None)
    if annot.kind == "lagrange_point":
        if annot.at is None:
            return None
        ra = float(annot.at.get("ra_deg", 0))
        au = float(annot.at.get("au", 0))
        x, y = _polar_to_cartesian(au, ra, vp.au_to_px)
        r = 4
        pts = [(x, y - r), (x - r * 0.866, y + r * 0.5), (x + r * 0.866, y + r * 0.5)]
        group = svgwrite.container.Group()
        group.add(
            svgwrite.shapes.Polygon(
                points=pts, fill=palette.BG, stroke=palette.BRASS, stroke_width=0.8
            )
        )
        if annot.label:
            group.add(
                _haloed_text(
                    annot.label,
                    insert=(x + 6, y + 3),
                    fill=palette.BRASS,
                    font_family=palette.FONT_NUMERIC,
                    font_size=8,
                )
            )
        return (group, None)
    if annot.kind == "flight_corridor":
        if annot.at is None:
            return None
        try:
            x1, y1 = _polar_to_cartesian(
                float(annot.at["from_au"]),
                float(annot.at["from_ra_deg"]),
                vp.au_to_px,
            )
            x2, y2 = _polar_to_cartesian(
                float(annot.at["to_au"]),
                float(annot.at["to_ra_deg"]),
                vp.au_to_px,
            )
        except KeyError as e:
            raise ValueError(
                f"flight_corridor annotation requires from_ra_deg/from_au/"
                f"to_ra_deg/to_au in `at`; missing {e.args[0]!r}"
            ) from e
        line = svgwrite.shapes.Line(
            start=(x1, y1), end=(x2, y2), stroke=palette.DIM, stroke_width=0.8
        )
        line["stroke-dasharray"] = "4,4"
        return (line, None)
    raise ValueError(
        f"renderer has no handler for annotation kind {annot.kind!r}; "
        f"either the model validator is out of sync with KNOWN_ANNOTATION_KINDS "
        f"or this code is missing a branch."
    )


# ---------------------------------------------------------------------------
# Party layer (unchanged from prior)
# ---------------------------------------------------------------------------


def _render_party_layer(
    orbits: OrbitsConfig,
    center_id: str,
    vp: _Viewport,
    t_hours: float,
    party_at: str | None,
) -> svgwrite.container.Group:
    g = svgwrite.container.Group(id="layer-party")
    if party_at is None or party_at not in orbits.bodies:
        return g
    body = orbits.bodies[party_at]
    if party_at == center_id:
        x, y = (0.0, 0.0)
    elif body.parent == center_id:
        au, theta = _body_position_au_polar(body, t_hours)
        x, y = _polar_to_cartesian(au, theta, vp.au_to_px)
    else:
        x, y = (vp.half - 16, 0.0)
    marker = svgwrite.container.Group()
    marker.attribs["data-party-at"] = party_at
    marker.add(
        svgwrite.shapes.Circle(
            center=(x + 10, y - 10),
            r=4,
            fill="none",
            stroke=palette.PARTY,
            stroke_width=1.0,
        )
    )
    marker.add(
        svgwrite.shapes.Line(
            start=(x + 5, y - 10),
            end=(x + 15, y - 10),
            stroke=palette.PARTY,
            stroke_width=1.0,
        )
    )
    marker.add(
        svgwrite.shapes.Line(
            start=(x + 10, y - 15),
            end=(x + 10, y - 5),
            stroke=palette.PARTY,
            stroke_width=1.0,
        )
    )
    marker.add(
        _haloed_text(
            "← party",
            insert=(x + 18, y - 8),
            fill=palette.PARTY,
            font_size=9,
            font_style="italic",
        )
    )
    g.add(marker)
    return g
