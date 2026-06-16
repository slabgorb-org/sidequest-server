"""Magic-system runtime — plugins, ledger bars, validator.

See docs/design/magic-taxonomy.md for the framework.
See docs/superpowers/specs/2026-04-28-magic-system-coyote-star-implementation-design.md
for the v1 implementation scope.
"""

from sidequest.magic.context_builder import build_magic_context_block
from sidequest.magic.models import (
    Flag,
    FlagSeverity,
    HardLimit,
    LedgerBarSpec,
    MagicWorking,
    Plugin,
    StatusPromotion,
    WorldKnowledge,
    WorldMagicConfig,
)
from sidequest.magic.plugin import MAGIC_PLUGINS, MagicPlugin, get_plugin
from sidequest.magic.state import (
    ApplyWorkingResult,
    BarKey,
    LedgerBar,
    MagicState,
    ThresholdCrossingEvent,
    WorkingRecord,
)

__all__ = [
    "MAGIC_PLUGINS",
    "ApplyWorkingResult",
    "build_magic_context_block",
    "BarKey",
    "Flag",
    "FlagSeverity",
    "HardLimit",
    "LedgerBar",
    "LedgerBarSpec",
    "MagicPlugin",
    "MagicState",
    "MagicWorking",
    "Plugin",
    "StatusPromotion",
    "ThresholdCrossingEvent",
    "WorkingRecord",
    "WorldKnowledge",
    "WorldMagicConfig",
    "get_plugin",
]
