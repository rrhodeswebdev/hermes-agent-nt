"""Configuration loading and validation.

Reads `config/trading.yaml` into typed Pydantic models. Every field has a safe
default so the bridge (and the test-suite) can run without a config file. Env
vars `HERMES_BRIDGE_HOST` / `HERMES_BRIDGE_PORT` / `HERMES_BRIDGE_AGENT` override
the matching settings.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


def timeframe_seconds(tf: str, default: float = 60.0) -> float:
    """Parse a timeframe like '1m' / '30s' / '1h' / '1d' into seconds; default on bad input."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(tf).lower())
    if not m:
        return default
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def agent_timeout_s(cfg: BridgeConfig) -> float:
    """Decision-latency ceiling of the ACTIVE agent client (0 for the mock/rules client)."""
    if cfg.agent.client == "claude":
        return cfg.agent.claude.timeout_s
    return 0.0


def effective_entry_freshness_s(cfg: BridgeConfig) -> float:
    """Explicit execution.entry_freshness_s, or auto = max(one bar interval, agent timeout).

    The floor matters: a bare bar-interval default silently fights the agent's budget —
    1m bars + claude timeout_s 90 would stale-drop every entry decided in 60-90s. Set the
    knob explicitly to be STRICTER than the agent timeout (fill quality over completeness).
    """
    explicit = cfg.execution.entry_freshness_s
    if explicit > 0:
        return explicit
    return max(timeframe_seconds(cfg.instrument.timeframe), agent_timeout_s(cfg))


class InstrumentConfig(BaseModel):
    symbol: str = "ES"
    timeframe: str = "5m"
    tick_size: float = 0.25
    tick_value: float = 12.50  # USD per tick per contract (ES e-mini = $12.50)
    # Bar resampler (opt-in; neutral defaults keep today's behavior). feed_timeframe is what
    # NinjaTrader streams (empty => == timeframe); decision_timeframe is what the engine reasons
    # on: "static" => == timeframe, "auto" => 2m in RTH / 1m in ETH (session_for_ts), or a fixed
    # "1m"/"2m" override. See resample.py and the bridge-bar-resampler design spec.
    feed_timeframe: str = ""
    decision_timeframe: str = "static"


class StrategyParams(BaseModel):
    atr_period: int = Field(default=14, ge=1)
    swing_lookback: int = Field(  # bars each side of a pivot to confirm a swing high/low
        default=3, ge=1
    )
    atr_stop_mult: float = Field(default=1.5, gt=0)
    atr_target_mult: float = Field(default=2.0, gt=0)
    pullback_atr: float = Field(  # how close (in ATR) to the swing still counts as a pullback
        default=0.5, ge=0
    )
    min_confidence: float = Field(  # engine ignores Decisions below this confidence
        default=0.55, ge=0.0, le=1.0
    )
    # Deterministic regime discipline: when True, the engine converts an ENTRY to WAIT
    # whenever the structural regime read is "transitional" — the strategy's own master
    # switch (strategy.md / market-regime.md) says stand down in unclear/mixed structure.
    # A belt over the brain's judgment after it was observed firing authored setups in chop.
    # Exits and position management are never gated. False = neutral (trust the brain's read).
    wait_in_transitional: bool = False
    # Deterministic order-flow gate: when > 0, the engine converts an ENTRY to WAIT unless
    # delta_ratio confirms direction — a long needs delta_ratio >= +delta_floor, a short
    # needs <= -delta_floor. The armed plan trigger (plan.evaluate_plan) fires on a price
    # band ALONE, so without this the delta floor a setup names in its prose is never
    # enforced mechanically. 0.0 = off (the neutral default; also avoids gating replay /
    # backtests that carry no order-flow data). Only enable where real bid/ask delta is
    # streamed (live NT8). Exits and position management are never gated.
    delta_floor: float = Field(default=0.0, ge=0.0)
    # Stricter, delta-CONDITIONAL transitional gate (only consulted when wait_in_transitional
    # is False). When > 0 and the regime read is "transitional", an ENTRY fires only if
    # delta_ratio confirms direction at THIS floor (long >= +floor, short <= -floor) — a higher
    # order-flow bar than the global delta_floor, because transitional structure (mixed/breaking,
    # or too few pivots yet) needs stronger proof a breakout is real. Stacks above delta_floor:
    # in transitional the effective bar is the stricter of the two. 0.0 = off (the neutral
    # default). Exits and position management are never gated.
    transitional_delta_floor: float = Field(default=0.0, ge=0.0)
    # ETH session-scale for BOTH delta floors above. ETH (overnight/extended) carries a fraction
    # of RTH volume and a balanced-but-drifting tape: a genuine grind shows a persistent MILD
    # imbalance (delta_ratio ~ -0.02..-0.04), rarely an RTH-sized spike past delta_floor. When a
    # bar's session is ETH the effective floor becomes floor * eth_delta_scale (long needs
    # delta_ratio >= +eff, short <= -eff). 1.0 = neutral (no ETH change — the default; replay /
    # RTH are untouched). Set < 1 (e.g. 0.5) ONLY from a re-scored decline tape, and pair it with
    # delta_sustain_bars so a lower bar isn't tripped by one mild bar. Exits are never gated.
    eth_delta_scale: float = Field(default=1.0, gt=0.0)
    # Sustained-delta confirmation — the "ETH grinds, it doesn't spike" lever. When > 0, a delta
    # gate ALSO confirms if delta_ratio has held the trade direction's sign for this many
    # CONSECUTIVE recent decision-bars: a persistent lean qualifies even when the current bar's
    # magnitude is below the (session-scaled) spike floor. 0 = off (the neutral default — only the
    # per-bar magnitude check applies). Relaxation only (it can let MORE entries through), so
    # enable it WITH a calibrated eth_delta_scale and validate on tape. Exits are never gated.
    delta_sustain_bars: int = Field(default=0, ge=0)
    # Temporal hysteresis on the mechanical regime/trend label. classify_regime is stateless
    # and can flip bar-to-bar on a single mixed pivot; that thrash drives needless re-authoring
    # and directional indecision. A NEW (regime, trend) read must persist this many CONSECUTIVE
    # bars before it replaces the committed label. 1 = off (adopt every read = raw classifier).
    regime_hysteresis_bars: int = Field(default=1, ge=1)
    # Stop band (vol-scaled stop, then CLAMPED). The protective stop is
    # round(atr_stop_mult × ATR) in ticks, clamped into [min_stop_ticks, max_stop_ticks];
    # a bound of 0 = unbounded (the neutral default, so the legacy raw ATR stop is
    # unchanged). The floor is what stops a 1m noise wick from tagging a razor-thin stop;
    # the ceiling caps the stop in a vol spike (size then clamps down to fit max_risk). See
    # stops.atr_band_stop_ticks. Enforced as the final word by the RiskGate on every order.
    min_stop_ticks: int = Field(default=0, ge=0)
    max_stop_ticks: int = Field(default=0, ge=0)
    # Volatility-scaled stop FLOOR, enforced by the RiskGate on EVERY entry regardless of
    # which brain set the stop. The minimum protective-stop distance is
    # round(min_stop_atr_mult × ATR) in ticks (still capped by max_stop_ticks). This is the
    # fix for a brain that proposes a razor-thin stop while ATR is large: a fixed tick floor
    # can't adapt, so a 2-tick stop against a 40pt ATR slips through. 0 = disabled (neutral
    # default; only the fixed min_stop_ticks applies). See stops.vol_stop_floor_ticks.
    min_stop_atr_mult: float = Field(default=0.0, ge=0.0)
    # Winner management — enforced deterministically by the engine (brain-agnostic, like the
    # RiskGate), NOT delegated to the LLM. Once a position runs breakeven_r × (initial stop
    # distance) in our favor, the working stop is pulled to breakeven; with trail_enabled it
    # then trails behind each new swing (higher-low up / lower-high down), only ever
    # tightening. breakeven_r = 0 disables it entirely (static bracket, the legacy default),
    # so a wider initial stop never turns a trade that worked into a big give-back. See
    # stops.managed_stop_price.
    breakeven_r: float = Field(default=0.0, ge=0.0)
    trail_enabled: bool = False

    @model_validator(mode="after")
    def _check_stop_band(self) -> StrategyParams:
        # A bound of 0 = "unbounded", so only a band where BOTH ends are active can invert.
        lo, hi = self.min_stop_ticks, self.max_stop_ticks
        if lo > 0 and hi > 0 and lo > hi:
            raise ValueError(
                f"min_stop_ticks ({lo}) must not exceed max_stop_ticks ({hi}); "
                "the stop band is inverted"
            )
        return self


class RiskParams(BaseModel):
    max_contracts: int = Field(default=2, ge=1)
    max_risk_per_trade: float = Field(default=250.0, gt=0)   # USD
    max_trades_per_day: int = Field(default=10, ge=1)
    # Injected only if a decision lacks a stop. Kept within max_risk_per_trade for a
    # single contract (16 ticks * $12.50 = $200 < $250) so the safety net is usable.
    default_stop_ticks: int = Field(default=16, ge=1)
    # ATR-regime risk scaling. When the current ATR is >= strategies.reauthor.shock_ratio ×
    # the longer-window baseline ATR (a volatility spike — the SAME shock the re-author
    # governor reacts to), the per-trade dollar budget is multiplied by this factor (e.g.
    # 0.5 = halve size in a shock; size clamps down accordingly). 1.0 disables scaling
    # (the neutral default). See stops.risk_scale_for_atr.
    shock_risk_scale: float = Field(default=1.0, gt=0)
    # Confidence-scaled sizing. When True, an entry's size ramps with the decision's
    # confidence: 1 contract at strategy.min_confidence (the lowest confidence an entry is
    # taken at) up to the full budget — the lesser of max_contracts and the per-trade
    # dollar cap — at full_size_confidence. When False (the neutral default), size is the
    # brain's requested qty clamped DOWN to the caps (legacy). See stops.size_for_confidence.
    confidence_sizing: bool = False
    full_size_confidence: float = Field(  # confidence at/above which the full budget is used
        default=0.85, ge=0.0, le=1.0
    )
    # Exchange holiday / early-close protection. On a full US market holiday (all day) or a
    # futures early-close half day (13:00 ET), flatten any open position once within this many
    # minutes of the close and take no new entries for the rest of that session — so a position
    # can't carry the holiday/weekend gap (15 -> 12:45 ET, matching the manual practice).
    # Deterministic + server-side (same authority as the daily-goal flatten). <= 0 disables it.
    # See market_calendar; the entry-window pill shows CLOSED / WIND_DOWN accordingly.
    early_close_flat_lead_min: int = 15


class DailyGoal(BaseModel):
    profit_target: float = Field(  # USD — halt new entries for the day when reached
        default=500.0, gt=0
    )
    max_daily_loss: float = Field(  # USD — flatten + halt when reached (stored positive)
        default=400.0, gt=0
    )


class SessionWindow(BaseModel):
    enforce_hours: bool = False
    start: str = "09:30"
    end: str = "16:00"
    timezone: str = "America/New_York"


class ClaudeClientConfig(BaseModel):
    # The decision brain via the Claude Code CLI in headless print mode, on your
    # subscription (no ANTHROPIC_API_KEY / metered API).
    claude_bin: str = "claude"          # path/name of the claude launcher
    model: str = "sonnet"               # sonnet | haiku | opus | full model id (haiku = fastest)
    safe_mode: bool = True              # --safe-mode: isolate from CLAUDE.md/hooks/MCP/skills
    # Latency lever. Extended-"thinking" tokens dominate decision latency: uncapped, one
    # decision can emit thousands of tokens (~30–50s) and even blow past timeout_s → WAIT.
    # Threaded into the subprocess as MAX_THINKING_TOKENS. 0 = minimal thinking (fastest,
    # ~10s); raise (e.g. 1024) if decisions show a WAIT bias from too little reasoning;
    # None = uncapped (slowest, most deliberation).
    max_thinking_tokens: int | None = 0
    timeout_s: float = 30.0
    # Model for the one-time session study (planner). None = use `model`. The study
    # reads a long history once and writes the brief the fast per-bar plans build on,
    # so it can afford a bigger model (e.g. model: haiku, session_model: sonnet).
    session_model: str | None = None
    # Keep one `claude` child alive across requests (system prompt paid once) instead
    # of a fresh process per decision. Falls back to one-shot calls on any session
    # failure. Saves the 1-3s CLI cold start on every analysis.
    persistent: bool = False
    # A persistent session accumulates one conversation turn per analysis, so its
    # context (and therefore latency) grows all session long. Recycle the child after
    # this many turns — one cold start every N analyses instead of latency creeping
    # past the plan budget. None = never recycle.
    max_session_turns: int | None = 40
    extra_args: list[str] = Field(default_factory=list)  # appended verbatim to the claude argv
    # Per-model overload fallback: appended verbatim as `--fallback-model <csv>` to the claude
    # argv (one-shot AND persistent). A per-model 529/overload routes down the list inside one
    # CLI call. [] = off (the neutral default; today's behavior). Live: ["sonnet", "haiku"].
    fallback_models: list[str] = Field(default_factory=list)
    # Directory of *.md context files loaded verbatim into the system prompt (this is
    # how the agent learns the strategy/order-flow/risk/goal). Absolute or relative to CWD.
    context_dir: str = "hermes/context"
    # Used only if context_dir is missing/empty.
    context_hint: str = (
        "You are a disciplined futures day-trader. Trade a trend-pullback strategy "
        "with order-flow confirmation, ATR brackets, and strict risk limits."
    )


class ResilienceConfig(BaseModel):
    """Brain-outage resilience for the Claude client (see the heartbeat design spec).

    `enabled` wraps the Claude client in `ResilientBrain` (heartbeat + model-fallback +
    auto-recover + loud status). `mock_fallback_enabled` separately gates the Tier-2 escalation
    that trades on the deterministic mock engine once Claude is confirmed dead across all models
    for a SUSTAINED window (both thresholds must be met). All neutral/off by default."""

    enabled: bool = False
    mock_fallback_enabled: bool = False
    mock_after_consecutive_failures: int = Field(default=3, ge=1)
    mock_after_seconds_down: float = Field(default=300.0, gt=0)


class AgentConfig(BaseModel):
    # Validated: a stale value (e.g. the legacy "hermes" brain, replaced by "claude")
    # must fail loudly at load — never silently fall back to the mock rules brain,
    # which arms triggers and places orders on its own.
    client: Literal["mock", "claude"] = "mock"
    prefilter: str = "none"           # none | mock (mock rules screen entries before Claude)
    # After Claude DECLINES a prefilter candidate, near-identical candidates (same
    # direction, close within dedup_atr × ATR of the declined close) are answered
    # locally for up to dedup_bars bars instead of burning another Claude call —
    # extended trends otherwise produce the same candidate bar after bar. 0 disables.
    prefilter_dedup_bars: int = 5
    prefilter_dedup_atr: float = 0.5
    claude: ClaudeClientConfig = Field(default_factory=ClaudeClientConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)


class PlannerConfig(BaseModel):
    """The pre-armed plan cycle (plan.py): analysis between bars, instant closes."""

    enabled: bool = True
    # Budgets for the background analyses. These are bridge-side limits (surfaced in
    # dashboard error tags), unrelated to NinjaTrader's HttpTimeoutMs. The plan
    # analysis runs between bars, so it can afford more than the per-bar timeout_s.
    plan_timeout_s: float = 75.0
    session_timeout_s: float = 180.0
    # A plan armed from a bar this many closes old no longer fires (market moved on).
    max_plan_age_bars: int = 2


class ReauthorConfig(BaseModel):
    """Structure-driven re-authoring (agent mode): when the brain re-runs the pre-session
    study to refresh its playbook WHILE a session is live.

    The playbook is a STRUCTURAL artifact (a regime read + setups built around specific
    levels), so re-authoring is triggered by playbook *invalidation* — not by raw
    volatility, which is orthogonal to whether the setups still fit:

    - **trend flip**: the live trend turned opposite to the trend the playbook was authored
      under (a long-biased uptrend playbook is simply wrong in a downtrend),
    - **uncovered regime**: no authored setup is tagged for the live regime, so the brain is
      benched with nothing to arm,
    - **volatility shock** (secondary): an ATR spike/collapse mis-scales the playbook's
      ATR-based stops/targets even when structure holds — kept because bracket sizing IS a
      genuinely volatility-driven concern.

    A structural change must persist ``confirm_bars`` closes before it fires, so a one-bar
    wobble through "transitional" doesn't thrash the playbook. ``min_interval_bars`` is a hard
    debounce floor; ``max_interval_bars`` is a freshness ceiling that re-authors even in a
    calm, unchanging market. If the study failed to author any playbook, ``retry_bars``
    re-attempts instead of leaving the brain stuck in WAIT. All intervals are in BARS, so they
    auto-scale with the chart timeframe. Re-authoring is seamless: the old playbook keeps
    trading until the new one lands (no WAIT gap)."""

    enabled: bool = True
    confirm_bars: int = 3            # a structural change must persist this many closes to fire
    min_interval_bars: int = 10      # debounce floor — never re-author more often than this
    max_interval_bars: int = 60      # freshness ceiling — re-author at least this often
    retry_bars: int = 5              # re-attempt this many bars after a failed/empty author
    baseline_atr_period: int = 100   # longer-window ATR = the "normal" volatility reference
    shock_ratio: float = 2.0         # |current/baseline ATR| past this (or its inverse) = a shock
    reauthor_after_trade: bool = False  # re-author when a trade closes (own small floor below)
    post_trade_min_bars: int = 2        # bars-since-author debounce for the post-trade trigger
    drift_atr_mult: float = 0.0         # re-author when |close-authored_close| >= this x ATR; 0=off


class StrategyAuthoringConfig(BaseModel):
    """Where the regime playbooks (the swappable "strategy") come from.

    - ``custom``: load the user's own playbooks from ``context_dir/strategies/**`` — the
      brain invents nothing (the legacy behavior). Empty dirs ⇒ no playbook ⇒ WAIT.
    - ``agent``: the brain AUTHORS its own playbook from the one-time session history
      study and trades that instead of any on-disk playbook ("always use what it
      invented"). The framework files (regime/order-flow/risk/goal + hard rules) are
      still loaded in both modes; only the regime playbooks are swapped.

    NinjaTrader's ``UseAgentStrategies`` toggle overrides ``source`` at runtime (reported
    over ``/ingest/account``), exactly like the reported account name overrides
    ``execution.account``. Failure to author / not-yet-authored degrades to WAIT.
    """

    source: Literal["custom", "agent"] = "agent"
    # Authored playbooks are written here (one file per session) for review/audit, plus a
    # stable ``latest.md``. Created on demand. Gitignored — they are session artifacts.
    generated_dir: str = "hermes/generated"
    # Cap on the authored playbook fed back into the system prompt (keeps it bounded).
    max_chars: int = 6000
    # Structure-driven re-authoring cadence (agent mode).
    reauthor: ReauthorConfig = Field(default_factory=ReauthorConfig)


class LevelsConfig(BaseModel):
    """Swing-pivot S/R detection (levels.py) for `GET /levels` + the plan prompt."""

    enabled: bool = True
    lookback: int = 3        # bars on each side that must be lower/higher to confirm a pivot
    merge_ticks: int = 8     # pivots within this many ticks cluster into one zone
    min_touches: int = 1     # zones with fewer pivots are dropped
    max_levels: int = 12     # strongest-first cap on the returned zones


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787


class StorageConfig(BaseModel):
    """Optional SQLite persistence for the bar store. Empty ``bars_db`` = disabled (the
    neutral default; the store stays a pure in-memory deque). A path turns on write-through:
    every bar is mirrored to SQLite and the tail is reloaded on startup, so multi-day history
    — and the levels/calibration tooling that reads it — survives a bridge restart. DB
    failures degrade to memory-only; persistence never breaks the bar loop."""

    bars_db: str = ""  # path to the SQLite file; "" = in-memory only
    # Day-state JSON (realized P&L + trade count + halt state), restored on the first bar of
    # the SAME trading day so a mid-day restart doesn't zero the dashboard / daily-loss
    # headroom. "" = disabled (in-memory only).
    session_state: str = ""


class ExecutionConfig(BaseModel):
    # Hard gate on real-money trading. Must be explicitly true AND acknowledged.
    allow_live: bool = False
    account: str = "Sim101"
    # 0 = auto: max(one bar interval, the active agent's timeout_s) — see
    # effective_entry_freshness_s(). Drops an ENTRY whose decision took >= this many
    # seconds as stale (the bar it reasoned about is old). EXIT/FLATTEN always execute.
    # Set explicitly to be stricter (e.g. 60 on 2m bars, prioritizing fill quality).
    entry_freshness_s: float = 0.0


class LearningConfig(BaseModel):
    enabled: bool = True
    learned_dir: str = "hermes/learned"          # trader-profile.md, agent-notes.md, lessons/*.md
    journal_path: str = "bridge/state/journal.jsonl"  # episodic record of closed trades
    # Resolved counterfactuals for declined/unfilled setups (the over-blocking evidence
    # stream a closed-trades journal can never carry). Written by the engine's replay hook.
    declines_path: str = "bridge/state/declines.jsonl"
    retrieve_k: int = 3                           # similar past trades fed into each decision
    profile_char_limit: int = 1400
    notes_char_limit: int = 2200
    lessons_char_limit: int = 2500
    # Notes triage: the prompt shows the NEWEST notes within notes_char_limit; once the
    # live agent-notes.md outgrows notes_archive_over_chars, the oldest bullets move to
    # hermes/learned/archive/ (long-term memory — never deleted), keeping the newest
    # notes_keep_chars as the working set. 0 = archival off (neutral).
    notes_archive_over_chars: int = Field(8000, ge=0)
    notes_keep_chars: int = Field(4000, ge=0)
    reflect_enabled: bool = True
    reflect_on_trade_close: bool = True
    reflect_model: str = "sonnet"     # model for reflection/curation calls
    reflect_recent: int = 20          # recent trades shown to reflection for context
    # Flat-only reflection: when no trade has closed but this many DECLINED setups have
    # resolved would-win (the over-blocking signal), a reflection fires to consider
    # narrowing the lesson that vetoed them. Counts unreported would-wins; see DeclineLog.
    reflect_missed_wins: int = Field(3, ge=1)
    # Counterfactual self-correction: replay entry setups the brain armed but did NOT take
    # (the plan cycle re-arms every bar; the engine dedups by band so one missed pullback is
    # logged once). Resolved outcomes feed reflect_on_missed. OFF by default (neutral).
    counterfactuals_enabled: bool = False
    counterfactual_horizon_bars: int = Field(20, ge=1)   # bars before a replay gives up
    counterfactual_dedup_atr: float = Field(0.5, ge=0)   # bands within this x ATR = same setup
    # Exit replay: when a trade closes BELOW its target (an invalidation/discretionary exit),
    # replay it forward on its ORIGINAL target/stop. would_win = the exit LEFT MONEY (price
    # reached the target — a shakeout); would_lose = the exit dodged the stop. Resolved
    # early_exit records feed reflect_on_missed like declines. OFF by default (neutral).
    exit_replays_enabled: bool = False
    max_lessons: int = 40             # cap applied lessons per reflection
    # Staggered distillation: a slower, deeper model periodically compresses the full
    # lesson/note corpus into ONE bounded distilled.md that the realtime decision prompt
    # reads INSTEAD of raw lessons — knowledge can grow without bloating the per-bar
    # prompt. Trigger via POST /control/distill.
    distill_model: str = "opus"       # the slow, deep tier for the distillation pass
    distilled_char_limit: int = Field(1600, ge=1)  # hard cap on the distilled artifact
    # Distillation is an opus pass over the FULL corpus — it needs far more than the
    # per-bar claude.timeout_s (30s). It runs off the hot path (manual /control/distill),
    # so a generous budget is safe.
    distill_timeout_s: float = Field(300.0, gt=0)
    # Automatic consolidation cadence: a wall-clock daemon (server._start_consolidation)
    # periodically runs curate() then distill() so the learned corpus stays compressed
    # without a manual /control/curate|distill. Material-gated — no model call when nothing
    # changed. OFF by default (neutral); trading.local.yaml opts in. Freshness/liveness show
    # on /dashboard + /panel.txt (consolidate_*).
    consolidate_enabled: bool = False
    consolidate_interval_minutes: float = Field(120.0, gt=0)
    consolidate_startup_delay_s: float = Field(90.0, ge=0)
    # End-of-day reflection (descriptive day-review + gated cross-day lesson). OFF by default
    # (neutral); trading.local.yaml opts in. Fires once per CME day after the ET cutoff.
    eod_review_enabled: bool = False
    eod_review_cutoff_et: str = "16:05"   # ET HH:MM after which the day-review may fire
    day_review_keep: int = Field(10, ge=1)        # rolling day-reviews retained + shown in prompt
    day_lesson_repeat_n: int = Field(3, ge=2)     # theme must recur in this many reviews
    day_lesson_lookback_m: int = Field(5, ge=1)   # window of recent reviews scanned for a repeat


class NewsConfig(BaseModel):
    """Major-news blackout. The bridge fetches an economic calendar and the RiskGate
    blocks new ENTRIES (exits always allowed) within ``window_minutes`` of a high-impact
    event for the configured currencies. Deterministic + server-side — never the LLM. Fails
    OPEN: a fetch error keeps the last-good calendar, and with none cached, trading proceeds.
    Disabled by default (neutral); turn it on in ``config/trading.yaml``."""

    enabled: bool = False
    # Where the calendar comes from:
    #   "json"         — fetch ``feed_url`` (stable, sanctioned, RECOMMENDED).
    #   "forexfactory" — scrape ``forexfactory_url`` directly (the embedded calendar blob).
    #     Same events, but a more brittle path (internal markup) behind Cloudflare; use as an
    #     alternate/fallback source. Both still fail OPEN.
    source: Literal["json", "forexfactory"] = "json"
    # Economic-calendar JSON. Default = the free ForexFactory weekly mirror (no key); items
    # carry {title, country (currency code), date (ISO8601), impact}. Any source with the
    # same shape works. Used when source == "json".
    feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    # The ForexFactory calendar page scraped when source == "forexfactory".
    forexfactory_url: str = "https://www.forexfactory.com/calendar"
    # Only events whose currency is in this list trigger a blackout (MNQ/ES/NQ ⇒ USD).
    currencies: list[str] = Field(default_factory=lambda: ["USD"])
    # Which impact tiers count as "major". The feed uses High | Medium | Low | Holiday.
    block_impacts: list[str] = Field(default_factory=lambda: ["High"])
    # Block entries within ± this many minutes of a matching event's scheduled time.
    window_minutes: float = Field(default=2.0, ge=0)
    # Background refetch cadence (the weekly feed is near-static, so this is cheap).
    refresh_minutes: float = Field(default=30.0, gt=0)
    fetch_timeout_s: float = Field(default=10.0, gt=0)


class AccountProfileConfig(BaseModel):
    """The user-selected prop firm + account program.

    Selecting one (in the dashboard, or here) does two things:
    1. loads the firm's plain-English context file into the brain's system prompt, and
    2. applies the account's hard numbers into the ENFORCED config the RiskGate reads
       (the daily loss limit and the contract ceiling — the numbers that map onto the
       bridge's existing safety primitives; see ``prop_firms.apply_account_profile``).

    The firm CATALOG (firms -> account types -> sizes + numbers) lives in
    ``config/prop-firms.yaml`` (committed reference data). The PERSONAL selection lives in
    ``config/trading.local.yaml`` (gitignored, deep-merged on top), exactly like the account
    name and personal risk. ``None`` for all three fields ⇒ no firm selected ⇒ nothing is
    loaded or overridden (neutral default)."""

    prop_firm: str | None = None       # firm name; must match a catalog entry
    account_type: str | None = None    # account program name within the firm
    account_size: float | None = None  # account size within the program
    # The committed catalog of firms/accounts and the directory of firm context *.md files.
    # context_dir is deliberately OUTSIDE hermes/context/ so the framework loader does not
    # concatenate every firm file into the prompt — only the selected one is loaded.
    catalog_path: str = "config/prop-firms.yaml"
    context_dir: str = "hermes/prop-firms"


class BridgeConfig(BaseModel):
    strategy_id: str = "hermes-default"
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    strategy: StrategyParams = Field(default_factory=StrategyParams)
    risk: RiskParams = Field(default_factory=RiskParams)
    daily_goal: DailyGoal = Field(default_factory=DailyGoal)
    session: SessionWindow = Field(default_factory=SessionWindow)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    strategies: StrategyAuthoringConfig = Field(default_factory=StrategyAuthoringConfig)
    levels: LevelsConfig = Field(default_factory=LevelsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    account_profile: AccountProfileConfig = Field(default_factory=AccountProfileConfig)

    def apply_env(self) -> BridgeConfig:
        host = os.getenv("HERMES_BRIDGE_HOST")
        port = os.getenv("HERMES_BRIDGE_PORT")
        agent = os.getenv("HERMES_BRIDGE_AGENT")
        if host:
            self.server.host = host
        if port:
            self.server.port = int(port)
        if agent:
            self.agent.client = agent
        return self


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` onto `base` (override wins; nested dicts merged)."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load config from YAML; fall back to all-defaults if `path` is None/missing.

    If a sibling `*.local.yaml` exists next to `path` (e.g. `config/trading.local.yaml`),
    it is deep-merged ON TOP of the base file. Keep personal values (account, daily risk)
    in that gitignored local file so they never get committed; the base file stays a
    neutral, shareable template.
    """
    if path is None:
        return BridgeConfig().apply_env()
    p = Path(path)
    if not p.exists():
        return BridgeConfig().apply_env()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    local = p.with_name(f"{p.stem}.local{p.suffix}")
    if local.exists():
        overrides = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        data = _deep_merge(data, overrides)
    return BridgeConfig.model_validate(data).apply_env()
