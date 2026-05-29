from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidequest.genre.models.rules import CwnConfig, HackingConfig


def test_hacking_config_valid():
    cfg = HackingConfig(
        default_tier="office",
        security_tiers={"home": 7, "office": 9, "facility": 11, "black_site": 12},
    )
    assert cfg.security_tiers["black_site"] == 12
    assert cfg.default_tier == "office"


def test_hacking_config_rejects_empty_ladder():
    with pytest.raises(ValidationError, match="security_tiers must be non-empty"):
        HackingConfig(default_tier="office", security_tiers={})


def test_hacking_config_rejects_default_not_in_ladder():
    with pytest.raises(ValidationError, match="default_tier"):
        HackingConfig(default_tier="moon_base", security_tiers={"office": 9})


def test_hacking_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        HackingConfig(
            default_tier="office", security_tiers={"office": 9}, bogus=1
        )


def test_cwn_config_hacking_defaults_none():
    # hacking is optional on the model; a CWN pack that never runs net_run
    # need not author it. A net_run firing without it fails loud at dispatch
    # (Task 5), not at model load.
    cfg = CwnConfig(attribute_map={})
    assert cfg.hacking is None
