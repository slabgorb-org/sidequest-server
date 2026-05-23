"""Render parsed-YAML trees as a hypertext document.

Pure functions only. The HTTP boundary (404 / 500 / file IO) lives in
reference_routes.py. This module just walks dict/list/scalar trees and produces
HTML fragments.

Headings get stable slugified ``id`` attributes so future deep-link work can
target them without schema changes.
"""
from __future__ import annotations

import re
from html import escape

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ASCII slug. Non-ASCII chars become separators; runs collapse."""
    lowered = text.lower()
    return _SLUG_RE.sub("-", lowered).strip("-")


def render_node(node: object) -> str:
    """Render a parsed-YAML node to an HTML fragment.

    Handles: dict, list (later tasks), scalar (str/int/float/bool/None).
    """
    if isinstance(node, dict):
        return _render_dict(node)
    return _render_scalar(node)


def _render_scalar(value: object) -> str:
    if value is None:
        return "<p><em>(none)</em></p>"
    text = str(value)
    if "\n" in text:
        return f'<p class="multiline">{escape(text)}</p>'
    return f"<p>{escape(text)}</p>"


def _render_dict(node: dict) -> str:
    parts: list[str] = []
    for key, value in node.items():
        slug = slugify(str(key))
        parts.append(f'<section id="{slug}">')
        parts.append(f"<h2>{escape(str(key))}</h2>")
        parts.append(render_node(value))
        parts.append("</section>")
    return "".join(parts)
