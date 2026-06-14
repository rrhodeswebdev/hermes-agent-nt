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
from .config import (
    BridgeConfig,
    effective_entry_freshness_s,
    load_config,
    timeframe_seconds,
)
from .dashboard import DASHBOARD_HTML, render_panel, render_text
from .engine import TradingEngine
from .journal import JournalStore
from .levels import detect_levels
from .memory import LearnedStore
from .models import (
    AccountReport,
    AccountState,
    Action,
    BarBatch,
    BarIngest,
    Decision,
    Fill,
    OrderCommand,
)
from .news import NewsGuard
from .plan import Planner
from .reflect import Reflector
from .risk import RiskGate
from .session import SessionState
from .store import BarStore


def is_stale_entry(action: Action, elapsed_s: float, budget_s: float) -> bool:
    """An ENTRY whose decision took >= budget is stale; exits/flatten are never stale."""
    return (budget_s > 0 and elapsed_s >= budget_s
            and action in (Action.ENTER_LONG, Action.ENTER_SHORT))


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
        # Major-news blackout guard (shared: this server refreshes it on a background
        # thread; the RiskGate only reads it). Disabled ⇒ inert (no thread, never blocks).
        self.news = NewsGuard(config)
        self.risk = RiskGate(config, news=self.news)
        self.agent = build_agent_client(config)
        # The strategy source (agent vs custom) lives in ONE place: the agent client. It is
        # seeded from config in the client's __init__ and overridden at runtime by
        # NinjaTrader's UseAgentStrategies toggle (set_strategy_source); effective_strategy_source()
        # reads it back. Keeping a second copy here was a split-brain waiting to happen — the
        # engine read the agent's copy while the dashboard read the server's.
        # Background worker: analyses run between bars, never on the ingest path.
        self.planner = Planner(config, self.agent) if config.planner.enabled else None
        self.journal = JournalStore(config.learning.journal_path)
        self.reflector = Reflector(config, LearnedStore(config.learning.learned_dir), self.journal)
        self.engine = TradingEngine(
            config, self.store, self.session, self.agent, self.risk,
            planner=self.planner, journal=self.journal, on_close=self._on_trade_closed)
        self.queue = CommandQueue()
        self.lock = threading.Lock()  # serialize engine.on_bar / on_fill mutations
        self.decisions: deque[dict] = deque(maxlen=60)  # recent decisions for the dashboard
        # Server-side (true-UTC) arrival stamp of the latest realtime bar, so "data age"
        # is correct regardless of the timezone the strategy stamps bar.ts in.
        self.last_bar_received_at: float | None = None
        # The account NinjaTrader's strategy reports it is actually trading on (and
        # whether live is permitted there). None until the strategy first reports;
        # effective_account() falls back to the static config default until then.
        self.reported_account: str | None = None
        self.reported_allow_live: bool | None = None
        self._start_news_refresh()

    def _start_news_refresh(self) -> None:
        """Refresh the economic calendar off the hot path. No-op when news is disabled, so
        tests and the default config never touch the network."""
        if not self.cfg.news.enabled:
            return

        def _loop() -> None:
            while True:
                self.news.refresh(time.time())
                time.sleep(max(60.0, self.cfg.news.refresh_minutes * 60.0))

        threading.Thread(target=_loop, daemon=True).start()

    def effective_account(self) -> str:
        """Live NT account if the strategy has reported one, else the config default."""
        return self.reported_account or self.cfg.execution.account

    def effective_strategy_source(self) -> str:
        """The single source of truth for the strategy source, owned by the agent client:
        NinjaTrader's UseAgentStrategies override if it has reported one, else the config
        default it was seeded with. "agent" = brain authors its own playbook; "custom" =
        on-disk playbooks."""
        return self.agent.strategy_source()

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
    print(f"[hermes-bridge] execution posture: {posture}; "
          f"account(config default)={cfg.execution.account}; "
          f"strategy_id={cfg.strategy_id}; agent={cfg.agent.client} "
          f"(NinjaTrader reports its live account on connect)")
    if cfg.execution.allow_live:
        print("[hermes-bridge] WARNING: allow_live=true — real-money orders permitted. "
              "Confirm this is intended and that NinjaTrader's AllowLive is set deliberately.")

    # ---- health / status ------------------------------------------------------
    @app.get("/health")
    def health(request: Request) -> dict:
        st = _state(request)
        return {
            "ok": True,
            "version": __version__,
            "agent": cfg.agent.client,
            "strategy_id": cfg.strategy_id,
            "allow_live": cfg.execution.allow_live,
            "account": st.effective_account(),
            "nt_allow_live": st.reported_allow_live,
            "strategy_source": st.effective_strategy_source(),
            "news": st.news.status(time.time()),
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

    @app.get("/levels")
    def levels(request: Request) -> dict:
        """Swing-pivot S/R zones over the stored history, for the chart overlay."""
        st = _state(request)
        lc = cfg.levels
        if not lc.enabled:
            return {"levels": []}
        zones = detect_levels(
            st.store.all(), lookback=lc.lookback, tick_size=cfg.instrument.tick_size,
            merge_ticks=lc.merge_ticks, min_touches=lc.min_touches,
            max_levels=lc.max_levels,
        )
        return {"levels": [z.model_dump() for z in zones]}

    @app.get("/strategy")
    def strategy(request: Request) -> dict:
        """The active playbook so you can SEE what the agent invented (or which custom
        files are loaded). In agent mode: the self-authored playbook + the file it was
        persisted to. In custom mode: the concatenated on-disk playbooks (null if the
        strategies/ dirs are empty)."""
        st = _state(request)
        source = st.effective_strategy_source()
        if source == "agent":
            playbook = st.agent.generated_strategy()
            return {
                "source": "agent",
                **_strategy_block(st),
                "path": getattr(st.agent, "_generated_path", None),
                "playbook": playbook,
            }
        from .agent_client import load_playbook_files
        playbook = load_playbook_files(cfg.agent.claude.context_dir) or None
        return {"source": "custom", "generated": False, "name": None, "summary": None,
                "regime": None, "active_index": None, "active_source": None, "list": [],
                "path": None, "playbook": playbook}

    # ---- dashboard -----------------------------------------------------------
    def _declared_strategy(st: AppState) -> str | None:
        """The setup name the brain says it is trading, from the currently armed plan
        (agent mode). None when there is no planner / no plan / it named none."""
        if st.planner is None:
            return None
        plan = st.planner.current_plan()
        if plan is None:
            return None
        return (getattr(plan, "active_strategy", "") or "").strip() or None

    def _strategy_block(st: AppState) -> dict:
        """The agent-authored strategy for the dashboards: every named setup the agent
        wrote (``list``), the live regime, and which setup is active — the one the brain
        declared in its plan, else the one matching the regime (``active_source`` says
        which). ``name``/``summary`` are a single-line headline = the active setup (else the
        first), for the legacy panel keys. All display-only — never gates trading."""
        if st.effective_strategy_source() != "agent":
            # Custom mode trades the on-disk playbooks, not an authored roster. The agent
            # client keeps its last authored list cached across a runtime source toggle, so
            # gate on the effective source here — never list a stale agent roster as if it
            # were what's being traded.
            return {"generated": False, "regime": None, "active_index": None,
                    "active_source": None, "list": [], "name": None, "summary": None}
        strategies = st.agent.generated_strategies()
        regime = current_regime(st.engine.last_context)
        items, active_index, active_source = strategy_list_with_active(
            strategies, regime, _declared_strategy(st))
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
            "authored": _authoring_view(st),
        }

    def _authoring_view(st: AppState) -> dict | None:
        """The agent's authoring telemetry with the age expressed in bars of the live
        timeframe (more meaningful than wall-clock and unit-consistent with the cadence
        config). None when nothing has been authored yet."""
        status = st.agent.authoring_status()
        if not status:
            return None
        bars_ago: int | None = None
        at = status.get("authored_at_bar_ts")
        last = st.store.last()
        if at and last is not None:
            tf_s = timeframe_seconds(cfg.instrument.timeframe)
            if tf_s > 0:
                bars_ago = int(max(0.0, last.ts - at) // tf_s)
        return {
            "count": status.get("count", 0),
            "reason": status.get("reason", ""),
            "bars_ago": bars_ago,
        }

    def _levels(st: AppState) -> dict | None:
        """The agent's current swing support/resistance (from the last bar's context).
        Regime now comes from structure, so there are no EMA lines to plot."""
        lc = st.engine.last_context
        if lc is None:
            return None
        return {"swing_high": lc.swing_high, "swing_low": lc.swing_low}

    def _agent_model() -> str:
        """Model label for the dashboard header (e.g. claude · sonnet)."""
        if cfg.agent.client == "claude":
            return cfg.agent.claude.model
        return ""

    def _dashboard_payload(st: AppState) -> dict:
        last = st.store.last()
        acct = st.session.account_state(mark_price=last.close if last else None)
        now = time.time()
        recent = list(st.decisions)[-15:]
        return {
            "agent": cfg.agent.client,
            "brain": st.engine.agent.describe(),
            "model": _agent_model(),
            "strategy_id": cfg.strategy_id,
            "strategy_source": st.effective_strategy_source(),
            # The agent-authored strategy for this session (agent mode): every named setup
            # the brain wrote (`list`) and which one is active for the live regime, so the
            # dashboard can show them all and highlight the active one. Display only.
            "strategy": {"source": st.effective_strategy_source(), **_strategy_block(st)},
            "account": st.effective_account(),
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
            "planner": st.planner.snapshot() if st.planner else None,
            "levels": _levels(st),
            "news": st.news.status(now),
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
        # Kick off the one-time session study + initial plan in the background.
        with st.lock:
            st.engine.on_history(batch.bars)
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

    @app.post("/ingest/account")
    def ingest_account(request: Request, report: AccountReport) -> dict:
        """NinjaTrader reports which account its strategy is actually trading on, so the
        bridge's logs / health / dashboard reflect the live selection instead of the
        static config default. Advisory only — NinjaTrader's own account guard is the
        execution interlock; this never changes where orders go."""
        st = _state(request)
        name = (report.account or "").strip()
        if name and name != st.reported_account:
            print(f"[hermes-bridge] NinjaTrader account is now '{name}' "
                  f"(nt_allow_live={report.allow_live})", flush=True)
        if name:
            st.reported_account = name
        st.reported_allow_live = report.allow_live
        # Strategy source toggle (UseAgentStrategies). Reported before history so the
        # pre-session study authors (agent) or skips authoring (custom) correctly. The agent
        # client is the single store — set it, and read it back for the changed-log compare.
        if report.use_agent_strategies is not None:
            source = "agent" if report.use_agent_strategies else "custom"
            if source != st.agent.strategy_source():
                print(f"[hermes-bridge] strategy source is now '{source}' "
                      f"(NinjaTrader UseAgentStrategies={report.use_agent_strategies})",
                      flush=True)
            st.agent.set_strategy_source(source)
        return {"ok": True, "account": st.effective_account(),
                "strategy_source": st.effective_strategy_source()}

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

    @app.post("/control/reauthor")
    def control_reauthor(request: Request) -> dict:
        """Force a FRESH pre-session study: re-author the agent playbook from the current
        stored history without a bridge restart (agent mode only). Use this when you want a
        new playbook within a running bridge — by default the study runs once per process and
        a NinjaTrader re-enable reuses the existing one. The new study runs in the background;
        the dashboard shows "authoring…" until it lands, and any open position stays protected
        by its resting bracket meanwhile."""
        st = _state(request)
        if st.planner is None:
            return {"ok": False, "note": "planner disabled; nothing to re-author"}
        if st.effective_strategy_source() != "agent":
            return {"ok": False, "note": "strategy source is custom; the agent authors nothing"}
        bars = st.store.all()
        if len(bars) < HISTORY_MIN_BARS:
            return {"ok": False,
                    "note": f"need >= {HISTORY_MIN_BARS} bars of history, have {len(bars)}"}
        with st.lock:
            st.planner.clear_session()           # so on_history re-runs the study, not skips it
            st.agent.clear_generated_strategy()  # dashboard shows "authoring…" until the new lands
            st.engine.on_history(bars)           # kick the fresh study + initial plan
        print(f"[hermes-bridge] re-authoring agent strategy from {len(bars)} bars "
              "(manual /control/reauthor)", flush=True)
        return {"ok": True, "source": "agent", "bars": len(bars),
                "status": st.planner.snapshot()["status"]}

    return app


# Module-level app for `uvicorn hermes_bridge.server:app`.
app = create_app()
