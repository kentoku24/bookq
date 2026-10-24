"""dual-reader (2-device synchronized spread) shared contracts.

S01 (#76): pure model + wire types. No transport, no DOM, no I/O here —
everything in this package must stay deterministic and unit-testable.
S04 (#79): catalog builds the shared work manifest off the existing
scan_tree + /api/images enumeration (the only I/O in the package).
"""
