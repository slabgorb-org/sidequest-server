"""Assert that the server-rendered /dashboard and /forensics HTML routes have been
removed. The React Inspector is now the canonical UI; these server-side pages
are dead duplicates (Task C1 of the unified-inspector teardown plan).

The /api/debug/* and /api/sessions/* REST data routes MUST remain — this test
only checks the two HTML page routes are gone.
"""

from pathlib import Path

from sidequest.server.app import create_app


def test_legacy_dashboard_and_forensics_html_routes_removed(tmp_path: Path) -> None:
    """The /dashboard and /forensics HTML page routes must not be registered."""
    packs = tmp_path / "genre_packs"
    packs.mkdir(parents=True, exist_ok=True)
    saves = tmp_path / "saves"
    saves.mkdir(parents=True, exist_ok=True)
    app = create_app(genre_pack_search_paths=[packs], save_dir=saves)

    paths = {route.path for route in app.routes}
    assert "/dashboard" not in paths, (
        "/dashboard HTML route is still registered — it must be deleted as part of "
        "the unified-inspector teardown (Task C1)."
    )
    assert "/forensics" not in paths, (
        "/forensics HTML route is still registered — it must be deleted as part of "
        "the unified-inspector teardown (Task C1)."
    )
