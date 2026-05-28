"""Story 63-9 — Reference renderer humanization guard (RED phase).

The generic fallback walk in ``reference_renderer.py`` leaks raw developer
strings onto the player- and author-facing reference wiki:

- raw Python bools (``True`` / ``False``) instead of ``Yes`` / ``No``,
- raw ``snake_case`` / underscore-bearing keys as headings,
- dev-note / placeholder prose (``TODO``, ``FIXME``, ``DEV NOTE``, …),
- underscore-prefixed private keys (``_dev_note``),
- (regression-lock) Python ``dict``/``list`` ``repr()`` signatures.

This module pins the *guard contract* against the **served HTML artifact**
(per ``sidequest-server/CLAUDE.md`` "No Source-Text Wiring Tests") plus the
pure ``render_node`` transform, and asserts the suppression decision emits an
OTEL span (per the OTEL Observability Principle — a dropped author string is a
loud decision, never a silent fallback).

Test taxonomy (derived ACs, context-story-63-9.md):
- AC1  heading humanization — no raw underscore survives in a heading.
- AC2  no raw container repr — regression-lock (already structurally held).
- AC3  no raw bools — ``True``/``False`` → ``Yes``/``No`` (or suppressed).
- AC4  no dev-note leakage — placeholder prose + private keys suppressed.
- AC5  output-scan gate — assembled HTML free of every raw signature.
- AC6  OTEL engagement — suppression fires a ``SPAN_REFERENCE_*`` span.

NOTE (TEA, 2026-05-27): this story has no Architect-ratified ACs yet. The
exact dev-note marker set and the underscore-prefixed-key policy below are
TEA-derived and logged as a Delivery Finding for Architect ratification. The
assertions are written to constrain only the *safe invariant* (no raw dev
string in HTML), not a specific casing/marker implementation, so a reasonable
guard satisfies them regardless of the final policy call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.server.reference_renderer import (
    _DEVNOTE_MARKERS,
    assemble_lore_page,
    render_node,
)

# --- Fixture scaffolding (no live content/pack slugs) ---------------------

_MINIMAL_THEME_YAML = (
    "primary: '#5C7A4F'\n"
    "accent: '#C9A96E'\n"
    "background: '#F4EBDA'\n"
    "archetype: parchment\n"
    "web_font_family: Lora\n"
    "display_font_family: Playfair Display\n"
    "dinkus:\n  glyph:\n    light: '—'\n    medium: '❧'\n    heavy: '❧❧❧'\n"
)

# Dev-note markers are imported from the renderer (single source of truth) so
# the test never goes stale silently when the production set changes.


def _write_lore_pack(
    tmp_path: Path,
    *,
    lore_yaml: str,
    world_yaml: str = "description: A quiet fixture plateau.\n",
) -> tuple[Path, Path]:
    """Build a throwaway pack whose pack-tier ``lore.yaml`` exercises the
    generic fallback walk (``lore`` has sub-key presenters but no file-root
    presenter, so an unknown PUBLIC key falls through to the fallback path).

    Returns ``(pack_dir, world_dir)`` for ``assemble_lore_page``.
    """
    pack_dir = tmp_path / "demo"
    world_dir = pack_dir / "worlds" / "demoworld"
    world_dir.mkdir(parents=True)
    (pack_dir / "theme.yaml").write_text(_MINIMAL_THEME_YAML)
    (pack_dir / "lore.yaml").write_text(lore_yaml)
    (world_dir / "world.yaml").write_text(world_yaml)
    return pack_dir, world_dir


def _headings(html: str) -> list[str]:
    import re

    return re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", html, flags=re.DOTALL)


# =========================================================================
# AC3 — No raw bools (True/False → Yes/No)
# =========================================================================


def test_render_bool_true_renders_yes_not_true():
    """A bare ``True`` scalar must humanize to ``Yes``; the token ``True``
    must never reach the reader. (Currently emits ``<p>True</p>``.)"""
    html = render_node(True)
    assert "Yes" in html
    assert "True" not in html


def test_render_bool_false_renders_no_not_false():
    html = render_node(False)
    assert "No" in html
    assert "False" not in html


def test_render_bool_in_scalar_list_humanized():
    """Bools inside a scalar-only list also leak via ``escape(str(item))``."""
    html = render_node([True, False])
    assert "<li>Yes</li>" in html
    assert "<li>No</li>" in html
    assert ">True<" not in html
    assert ">False<" not in html


def test_render_bool_dict_value_humanized():
    """A bool dict-value rendered through the fallback walk must humanize."""
    html = render_node({"is_secret": True})
    assert "<h2>Is Secret</h2>" in html
    assert "Yes" in html
    assert "True" not in html


# =========================================================================
# AC1 — Heading humanization (no raw underscore survives)
# =========================================================================


def test_heading_with_underscore_and_caps_strips_underscore():
    """The conservative humanizer bails on any uppercase, so an
    underscore-bearing key that ALSO contains caps (``MECHANICAL_surface``)
    currently leaks its underscore into the heading. A key containing an
    underscore is distinguishable from an acronym (``USB``) or authored prose
    (``Floor It``); the underscore must never survive into heading text.

    Constrains only the safe invariant (no raw ``_`` in the heading), not the
    final casing — acronym policy is the Architect's call.
    """
    html = render_node({"MECHANICAL_surface": "x"})
    headings = _headings(html)
    assert headings, "expected at least one heading"
    for h in headings:
        assert "_" not in h, f"raw underscore leaked into heading: {h!r}"
    assert "MECHANICAL_surface" not in html


# =========================================================================
# AC2 — No raw container repr (regression-lock; already structurally held)
# =========================================================================


def test_dict_value_renders_as_markup_not_repr():
    """REGRESSION-LOCK (passes today). A dict/list value reaching the fallback
    walk must render as structured markup, never as Python ``repr()``. Pins
    the invariant so a future "simplification" that str()'s a container can't
    silently regress it."""
    html = render_node({"stats": {"hp": 5, "atk": 2}, "tags": ["a", "b"]})
    assert "{'" not in html
    assert "[{" not in html
    assert "': " not in html
    # Structured markup is present instead.
    assert "<h2>Hp</h2>" in html
    assert "<li>a</li>" in html


# =========================================================================
# AC4 — No dev-note / placeholder leakage
# =========================================================================


@pytest.mark.parametrize("marker", _DEVNOTE_MARKERS)
def test_devnote_value_suppressed_from_output(tmp_path, marker):
    """A PUBLIC field whose value is a leading-token dev-note marker must not
    appear in the rendered HTML — suppressed, not rendered as prose."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=(f"genre_conventions:\n  designer_note: '{marker}: rewrite this section'\n"),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert marker not in html
    assert "rewrite this section" not in html


def test_devnote_substring_in_legit_prose_is_not_suppressed(tmp_path):
    """GUARD AGAINST OVER-SUPPRESSION. A marker appearing mid-sentence (not a
    leading token) is legitimate in-world prose and must still render. Keeps
    the guard from eating author content like 'a list of todos'."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=("genre_conventions:\n  custom: 'The clerk kept a list of todos on the wall.'\n"),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert "The clerk kept a list of todos on the wall." in html


def test_underscore_prefixed_key_suppressed(tmp_path):
    """A leading-underscore key is a private/dev field convention; the
    visibility stem-default makes it PUBLIC, so it currently leaks both a
    heading and its value. It must be suppressed."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=("genre_conventions:\n  _dev_note: 'internal scaffolding only'\n  setting: 'A real fact.'\n"),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert "internal scaffolding only" not in html
    # The non-private sibling still renders — suppression is surgical.
    assert "A real fact." in html


def test_devnote_in_scalar_list_item_suppressed(tmp_path):
    """AC4 HOLE (Reviewer HIGH #1). A dev-note marker inside a scalar-LIST
    value leaks today: ``_is_devnote`` is only checked on scalar dict values,
    so ``_render_list``'s scalar fast-path renders ``<li>TODO: …</li>`` raw.
    The marker item must be suppressed while a benign sibling list item still
    renders — surgical, exactly like the dict-value path."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=(
            "genre_conventions:\n"
            "  notes:\n"
            "    - 'TODO: fix this section'\n"
            "    - 'A real surviving note'\n"
        ),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert "TODO" not in html, "dev-note leaked through a scalar-list item"
    assert "fix this section" not in html
    # Surgical: the benign sibling list item still renders.
    assert "A real surviving note" in html


def test_devnote_list_item_suppression_fires_span(tmp_path, otel_capture):
    """Loud suppression on the list path too (No Silent Fallbacks) — dropping
    a list item is the same author-content decision as dropping a dict value,
    so it must fire the same span and be visible on the GM panel."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=("genre_conventions:\n  notes:\n    - 'FIXME: rebalance this'\n    - 'Keeps rendering'\n"),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert "FIXME" not in html
    assert "Keeps rendering" in html

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "sidequest.reference.devnote_suppressed"
    ]
    assert spans, (
        "expected a devnote_suppressed span for the list-item drop; emitted: "
        f"{sorted({s.name for s in otel_capture.get_finished_spans()})}"
    )


# =========================================================================
# AC6 — OTEL engagement (suppression fires a SPAN_REFERENCE_* span)
# =========================================================================


def test_devnote_suppressed_span_constant():
    """The guard's suppression span constant must exist and follow the
    ``sidequest.reference.*`` convention."""
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_DEVNOTE_SUPPRESSED

    assert SPAN_REFERENCE_DEVNOTE_SUPPRESSED == "sidequest.reference.devnote_suppressed"


def test_devnote_suppressed_span_registered_flat_only():
    """The span must be flat-only so the GM-panel ``agent_span_close`` fan-out
    reads it without a typed-event extractor (matches every other reference
    span)."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS
    from sidequest.telemetry.spans.reference import SPAN_REFERENCE_DEVNOTE_SUPPRESSED

    assert SPAN_REFERENCE_DEVNOTE_SUPPRESSED in FLAT_ONLY_SPANS


def test_devnote_suppressed_span_helper_name_and_attrs():
    """``reference_devnote_suppressed_span`` mirrors the established helper
    shape (pack / file_stem / key_path, world optional) so the GM panel can
    show WHICH field was dropped on WHICH page."""
    from unittest.mock import MagicMock

    from sidequest.telemetry.spans.reference import reference_devnote_suppressed_span

    tracer = MagicMock()
    cm = tracer.start_as_current_span.return_value
    cm.__enter__.return_value = MagicMock()
    cm.__exit__.return_value = False

    with reference_devnote_suppressed_span(
        pack="demo",
        world="demoworld",
        file_stem="lore",
        key_path=("genre_conventions", "designer_note"),
        _tracer=tracer,
    ):
        pass

    name = tracer.start_as_current_span.call_args[0][0]
    assert name == "sidequest.reference.devnote_suppressed"
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert attrs["reference.pack"] == "demo"
    assert attrs["reference.file_stem"] == "lore"
    assert attrs["reference.key_path"] == "genre_conventions.designer_note"


def test_devnote_suppression_fires_span_during_real_render(tmp_path, otel_capture):
    """WIRING TEST — drive a full ``assemble_lore_page`` render through the
    production code path and assert the suppression span actually fired. This
    keeps the guard honest: it proves the helper is wired into the fallback
    walk, not merely defined."""
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=("genre_conventions:\n  designer_note: 'TODO: rewrite this section'\n"),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)
    assert "TODO" not in html  # suppressed in output

    spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "sidequest.reference.devnote_suppressed"
    ]
    assert spans, (
        "expected a devnote_suppressed span; emitted: "
        f"{sorted({s.name for s in otel_capture.get_finished_spans()})}"
    )


# =========================================================================
# AC5 — Output-scan GATE (the comprehensive artifact scan)
# =========================================================================


def test_lore_page_output_scan_has_no_raw_dev_strings(tmp_path):
    """THE GATE. Render a fixture lore page whose fallback-walk content mixes
    every hazard — snake_case + caps key, bool, dev-note value (dict AND list),
    private key, nested container — and assert the *served HTML artifact* is
    free of every raw developer signature. Behavior-against-artifact, not
    source-text.

    Strengthened (Reviewer HIGH #2): the original scan was all-negative and
    would pass green even if the entire ``genre_conventions`` section were
    dropped (no content, vacuous heading loop). The POSITIVE assertions below
    prove suppression is *surgical* — real humanized content survives — so the
    gate distinguishes a clean fix from a total-failure regression. It now also
    covers the list-valued dev-note path (Reviewer HIGH #1).
    """
    pack_dir, world_dir = _write_lore_pack(
        tmp_path,
        lore_yaml=(
            "genre_conventions:\n"
            "  is_lethal: true\n"
            "  is_optional: false\n"
            "  MECHANICAL_surface: 'present'\n"
            "  designer_note: 'TODO: rebalance the lethality dial'\n"
            "  _internal_flag: 'scaffolding'\n"
            "  notes:\n"
            "    - 'FIXME: drop this list item'\n"
            "    - 'This in-world note survives'\n"
            "  nested:\n"
            "    sub_value: 5\n"
        ),
    )
    html = assemble_lore_page("demo", "demoworld", pack_dir, world_dir)

    # --- POSITIVE: real humanized content IS present (suppression is surgical,
    # not total — guards against the section being dropped wholesale). ---
    headings = _headings(html)
    assert headings, "expected rendered headings; output appears empty"
    assert "Mechanical Surface" in html, "humanized snake_case+caps heading missing"
    assert "Yes" in html, "bool True should humanize to Yes and survive"
    assert "No" in html, "bool False should humanize to No and survive"
    assert "This in-world note survives" in html, "benign list item must survive"
    assert "<p>5</p>" in html, "nested scalar value must survive"

    # --- NEGATIVE: no raw bool tokens in text nodes. ---
    assert ">True<" not in html and "<p>True</p>" not in html, "raw bool True leaked"
    assert ">False<" not in html and "<p>False</p>" not in html, "raw bool False leaked"

    # No dev-note / placeholder prose — dict value, list item, or private key.
    assert "TODO" not in html, "dev-note marker leaked (dict value)"
    assert "rebalance the lethality dial" not in html, "dev-note prose leaked"
    assert "FIXME" not in html, "dev-note marker leaked (list item)"
    assert "drop this list item" not in html, "dev-note list prose leaked"
    assert "scaffolding" not in html, "private-key value leaked"

    # No raw underscore in any heading.
    for h in headings:
        assert "_" not in h, f"raw underscore in heading: {h!r}"
    assert "MECHANICAL_surface" not in html, "raw snake_case+caps key leaked"

    # No Python container repr signatures.
    assert "{'" not in html, "dict repr leaked"
    assert "[{" not in html, "list-of-dict repr leaked"
    assert "': " not in html, "dict-item repr leaked"
