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
)
from .dashboard import DASHBOARD_HTML, render_panel, render_text
from .engine import TradingEngine
from .journal import DeclineLog, JournalStore
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
from .prop_firms import (
    apply_account_profile,
    find_account,
    load_catalog,
    persist_account_profile,
)
from .reflect import Reflector
from .resample import Resampler, feed_tf_of, resampler_engaged
from .risk import RiskGate
from .session import SessionState
from .store import BarStore
from .views import build_dashboard_payload, dashboard_levels, strategy_block


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
    def __init__(self, config: BridgeConfig, config_path: str | None = None) -> None:
        self.cfg = config
        # Where config was loaded from, so a dashboard profile change can persist to the
        # sibling *.local.yaml. Falls back to the conventional path when run without a file.
        self.config_path = config_path or "config/trading.yaml"
        self.entry_freshness_s = effective_entry_freshness_s(config)
        self.stale_drops = 0  # entries dropped by the freshness guard (shown on the panel)
        # Bar resampler (opt-in): keep a persisted FEED store (raw feed TF) and an in-memory
        # DECISION store the engine reasons on, rebuilt losslessly from the feed on each session
        # switch. Disabled => a single persisted store, exactly as before.
        self.resampler: Resampler | None = None
        self.feed_store: BarStore | None = None
        inst = config.instrument
        if resampler_engaged(inst):
            feed_tf = feed_tf_of(inst)
            self.feed_store = BarStore(inst.symbol, feed_tf,
                                       db_path=config.storage.bars_db or None)
            self.store = BarStore(inst.symbol, feed_tf, db_path=None)  # decision store (in-memory)
            self.resampler = Resampler(self.feed_store, self.store, feed_tf=feed_tf,
                                       decision_timeframe=inst.decision_timeframe)
            self.resampler.initial_rebuild()
        else:
            self.store = BarStore(inst.symbol, inst.timeframe,
                                  db_path=config.storage.bars_db or None)
        self.session = SessionState(
            instrument=config.instrument.symbol,
            timeframe=config.instrument.timeframe,
            tick_size=config.instrument.tick_size,
            tick_value=config.instrument.tick_value,
            profit_target=config.daily_goal.profit_target,
            max_daily_loss=config.daily_goal.max_daily_loss,
            state_path=config.storage.session_state or None,
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
        # Resolved counterfactuals for declined/unfilled setups. The engine PR wires the
        # replay hook that appends here and the on_bar call to maybe_reflect_missed(); the
        # attribute + method are defined now so both are unit-testable ahead of that wiring.
        self.declines = DeclineLog(config.learning.declines_path)
        self.reflector = Reflector(config, LearnedStore(config.learning.learned_dir), self.journal)
        self.engine = TradingEngine(
            config, self.store, self.session, self.agent, self.risk,
            planner=self.planner, journal=self.journal, on_close=self._on_trade_closed,
            declines=self.declines,
            decision_tf=(lambda: self.resampler.current_tf) if self.resampler else None)
        # Warm restart: the decision store was rebuilt from bars.db (resampler) or loaded from
        # the persisted store, so NT8's need_history handshake won't fire and /ingest/history
        # won't kick the pre-session study. Kick it here so the brain authors a playbook without
        # a manual /control/reauthor. A cold start (thin store) still relies on the NT8 push.
        if self.planner is not None and len(self.store) >= HISTORY_MIN_BARS:
            self.engine.on_history(self.store.all())
        self.queue = CommandQueue()
        self.lock = threading.Lock()  # serialize engine.on_bar / on_fill mutations
        self.decisions: deque[dict] = deque(maxlen=60)  # recent decisions for the dashboard
        # Dedicated lock for the decisions ring: the dashboard snapshots it (list(deque)) on a
        # worker thread while /ingest/bar appends on another, and an unsynchronized list(deque)
        # raises "deque mutated during iteration". Kept OFF st.lock so a dashboard poll never
        # blocks on a slow on_bar (the dashboard reads all other state lock-free by design).
        self.decisions_lock = threading.Lock()
        # Server-side (true-UTC) arrival stamp of the latest realtime bar, so "data age"
        # is correct regardless of the timezone the strategy stamps bar.ts in.
        self.last_bar_received_at: float | None = None
        # The account NinjaTrader's strategy reports it is actually trading on (and
        # whether live is permitted there). None until the strategy first reports;
        # effective_account() falls back to the static config default until then.
        self.reported_account: str | None = None
        self.reported_allow_live: bool | None = None
        # Prop-firm catalog (committed reference data) + the numbers the active selection
        # applied. Seeding applies the configured selection's hard limits (daily loss, max
        # contracts) into cfg/session and loads the firm's context file into the brain.
        self.catalog = load_catalog(config.account_profile.catalog_path)
        self.applied_profile: dict | None = None
        self._seed_account_profile()
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

    # ---- prop-firm account profile -------------------------------------------
    def _seed_account_profile(self) -> None:
        """Apply the account profile configured at startup (from trading.yaml +
        trading.local.yaml): set the firm's context file on the brain and write its hard limits
        into cfg/session. No-op (and no error) when nothing is selected or the selection isn't
        in the catalog — the bridge just runs on its base config."""
        ap = self.cfg.account_profile
        match = find_account(self.catalog, ap.prop_firm, ap.account_type, ap.account_size)
        if match is None:
            return
        firm, _prog, tier = match
        self.agent.set_prop_firm_context(firm.context_file)
        self.applied_profile = apply_account_profile(self.cfg, self.session, tier)
        print(f"[hermes-bridge] account profile: {firm.name} / {ap.account_type} / "
              f"{ap.account_size:g} -> max_daily_loss={self.cfg.daily_goal.max_daily_loss:g}, "
              f"max_contracts={self.cfg.risk.max_contracts}", flush=True)

    def select_account_profile(
        self, prop_firm: str | None, account_type: str | None, account_size: float | None,
        *, persist: bool = True,
    ) -> dict:
        """Apply a new account-profile selection: validate against the catalog, write the hard
        limits into cfg/session, load the firm's context file into the brain, record the
        selection on cfg, and (by default) persist it to the sibling *.local.yaml so it
        survives a restart. Returns ``{ok, ...}``; ``ok=False`` with a note on a bad selection."""
        match = find_account(self.catalog, prop_firm, account_type, account_size)
        if match is None:
            return {"ok": False, "note": "no matching account in the catalog "
                    f"(firm={prop_firm!r}, type={account_type!r}, size={account_size!r})"}
        firm, prog, tier = match
        self.agent.set_prop_firm_context(firm.context_file)
        self.applied_profile = apply_account_profile(self.cfg, self.session, tier)
        ap = self.cfg.account_profile
        ap.prop_firm, ap.account_type, ap.account_size = firm.name, prog.name, float(tier.size)
        persisted: str | None = None
        if persist:
            persisted = str(persist_account_profile(
                self.config_path, firm.name, prog.name, float(tier.size)))
        print(f"[hermes-bridge] account profile selected: {firm.name} / {prog.name} / "
              f"{tier.size:g} -> max_daily_loss={self.cfg.daily_goal.max_daily_loss:g}, "
              f"max_contracts={self.cfg.risk.max_contracts}"
              f"{f' (saved to {persisted})' if persisted else ''}", flush=True)
        return {"ok": True, "selected": self.account_profile_selection(),
                "applied": self.applied_profile, "persisted": persisted}

    def account_profile_selection(self) -> dict:
        """The current selection (firm/type/size) + the firm's context filename, for the UI."""
        ap = self.cfg.account_profile
        return {
            "prop_firm": ap.prop_firm,
            "account_type": ap.account_type,
            "account_size": ap.account_size,
            "context_file": self.agent.prop_firm_context(),
        }

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

    def maybe_reflect_missed(self) -> None:
        """Flat-only reflection trigger: when no trade has closed but enough DECLINED
        setups have resolved would-win, reflect on whether a lesson is over-blocking.
        Fires only while FLAT (a missed-opportunity signal is meaningless mid-trade) and
        only once the unreported would-wins reach reflect_missed_wins. take_unreported()
        marks them reported so this won't double-fire; a win resolved concurrently lands
        in the next snapshot. Off the hot path, in a daemon thread like _on_trade_closed."""
        lc = self.cfg.learning
        if not (lc.reflect_enabled and lc.enabled):
            return
        if lc.reflect_missed_wins <= 0:
            return  # misconfig guard: a 0 threshold would fire on every flat bar
        # Snapshot the trigger under the lock so a concurrent fill can't race the
        # check-then-take and drain declines on a stale read (or fire on an empty set).
        with self.lock:
            if self.session.position != 0:
                return
            if len(self.declines.unreported_wins()) < lc.reflect_missed_wins:
                return
            declines = self.declines.take_unreported()
        if not declines:
            return
        recent = self.journal.recent(lc.reflect_recent)

        def _run() -> None:
            applied = self.reflector.reflect_on_missed(declines, recent)
            if any(v for k, v in applied.items() if k != "error"):
                print(f"[reflect:missed] updated learned memory: {applied}", flush=True)

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


class AccountProfileRequest(BaseModel):
    """Payload the dashboard POSTs to /control/account-profile to select a prop-firm account."""

    prop_firm: str | None = None
    account_type: str | None = None
    account_size: float | None = None


def _state(request: Request) -> AppState:
    return request.app.state.appstate


def create_app(config: BridgeConfig | None = None, config_path: str | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="Hermes Bridge", version=__version__)
    app.state.appstate = AppState(cfg, config_path=config_path)

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
            "account_profile": st.account_profile_selection(),
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
                **strategy_block(st),
                "path": getattr(st.agent, "_generated_path", None),
                "playbook": playbook,
            }
        from .agent_client import load_playbook_files
        playbook = load_playbook_files(cfg.agent.claude.context_dir) or None
        return {"source": "custom", "generated": False, "name": None, "summary": None,
                "regime": None, "active_index": None, "active_source": None, "list": [],
                "path": None, "playbook": playbook}

    # ---- prop-firm account profile -------------------------------------------
    @app.get("/account-profile")
    def account_profile(request: Request) -> dict:
        """The firm catalog (firms -> account types -> sizes + numbers) that powers the
        dashboard dropdowns, the current selection, and the numbers it applied. Read-only."""
        st = _state(request)
        return {
            "catalog": st.catalog.model_dump(),
            "selected": st.account_profile_selection(),
            "applied": st.applied_profile,
            "context_dir": st.cfg.account_profile.context_dir,
        }

    # ---- dashboard -----------------------------------------------------------
    # The payload + its projection helpers live in views.py (build_dashboard_payload /
    # strategy_block / dashboard_levels); these endpoints just serve them.
    @app.get("/dashboard")
    def dashboard_json(request: Request) -> dict:
        return build_dashboard_payload(_state(request))

    @app.get("/dashboard.txt", response_class=PlainTextResponse)
    def dashboard_txt(request: Request) -> str:
        # Pre-formatted panel the NinjaScript indicator draws verbatim (no JSON parsing).
        return render_text(build_dashboard_payload(_state(request)))

    @app.get("/panel.txt", response_class=PlainTextResponse)
    def panel_txt(request: Request) -> str:
        # Structured key=value snapshot for the HermesDashboard card (no JSON in C#).
        return render_panel(build_dashboard_payload(_state(request)))

    @app.get("/levels.txt", response_class=PlainTextResponse)
    def levels_txt(request: Request) -> str:
        # Machine-readable S/R + EMAs for the chart indicator (key=value lines, no JSON).
        lv = dashboard_levels(_state(request))
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
        # With the resampler, NinjaTrader's history is the FEED series; rebuild the decision
        # series from it before the engine's one-time session study runs.
        if st.resampler is not None:
            stored = st.resampler.replace_feed_history(batch.bars)
            with st.lock:
                st.engine.on_history(st.store.all())
        else:
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
            if st.resampler is not None:
                # Aggregate the 1m feed into the decision timeframe. A forming (intra-decision)
                # bar gets an instant WAIT — the engine only runs on a decision-bar close. Switch
                # when flat (no open position); an armed plan is just resting triggers, and we
                # re-author for the new TF on a switch (below).
                is_flat = st.session.position == 0
                decision_bar = st.resampler.on_feed_bar(payload.bar, is_flat=is_flat)
                if st.resampler.take_switch():
                    st.engine.reauthor(outcome="tf_switch")
                if decision_bar is None:
                    d = Decision(action=Action.WAIT, rationale="resample:forming")
                    if len(st.store) < HISTORY_MIN_BARS:
                        d = d.model_copy(update={"need_history": True})
                    return d
                bar = decision_bar
            else:
                bar = payload.bar
            result = st.engine.on_bar(bar)
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
        print(f"[decision] close={bar.close} {d.action} [{result.mode}] "
              f"conf={d.confidence:.2f} lat={elapsed:.1f}s -> {queued}{why} | {d.rationale[:160]}",
              flush=True)
        with st.decisions_lock:
            st.decisions.append({
                "ts": bar.ts,
                "close": bar.close,
                "action": str(d.action),
                "confidence": round(d.confidence, 2),
                "mode": result.mode,
                "latency_s": round(elapsed, 1),
                "rationale": d.rationale,
                "queued": f"{cmd.action}:{cmd.qty}" if cmd is not None else None,
            })
        # Flat-only, threshold-gated: when declines have resolved would-win and we're flat,
        # reflect on whether a lesson is over-blocking (self-gates + runs off the lock).
        st.maybe_reflect_missed()
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
        # Prop-firm account selected in the strategy settings (cascading dropdowns). Apply at
        # runtime — load the firm context + enforce the account's limits — but DON'T persist:
        # the chart's selection is live state, like the reported account name. Only act on a
        # CHANGE (the strategy re-reports on every reconnect) so it's idempotent + quiet. A
        # blank prop_firm is "unspecified" (keep the current selection), like the toggle above.
        if report.prop_firm:
            sel = st.account_profile_selection()
            changed = (report.prop_firm != sel["prop_firm"]
                       or report.account_type != sel["account_type"]
                       or report.account_size != sel["account_size"])
            if changed:
                with st.lock:
                    result = st.select_account_profile(
                        report.prop_firm, report.account_type, report.account_size,
                        persist=False)
                if not result.get("ok"):
                    print(f"[hermes-bridge] NinjaTrader prop-firm selection ignored: "
                          f"{result.get('note')}", flush=True)
        return {"ok": True, "account": st.effective_account(),
                "strategy_source": st.effective_strategy_source(),
                "account_profile": st.account_profile_selection()}

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

    @app.post("/control/distill")
    def control_distill(request: Request) -> dict:
        """Run the slow-tier distillation: compress the full lesson/note corpus into one
        bounded hermes/learned/distilled.md that the realtime prompt reads instead of raw
        lessons. Text-only — it never writes risk/config numbers or places orders."""
        return {"ok": True, "applied": _state(request).reflector.distill()}

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

    @app.post("/control/account-profile")
    def control_account_profile(request: Request, body: AccountProfileRequest) -> dict:
        """Select a prop firm + account from the catalog. Applies the account's enforced hard
        limits (daily loss, max contracts) into the live config, loads the firm's context file
        into the brain, and persists the selection to config/trading.local.yaml so it survives a
        restart. The selection takes effect immediately (the RiskGate reads cfg live; the next
        decision's prompt carries the firm's rules)."""
        st = _state(request)
        with st.lock:
            return st.select_account_profile(
                body.prop_firm, body.account_type, body.account_size)

    return app


# Module-level app for `uvicorn hermes_bridge.server:app`.
app = create_app()
