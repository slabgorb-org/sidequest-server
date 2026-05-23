"""Unit tests for the reference-page YAML→HTML walker.

Renderer must produce stable, escaped HTML from arbitrary YAML trees. The walker
is pure (input dict/list/scalar → output str); no IO, no globals.
"""
import pytest

from sidequest.server.reference_renderer import render_node, slugify


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Amateur Sleuth") == "amateur-sleuth"


def test_slugify_strips_non_ascii():
    assert slugify("Café Crème") == "caf-cr-me"


def test_slugify_collapses_runs_of_separators():
    assert slugify("a // b -- c") == "a-b-c"


def test_render_scalar_string_escapes_html():
    assert render_node("Hello <world>") == "<p>Hello &lt;world&gt;</p>"


def test_render_scalar_int():
    assert render_node(42) == "<p>42</p>"


def test_render_scalar_multiline_uses_pre_wrap():
    html = render_node("line1\nline2\nline3")
    assert 'class="multiline"' in html
    assert "line1\nline2\nline3" in html


def test_render_flat_dict_emits_section_per_key():
    html = render_node({"name": "Sleuth", "tier": "novice"})
    assert '<section id="name">' in html
    assert "<h2>name</h2>" in html
    assert "<p>Sleuth</p>" in html
    assert '<section id="tier">' in html


def test_render_node_list_raises_until_task_2():
    with pytest.raises(NotImplementedError) as exc:
        render_node(["alpha", "beta"])
    assert "Task 2" in str(exc.value)


def test_render_nested_dict_recurses_as_section():
    """Locks down intentional behavior: nested dicts produce nested <section>.

    The spec design doc mentioned <dl>/<dt>/<dd> for nested dicts but the
    plan explicitly recurses via <section> (see Task 2 test for
    'h2 stats' inside a list-item context). This test pins the recursion.
    """
    html = render_node({"outer": {"inner": "value"}})
    assert '<section id="outer">' in html
    assert "<h2>outer</h2>" in html
    assert '<section id="inner">' in html
    assert "<h2>inner</h2>" in html
    assert "<p>value</p>" in html


def test_render_flat_dict_preserves_insertion_order():
    """Section ordering must follow dict insertion order — load-bearing for
    eventual file-by-file page assembly (Task 5).
    """
    html = render_node({"first": "a", "second": "b", "third": "c"})
    assert html.index('id="first"') < html.index('id="second"') < html.index('id="third"')


def test_render_multiline_escapes_html_content():
    """Multiline scalars must HTML-escape just like single-line scalars — XSS
    regression lock for the multiline branch.
    """
    html = render_node("line1\n<script>evil</script>\nline3")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_render_none_emits_placeholder():
    """YAML null renders as a labeled placeholder, not as silent empty <p>."""
    assert render_node(None) == "<p><em>(none)</em></p>"
