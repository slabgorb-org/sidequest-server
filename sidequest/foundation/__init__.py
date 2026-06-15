"""Foundation floor — pure helpers below the server tier (ADR-147).

The lowest application tier. Modules here depend only on third-party
libraries, ``sidequest.protocol`` (pure types), and the foundation-level
watcher API (``sidequest.telemetry``, ADR-132). Nothing under this package
may import from ``sidequest.server``; the import-direction law is
``foundation <- {game, genre, orbital, magic, interior} <- server``.
"""
