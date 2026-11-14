"""Legacy Safe bar for fixture tests that cover OTHER features (constraints, scopes, Over/Under modes, slip edits).

Their synthetic legs carry no team stats, Sportradar ids or last-10 records, so they test the rules layer with the old
Safe definition (est >= 80%, no safety.py check). The new Safe definition (market AND stats model >= 85%, league
quality, last-10 record rules, missing stats = out, no unders unless asked) is covered by tests/test_safety.py and
tests/test_ladder.py with realistic legs. Import this module BEFORE scanner / constraints / safety.
"""
import os

os.environ["SAFE_MIN_P"] = "0.80"


def install() -> None:
    import safety
    safety.is_safe = lambda l, allow_unders=False, widened=False: True
