"""Dashboard projection — turning the live `AppState` into the JSON/text the dashboards render.

This is the read-only view layer: which authored setup is active for the live regime, the
authoring telemetry (age expressed in bars), data-age from the bar's server-arrival time, the
recent-decisions ring. It lived inline among the HTTP endpoints in `server.py`, reaching into
the engine / agent / planner / session / store / news; pulled out here it can be tested without
standing up the FastAPI app, and `server.py` is left as the contract + the command queue.

Everything here is display-only — none of it gates trading.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .config import BridgeConfig, timeframe_seconds

if TYPE_CHECKING:
    from .server import AppState


def current_regime(context) -> str | None:
    """The structural regime on the latest bar's context — "trending" / "ranging" /
    "transitional" (read from swing structure, not EMAs; see indicators.classify_regime),
    or None if there is no context yet. These are the same labels the agent tags its
    setups with, so the dashboard can match the active setup to the live regime."""
    return getattr(context, "regime", None)


def strategy_list_with_active(
    strategies: list[dict] | None, regime: str | None, declared: str | None = None
) -> tuple[list[dict], int | None, str | None]:
    """Display items ``[{name, regime, summary, active}]`` for every authored setup, the
    index of the active one, and how it was chosen ("declared"/"regime"/None).

    The brain's own ``declared`` setup name (from the armed plan) wins — that is the setup
    it says it is trading. Failing that (no plan yet, waiting, or an unrecognized name) the
    setup whose regime matches the live ``regime`` is used. First match wins; with no setups
    or neither signal, none is active."""
    items = [
        {
            "name": s.get("name", ""),
            "regime": s.get("regime", "") or "",
            "summary": s.get("summary", "") or "",
            "active": False,
        }
        for s in (strategies or [])
    ]
    active_index: int | None = None
    source: str | None = None
    if declared:
        key = declared.strip().lower()
        for i, it in enumerate(items):
            if it["name"].strip().lower() == key:
                active_index, source = i, "declared"
                break
    if active_index is None and regime:
        for i, it in enumerate(items):
            if it["regime"] == regime:
                active_index, source = i, "regime"
                break
    if active_index is not None:
        items[active_index]["active"] = True
    return items, active_index, source


def declared_strategy(st: AppState) -> str | None:
    """The setup name the brain says it is trading, from the currently armed plan (agent
    mode). None when there is no planner / no plan / it named none."""
    if st.planner is None:
        return None
    plan = st.planner.current_plan()
    if plan is None:
        return None
    return (getattr(plan, "active_strategy", "") or "").strip() or None


def strategy_block(st: AppState) -> dict:
    """The agent-authored strategy for the dashboards: every named setup the agent wrote
    (``list``), the live regime, and which setup is active — the one the brain declared in its
    plan, else the one matching the regime (``active_source`` says which). ``name``/``summary``
    are a single-line headline = the active setup (else the first), for the legacy panel keys.
    All display-only — never gates trading."""
    if st.effective_strategy_source() != "agent":
        # Custom mode trades the on-disk playbooks, not an authored roster. The agent client
        # keeps its last authored list cached across a runtime source toggle, so gate on the
        # effective source here — never list a stale agent roster as if it were what's traded.
        return {"generated": False, "regime": None, "active_index": None,
                "active_source": None, "list": [], "name": None, "summary": None}
    strategies = st.agent.generated_strategies()
    regime = current_regime(st.engine.last_context)
    items, active_index, active_source = strategy_list_with_active(
        strategies, regime, declared_strategy(st))
    head = items[active_index] if active_index is not None else (items[0] if items else None)
    return {
        "generated": st.agent.generated_strategy() is not None,
        "regime": regime,
        "active_index": active_index,
        "active_source": active_source,
        "list": items,
        "name": head["name"] if head else None,
        "summary": head["summary"] if head else None,
        # Authoring telemetry so a static-looking list can be told apart from a never-
        # re-authored one: how many playbooks have installed, how long ago (in bars), and
        # why the latest fired. None until the first author lands.
        "authored": authoring_view(st),
    }


def authoring_view(st: AppState) -> dict | None:
    """The agent's authoring telemetry with the age expressed in bars of the live timeframe
    (more meaningful than wall-clock and unit-consistent with the cadence config). None when
    nothing has been authored yet."""
    status = st.agent.authoring_status()
    if not status:
        return None
    bars_ago: int | None = None
    at = status.get("authored_at_bar_ts")
    last = st.store.last()
    if at and last is not None:
        tf_s = timeframe_seconds(st.cfg.instrument.timeframe)
        if tf_s > 0:
            bars_ago = int(max(0.0, last.ts - at) // tf_s)
    return {
        "count": status.get("count", 0),
        "reason": status.get("reason", ""),
        "bars_ago": bars_ago,
    }


def dashboard_levels(st: AppState) -> dict | None:
    """The agent's current swing support/resistance (from the last bar's context). Regime now
    comes from structure, so there are no EMA lines to plot."""
    lc = st.engine.last_context
    if lc is None:
        return None
    return {"swing_high": lc.swing_high, "swing_low": lc.swing_low}


def agent_model(cfg: BridgeConfig) -> str:
    """Model label for the dashboard header (e.g. claude · sonnet)."""
    if cfg.agent.client == "claude":
        return cfg.agent.claude.model
    return ""


def build_dashboard_payload(st: AppState) -> dict:
    """The full dashboard snapshot: brain/model/strategy, account + daily goal, data-age, the
    recent-decisions ring, planner status, levels, and news. Read live on every poll."""
    cfg = st.cfg
    last = st.store.last()
    acct = st.session.account_state(mark_price=last.close if last else None)
    now = time.time()
    recent = list(st.decisions)[-15:]
    return {
        "agent": cfg.agent.client,
        "brain": st.engine.agent.describe(),
        "model": agent_model(cfg),
        "strategy_id": cfg.strategy_id,
        "strategy_source": st.effective_strategy_source(),
        # The agent-authored strategy for this session (agent mode): every named setup the
        # brain wrote (`list`) and which one is active for the live regime, so the dashboard
        # can show them all and highlight the active one. Display only.
        "strategy": {"source": st.effective_strategy_source(), **strategy_block(st)},
        "account": st.effective_account(),
        "instrument": cfg.instrument.symbol,
        "timeframe": cfg.instrument.timeframe,
        "now": now,
        "last_bar": {"ts": last.ts, "close": last.close} if last else None,
        # Age from the bar's server arrival time (true UTC), not bar.ts — the strategy may
        # stamp bar.ts in a different timezone, which would skew this readout.
        "data_age_seconds": (
            now - st.last_bar_received_at if st.last_bar_received_at is not None else None
        ),
        "session": acct.model_dump(),
        "goal": {
            "profit_target": cfg.daily_goal.profit_target,
            "max_daily_loss": cfg.daily_goal.max_daily_loss,
        },
        "stale_drops": st.stale_drops,
        "last_decision": recent[-1] if recent else None,
        "recent_decisions": list(reversed(recent)),
        "planner": st.planner.snapshot() if st.planner else None,
        "levels": dashboard_levels(st),
        "news": st.news.status(now),
    }
