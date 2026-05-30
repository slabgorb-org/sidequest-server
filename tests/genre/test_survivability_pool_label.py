"""RED tests — Story 68-1: per-genre survivability-pool label reskin.

Epic 68 (genre tone, terminology & narrator reliability): social genre packs
should surface a flavor-appropriate label for the survivability (HP) pool —
Composure / Standing / Poise — instead of the default "HP" / "Vitality". The
label is a **paired content+server field**: it originates in a pack's
``rules.yaml`` and must be accepted by the server's ``RulesConfig`` model.

Because ``RulesConfig`` is ``extra="forbid"`` (No Silent Fallbacks — a typo'd
key fails the pack load loudly), the field must be added to the model itself; a
content-only change would be rejected at load. These tests pin both halves of
that contract plus the per-pack behavior:

* AC1/AC2 — ``RulesConfig`` accepts ``survivability_pool_label`` while keeping
  ``extra="forbid"`` intact (unknown keys still rejected).
* AC3 — ``tea_and_murder`` (a social pack) loads with a social label.
* AC5 — ``caverns_and_claudes`` (a mechanical pack) leaves the label unset so
  the UI falls back to the default "HP".

The pack-loading tests are the wiring/integration tests: they drive real
content through the production ``load_genre_pack`` loader, not a synthetic
fixture, so a regression in either the model or the YAML is caught.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import RulesConfig

from tests._helpers.genre_paths import find_pack_path

# The flavor labels Epic 68 sanctions for social-register survivability pools.
SOCIAL_SURVIVABILITY_LABELS = {"Composure", "Standing", "Poise"}


# --- AC1/AC2: the paired server field on RulesConfig -----------------------


def test_rules_config_accepts_survivability_pool_label():
    """AC1/AC2 — the model round-trips an explicit survivability label."""
    rules = RulesConfig.model_validate({"survivability_pool_label": "Composure"})
    assert rules.survivability_pool_label == "Composure"


def test_rules_config_defaults_survivability_pool_label_to_none():
    """AC5 — omitting the field yields None so downstream applies the default.

    The label must be optional; the overwhelming majority of (mechanical) packs
    never set it and must keep the legacy "HP" surface.
    """
    rules = RulesConfig.model_validate({})
    assert rules.survivability_pool_label is None


def test_rules_config_still_forbids_unknown_field():
    """AC2 — the field is added to the model, NOT smuggled in via extra='allow'.

    This is the No-Silent-Fallbacks guard: a typo'd label key (e.g.
    ``survivabilty_pool_label``) must still raise, proving the new field was
    declared explicitly rather than the whole model being loosened.
    """
    with pytest.raises(ValidationError):
        RulesConfig.model_validate({"survivabilty_pool_label": "Composure"})


# --- AC3: social pack carries a social label (real content through loader) --


def test_tea_and_murder_pack_has_social_survivability_label():
    """AC3 — tea_and_murder (social) reskins the survivability pool.

    Loads the real pack through the production loader. The label must be set to
    one of the sanctioned social terms — never the default HP/Vitality.
    """
    pack = load_genre_pack(find_pack_path("tea_and_murder"))
    label = pack.rules.survivability_pool_label
    assert label is not None, "tea_and_murder must set a survivability_pool_label"
    assert label in SOCIAL_SURVIVABILITY_LABELS, (
        f"tea_and_murder survivability label {label!r} is not a sanctioned social "
        f"term {sorted(SOCIAL_SURVIVABILITY_LABELS)}"
    )
    assert label not in {"HP", "Vitality"}


# --- AC5: mechanical pack keeps the default (loads to None) -----------------


def test_caverns_and_claudes_keeps_default_survivability_label():
    """AC5 — a mechanical fantasy pack leaves the label unset (→ default HP).

    Reskinning social packs must not shift the default for mechanical packs.
    """
    pack = load_genre_pack(find_pack_path("caverns_and_claudes"))
    assert pack.rules.survivability_pool_label is None
