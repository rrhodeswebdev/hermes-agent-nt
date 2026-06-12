"""FastAPI server: the message-contract endpoints + the command queue.

Endpoints are SYNC (`def`) so FastAPI runs them in its worker threadpool. That
matters for the Hermes in-process path: while `/ingest/bar` is blocked inside the
LLM call, the agent's `nt_*` tools can still hit `/bars/recent`, `/account`, and
`/agent/command` on other worker threads without deadlocking.

The CommandQueue is the SINGLE place orders leave the bridge for NinjaTrader, and
everything that enqueues goes through the RiskGate first.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .agent_client import build_agent_client
from .config import BridgeConfig, effective_entry_freshness_s, load_config
from .dashboard import DASHBOARD_HTML, render_panel, render_text
from .engine import TradingEngine
from .journal import JournalStore
from .memory import LearnedStore
from .models import (
    AccountState,
    Action,
    BarBatch,
    BarIngest,
    Decision,
    Fill,
    OrderCommand,
)
from .reflect import Reflector
from .risk import RiskGate
from .session import SessionState
from .store import BarStore


def is_stale_entry(action: Action, elapsed_s: float, budget_s: float) -> bool:
    """An ENTRY whose decision took >= budget is stale; exits/flatten are never stale."""
    return (budget_s > 0 and elapsed_s >= budget_s
            and action in (Action.ENTER_LONG, Action.ENTER_SHORT))


# Below this many stored bars, every /ingest/bar response carries need_history=True so
# NinjaTrader re-sends /ingest/history. Closes the bridge-restart gap: the strategy
# pushes history once per ENABLE, so a bridge restarted mid-session would otherwise
# compute EMAs/ATR/swings on a thin live-bar seed (2026-06-11 incident: 25 bars).
HISTORY_MIN_BARS = 50


class CommandQueue:
    def __init__(self) -> None:
        self._q: dict[str, deque[OrderCommand]] = defaultdict(deque)
        self._lock = threading.Lock()

    def push(self, cmd: OrderCommand) -> None:
        with self._lock:
            self._q[cmd.strategy_id].append(cmd)

    def pop(self, strategy_id: str) -> OrderCommand | None:
        with self._lock:
            q = self._q.get(strategy_id)
            if q:
                return q.popleft()
            return None

    def pending(self, strategy_id: str) -> int:
        with self._lock:
            return len(self._q.get(strategy_id, ()))


class AppState:
    def __init__(self, config: BridgeConfig) -> None:
        self.cfg = config
        self.entry_freshness_s = effective_entry_freshness_s(config)
        self.stale_drops = 0  # entries dropped by the freshness guard (shown on the panel)
        self.store = BarStore(config.instrument.symbol, config.instrument.timeframe)
        self.session = SessionState(
            instrument=config.instrument.symbol,
            timeframe=config.instrument.timeframe,
            tick_size=config.instrument.tick_size,
            tick_value=config.instrument.tick_value,
            profit_target=config.daily_goal.profit_target,
            max_daily_loss=config.daily_goal.max_daily_loss,
        )
        self.risk = RiskGate(config)
        self.agent = build_agent_client(config)
        self.journal = JournalStore(config.learning.journal_path)
        self.reflector = Reflector(config, LearnedStore(config.learning.learned_dir), self.journal)
        self.engine = TradingEngine(
            config, self.store, self.session, self.agent, self.risk,
            journal=self.journal, on_close=self._on_trade_closed)
        self.queue = CommandQueue()
        self.lock = threading.Lock()  # serialize engine.on_bar / on_fill mutations
        self.decisions: deque[dict] = deque(maxlen=60)  # recent decisions for the dashboard
        # Server-side (true-UTC) arrival stamp of the latest realtime bar, so "data age"
        # is correct regardless of the timezone the strategy stamps bar.ts in.
        self.last_bar_received_at: float | None = None

    def _on_trade_closed(self, trade) -> None:
        lc = self.cfg.learning
        if not (lc.reflect_enabled and lc.reflect_on_trade_close):
            return
        recent = self.journal.recent(lc.reflect_recent)

        def _run() -> None:
            applied = self.reflector.reflect_on_close(trade, recent)
            if any(applied.values()):
                print(f"[reflect] updated learned memory: {applied}", flush=True)

        threading.Thread(target=_run, daemon=True).start()


class AgentCommandRequest(BaseModel):
    """Payload the Hermes `nt_place_order` / `nt_flatten` tools POST to /agent/command."""

    strategy_id: str | None = None
    action: Action
    qty: int = 1
    stop_ticks: int | None = None
    target_ticks: int | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reason: str = "agent"


def _state(request: Request) -> AppState:
    return request.app.state.appstate


def create_app(config: BridgeConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="Hermes Bridge", version=__version__)
    app.state.appstate = AppState(cfg)

    # Make the execution posture loud and visible. The bridge cannot know whether
    # NinjaTrader's selected account is sim or live (only NinjaTrader does — its
    # account guard is the real interlock), so this flag is an advisory posture that
    # we surface in logs and /health rather than a silent default.
    posture = "LIVE-ENABLED" if cfg.execution.allow_live else "sim-only (allow_live=false)"
    print(f"[hermes-bridge] execution posture: {posture}; account={cfg.execution.account}; "
          f"strategy_id={cfg.strategy_id}; agent={cfg.agent.client}")
    if cfg.execution.allow_live:
        print("[hermes-bridge] WARNING: allow_live=true — real-money orders permitted. "
              "Confirm this is intended and that NinjaTrader's AllowLive is set deliberately.")

    # ---- health / status ------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "agent": cfg.agent.client,
            "strategy_id": cfg.strategy_id,
            "allow_live": cfg.execution.allow_live,
            "account": cfg.execution.account,
        }

    @app.get("/session/status", response_model=AccountState)
    def session_status(request: Request) -> AccountState:
        st = _state(request)
        last = st.store.last()
        return st.session.account_state(mark_price=last.close if last else None)

    @app.get("/account", response_model=AccountState)
    def account(request: Request) -> AccountState:
        return session_status(request)

    @app.get("/bars/recent")
    def bars_recent(request: Request, n: int = Query(50, ge=1, le=2000)) -> dict:
        st = _state(request)
        return {"bars": [b.model_dump() for b in st.store.recent(n)]}

    # ---- dashboard -----------------------------------------------------------
    def _levels(st: AppState) -> dict | None:
        """The agent's current support/resistance + EMAs (from the last bar's context)."""
        lc = st.engine.last_context
        if lc is None:
            return None
        return {"swing_high": lc.swing_high, "swing_low": lc.swing_low,
                "ema_fast": lc.ema_fast, "ema_slow": lc.ema_slow}

    def _agent_model() -> str:
        """Model label for the dashboard header (e.g. claude · sonnet · hermes-default)."""
        if cfg.agent.client == "claude":
            return cfg.agent.claude.model
        if cfg.agent.client == "hermes":
            return cfg.agent.hermes.model
        return ""

    def _dashboard_payload(st: AppState) -> dict:
        last = st.store.last()
        acct = st.session.account_state(mark_price=last.close if last else None)
        now = time.time()
        recent = list(st.decisions)[-15:]
        return {
            "agent": cfg.agent.client,
            "model": _agent_model(),
            "mode": cfg.agent.hermes.mode,
            "strategy_id": cfg.strategy_id,
            "instrument": cfg.instrument.symbol,
            "timeframe": cfg.instrument.timeframe,
            "now": now,
            "last_bar": {"ts": last.ts, "close": last.close} if last else None,
            # Age from the bar's server arrival time (true UTC), not bar.ts — the strategy
            # may stamp bar.ts in a different timezone, which would skew this readout.
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
            "levels": _levels(st),
        }

    @app.get("/dashboard")
    def dashboard_json(request: Request) -> dict:
        return _dashboard_payload(_state(request))

    @app.get("/dashboard.txt", response_class=PlainTextResponse)
    def dashboard_txt(request: Request) -> str:
        # Pre-formatted panel the NinjaScript indicator draws verbatim (no JSON parsing).
        return render_text(_dashboard_payload(_state(request)))

    @app.get("/panel.txt", response_class=PlainTextResponse)
    def panel_txt(request: Request) -> str:
        # Structured key=value snapshot for the HermesDashboard card (no JSON in C#).
        return render_panel(_dashboard_payload(_state(request)))

    @app.get("/levels.txt", response_class=PlainTextResponse)
    def levels_txt(request: Request) -> str:
        # Machine-readable S/R + EMAs for the chart indicator (key=value lines, no JSON).
        lv = _levels(_state(request))
        if not lv:
            return ""
        return "\n".join(f"{k}={v}" for k, v in lv.items() if v is not None)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard.html", response_class=HTMLResponse)
    def dashboard_html() -> str:
        return DASHBOARD_HTML

    # ---- ingest from NinjaTrader ---------------------------------------------
    @app.post("/ingest/history")
    def ingest_history(request: Request, batch: BarBatch) -> dict:
        st = _state(request)
        stored = st.store.replace_history(batch.bars)
        return {"ok": True, "stored": stored}

    @app.post("/ingest/bar", response_model=Decision)
    def ingest_bar(request: Request, payload: BarIngest) -> Decision:
        st = _state(request)
        t0 = time.time()
        st.last_bar_received_at = t0
        # One-shot sanity check: bar.ts should be true UTC. A big skew means the
        # strategy's timezone conversion is wrong and session (RTH/ETH) labels — in
        # prompts AND the journal — are unreliable (2026-06-11: +3h PT-vs-ET skew).
        skew = payload.bar.ts - t0
        if abs(skew) > 1800 and not getattr(st, "ts_skew_warned", False):
            st.ts_skew_warned = True
            print(f"[warn] bar.ts is {skew / 3600:+.1f}h off server UTC — fix the "
                  "strategy's EpochSeconds timezone conversion (session labels "
                  "depend on it)", flush=True)
        with st.lock:
            result = st.engine.on_bar(payload.bar)
            d = result.decision
            cmd = result.command
            elapsed = time.time() - t0
            if cmd is not None and is_stale_entry(cmd.action, elapsed, st.entry_freshness_s):
                st.stale_drops += 1
                st.engine.entry_dropped(cmd.id)  # disarm journal attribution for this entry
                d = Decision(action=Action.WAIT, rationale=(
                    f"stale_entry:{elapsed:.0f}s>{st.entry_freshness_s:.0f}s "
                    f"(dropped {cmd.action} — {d.rationale})"))
                cmd = None
            if cmd is not None:
                st.queue.push(cmd)
        if len(st.store) < HISTORY_MIN_BARS:
            d = d.model_copy(update={"need_history": True})
        queued = f"QUEUED:{cmd.action} qty={cmd.qty}" if cmd is not None else "no-order"
        why = f" reasons={result.risk_reasons}" if cmd is None and result.risk_reasons else ""
        print(f"[decision] close={payload.bar.close} {d.action} [{result.mode}] "
              f"conf={d.confidence:.2f} lat={elapsed:.1f}s -> {queued}{why} | {d.rationale[:160]}",
              flush=True)
        st.decisions.append({
            "ts": payload.bar.ts,
            "close": payload.bar.close,
            "action": str(d.action),
            "confidence": round(d.confidence, 2),
            "mode": result.mode,
            "latency_s": round(elapsed, 1),
            "rationale": d.rationale,
            "queued": f"{cmd.action}:{cmd.qty}" if cmd is not None else None,
        })
        return d

    @app.post("/ingest/fill")
    def ingest_fill(request: Request, fill: Fill) -> dict:
        st = _state(request)
        with st.lock:
            follow_up = st.engine.on_fill(fill)
            if follow_up is not None:
                st.queue.push(follow_up)
        last = st.store.last()
        return {"ok": True, "account": st.session.account_state(
            mark_price=last.close if last else None).model_dump()}

    # ---- command delivery to NinjaTrader -------------------------------------
    @app.get("/commands/next")
    def commands_next(request: Request, strategy_id: str = Query(...)) -> dict:
        st = _state(request)
        cmd = st.queue.pop(strategy_id)
        return {"command": cmd.model_dump() if cmd else None}

    # ---- agent-initiated orders (Hermes tools) -------------------------------
    @app.post("/agent/command")
    def agent_command(request: Request, body: AgentCommandRequest) -> dict:
        st = _state(request)
        last = st.store.last()
        cmd = OrderCommand(
            id=st.engine._new_id(),
            strategy_id=body.strategy_id or cfg.strategy_id,
            action=body.action,
            qty=body.qty,
            stop_ticks=body.stop_ticks,
            target_ticks=body.target_ticks,
            stop_price=body.stop_price,
            target_price=body.target_price,
            reason=body.reason,
        )
        with st.lock:
            rd = st.risk.evaluate(
                cmd, st.session,
                last_price=last.close if last else None,
                now_ts=last.ts if last else None,
            )
            if rd.approved and rd.command is not None:
                st.queue.push(rd.command)
        return {
            "approved": rd.approved,
            "reasons": rd.reasons,
            "command": rd.command.model_dump() if rd.command else None,
        }

    # ---- kill switch ----------------------------------------------------------
    @app.post("/control/flatten")
    def control_flatten(request: Request, reason: str = Body("manual_kill", embed=True)) -> dict:
        st = _state(request)
        with st.lock:
            st.session.halt(reason)
            cmd = OrderCommand(
                id=st.engine._new_id(), strategy_id=cfg.strategy_id,
                action=Action.FLATTEN, qty=abs(st.session.position), reason=reason,
            )
            st.queue.push(cmd)
        return {"ok": True, "halted": True, "reason": reason}

    @app.post("/control/resume")
    def control_resume(request: Request) -> dict:
        st = _state(request)
        with st.lock:
            st.session.resume()
        return {"ok": True, "halted": False}

    @app.post("/control/reflect")
    def control_reflect(request: Request) -> dict:
        st = _state(request)
        recent = st.journal.recent(st.cfg.learning.reflect_recent)
        if not recent:
            return {"ok": True, "applied": {"lessons": 0, "notes": 0, "profile": 0},
                    "note": "no trades to reflect on"}
        from .journal import ClosedTrade
        applied = st.reflector.reflect_on_close(ClosedTrade(**recent[-1]), recent)
        return {"ok": True, "applied": applied}

    @app.post("/control/curate")
    def control_curate(request: Request) -> dict:
        return {"ok": True, "applied": _state(request).reflector.curate()}

    return app


# Module-level app for `uvicorn hermes_bridge.server:app`.
app = create_app()
