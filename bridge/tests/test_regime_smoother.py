"""RegimeSmoother — temporal hysteresis on the mechanical regime/trend label.

A new (regime, trend) read must persist `min_bars` consecutive bars before it replaces
the committed label; transient 1-2 bar flips are held off. min_bars <= 1 is a no-op.
"""

from __future__ import annotations

from hermes_bridge.engine import RegimeSmoother


def test_min_bars_1_is_passthrough():
    s = RegimeSmoother(min_bars=1)
    assert s.update("trending", "down") == ("trending", "down")
    assert s.update("transitional", "flat") == ("transitional", "flat")   # adopts at once
    assert s.update("trending", "up") == ("trending", "up")


def test_first_read_adopts_immediately():
    s = RegimeSmoother(min_bars=3)
    assert s.update("trending", "down") == ("trending", "down")


def test_transient_flip_is_held_off():
    s = RegimeSmoother(min_bars=3)
    s.update("trending", "down")                                       # commit trending/down
    assert s.update("trending", "up") == ("trending", "down")          # 1-bar blip: held
    assert s.update("trending", "down") == ("trending", "down")        # confirms committed
    assert s.update("transitional", "flat") == ("trending", "down")    # blip 1
    assert s.update("transitional", "flat") == ("trending", "down")    # blip 2: still held


def test_sustained_change_flips_after_min_bars():
    s = RegimeSmoother(min_bars=3)
    s.update("trending", "down")
    assert s.update("trending", "up") == ("trending", "down")          # 1
    assert s.update("trending", "up") == ("trending", "down")          # 2
    assert s.update("trending", "up") == ("trending", "up")            # 3 -> flip


def test_interrupted_streak_resets():
    s = RegimeSmoother(min_bars=3)
    s.update("trending", "down")
    s.update("trending", "up")                                         # candidate up, streak 1
    s.update("ranging", "flat")                                        # new candidate -> streak 1
    assert (s.regime, s.trend) == ("trending", "down")                # still committed
    s.update("ranging", "flat")                                        # streak 2
    assert s.update("ranging", "flat") == ("ranging", "flat")          # streak 3 -> flip
