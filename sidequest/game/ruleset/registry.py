from __future__ import annotations

from sidequest.game.ruleset.base import RulesetModule, UnknownRulesetError
from sidequest.game.ruleset.native import NativeRulesetModule

# Modules are stateless behavior -> safe singletons. New modules register here as their plans land.
_REGISTRY: dict[str, RulesetModule] = {
    NativeRulesetModule.slug: NativeRulesetModule(),
}


def get_ruleset_module(slug: str) -> RulesetModule:
    """Resolve a registered ruleset module. Fails loud — never returns a default/fallback."""
    module = _REGISTRY.get(slug)
    if module is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownRulesetError(f"Unknown ruleset {slug!r}; registered rulesets: {known}")
    return module
