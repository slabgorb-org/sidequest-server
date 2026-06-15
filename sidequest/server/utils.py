import re

from sidequest.foundation.slug_fold import fold_to_ascii


def slugify_player_name(name: str) -> str:
    """Mirror of ``sidequest_daemon.media.catalogs._slugify_name``.

    NFKD-fold non-ASCII (the shared :func:`fold_to_ascii` core, Story 101-8),
    then lowercase, collapse runs of whitespace to ``_``, and drop punctuation
    except ``_`` and ``-``. We mirror the daemon's rule rather than importing the
    helper because the server doesn't depend on the daemon package — and
    duplicating five lines is cheaper than introducing a cross-repo runtime
    dependency for a single call site. The contract that matters is *output
    equality* on the same input; the wiring test pins shared cases. ASCII output
    is unchanged by the fold (it is a no-op on ASCII); diacritics now fold to
    base letters (``café`` → ``cafe``) instead of being dropped.
    """
    lowered = fold_to_ascii(name).strip().lower()
    collapsed = re.sub(r"\s+", "_", lowered)
    return re.sub(r"[^a-z0-9_-]", "", collapsed)
