"""Handler helpers extracted from ``websocket_session_handler``.

These are the module-level (non-``self``-bound) helpers that the
``WebSocketSessionHandler`` class delegates to. They live here so the
6.8k-line session handler can shed its free-function surface without
churning the class itself. The class re-imports the names it calls, so
external importers (e.g. ``session_handler``) keep working unchanged.
"""
