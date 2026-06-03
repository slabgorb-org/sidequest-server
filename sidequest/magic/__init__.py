"""Magic-system runtime — plugins, ledger bars, validator.

See docs/design/magic-taxonomy.md for the framework.
See docs/superpowers/specs/2026-04-28-magic-system-coyote-star-implementation-design.md
for the v1 implementation scope.
"""

# Production wiring (ADR-126, story 81-1): importing the plugins package fires
# each plugin submodule's import-time registration into MAGIC_PLUGINS, mirroring
# the sidequest/telemetry/spans star-import-of-domain-modules pattern that
# plugins/__init__.py cites. Without this, nothing in production imports the
# package, MAGIC_PLUGINS stays empty at runtime, and validator.validate()
# mis-fires `plugin_known_but_not_registered` for correctly-configured workings.
# Cycle-safe regardless of import order within this __init__: the plugin
# submodules import `sidequest.magic.models` / `sidequest.magic.plugin` as
# standalone modules (neither reaches back into this package's __init__), so
# they load cleanly even while `sidequest.magic` is still initializing. Fail
# loud: a broken plugin import must raise here, never be silently swallowed.
import sidequest.magic.plugins  # noqa: E402, F401
from sidequest.magic.context_builder import (
    build_magic_context_block,
    build_magic_static_block,
    build_magic_volatile_block,
)
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
    "build_magic_static_block",
    "build_magic_volatile_block",
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
