"""tools/_retired — package for retired SDK tools (Story 59-4+).

Modules in this package are NOT imported by the active tool barrel at
``sidequest/agents/tools/__init__.py`` and therefore do NOT auto-register
on ``default_registry``. They exist for developer-facing breadcrumb
purposes: a grep for an old tool name lands on the relocated stub
whose docstring points at the live mechanism that replaced it.

Adding a tool here is a one-way operation — once a tool is retired,
nothing in production code should import it. If you find yourself
adding a runtime caller, you are undoing the retirement; re-open the
ADR that retired it and decide explicitly.
"""
