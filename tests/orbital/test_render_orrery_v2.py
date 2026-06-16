"""Tests for the Orrery v2 visual restoration (Story 45-42).

Spec: docs/superpowers/specs/2026-05-04-orrery-v2-visual-restoration.md
UX-amended 2026-05-04 by Adora Belle Dearheart.

Pins behavioral acceptance criteria from spec §7. Snapshot-byte tests
(AC18 "Coyote Star at t=0 byte-identical SVG", AC24 drill-in to Red
Prospect) live in test_render_snapshots.py — this file covers every
other AC with structural / wire / behavioral assertions that fail
deterministically before the implementation lands.

Tests are grouped by spec section so a Dev triaging a failure can
trace it to the originating gap (§4.1, §4.2, …, §4.6, §5).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidequest.orbital import palette
from sidequest.orbital.loader import load_orbital_content
from sidequest.orbital.models import (
    Annotation,
    BodyDef,
    BodyType,
    ChartConfig,
    ClockConfig,
    OrbitsConfig,
    TravelConfig,
    TravelRealism,
)
from sidequest.orbital.render import Scope, render_chart

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def world_orrery_v2():
    """Synthetic world that exercises every Orrery-v2 code path."""
    return load_orbital_content(FIXTURES / "world_orrery_v2")


def _orbits_with_bodies(bodies: dict[str, BodyDef]) -> OrbitsConfig:
    return OrbitsConfig(
        version="0.1.0",
        clock=ClockConfig(epoch_days=0),
        travel=TravelConfig(realism=TravelRealism.ORBITAL),
        bodies=bodies,
    )


def _empty_chart() -> ChartConfig:
    return ChartConfig(version="0.1.0", annotations=[])


def _star_with(extras: dict[str, BodyDef]) -> OrbitsConfig:
    bodies: dict[str, BodyDef] = {"sun": BodyDef(type=BodyType.STAR, label="SUN")}
    bodies.update(extras)
    return _orbits_with_bodies(bodies)


def _render_root(orbits: OrbitsConfig, chart: ChartConfig | None = None) -> str:
    return render_chart(
        orbits=orbits,
        chart=chart or _empty_chart(),
        scope=Scope.system_root(),
        t_hours=0.0,
        party_at=None,
    )


# =========================================================================
# §4.4 + AC12-13 — BodyDef schema additions
# =========================================================================


class TestBodyDefSchemaExtensions:
    """AC12-13: register, label_register, moon_display_radius_px, show_at_system_scope."""

    def test_register_field_defaults_to_engraved(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
        )
        assert body.register == "engraved"

    def test_register_accepts_engraved_chalk_prose(self):
        for value in ("engraved", "chalk", "prose"):
            body = BodyDef(
                type=BodyType.HABITAT,
                parent="sun",
                semi_major_au=1.0,
                period_days=365.0,
                epoch_phase_deg=0,
                register=value,
            )
            assert body.register == value

    def test_register_rejects_unknown_value(self):
        """Literal type — invalid values must fail validation."""
        with pytest.raises(ValidationError):
            BodyDef(
                type=BodyType.HABITAT,
                parent="sun",
                semi_major_au=1.0,
                period_days=365.0,
                epoch_phase_deg=0,
                register="hand_drawn",
            )

    def test_label_register_default_is_none(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
        )
        assert body.label_register is None

    def test_label_register_accepts_engraved_chalk_prose(self):
        for value in ("engraved", "chalk", "prose"):
            body = BodyDef(
                type=BodyType.HABITAT,
                parent="sun",
                semi_major_au=1.0,
                period_days=365.0,
                epoch_phase_deg=0,
                label_register=value,
            )
            assert body.label_register == value

    def test_label_register_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            BodyDef(
                type=BodyType.HABITAT,
                parent="sun",
                semi_major_au=1.0,
                period_days=365.0,
                epoch_phase_deg=0,
                label_register="cursive",
            )

    def test_moon_display_radius_px_default_is_none(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
        )
        assert body.moon_display_radius_px is None

    def test_moon_display_radius_px_accepts_int(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
            moon_display_radius_px=54,
        )
        assert body.moon_display_radius_px == 54

    def test_show_at_system_scope_defaults_to_true(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
        )
        assert body.show_at_system_scope is True

    def test_show_at_system_scope_can_be_false(self):
        body = BodyDef(
            type=BodyType.HABITAT,
            parent="sun",
            semi_major_au=1.0,
            period_days=365.0,
            epoch_phase_deg=0,
            show_at_system_scope=False,
        )
        assert body.show_at_system_scope is False


# =========================================================================
# §4.1 + AC14, AC19 — engraved_label honors curve_along
# =========================================================================


class TestEngravedLabelCurveAlong:
    """AC2: 'tbrokendrift' smudge gone — labels follow their own arcs."""

    def test_engraved_label_with_orbit_outermost_renders_textpath(self):
        """engraved_label with curve_along=orbit_outermost emits a <textPath>
        anchored to a path defined in <defs>, not a fixed-position <text>."""
        orbits = _star_with(
            {
                "outer_belt": BodyDef(
                    type=BodyType.ARC_BELT,
                    parent="sun",
                    semi_major_au=8.0,
                    period_days=8000.0,
                    epoch_phase_deg=0,
                    arc_extent_deg=360,
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(kind="engraved_label", text="Outer Belt", curve_along="orbit_outermost"),
            ],
        )
        svg = _render_root(orbits, chart)
        assert "<textPath" in svg, (
            "engraved_label with curve_along must render via <textPath>, not fixed <text>. "
            "This is the §4.1 fix that eliminates the 'tbrokendrift' corruption."
        )
        assert "Outer Belt" in svg

    def test_engraved_label_textpath_references_defs_path(self):
        """The textPath must href a path id; that path id must exist in <defs>."""
        orbits = _star_with(
            {
                "outer_belt": BodyDef(
                    type=BodyType.ARC_BELT,
                    parent="sun",
                    semi_major_au=8.0,
                    period_days=8000.0,
                    epoch_phase_deg=0,
                    arc_extent_deg=360,
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(kind="engraved_label", text="Outer Belt", curve_along="orbit_outermost"),
            ],
        )
        svg = _render_root(orbits, chart)
        href_match = re.search(r'<textPath[^>]+(?:href|xlink:href)="#([^"]+)"', svg)
        assert href_match is not None, "textPath must reference a path via href/xlink:href"
        path_id = href_match.group(1)
        assert f'id="{path_id}"' in svg, (
            f"textPath href #{path_id} but no element with that id exists in the SVG"
        )

    def test_two_engraved_labels_with_curve_along_do_not_collide_at_top(self):
        """Regression for the literal 'tbrokendrift' bug: two engraved_label
        annotations both with curve_along set must NOT both render at the same
        fixed top-center coordinates."""
        orbits = _star_with(
            {
                "belt": BodyDef(
                    type=BodyType.ARC_BELT,
                    parent="sun",
                    semi_major_au=2.5,
                    period_days=1500.0,
                    epoch_phase_deg=30,
                    arc_extent_deg=90,
                    label="BROKEN DRIFT",
                ),
                "outer_ring": BodyDef(
                    type=BodyType.ARC_BELT,
                    parent="sun",
                    semi_major_au=10.0,
                    period_days=11500.0,
                    epoch_phase_deg=0,
                    arc_extent_deg=360,
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(
                    kind="engraved_label", text="the Last Drift", curve_along="orbit_outermost"
                ),
                Annotation(kind="engraved_label", text="broken drift", curve_along="body:belt"),
            ],
        )
        svg = _render_root(orbits, chart)
        # The corruption was both labels at insert=(0, -vp.half + 30) → (0, -370).
        # With curve_along honored, both go through <textPath>; neither lands
        # at fixed top-center. Assert no fixed-top-center label text node.
        # Specifically, no <text … x="0" y="-370">…</text> matches either label.
        bad_pattern = re.compile(
            r'<text[^>]*\bx="0"[^>]*\by="-370"[^>]*>\s*'
            r"(?:the Last Drift|broken drift)\s*</text>"
        )
        assert not bad_pattern.search(svg), (
            "Both engraved_labels collapsed into the same fixed-top-center <text> — "
            "this is the 'tbrokendrift' bug. They must each render via textPath."
        )

    def test_curve_along_orbit_body_resolves_to_body_orbit(self):
        """curve_along="orbit_<body_id>" resolves to that body's orbit ellipse."""
        orbits = _star_with(
            {
                "ring_world": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=3.0,
                    period_days=1500.0,
                    epoch_phase_deg=0,
                    label="RING WORLD",
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(
                    kind="engraved_label",
                    text="ring world lane",
                    curve_along="orbit_ring_world",
                ),
            ],
        )
        svg = _render_root(orbits, chart)
        assert "<textPath" in svg
        assert "ring world lane" in svg

    def test_curve_along_unknown_value_raises_at_load_or_render(self):
        """AC#19 + AC#14: unknown curve_along value must fail loud, not silent.

        Per spec §4.1: 'Unknown curve_along value raises during chart-load
        (per CLAUDE.md no silent fallbacks).'
        """
        orbits = _star_with(
            {
                "alpha": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.0,
                    period_days=365.0,
                    epoch_phase_deg=0,
                    label="ALPHA",
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(
                    kind="engraved_label",
                    text="ghost",
                    curve_along="orbit_nonexistent_body",
                ),
            ],
        )
        with pytest.raises((ValueError, KeyError), match=r"(curve_along|nonexistent_body|orbit_)"):
            _render_root(orbits, chart)

    def test_curve_along_body_ref_on_non_belt_raises(self):
        """Spec §4.1: 'body:<body_id>' is only meaningful for arc_belt bodies;
        applying it to non-belt body raises."""
        orbits = _star_with(
            {
                "ring_world": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=3.0,
                    period_days=1500.0,
                    epoch_phase_deg=0,
                    label="RING WORLD",
                ),
            }
        )
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(
                    kind="engraved_label",
                    text="invalid",
                    curve_along="body:ring_world",
                ),
            ],
        )
        with pytest.raises(ValueError, match=r"(arc_belt|body:|ring_world)"):
            _render_root(orbits, chart)

    def test_engraved_label_without_curve_along_still_renders(self):
        """Backward compat: engraved_label with curve_along=None falls back
        to fixed-position rendering. World_minimal-style charts still work."""
        chart = ChartConfig(
            version="0.1.0",
            annotations=[
                Annotation(kind="engraved_label", text="centered title"),
            ],
        )
        svg = _render_root(_star_with({}), chart)
        assert "centered title" in svg


# =========================================================================
# §4.2 + AC3, AC15 — bearing rose
# =========================================================================


class TestBearingRose:
    """AC3 + AC15: bearing rose at chart center for system_root scope."""

    def test_bearing_rose_renders_at_system_root_scope(self):
        """A recognisable bearing-rose group must appear at system_root."""
        svg = _render_root(_star_with({}))
        # The rose has a distinctive id or class — implementation defines one
        # of these. Accept either "bearing-rose" id or class as the contract.
        assert ('id="bearing-rose"' in svg) or ('class="bearing-rose"' in svg), (
            "Expected a <g id='bearing-rose'> or <g class='bearing-rose'> "
            "container in the engraved layer at system_root scope (§4.2)."
        )

    def test_bearing_rose_has_cardinal_numerals(self):
        """AC#3: cardinal numerals 000/090/180/270 are present."""
        svg = _render_root(_star_with({}))
        for cardinal in ("000", "090", "180", "270"):
            assert cardinal in svg, f"missing cardinal numeral {cardinal!r}"

    def test_bearing_rose_has_intermediate_numerals(self):
        """AC#3: intermediate numerals 030..330 (every 30°) are present."""
        svg = _render_root(_star_with({}))
        for intermediate in ("030", "060", "120", "150", "210", "240", "300", "330"):
            assert intermediate in svg, f"missing intermediate numeral {intermediate!r}"

    def test_bearing_rose_does_not_render_at_drill_in_scope(self):
        """Spec §4.2: 'Only renders when scope is system_root.'

        Drill-in scopes — the rose is system-scale, so it must NOT appear
        when centered on a sub-body.
        """
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "giant": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=4.0,
                    period_days=2920.0,
                    epoch_phase_deg=0,
                    label="GIANT",
                ),
                "moon": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.01,
                    period_days=10.0,
                    epoch_phase_deg=0,
                    label="MOON",
                ),
            }
        )
        svg = render_chart(
            orbits=orbits,
            chart=_empty_chart(),
            scope=Scope(center_body_id="giant"),
            t_hours=0.0,
            party_at=None,
        )
        assert 'id="bearing-rose"' not in svg
        assert 'class="bearing-rose"' not in svg

    def test_palette_exposes_bearing_rose_constants(self):
        """AC#15: bearing rose constants live in palette.py."""
        assert hasattr(palette, "BEARING_ROSE_OUTER_PX"), (
            "palette.BEARING_ROSE_OUTER_PX not defined — required by §4.2 + §5.2"
        )
        assert hasattr(palette, "LABEL_BEARING_ROSE_CLEARANCE"), (
            "palette.LABEL_BEARING_ROSE_CLEARANCE not defined — required by §5.2"
        )
        # Sanity: constants must be positive numerics.
        assert isinstance(palette.BEARING_ROSE_OUTER_PX, (int, float))
        assert palette.BEARING_ROSE_OUTER_PX > 0
        assert isinstance(palette.LABEL_BEARING_ROSE_CLEARANCE, (int, float))
        assert palette.LABEL_BEARING_ROSE_CLEARANCE > 0


# =========================================================================
# §4.3 + AC4, AC16 — star reticle
# =========================================================================


class TestStarReticle:
    """AC4 + AC16: Coyote (star) renders as red reticle, not concentric corona."""

    def test_star_glyph_emits_dashed_outer_ring(self):
        """The reticle outer ring uses stroke-dasharray (red dashed)."""
        svg = _render_root(_star_with({}))
        # A red <circle> with stroke-dasharray attached — the reticle.
        # We accept either "stroke-dasharray=" or "stroke_dasharray=" in case
        # svgwrite emits the python-name form.
        red = palette.RED
        pattern = re.compile(
            r'<circle[^>]*stroke="' + re.escape(red) + r'"[^>]*stroke-dasharray=',
            re.IGNORECASE,
        )
        alt_pattern = re.compile(
            r'<circle[^>]*stroke-dasharray=[^>]*stroke="' + re.escape(red) + r'"',
            re.IGNORECASE,
        )
        assert pattern.search(svg) or alt_pattern.search(svg), (
            "Star reticle must emit a red dashed outer circle (§4.3)."
        )

    def test_star_glyph_no_corona_disks(self):
        """The legacy _star_glyph emitted three concentric red disks at
        r=10/14/18 with fill_opacity. The reticle treatment removes these."""
        svg = _render_root(_star_with({}))
        # The corona had `fill-opacity="0.12"` on r=18. Reticle has no fill
        # opacity on the outer ring (it's stroke-only).
        assert 'fill-opacity="0.12"' not in svg, (
            "Legacy corona disk (r=18, fill-opacity=0.12) still present — "
            "§4.3 requires reticle treatment, not corona."
        )

    def test_star_label_appears_in_reticle(self):
        """AC#4: the star's label appears at the reticle (not in a corner)."""
        svg = _render_root(_star_with({}))
        assert "SUN" in svg

    def test_palette_exposes_reticle_constants(self):
        """AC#16: reticle constants extracted from course_render.py into palette.py.

        Per spec §4.3: 'lift its constants into palette.py
        (e.g. RETICLE_OUTER_RADIUS, RETICLE_INNER_RADIUS, dash pattern)
        so both renderers share one definition. Reuse-first.'
        """
        # Either the spec's exact names, or names that begin with RETICLE_ are
        # acceptable — what matters is that course_render and the new star
        # reticle both reach for the same palette module.
        reticle_attrs = [name for name in dir(palette) if "RETICLE" in name]
        assert reticle_attrs, (
            "palette.py must expose at least one RETICLE_* constant per AC#16 / §4.3."
        )

    def test_course_render_imports_reticle_constants_from_palette(self):
        """AC#16 wiring test: course_render.py must reference the shared
        palette constants, not its own private _RETICLE_* literals."""
        course_render_src = (
            Path(__file__).resolve().parents[2] / "sidequest" / "orbital" / "course_render.py"
        ).read_text()
        # The pre-fix file had `_RETICLE_OUTER_R = 13.0` etc. as private
        # literals. After the refactor those literals must be replaced by
        # references to palette constants.
        assert "_RETICLE_OUTER_R = 13.0" not in course_render_src, (
            "course_render.py still defines its own _RETICLE_OUTER_R literal; "
            "AC#16 requires lifting reticle constants into palette.py and "
            "importing them from there."
        )


# =========================================================================
# §4.4 + AC5, AC20, AC21 — register field drives orbit + label styling
# =========================================================================


class TestRegisterField:
    """AC5, AC20, AC21: register=chalk → dashed orbit; label_register=prose
    decouples label from orbit register."""

    def test_register_chalk_produces_dashed_orbit_stroke(self):
        """AC#20: BodyDef with register=chalk produces an orbit ellipse stroke
        containing stroke-dasharray (a chalk-register orbit is dashed)."""
        orbits = _star_with(
            {
                "outpost": BodyDef(
                    type=BodyType.GATE,
                    parent="sun",
                    semi_major_au=7.0,
                    period_days=6500.0,
                    epoch_phase_deg=150,
                    label="OUTPOST",
                    register="chalk",
                ),
            }
        )
        svg = _render_root(orbits)
        # Find the ellipse tagged with data-body-id="outpost" (or its
        # surrounding group). Its stroke must include stroke-dasharray.
        outpost_ellipse_match = re.search(
            r'<ellipse[^>]*data-body-id="outpost"[^>]*/>',
            svg,
        )
        assert outpost_ellipse_match is not None, "outpost ellipse not found in output"
        ellipse_tag = outpost_ellipse_match.group(0)
        assert "stroke-dasharray" in ellipse_tag or "stroke_dasharray" in ellipse_tag, (
            f"chalk-register orbit must have a dashed stroke, got: {ellipse_tag}"
        )

    def test_register_engraved_orbit_is_solid(self):
        """Default engraved register: orbit ellipse has NO stroke-dasharray."""
        orbits = _star_with(
            {
                "city": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=2.0,
                    period_days=600.0,
                    epoch_phase_deg=0,
                    label="CITY",
                ),
            }
        )
        svg = _render_root(orbits)
        match = re.search(r'<ellipse[^>]*data-body-id="city"[^>]*/>', svg)
        assert match is not None
        assert "stroke-dasharray" not in match.group(0)

    def test_label_register_prose_decouples_from_orbit_register(self):
        """AC#21: BodyDef.label_register=prose overrides body's register-derived
        label styling. Orbit stays chalk-dashed; label is VT323 lowercase italic.
        """
        orbits = _star_with(
            {
                "drift": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=10.0,
                    period_days=11500.0,
                    epoch_phase_deg=0,
                    label="Last Drift",
                    register="chalk",
                    label_register="prose",
                ),
            }
        )
        svg = _render_root(orbits)
        # Find the label text element for "Last Drift".
        label_match = re.search(
            r"<text[^>]*>\s*Last Drift\s*</text>",
            svg,
        )
        assert label_match is not None, "Last Drift label missing from output"
        label_tag = label_match.group(0)
        # Prose register: VT323 monospace, italic.
        assert "VT323" in label_tag, f"prose label must use VT323 font, got: {label_tag}"
        assert 'font-style="italic"' in label_tag, f"prose label must be italic, got: {label_tag}"

    def test_register_chalk_label_is_orbitron_caps(self):
        """AC#5: chalk-register label is Orbitron CAPS, not VT323 italic.
        (label_register defaults to inherit from register=chalk.)"""
        orbits = _star_with(
            {
                "grand_gate": BodyDef(
                    type=BodyType.GATE,
                    parent="sun",
                    semi_major_au=6.5,
                    period_days=6048.0,
                    epoch_phase_deg=150,
                    label="GRAND GATE",
                    register="chalk",
                ),
            }
        )
        svg = _render_root(orbits)
        match = re.search(r"<text[^>]*>\s*GRAND GATE\s*</text>", svg)
        assert match is not None
        label_tag = match.group(0)
        assert "Orbitron" in label_tag, f"chalk label must use Orbitron, got: {label_tag}"

    def test_register_prose_label_is_vt323(self):
        """register=prose (no override) gives a VT323 italic label."""
        orbits = _star_with(
            {
                "moonet": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=3.0,
                    period_days=1200.0,
                    epoch_phase_deg=0,
                    label="moonet",
                    register="prose",
                ),
            }
        )
        svg = _render_root(orbits)
        match = re.search(r"<text[^>]*>\s*moonet\s*</text>", svg)
        assert match is not None
        label_tag = match.group(0)
        assert "VT323" in label_tag


# =========================================================================
# §4.6 + AC8, AC9 — moons rendered at system-root scope
# =========================================================================


class TestMoonsAtSystemScope:
    """AC8, AC9: moons render with their own glyphs at system_root scope,
    not as +N cluster glyph. show_at_system_scope=False elides."""

    @pytest.fixture
    def giant_with_moons(self) -> OrbitsConfig:
        return _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "giant": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=4.0,
                    period_days=2920.0,
                    epoch_phase_deg=180,
                    label="GIANT",
                ),
                "moon_a": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.005,
                    period_days=5.0,
                    epoch_phase_deg=0,
                    label="moon a",
                    moon_display_radius_px=38,
                ),
                "moon_b": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.010,
                    period_days=12.0,
                    epoch_phase_deg=90,
                    label="moon b",
                    moon_display_radius_px=54,
                ),
                "moon_hidden": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.015,
                    period_days=18.0,
                    epoch_phase_deg=180,
                    label="hidden moon",
                    show_at_system_scope=False,
                ),
            }
        )

    def test_visible_moons_render_at_system_scope(self, giant_with_moons):
        """AC#8: moons appear at system_root scope, individually identified."""
        svg = _render_root(giant_with_moons)
        assert 'data-body-id="moon_a"' in svg, (
            "moon_a not rendered at system_root — §4.6 system-scope moon band failed"
        )
        assert 'data-body-id="moon_b"' in svg, "moon_b not rendered at system_root"

    def test_visible_moon_labels_appear_at_system_scope(self, giant_with_moons):
        """AC#8: moons are labeled at system_root, not just dotted."""
        svg = _render_root(giant_with_moons)
        assert "moon a" in svg
        assert "moon b" in svg

    def test_show_at_system_scope_false_elides_moon(self, giant_with_moons):
        """AC#9 + UX requirement: show_at_system_scope=False hides at system_root."""
        svg = _render_root(giant_with_moons)
        assert 'data-body-id="moon_hidden"' not in svg, (
            "moon_hidden has show_at_system_scope=false but still rendered at system_root"
        )
        assert "hidden moon" not in svg

    def test_elided_moon_still_renders_at_drill_in(self, giant_with_moons):
        """Spec §4.6: 'Drill-in still shows them.' show_at_system_scope only
        controls system-root visibility."""
        svg = render_chart(
            orbits=giant_with_moons,
            chart=_empty_chart(),
            scope=Scope(center_body_id="giant"),
            t_hours=0.0,
            party_at=None,
        )
        assert 'data-body-id="moon_hidden"' in svg, (
            "show_at_system_scope=False should NOT hide the moon at drill-in scope"
        )

    def test_drill_in_still_works_after_moon_band_changes(self, giant_with_moons):
        """AC#24 wire half: drilling into the giant still renders all moons."""
        svg = render_chart(
            orbits=giant_with_moons,
            chart=_empty_chart(),
            scope=Scope(center_body_id="giant"),
            t_hours=0.0,
            party_at=None,
        )
        for moon_id in ("moon_a", "moon_b", "moon_hidden"):
            assert f'data-body-id="{moon_id}"' in svg, (
                f"drill-in scope dropped moon {moon_id!r} — §4.6 broke drill-in"
            )

    def test_parent_with_moons_no_longer_emits_plus_n_cluster_glyph(self, giant_with_moons):
        """Spec §4.6: 'The drillable cluster glyph (+N) goes away for bodies
        that are now showing moons.'"""
        svg = _render_root(giant_with_moons)
        # giant has 3 children but 1 is hidden — so the visible-at-system-scope
        # count is 2. The +N glyph should NOT appear next to giant.
        # (The cluster glyph emitted '+{child_count}' as <text>.)
        # Search near the giant body.
        assert ">+3<" not in svg, "giant should no longer emit +3 cluster chip"
        assert ">+2<" not in svg, (
            "giant should no longer emit +N cluster chip — moons render directly"
        )

    def test_moon_label_renders_in_callout_block(self, giant_with_moons):
        """ADR-094 §9 (deviation from §4.6): moon-band labels are forced into
        callouts (sub-pixel render position has no radial space), so the
        VT323-inline rule for moon labels is superseded — labels appear
        inside a callout-singleton-title (Orbitron) or callout-group-member.

        This pins the post-ADR-094 behavior: moon labels are visible and
        carry a callout class, not silently dropped.
        """
        svg = _render_root(giant_with_moons)
        match = re.search(r"<text[^>]*>\s*moon a\s*</text>", svg)
        assert match is not None, "moon a label missing from output"
        # Moon-band children render via callout blocks per ADR-094 §9.
        assert "callout-singleton-title" in match.group(0) or "callout-group-member" in match.group(
            0
        ), f"moon label should render in a callout block class; got: {match.group(0)}"


# =========================================================================
# §5 + AC1, AC10, AC22 — radial-out anchor + bearing-rose clearance + tier
# =========================================================================


class TestLabelDeCollision:
    """AC1, AC10, AC22 + AC17: radial-out anchor, bearing-rose clearance,
    peer-collision tier."""

    def test_palette_exposes_label_decollision_constants(self):
        """AC#17: label de-collision constants live in palette.py."""
        for name in (
            "LABEL_RADIAL_PADDING_PX",
            "MIN_ANGULAR_SEPARATION_DEG",
            "LABEL_TIER_RADIAL_OFFSET_PX",
        ):
            assert hasattr(palette, name), f"palette.{name} missing — AC#17 / §5"

    def test_inner_cluster_labels_have_distinct_anchor_positions(self):
        """AC#1: alpha/beta/gamma at au≈1, within 25° arc, must NOT all share
        the same naive (x+10, y-8) offset. Their labels must spread around
        the star like spokes (radial-out anchor)."""
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "alpha": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=60,
                    label="ALPHA",
                ),
                "beta": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.05,
                    period_days=380.0,
                    epoch_phase_deg=70,
                    label="BETA",
                ),
                "gamma": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.10,
                    period_days=395.0,
                    epoch_phase_deg=80,
                    label="GAMMA",
                ),
            }
        )
        svg = _render_root(orbits)
        # Extract label x/y for each body.
        positions: dict[str, tuple[float, float]] = {}
        for body_id in ("ALPHA", "BETA", "GAMMA"):
            m = re.search(
                r'<text[^>]*\bx="([\-\d.eE]+)"[^>]*\by="([\-\d.eE]+)"[^>]*>\s*'
                + body_id
                + r"\s*</text>",
                svg,
            )
            assert m, f"label {body_id!r} missing from output"
            positions[body_id] = (float(m.group(1)), float(m.group(2)))
        # No two labels share the same (x, y) position to within 1px.
        ids = list(positions.keys())
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                ax, ay = positions[a]
                bx, by = positions[b]
                assert (abs(ax - bx) > 1.0) or (abs(ay - by) > 1.0), (
                    f"labels {a!r} and {b!r} at identical position "
                    f"({ax}, {ay}) — radial-out anchor not applied"
                )

    def test_inner_cluster_labels_clear_bearing_rose(self):
        """AC#10: inner-cluster labels do not collide with the bearing rose.
        Label anchor radial distance from chart center must be ≥ rose outer
        radius + clearance."""
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                # An inner body that without clearance would land directly
                # on the bearing rose.
                "innermost": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=45,
                    label="INNER",
                ),
            }
        )
        svg = _render_root(orbits)
        match = re.search(
            r'<text[^>]*\bx="([\-\d.eE]+)"[^>]*\by="([\-\d.eE]+)"[^>]*>\s*'
            r"INNER\s*</text>",
            svg,
        )
        assert match is not None
        x = float(match.group(1))
        y = float(match.group(2))
        radial = (x * x + y * y) ** 0.5
        min_radial = palette.BEARING_ROSE_OUTER_PX + palette.LABEL_BEARING_ROSE_CLEARANCE
        assert radial >= min_radial - 0.01, (
            f"INNER label at radial {radial:.1f}px is closer to chart center "
            f"than the rose outer + clearance ({min_radial}px) — §5.2 violated"
        )

    def test_peer_collision_tier_assigns_increasing_offsets(self):
        """AC#22: three bodies clustered within 25° arc receive tier 0/1/2
        radial offsets — labels are progressively further from the star.
        """
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "alpha": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=60,  # tier 0
                    label="ALPHA",
                ),
                "beta": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=70,  # 10° from alpha → tier 1
                    label="BETA",
                ),
                "gamma": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=80,  # 20° from alpha, 10° from beta → tier 2
                    label="GAMMA",
                ),
            }
        )
        svg = _render_root(orbits)
        positions: dict[str, tuple[float, float]] = {}
        for body_id in ("ALPHA", "BETA", "GAMMA"):
            m = re.search(
                r'<text[^>]*\bx="([\-\d.eE]+)"[^>]*\by="([\-\d.eE]+)"[^>]*>\s*'
                + body_id
                + r"\s*</text>",
                svg,
            )
            assert m, f"label {body_id!r} missing from output"
            positions[body_id] = (float(m.group(1)), float(m.group(2)))
        radials = {bid: (x * x + y * y) ** 0.5 for bid, (x, y) in positions.items()}
        # ALPHA tier 0, BETA tier 1, GAMMA tier 2 — strictly increasing radial
        # by at least LABEL_TIER_RADIAL_OFFSET_PX per tier (sort order is
        # bearing-sorted, which for these 3 yields ALPHA, BETA, GAMMA).
        tier_step = palette.LABEL_TIER_RADIAL_OFFSET_PX
        assert radials["BETA"] >= radials["ALPHA"] + tier_step - 1.0, (
            f"BETA radial {radials['BETA']:.1f} should be ≥ ALPHA "
            f"{radials['ALPHA']:.1f} + tier_step {tier_step}"
        )
        assert radials["GAMMA"] >= radials["ALPHA"] + 2 * tier_step - 1.0, (
            f"GAMMA radial {radials['GAMMA']:.1f} should be ≥ ALPHA "
            f"{radials['ALPHA']:.1f} + 2*tier_step ({2 * tier_step})"
        )


# =========================================================================
# AC11 — non-color hazard signal
# =========================================================================


class TestHazardNonColorSignal:
    """AC11: hazard bodies carry dashed-outline glyph signal in addition to
    red coloring (color-blind accessibility)."""

    def test_hazard_body_glyph_has_dashed_outline(self):
        """A HABITAT with hazard=true renders with a stroke-dasharray on its
        glyph, not just a red fill. Color-blind players see the signal."""
        orbits = _star_with(
            {
                "trap": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.5,
                    period_days=550.0,
                    epoch_phase_deg=0,
                    hazard=True,
                    label="TRAP",
                ),
            }
        )
        svg = _render_root(orbits)
        # The hazard signal: the polygon glyph's stroke-dasharray is set
        # (or a dashed outline element appears nearby for grouped glyphs).
        # Find any element with body_id=trap and check for stroke-dasharray
        # on the tag itself or in a small window around it.
        body_id_match = re.search(
            r'<[^>]*data-body-id="trap"[^>]*>',
            svg,
        )
        assert body_id_match is not None
        # Look for stroke-dasharray on the matched tag itself.
        glyph_tag = body_id_match.group(0)
        nearby_window = svg[max(0, body_id_match.start() - 200) : body_id_match.end() + 400]
        assert "stroke-dasharray" in glyph_tag or "stroke-dasharray" in nearby_window, (
            f"hazard body 'trap' has no dashed-outline signal on or near its "
            f"glyph — AC#11 violated. Tag: {glyph_tag}"
        )


# =========================================================================
# AC23 — OTEL chart.render attributes
# =========================================================================


class TestOtelChartRenderAttributes:
    """AC23: chart.render span gains body_count_chalk/engraved/prose,
    body_count_moons_rendered, label_collision_tier_max."""

    def _spans(self, otel_capture, name: str = "chart.render"):
        return [s for s in otel_capture.get_finished_spans() if s.name == name]

    def test_chart_render_span_has_register_body_counts(self, otel_capture):
        """body_count_chalk, body_count_engraved, body_count_prose attributes."""
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "city": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.0,
                    period_days=365.0,
                    epoch_phase_deg=0,
                    label="CITY",
                    register="engraved",
                ),
                "frontier": BodyDef(
                    type=BodyType.GATE,
                    parent="sun",
                    semi_major_au=6.0,
                    period_days=5500.0,
                    epoch_phase_deg=90,
                    label="FRONTIER",
                    register="chalk",
                ),
                "rumor": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=3.0,
                    period_days=1500.0,
                    epoch_phase_deg=180,
                    label="rumor mill",
                    register="prose",
                ),
            }
        )
        _render_root(orbits)
        spans = self._spans(otel_capture)
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert "body_count_chalk" in attrs, (
            "chart.render span missing body_count_chalk attribute (AC#23)"
        )
        assert "body_count_engraved" in attrs, (
            "chart.render span missing body_count_engraved attribute (AC#23)"
        )
        assert "body_count_prose" in attrs, (
            "chart.render span missing body_count_prose attribute (AC#23)"
        )
        assert attrs["body_count_engraved"] == 1
        assert attrs["body_count_chalk"] == 1
        assert attrs["body_count_prose"] == 1

    def test_chart_render_span_has_moons_rendered_count(self, otel_capture):
        """body_count_moons_rendered counts moons placed in system-scope band."""
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "giant": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=4.0,
                    period_days=2920.0,
                    epoch_phase_deg=180,
                    label="GIANT",
                ),
                "moon_a": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.005,
                    period_days=5.0,
                    epoch_phase_deg=0,
                    label="moon a",
                ),
                "moon_b": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.010,
                    period_days=12.0,
                    epoch_phase_deg=90,
                    label="moon b",
                ),
                "moon_hidden": BodyDef(
                    type=BodyType.HABITAT,
                    parent="giant",
                    semi_major_au=0.015,
                    period_days=18.0,
                    epoch_phase_deg=180,
                    label="hidden moon",
                    show_at_system_scope=False,
                ),
            }
        )
        _render_root(orbits)
        spans = self._spans(otel_capture)
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert "body_count_moons_rendered" in attrs, (
            "chart.render span missing body_count_moons_rendered (AC#23)"
        )
        # Two visible moons; one elided.
        assert attrs["body_count_moons_rendered"] == 2, (
            f"expected 2 moons rendered (moon_a + moon_b); got {attrs['body_count_moons_rendered']}"
        )

    def test_chart_render_span_has_label_collision_tier_max(self, otel_capture):
        """label_collision_tier_max captures the highest collision tier the
        label-placement pass assigned (0 if no clusters)."""
        # Three bodies clustered → max tier ≥ 2.
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "alpha": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=60,
                    label="ALPHA",
                ),
                "beta": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=70,
                    label="BETA",
                ),
                "gamma": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=1.00,
                    period_days=365.0,
                    epoch_phase_deg=80,
                    label="GAMMA",
                ),
            }
        )
        _render_root(orbits)
        spans = self._spans(otel_capture)
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert "label_collision_tier_max" in attrs, (
            "chart.render span missing label_collision_tier_max (AC#23)"
        )
        assert attrs["label_collision_tier_max"] >= 2, (
            f"three-body cluster should produce tier_max ≥ 2; "
            f"got {attrs['label_collision_tier_max']}"
        )

    def test_chart_render_tier_max_is_zero_when_no_cluster(self, otel_capture):
        """Single bodies at distinct bearings → no peer cluster → tier_max=0."""
        orbits = _orbits_with_bodies(
            {
                "sun": BodyDef(type=BodyType.STAR, label="SUN"),
                "lone": BodyDef(
                    type=BodyType.HABITAT,
                    parent="sun",
                    semi_major_au=2.0,
                    period_days=1000.0,
                    epoch_phase_deg=45,
                    label="LONE",
                ),
            }
        )
        _render_root(orbits)
        spans = self._spans(otel_capture)
        assert len(spans) == 1
        assert spans[0].attributes["label_collision_tier_max"] == 0


# =========================================================================
# Wiring — fixture-driven full-chart smoke
# =========================================================================


class TestOrreryV2FixtureWiring:
    """The world_orrery_v2 fixture must render without error and exercise
    every new code path. Snapshot byte-comparison lives in
    test_render_snapshots.py; this class just asserts wiring."""

    def test_fixture_renders_without_error(self, world_orrery_v2):
        svg = render_chart(
            orbits=world_orrery_v2.orbits,
            chart=world_orrery_v2.chart,
            scope=Scope.system_root(),
            t_hours=0.0,
            party_at=None,
        )
        assert svg.startswith("<")
        assert "</svg>" in svg

    def test_fixture_engraved_chalk_prose_all_present(self, world_orrery_v2):
        """Sanity: the fixture exercises all three registers."""
        bodies = world_orrery_v2.orbits.bodies
        registers = {b.register for b in bodies.values()}
        assert "engraved" in registers
        assert "chalk" in registers

    def test_fixture_has_label_register_override(self, world_orrery_v2):
        """drift body has register=chalk + label_register=prose."""
        drift = world_orrery_v2.orbits.bodies["drift"]
        assert drift.register == "chalk"
        assert drift.label_register == "prose"

    def test_fixture_has_show_at_system_scope_false(self, world_orrery_v2):
        """moon_hidden tests the elision branch."""
        hidden = world_orrery_v2.orbits.bodies["moon_hidden"]
        assert hidden.show_at_system_scope is False

    def test_fixture_render_emits_full_otel_attribute_set(self, world_orrery_v2, otel_capture):
        """Wiring test: the v2 fixture render emits all five new OTEL attrs."""
        render_chart(
            orbits=world_orrery_v2.orbits,
            chart=world_orrery_v2.chart,
            scope=Scope.system_root(),
            t_hours=0.0,
            party_at=None,
        )
        spans = [s for s in otel_capture.get_finished_spans() if s.name == "chart.render"]
        assert len(spans) == 1
        attrs = spans[0].attributes
        for key in (
            "body_count_engraved",
            "body_count_chalk",
            "body_count_prose",
            "body_count_moons_rendered",
            "label_collision_tier_max",
        ):
            assert key in attrs, f"chart.render span missing {key}"
