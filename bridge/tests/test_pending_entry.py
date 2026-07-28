"""Fill-time attribution of `_pending_entry` (journal hygiene for the learning loop).

The memo is stamped with cmd_id/ts/side when risk approves an entry. A fill is only
journaled under that memo's context/rationale when it plausibly came from it: same
side, recent enough. Stale-dropped commands disarm the memo via entry_dropped().
Anything else (manual fills, /agent/command, a dropped command that filled anyway)
is journaled as an unattributed fill so reflection never learns from mislabeled trades.
"""

from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.indicators import build_context
from hermes_bridge.journal import JournalStore
from hermes_bridge.models import Action, Fill, OrderCommand, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def _engine(cfg, tmp_path):
    js = JournalStore(str(tmp_path / "j.jsonl"))
    eng = TradingEngine(cfg, BarStore("ES", "5m"),
                        SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                        build_agent_client(cfg), RiskGate(cfg), journal=js)
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    eng.last_context = build_context(bars, atr_period=14)
    return eng, js, bars


def _memo(eng, ts, side=Side.LONG):
    return {"cmd_id": "cmd-1", "ts": ts, "side": side,
            "context": eng.last_context, "rationale": "armed entry", "confidence": 0.7}


def _round_trip(eng, ts, entry_side=Side.LONG):
    exit_side = Side.SHORT if entry_side == Side.LONG else Side.LONG
    eng.on_fill(Fill(side=entry_side, qty=1, price=100.0, ts=ts))
    eng.on_fill(Fill(side=exit_side, qty=1, price=100.5, ts=ts + 60))


def test_fresh_matching_memo_attributes(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    _round_trip(eng, bars[-1].ts + 10)
    recs = js.all()
    assert len(recs) == 1
    assert recs[0]["rationale"] == "armed entry"
    assert recs[0]["confidence"] == 0.7


def test_entry_dropped_clears_memo_and_fill_is_unattributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    eng.entry_dropped("cmd-1")
    assert eng._pending_entry is None
    _round_trip(eng, bars[-1].ts + 10)
    recs = js.all()
    assert len(recs) == 1
    assert "unattributed" in recs[0]["rationale"]


def test_entry_dropped_ignores_other_command_ids(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    eng.entry_dropped("someone-else")
    assert eng._pending_entry is not None


def test_side_mismatch_is_not_attributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts, side=Side.LONG)
    _round_trip(eng, bars[-1].ts + 10, entry_side=Side.SHORT)  # short fill vs long memo
    recs = js.all()
    assert len(recs) == 1
    assert recs[0]["side"] == "SHORT"
    assert "unattributed" in recs[0]["rationale"]


def test_stale_memo_is_not_attributed(cfg, tmp_path):
    eng, js, bars = _engine(cfg, tmp_path)
    eng._pending_entry = _memo(eng, bars[-1].ts)
    _round_trip(eng, bars[-1].ts + 3600)  # an hour later — far past budget + one bar
    recs = js.all()
    assert len(recs) == 1
    assert "unattributed" in recs[0]["rationale"]


def _armed_memo(eng, bars, cmd, side=Side.LONG):
    """A memo built the way the engine builds one at APPROVAL time: brackets/1R derived
    around the just-closed bar, because the fill price does not exist yet."""
    memo = _memo(eng, bars[-1].ts, side=side)
    memo["command"] = cmd
    memo["stop_ticks"] = eng._command_stop_ticks(cmd, bars[-1].close)
    memo["brackets"] = eng._command_brackets(cmd, bars[-1].close)
    return memo


def test_tick_bracket_anchors_to_fill_not_bar_close(cfg, tmp_path):
    """A TICK bracket must be journaled around the ACTUAL FILL, not the bar close.

    NinjaTrader places a tick bracket with CalculationMode.Ticks, which it anchors to the
    real entry fill. Journaling it around bar.close instead put the recorded stop/target a
    fill-gap away from the bracket that was actually resting (seen live 2026-07-28: both
    trades logged levels exactly 1.00 off NT8's). The learning loop reads these levels, so
    the anchors have to agree.
    """
    eng, js, bars = _engine(cfg, tmp_path)
    tick = cfg.instrument.tick_size or 0.25
    cmd = OrderCommand(id="cmd-1", strategy_id=cfg.strategy_id, action=Action.ENTER_LONG,
                       qty=1, stop_ticks=40, target_ticks=60)
    eng._pending_entry = _armed_memo(eng, bars, cmd)

    # Fill a clean 1.00 away from the close, as happened live. Snapped to the tick grid so
    # the assertions compare exact prices rather than synthetic-bar float noise.
    fill_price = round(bars[-1].close * 4) / 4 + 1.0
    assert fill_price != bars[-1].close, "fill must differ from the close to prove the anchor"
    ts = bars[-1].ts + 10
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=fill_price, ts=ts))
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=fill_price + 0.5, ts=ts + 60))

    rec = js.all()[0]
    assert rec["entry_price"] == fill_price
    assert rec["stop_price"] == fill_price - 40 * tick
    assert rec["target_price"] == fill_price + 60 * tick
    # 1R for the trade manager must measure off the same anchor.
    assert eng._command_stop_ticks(cmd, fill_price) == 40


def test_explicit_price_bracket_is_journaled_verbatim(cfg, tmp_path):
    """An ABSOLUTE bracket goes to NinjaTrader as CalculationMode.Price, so the fill price
    is irrelevant — the journal must record exactly what was sent, not re-anchor it."""
    eng, js, bars = _engine(cfg, tmp_path)
    cmd = OrderCommand(id="cmd-1", strategy_id=cfg.strategy_id, action=Action.ENTER_LONG,
                       qty=1, stop_price=90.0, target_price=115.0)
    eng._pending_entry = _armed_memo(eng, bars, cmd)

    ts = bars[-1].ts + 10
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=bars[-1].close + 1.0, ts=ts))
    eng.on_fill(Fill(side=Side.SHORT, qty=1, price=101.0, ts=ts + 60))

    rec = js.all()[0]
    assert rec["stop_price"] == 90.0
    assert rec["target_price"] == 115.0
