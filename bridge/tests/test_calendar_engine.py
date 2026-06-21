"""The engine stands down for exchange holidays / early closes: it flattens any open position
ahead of the close (gap risk) and takes no new entries — deterministic + server-side, the same
authority as the daily-goal flatten, never delegated to the brain. (The Juneteenth 2026 gap the
operator had to flatten by hand.)"""

from datetime import UTC, datetime

from hermes_bridge.agent_client import build_agent_client
from hermes_bridge.engine import TradingEngine
from hermes_bridge.models import Action, Bar, Fill, Side
from hermes_bridge.risk import RiskGate
from hermes_bridge.session import SessionState
from hermes_bridge.store import BarStore
from tests.conftest import synthetic_bars


def _engine(cfg):
    return TradingEngine(cfg, BarStore("ES", "5m"),
                         SessionState("ES", "5m", 0.25, 12.5, 500, 400),
                         build_agent_client(cfg), RiskGate(cfg))


def _seed_long(eng):
    bars = synthetic_bars(60)
    for b in bars:
        eng.store.append(b)
    px = bars[-1].close
    eng.on_fill(Fill(side=Side.LONG, qty=1, price=px, ts=bars[-1].ts))
    assert eng.session.position == 1
    return px


def _bar(ts: float, px: float) -> Bar:
    return Bar(ts=ts, open=px, high=px + 1, low=px - 1, close=px)


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def test_early_close_flattens_open_position(cfg):
    eng = _engine(cfg)
    px = _seed_long(eng)
    # Black Friday 2026, 12:50 ET (17:50 UTC, EST) — inside the 15-min lead before 13:00.
    res = eng.on_bar(_bar(_ts(2026, 11, 27, 17, 50), px))
    assert res.decision.action is Action.FLATTEN
    assert res.decision.rationale == "early_close"
    assert res.command is not None and res.command.action is Action.FLATTEN


def test_holiday_flattens_open_position(cfg):
    eng = _engine(cfg)
    px = _seed_long(eng)
    # Juneteenth 2026, 12:00 ET (16:00 UTC, EDT) — a full holiday, flatten all day.
    res = eng.on_bar(_bar(_ts(2026, 6, 19, 16, 0), px))
    assert res.decision.action is Action.FLATTEN
    assert res.decision.rationale == "holiday:Juneteenth"


def test_holiday_blocks_new_entries(cfg):
    eng = _engine(cfg)
    for b in synthetic_bars(60):
        eng.store.append(b)
    res = eng.on_bar(_bar(_ts(2026, 6, 19, 16, 0), 4000.0))
    assert res.decision.action is Action.WAIT
    assert res.decision.rationale == "holiday:Juneteenth"
    assert res.command is None


def test_early_close_morning_does_not_flatten(cfg):
    eng = _engine(cfg)
    px = _seed_long(eng)
    # Black Friday 11:00 ET (16:00 UTC) — still tradeable; no calendar flatten.
    res = eng.on_bar(_bar(_ts(2026, 11, 27, 16, 0), px))
    assert res.decision.rationale != "early_close"


def test_normal_day_has_no_calendar_standdown(cfg):
    eng = _engine(cfg)
    px = _seed_long(eng)
    # Ordinary Thursday, 10:00 ET — the engine runs its normal path, no calendar reason.
    res = eng.on_bar(_bar(_ts(2026, 6, 18, 14, 0), px))
    assert res.decision.rationale != "early_close"
    assert not res.decision.rationale.startswith("holiday")


def test_lead_zero_disables_calendar_standdown(cfg):
    cfg.risk.early_close_flat_lead_min = 0
    eng = _engine(cfg)
    px = _seed_long(eng)
    res = eng.on_bar(_bar(_ts(2026, 6, 19, 16, 0), px))  # Juneteenth, feature off
    assert not res.decision.rationale.startswith("holiday")
