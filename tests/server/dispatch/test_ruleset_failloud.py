"""Tests for fail-loud ruleset slug enforcement (spec 2026-06-17 §1, No Silent Fallbacks).

All three silent-default sites must raise instead of returning a "dial" / "native"
default when pack/rules are absent.  The canonical shape is:
  - instantiate_table_encounter: ruleset_slug is a required parameter (no default).
  - pregen.seed_manual: raises ValueError if pack or pack.rules is None.
  - encounter_lifecycle call site: raises ValueError via _raise_missing_ruleset.
"""

import inspect

from sidequest.server.dispatch.encounter_lifecycle import instantiate_table_encounter


def test_table_encounter_requires_explicit_ruleset_slug():
    """ruleset_slug is now required (no 'dial' default).

    Omitting it is a TypeError at the call boundary — the param has no default.
    """
    sig = inspect.signature(instantiate_table_encounter)
    assert sig.parameters["ruleset_slug"].default is inspect.Parameter.empty, (
        "ruleset_slug must have no default — a missing ruleset is a config error, "
        "not a silent 'dial' default (spec 2026-06-17 §1, No Silent Fallbacks)"
    )
