import re


def slugify_player_name(name: str) -> str:
    """Mirror of ``sidequest_daemon.media.catalogs._slugify_name``.

    Lowercase, collapse runs of whitespace to ``_``, drop punctuation except
    ``_`` and ``-``. We mirror the daemon's rule rather than importing the
    helper because the server doesn't depend on the daemon package — and
    duplicating five lines is cheaper than introducing a cross-repo runtime
    dependency for a single call site. The contract that matters is *output
    equality* on the same input; the wiring test pins shared cases.
    """
    lowered = name.strip().lower()
    collapsed = re.sub(r"\s+", "_", lowered)
    return re.sub(r"[^a-z0-9_-]", "", collapsed)
