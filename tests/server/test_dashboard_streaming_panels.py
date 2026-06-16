"""Tests for the streaming-pipeline panels on the OTEL dashboard.

Task 14 adds two panels to the GM dashboard:
  1. TTFT histogram — distribution of narrator.stream.first_token.ttft_seconds
  2. JSON parse-status pie — counts of narrator.stream.complete.json_parse_status

These tests assert that the static dashboard.html:
  a) Carries the required DOM container IDs for both panels.
  b) References the correct span names the panels read from.
  c) References the correct attribute names that populate the panels.
  d) Shows "no data" explicitly when no streaming spans have arrived.
  e) Has a labeled tab entry so the panels are reachable.
"""

from __future__ import annotations

from importlib.resources import files


def _dashboard_html() -> str:
    asset = files("sidequest.server").joinpath("static/dashboard.html")
    return asset.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# DOM structure: panels must exist and be reachable
# ---------------------------------------------------------------------------


def test_streaming_tab_exists_in_dashboard() -> None:
    """Dashboard must have a Streaming tab so Keith can reach the panels."""
    html = _dashboard_html()
    # The tab label must reference Streaming so users can find it
    assert "Streaming" in html, (
        "dashboard.html must have a 'Streaming' tab for the streaming pipeline panels"
    )


def test_ttft_histogram_container_present() -> None:
    """The TTFT histogram panel must have a DOM container the JS can target."""
    html = _dashboard_html()
    assert 'id="ttft-hist-chart"' in html, (
        "dashboard.html must contain id='ttft-hist-chart' for the TTFT histogram"
    )


def test_parse_status_pie_container_present() -> None:
    """The JSON parse-status pie panel must have a DOM container the JS can target."""
    html = _dashboard_html()
    assert 'id="parse-status-chart"' in html, (
        "dashboard.html must contain id='parse-status-chart' for the JSON parse-status pie"
    )


# ---------------------------------------------------------------------------
# Span name references: panels must read from the correct spans
# ---------------------------------------------------------------------------


def test_dashboard_js_reads_first_token_span_name() -> None:
    """The TTFT histogram panel must collect narrator.stream.first_token spans."""
    html = _dashboard_html()
    assert "narrator.stream.first_token" in html, (
        "dashboard.html JS must reference 'narrator.stream.first_token' to populate "
        "the TTFT histogram"
    )


def test_dashboard_js_reads_complete_span_name() -> None:
    """The parse-status pie must collect narrator.stream.complete spans."""
    html = _dashboard_html()
    assert "narrator.stream.complete" in html, (
        "dashboard.html JS must reference 'narrator.stream.complete' to populate "
        "the JSON parse-status pie"
    )


# ---------------------------------------------------------------------------
# Attribute name references: panels must read the correct fields
# ---------------------------------------------------------------------------


def test_dashboard_js_reads_ttft_seconds_attribute() -> None:
    """The TTFT histogram must read the ttft_seconds attribute from spans."""
    html = _dashboard_html()
    assert "ttft_seconds" in html, (
        "dashboard.html JS must reference 'ttft_seconds' from narrator.stream.first_token"
    )


def test_dashboard_js_reads_json_parse_status_attribute() -> None:
    """The parse-status pie must read the json_parse_status attribute from spans."""
    html = _dashboard_html()
    assert "json_parse_status" in html, (
        "dashboard.html JS must reference 'json_parse_status' from narrator.stream.complete"
    )


# ---------------------------------------------------------------------------
# No-data guard: panels must show an explicit message when data is absent
# ---------------------------------------------------------------------------


def test_dashboard_ttft_shows_no_data_string() -> None:
    """Per CLAUDE.md 'No Silent Fallbacks': the TTFT panel must show a
    visible 'no data' message, not silently render nothing."""
    html = _dashboard_html()
    # The JS must include some form of "no data" or "No data" or "no streaming"
    # so an empty dashboard is never mistaken for a working-but-quiet system.
    has_no_data = (
        "no data" in html.lower()
        or "No streaming" in html
        or "no ttft" in html.lower()
        or "No TTFT" in html
    )
    assert has_no_data, (
        "dashboard.html must show an explicit 'no data' message when no "
        "narrator.stream.first_token spans have arrived"
    )


def test_dashboard_parse_status_shows_no_data_string() -> None:
    """Per CLAUDE.md 'No Silent Fallbacks': the parse-status pie must show a
    visible 'no data' message when no complete spans have been received."""
    html = _dashboard_html()
    has_no_data = (
        "no data" in html.lower()
        or "No streaming" in html
        or "no parse" in html.lower()
        or "No JSON" in html
    )
    assert has_no_data, (
        "dashboard.html must show an explicit 'no data' message when no "
        "narrator.stream.complete spans have arrived"
    )


# ---------------------------------------------------------------------------
# Wiring: the streaming tab must be activated by switchTab
# ---------------------------------------------------------------------------


def test_streaming_tab_wired_into_switchtab() -> None:
    """switchTab must activate the streaming tab when it becomes active,
    so renderStreaming is called on tab switch."""
    html = _dashboard_html()
    assert "renderStreaming" in html, (
        "dashboard.html must call renderStreaming() from switchTab so the panels "
        "repaint when the user switches to the Streaming tab"
    )
