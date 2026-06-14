#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Input;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using D2D = SharpDX.Direct2D1;
using DW = SharpDX.DirectWrite;
using DXRect = SharpDX.RectangleF;   // SharpDX 2.6.3 (NT8-shipped): ctor is (x, y, WIDTH, HEIGHT)
using DXVec = SharpDX.Vector2;
#endregion

// =============================================================================
//  HermesBridgeStrategy  (strategy + built-in dashboard)
// -----------------------------------------------------------------------------
//  Streams chart data to the Python "hermes-bridge", executes the risk-approved
//  orders it returns (Sim by default), AND renders the agent's dashboard card +
//  S/R levels directly on the chart. The dashboard used to be a separate
//  HermesDashboard indicator; it is now built into this strategy (NT8 strategies
//  support OnRender), so enabling the strategy is all that's needed — there is no
//  separate indicator to add (remove the old HermesDashboard indicator from the
//  chart, and from Custom\Indicators, so you don't get two cards).
//
//  TRADING (unchanged):
//    * Historical→realtime transition bulk-uploads loaded bars to /ingest/history.
//    * Each CLOSED realtime bar (Calculate.OnBarClose) POSTs to /ingest/bar, then
//      GETs /commands/next and runs any command on the strategy thread.
//    * Fills are reported to /ingest/fill from OnExecutionUpdate.
//    * AllowLive defaults FALSE; a live (non-Sim/Playback) account disables trading.
//    * The selected account name is reported to the bridge (/ingest/account) so the
//      dashboard/logs follow the chart's account selection, not a static config default.
//
//  DASHBOARD (folded in from HermesDashboard):
//    * A background timer polls /panel.txt + /levels.txt and caches an immutable
//      snapshot — NO chart/UI/dispatcher calls from the timer (AGENTS.md #17).
//    * The card is drawn in OnRender (SharpDX only, no Draw.* there — #13); S/R
//      lines use Draw.* from the throttled OnBarUpdate. Brushes are device
//      resources created eagerly in OnRenderTargetChanged with a self-heal in
//      OnRender (§5b). _terminated (set first in Terminated) gates DX teardown.
//    * The card is click-draggable (double-click resets); the header glyph folds
//      it. Offsets persist via serialized CardOffsetX/Y/CardFolded.
//
//  NOTE: compiles INSIDE NinjaTrader 8 (NinjaScript editor / F5) — it links against
//  NinjaTrader + SharpDX assemblies and cannot be built by a standalone toolchain.
//  OnRender/OnRenderTargetChanged/RenderTarget/ChartPanel are used from a Strategy
//  (supported in NT8); the strategy must be applied directly to a chart to render.
// =============================================================================

namespace NinjaTrader.NinjaScript.Strategies
{
    public class HermesBridgeStrategy : Strategy
    {
        // ---- trading state -------------------------------------------------
        // Timeout is neutralized at construction (the only legal moment — it locks after
        // the first request): the 100s default would silently cap any HttpTimeoutMs above
        // 100000. The per-request CancellationTokenSource is the single timeout authority.
        private static readonly HttpClient Http =
            new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        private bool historySent;
        private bool tradingDisabled;

        private const string LongSignal = "HermesLong";
        private const string ShortSignal = "HermesShort";

        #region Properties
        [NinjaScriptProperty]
        public string BridgeHost { get; set; } = "127.0.0.1";

        [NinjaScriptProperty]
        public int BridgePort { get; set; } = 8787;

        [NinjaScriptProperty]
        public string StrategyId { get; set; } = "hermes-default";

        [NinjaScriptProperty]
        public bool SendHistory { get; set; } = true;

        // Hard guard: must be explicitly enabled to trade a non-simulation account.
        [NinjaScriptProperty]
        public bool AllowLive { get; set; } = false;

        // Strategy source toggle. TRUE (default): the agent AUTHORS its own playbook from
        // this chart's historical bars and trades that. FALSE: the agent invents nothing
        // and trades the user's own playbooks (hermes/context/strategies/**); empty dirs
        // mean it simply WAITs. Reported to the bridge (/ingest/account) BEFORE history so
        // the pre-session study knows which mode to run in. Safety (risk gate, brackets,
        // the AllowLive account guard) is identical either way.
        [NinjaScriptProperty]
        public bool UseAgentStrategies { get; set; } = true;

        // Windows timezone id of the CHART's "Time zone" setting (what Time[0] is
        // expressed in). "Eastern Standard Time" = US ET incl. DST. If the bridge logs
        // a bar.ts skew warning, set this to match the chart.
        [NinjaScriptProperty]
        public string BarTimeZoneId { get; set; } = "Eastern Standard Time";

        // MUST exceed the agent's decision time, or the bar POST is abandoned before the
        // bridge finishes deciding and the command isn't fetched until the NEXT bar (late /
        // stale). Claude-on-subscription decisions run ~30-115s, so set this to your bridge's
        // agent timeout (config: agent.claude.timeout_s) and keep it just BELOW your bar
        // interval to avoid overlapping requests. 2m bar + 115s bridge timeout -> 115000.
        [NinjaScriptProperty]
        public int HttpTimeoutMs { get; set; } = 115000;

        private string BaseUrl => string.Format("http://{0}:{1}", BridgeHost, BridgePort);
        #endregion

        // ---- dashboard state -----------------------------------------------
        private Timer _timer;
        private int _pollBusy;   // Interlocked gate: timer ticks must not overlap

        // Snapshot published by the timer thread, consumed by OnRender. Immutable
        // after construction; volatile swap gives safe publication.
        private volatile Snapshot _snap;
        private volatile string _levelsText = "";   // raw /levels.txt, parsed on the chart thread
        private volatile string _error;             // last poll failure (null when healthy)

        private DateTime _lastDrawUtc = DateTime.MinValue;  // throttle for Draw.* (S/R lines)

        // ---- card dragging (handlers run on the UI thread; OnRender only reads
        //      the float offsets — 32-bit reads/writes are atomic) ---------------
        private ChartControl _mouseChart;     // chart whose Preview events we hooked
        private bool _mouseHooked;
        private volatile bool _hookQueued;    // an InvokeAsync hookup is in flight
        private bool _dragging;
        private float _dragGrabX, _dragGrabY; // mouse-to-card-origin grab offset
        private DateTime _lastDragRefreshUtc = DateTime.MinValue;
        private int _mouseErrorCount;
        private float _offX, _offY;           // clamped offset actually applied at replay
        private float _foldX1, _foldY1, _foldX2, _foldY2;   // fold-toggle hit box (built coords)
        private volatile bool _rebuildAsap;   // UI thread requests a display-list rebuild

        // ---- display list (built/replayed only on the render thread) ------------
        private readonly List<DrawOp> _ops = new List<DrawOp>();
        private readonly Dictionary<uint, D2D.SolidColorBrush> _brushes =
            new Dictionary<uint, D2D.SolidColorBrush>();
        private Snapshot _builtFrom;
        private string _builtError;
        private DateTime _lastBuildUtc = DateTime.MinValue;
        private float _cardX, _cardY, _cardW, _cardH;
        private bool _hasCard;
        private float _s = 1f;                      // FontSize/12 scale factor

        // Device-resource lifecycle (AGENTS.md §5b) + teardown guard (§5d).
        private volatile bool _terminated;          // set FIRST in Terminated; OnRender bails on it
        private D2D.RenderTarget _lastSeenRT;       // target the brush palette was created against
        private bool _dxInitialized;                // palette valid for _lastSeenRT
        private int _renderErrorCount;              // bounded error prints
        private int _barErrorCount;
        private int _stateErrorCount;

        private DW.TextFormat _fLabel, _fSub, _fBody, _fBodyB, _fHead, _fBig, _fRat;
        private DW.EllipsisTrimming _ratEllipsis;

        [NinjaScriptProperty]
        public int RefreshSeconds { get; set; } = 5;

        // Master size knob: the whole card scales by FontSize/12 — text AND the layout
        // (width, padding, boxes) in both BuildOps and EnsureFormats. 12 = original size,
        // 18 = 1.5×. Bump this if the card reads too small on a high-DPI / 4K chart.
        [NinjaScriptProperty]
        public int FontSize { get; set; } = 18;

        [NinjaScriptProperty]
        public int RecentRows { get; set; } = 5;    // decision-table rows (1..8)

        [NinjaScriptProperty]
        public bool ShowLevels { get; set; } = true;   // plot the agent's S/R (swing) lines

        // Dragged card position (px offset from the default top-left anchor). Not
        // browsable — set by dragging; serialized so it persists with the workspace.
        [Browsable(false)]
        public float CardOffsetX { get; set; }

        [Browsable(false)]
        public float CardOffsetY { get; set; }

        // Folded = header + position/P&L strip only. Toggled by the ▾/▸ glyph in
        // the card header; serialized so it persists with the workspace.
        [Browsable(false)]
        public bool CardFolded { get; set; }

        // ---- palette (AARRGGBB) --------------------------------------------------
        private const uint ColCardBg   = 0xF0131720;
        private const uint ColCardEdge = 0xFF2A3140;
        private const uint ColDivider  = 0xFF222836;
        private const uint ColBoxBg    = 0xFF191E29;
        private const uint ColBoxEdge  = 0xFF2A3140;
        private const uint ColTrack    = 0xFF262C3A;
        private const uint ColText     = 0xFFE6E9EF;
        private const uint ColMuted    = 0xFF9AA3B2;
        private const uint ColDim      = 0xFF6B7280;
        private const uint ColGreen    = 0xFF4ADE80;
        private const uint ColRed      = 0xFFF87171;
        private const uint ColAmber    = 0xFFFBBF24;
        private const uint ColBlue     = 0xFF60A5FA;
        private const uint ColGreenBg  = 0xFF143A22;
        private const uint ColRedBg    = 0xFF3A1717;
        private const uint ColAmberBg  = 0xFF3A2D12;
        private const uint ColBlueBg   = 0xFF142B44;

        // The complete brush palette, created EAGERLY against the current RenderTarget
        // (§5b). ReplayOps only ever looks brushes up — it never creates them.
        private static readonly uint[] Palette =
        {
            ColCardBg, ColCardEdge, ColDivider, ColBoxBg, ColBoxEdge, ColTrack,
            ColText, ColMuted, ColDim, ColGreen, ColRed, ColAmber, ColBlue,
            ColGreenBg, ColRedBg, ColAmberBg, ColBlueBg,
        };


        protected override void OnStateChange()
        {
            // §5d: a lifecycle exception counts toward MaxRestarts — never let one out.
            try { OnStateChangeInner(); }
            catch (Exception ex)
            {
                if (_stateErrorCount < 5)
                {
                    _stateErrorCount++;
                    Print("[Hermes] OnStateChange error #" + _stateErrorCount
                        + " (State=" + State + "): " + ex.Message);
                }
            }
        }

        private void OnStateChangeInner()
        {
            if (State == State.SetDefaults)
            {
                Description = "Streams bars to the Hermes bridge, executes approved orders (Sim), "
                    + "and renders the agent dashboard card + S/R levels on the chart.";
                Name = "HermesBridgeStrategy";
                Calculate = Calculate.OnBarClose;          // one decision per closed bar
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsUnmanaged = false;                        // managed orders + brackets
                BarsRequiredToTrade = 30;                   // warm up indicators on the agent side
                IncludeCommission = true;
                StartBehavior = StartBehavior.WaitUntilFlat;
            }
            else if (State == State.DataLoaded)
            {
                // Dashboard background poll (panel.txt + levels.txt). Cheap; the chart
                // render path reads only the cached snapshot.
                if (_timer == null)
                    _timer = new Timer(Poll, null, 0, Math.Max(1, RefreshSeconds) * 1000);
            }
            else if (State == State.Historical)
            {
                // Earliest chance to hook the card-drag mouse events; ChartControl can
                // still be null here (F5 clone-reload), so OnRender retries until hooked.
                HookMouse(ChartControl);
            }
            else if (State == State.Realtime)
            {
                // First realtime moment: read the selected account, apply the safety
                // guard, and tell the bridge which account we're on — so the bridge's
                // dashboard/logs follow the chart's account selection, not the static
                // config default. Done unconditionally (even if SendHistory is off).
                GuardAccount();
                // Transitioned historical → realtime: ship the full history once. The
                // account report (which carries UseAgentStrategies) must land BEFORE the
                // history POST, because the history triggers the bridge's pre-session
                // study — and that study must already know whether to author a playbook
                // (agent) or use the on-disk ones (custom). Build the payload here on the
                // strategy thread (series access is unsafe from the HTTP continuation),
                // then report-then-post.
                if (SendHistory && !historySent)
                {
                    historySent = true;
                    string histBody = BuildHistoryPayload();
                    _ = ReportThenHistoryAsync(histBody);
                }
                else
                {
                    _ = ReportAccountAsync();
                }
            }
            else if (State == State.Terminated)
            {
                _terminated = true;   // FIRST: stop OnRender touching DX before we dispose it
                if (_timer != null) { try { _timer.Dispose(); } catch { } _timer = null; }
                UnhookMouse();        // async if off the UI thread — never a sync Invoke here
                try { RemoveLevelDrawings(); }
                catch { }
                try { DisposeOps(); } catch { }
                try { DisposeBrushes(); } catch { }
                try { DisposeFormats(); } catch { }
            }
        }

        protected override void OnBarUpdate()
        {
            // Dashboard S/R lines: redraw (throttled) independent of trading state, so
            // the levels show even before BarsRequiredToTrade and outside realtime.
            if ((DateTime.UtcNow - _lastDrawUtc).TotalMilliseconds >= 200)
            {
                _lastDrawUtc = DateTime.UtcNow;
                try { DrawAgentLevels(); }
                catch (Exception ex)
                {
                    if (_barErrorCount < 5)
                    {
                        _barErrorCount++;
                        Print("[Hermes] OnBarUpdate draw error #" + _barErrorCount + ": " + ex.Message);
                    }
                }
            }

            // Trading: one decision per CLOSED realtime bar.
            if (BarsInProgress != 0) return;
            if (CurrentBar < BarsRequiredToTrade) return;
            if (State != State.Realtime) return;
            string barJson = BarJson(
                EpochSeconds(Time[0]), Open[0], High[0], Low[0], Close[0], Volume[0]);
            _ = HandleBarAsync(barJson);
        }

        // ---- trading: networking / execution -------------------------------
        private async Task HandleBarAsync(string barJson)
        {
            // Send the bar. The bridge computes the decision server-side; this call may
            // block for the agent's full think time (LLMs ~15s), hence HttpTimeoutMs.
            try
            {
                string body = string.Format(
                    "{{\"instrument\":\"{0}\",\"timeframe\":\"{1}\",\"bar\":{2}}}",
                    Escape(Instrument.FullName), Escape(BarsPeriodString()), barJson);
                string resp = await PostAsync("/ingest/bar", body);

                // Self-healing history handshake: history is normally pushed once per
                // ENABLE, so a bridge restarted mid-session has an empty bar store (it
                // then reads swing structure / ATR on a thin live seed — 2026-06-11 incident).
                // The bridge flags need_history on every bar response until
                // /ingest/history arrives; re-send it, throttled.
                if (resp != null && resp.Contains("\"need_history\":true"))
                    MaybeResendHistory();
            }
            catch (Exception ex)
            {
                Print("Hermes bridge bar post error: " + ex.Message);
            }

            // Always poll for a risk-approved command — even if the bar post timed out,
            // the bridge still queues commands and we must not skip execution.
            try
            {
                string resp = await GetAsync("/commands/next?strategy_id=" + Uri.EscapeDataString(StrategyId));
                HermesCommand cmd = ParseCommand(resp);
                if (cmd != null)
                    TriggerCustomEvent(o => ExecuteCommand((HermesCommand)o), cmd);
            }
            catch (Exception ex)
            {
                Print("Hermes bridge command poll error: " + ex.Message);
            }
        }

        private DateTime _lastHistResendUtc = DateTime.MinValue;

        private void MaybeResendHistory()
        {
            // The flag re-arrives with every bar until history lands; one push per
            // 120s is plenty (the payload is the full loaded series).
            if ((DateTime.UtcNow - _lastHistResendUtc).TotalSeconds < 120) return;
            _lastHistResendUtc = DateTime.UtcNow;
            Print("Hermes: bridge requested a history re-send (bridge restarted?) — pushing.");
            // Build the payload on the strategy thread — series access (Bars.GetTime
            // et al.) is not safe from the HTTP continuation's pool thread. A restarted
            // bridge also lost the reported account + strategy source, so re-send both
            // (account first) before the history re-triggers the pre-session study.
            TriggerCustomEvent(o =>
            {
                string histBody = BuildHistoryPayload();
                _ = ReportThenHistoryAsync(histBody);
            }, null);
        }

        private int _lastHistCount;   // bar count of the last BuildHistoryPayload (for logging)

        // Build the /ingest/history JSON on the STRATEGY thread. Series access
        // (Bars.GetTime et al.) is unsafe from an HTTP continuation's pool thread, so the
        // payload is built synchronously here and the string handed to the async poster.
        private string BuildHistoryPayload()
        {
            var sb = new StringBuilder();
            sb.Append("{\"instrument\":\"").Append(Escape(Instrument.FullName))
              .Append("\",\"timeframe\":\"").Append(Escape(BarsPeriodString()))
              .Append("\",\"bars\":[");
            int last = CurrentBar; // last closed historical bar
            bool first = true;
            for (int i = 0; i <= last; i++)
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append(BarJson(
                    EpochSeconds(Bars.GetTime(i)), Bars.GetOpen(i), Bars.GetHigh(i),
                    Bars.GetLow(i), Bars.GetClose(i), Bars.GetVolume(i)));
            }
            sb.Append("]}");
            _lastHistCount = last + 1;
            return sb.ToString();
        }

        // Report the account + strategy source, THEN post the pre-built history. The
        // ordering matters: the bridge's pre-session study (triggered by the history)
        // must already know the strategy source. Awaiting the account POST guarantees the
        // bridge processed it before the history arrives.
        private async Task ReportThenHistoryAsync(string histBody)
        {
            // Enforce the ordering contract: if the account report did not reach the bridge,
            // do NOT post history — the pre-session study it triggers would run without knowing
            // the strategy source. Skipping leaves the bridge's bar store empty, so it keeps
            // flagging need_history and the bar handshake (MaybeResendHistory) retries
            // account-then-history shortly.
            if (!await ReportAccountAsync())
            {
                Print("Hermes: account report failed — deferring history (will retry via handshake).");
                return;
            }
            try
            {
                await PostAsync("/ingest/history", histBody);
                Print(string.Format("Hermes: sent {0} historical bars to {1}",
                    _lastHistCount, BaseUrl));
            }
            catch (Exception ex)
            {
                Print("Hermes bridge history error: " + ex.Message);
            }
        }

        // Tell the bridge which account this strategy is actually trading on (and whether
        // AllowLive is set here). Advisory: the bridge uses it only for its dashboard /
        // logs — NinjaTrader's account guard (GuardAccount) is the real execution
        // interlock. The name is whatever is selected in the strategy dialog, so the rest
        // of the tool follows the chart's account selection automatically.
        // Returns true only if the bridge acknowledged the POST — callers that must
        // preserve the account-before-history ordering (ReportThenHistoryAsync) gate on it.
        private async Task<bool> ReportAccountAsync()
        {
            try
            {
                string name = Account != null ? Account.Name : "";
                string body = string.Format(
                    "{{\"account\":\"{0}\",\"allow_live\":{1},\"use_agent_strategies\":{2}}}",
                    Escape(name), AllowLive ? "true" : "false",
                    UseAgentStrategies ? "true" : "false");
                await PostAsync("/ingest/account", body);
                Print("Hermes: reported account '" + name + "' to bridge (agent_strategies="
                    + (UseAgentStrategies ? "on" : "off") + ").");
                return true;
            }
            catch (Exception ex)
            {
                Print("Hermes bridge account report error: " + ex.Message);
                return false;
            }
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition, string orderId,
            DateTime time)
        {
            if (execution == null || execution.Order == null) return;
            // Report the fill so the bridge can track position / P&L / the daily goal.
            try
            {
                string side = execution.Order.IsLong ? "LONG" : "SHORT";
                int posAfter = SignedPosition();
                string body = string.Format(CultureInfo.InvariantCulture,
                    "{{\"order_id\":\"{0}\",\"side\":\"{1}\",\"qty\":{2},\"price\":{3},\"ts\":{4},\"position_after\":{5}}}",
                    Escape(orderId), side, quantity, price.ToString(CultureInfo.InvariantCulture),
                    EpochSeconds(time).ToString(CultureInfo.InvariantCulture), posAfter);
                _ = PostAsync("/ingest/fill", body);
            }
            catch (Exception ex)
            {
                Print("Hermes bridge fill report error: " + ex.Message);
            }
        }

        // ---- order execution (runs on the strategy thread) -------------------
        private void ExecuteCommand(HermesCommand cmd)
        {
            if (tradingDisabled)
            {
                Print("Hermes: trading disabled (account guard). Ignoring " + cmd.Action);
                return;
            }
            switch (cmd.Action)
            {
                case "ENTER_LONG":
                    if (Position.MarketPosition != MarketPosition.Flat) return;
                    SetBracket(LongSignal, cmd);
                    EnterLong(cmd.Qty <= 0 ? 1 : cmd.Qty, LongSignal);
                    break;
                case "ENTER_SHORT":
                    if (Position.MarketPosition != MarketPosition.Flat) return;
                    SetBracket(ShortSignal, cmd);
                    EnterShort(cmd.Qty <= 0 ? 1 : cmd.Qty, ShortSignal);
                    break;
                case "EXIT":
                case "FLATTEN":
                    if (Position.MarketPosition == MarketPosition.Long) ExitLong();
                    else if (Position.MarketPosition == MarketPosition.Short) ExitShort();
                    break;
            }
        }

        private void SetBracket(string signal, HermesCommand cmd)
        {
            if (cmd.StopPrice.HasValue)
                SetStopLoss(signal, CalculationMode.Price, cmd.StopPrice.Value, false);
            else if (cmd.StopTicks.HasValue && cmd.StopTicks.Value > 0)
                SetStopLoss(signal, CalculationMode.Ticks, cmd.StopTicks.Value, false);

            if (cmd.TargetPrice.HasValue)
                SetProfitTarget(signal, CalculationMode.Price, cmd.TargetPrice.Value);
            else if (cmd.TargetTicks.HasValue && cmd.TargetTicks.Value > 0)
                SetProfitTarget(signal, CalculationMode.Ticks, cmd.TargetTicks.Value);
        }

        // ---- helpers ---------------------------------------------------------
        private void GuardAccount()
        {
            // Best-effort guard. NinjaTrader's simulated (no-real-money) accounts are the
            // built-in simulator "Sim101" and the Market-Replay/Playback account "Playback101".
            // Both are safe to trade; only refuse a genuine live (brokerage) account unless
            // AllowLive was explicitly enabled.
            string name = Account != null ? Account.Name : "";
            bool looksSim = name != null &&
                (name.IndexOf("Sim", StringComparison.OrdinalIgnoreCase) >= 0
                 || name.IndexOf("Playback", StringComparison.OrdinalIgnoreCase) >= 0
                 || name.IndexOf("Replay", StringComparison.OrdinalIgnoreCase) >= 0);
            if (!looksSim && !AllowLive)
            {
                tradingDisabled = true;
                Print("Hermes SAFETY: account '" + name + "' is not a simulation/playback account and "
                      + "AllowLive is false. Trading DISABLED. Set AllowLive=true to override.");
            }
        }

        private int SignedPosition()
        {
            if (Position.MarketPosition == MarketPosition.Long) return Position.Quantity;
            if (Position.MarketPosition == MarketPosition.Short) return -Position.Quantity;
            return 0;
        }

        private TimeZoneInfo _barTz;

        private double EpochSeconds(DateTime t)
        {
            // Time[0] / fill times arrive in the CHART's display timezone with
            // Kind=Unspecified. Neither ToUniversalTime() (assumes MACHINE tz) nor
            // Globals.GeneralOptions.TimeZoneInfo (the GLOBAL option — Time[0] follows
            // the per-chart Time zone, verified 2026-06-11: both skewed bar ts +3h on a
            // PT box with ET charts) converts correctly. So the timezone is an explicit
            // property (BarTimeZoneId, default ET). If it's ever wrong, the bridge logs
            // "[warn] bar.ts is Xh off server UTC" within one bar — fix the property.
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            DateTime utc;
            try
            {
                if (_barTz == null)
                    _barTz = TimeZoneInfo.FindSystemTimeZoneById(BarTimeZoneId);
                utc = TimeZoneInfo.ConvertTimeToUtc(
                    DateTime.SpecifyKind(t, DateTimeKind.Unspecified), _barTz);
            }
            catch
            {
                utc = t.ToUniversalTime();   // bad tz id / DST edge: old behavior
            }
            return Math.Floor((utc - epoch).TotalSeconds);
        }

        private string BarsPeriodString()
        {
            return string.Format("{0}{1}", BarsPeriod.Value, BarsPeriod.BarsPeriodType);
        }

        private static string BarJson(double ts, double o, double h, double l, double c, double v)
        {
            var ci = CultureInfo.InvariantCulture;
            return string.Format(ci,
                "{{\"ts\":{0},\"open\":{1},\"high\":{2},\"low\":{3},\"close\":{4},\"volume\":{5},\"is_closed\":true}}",
                ts.ToString(ci), o.ToString(ci), h.ToString(ci), l.ToString(ci),
                c.ToString(ci), v.ToString(ci));
        }

        private static string Escape(string s)
        {
            return s == null ? "" : s.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        // Per-request timeout via CancellationToken — the ONLY timeout in play: Http.Timeout
        // is InfiniteTimeSpan since construction (mutating it later throws once the shared
        // static client has sent a request; left at its 100s default it would race — and
        // beat — any HttpTimeoutMs above 100000, aborting the POST while the bridge still
        // queues the command for the NEXT bar's poll: a stale entry).
        // Returns the response body (used for the need_history handshake; ignored elsewhere).
        private async Task<string> PostAsync(string path, string json)
        {
            using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
            using (var cts = new CancellationTokenSource(HttpTimeoutMs))
            using (var resp = await Http.PostAsync(BaseUrl + path, content, cts.Token))
                return await resp.Content.ReadAsStringAsync();
        }

        private async Task<string> GetAsync(string path)
        {
            using (var cts = new CancellationTokenSource(HttpTimeoutMs))
            using (var resp = await Http.GetAsync(BaseUrl + path, cts.Token))
                return await resp.Content.ReadAsStringAsync();
        }

        // ---- minimal JSON parsing for the small command response -------------
        private sealed class HermesCommand
        {
            public string Action;
            public int Qty;
            public int? StopTicks;
            public int? TargetTicks;
            public double? StopPrice;
            public double? TargetPrice;
        }

        private static HermesCommand ParseCommand(string resp)
        {
            if (string.IsNullOrEmpty(resp) || resp.Contains("\"command\":null")
                || resp.Contains("\"command\": null"))
                return null;
            string action = MatchString(resp, "action");
            if (string.IsNullOrEmpty(action)) return null;
            return new HermesCommand
            {
                Action = action,
                Qty = MatchInt(resp, "qty") ?? 1,
                StopTicks = MatchInt(resp, "stop_ticks"),
                TargetTicks = MatchInt(resp, "target_ticks"),
                StopPrice = MatchDouble(resp, "stop_price"),
                TargetPrice = MatchDouble(resp, "target_price"),
            };
        }

        private static string MatchString(string s, string key)
        {
            var m = Regex.Match(s, "\"" + key + "\"\\s*:\\s*\"([^\"]*)\"");
            return m.Success ? m.Groups[1].Value : null;
        }

        private static int? MatchInt(string s, string key)
        {
            var m = Regex.Match(s, "\"" + key + "\"\\s*:\\s*(-?\\d+)");
            return m.Success ? (int?)int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture) : null;
        }

        private static double? MatchDouble(string s, string key)
        {
            var m = Regex.Match(s, "\"" + key + "\"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)");
            return m.Success ? (double?)double.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture) : null;
        }

        // =====================================================================
        //  Background poll — ONLY fetch + parse + cache. No chart / UI / dispatcher
        //  calls here (AGENTS.md #17). Rendering reads the cached snapshot.
        // =====================================================================
        private async void Poll(object state)
        {
            if (Interlocked.Exchange(ref _pollBusy, 1) == 1)
                return;   // previous tick still in flight (slow bridge) — skip, don't stack
            try
            {
                using (var cts = new CancellationTokenSource(Math.Max(2, RefreshSeconds) * 1000))
                using (var resp = await Http.GetAsync(BaseUrl + "/panel.txt", cts.Token))
                {
                    if (!resp.IsSuccessStatusCode)
                    {
                        // A 404 here means the bridge predates /panel.txt — restart it.
                        _error = "HTTP " + (int)resp.StatusCode + " /panel.txt (old bridge? restart it)";
                        return;
                    }
                    string text = await resp.Content.ReadAsStringAsync();
                    if (!string.IsNullOrWhiteSpace(text))
                        _snap = ParsePanel(text);
                }
                using (var cts2 = new CancellationTokenSource(Math.Max(2, RefreshSeconds) * 1000))
                using (var resp2 = await Http.GetAsync(BaseUrl + "/levels.txt", cts2.Token))
                {
                    if (resp2.IsSuccessStatusCode)
                        _levelsText = await resp2.Content.ReadAsStringAsync() ?? "";
                }
                _error = null;
            }
            catch (Exception ex)
            {
                _error = ex.Message;   // keep the last good snapshot; the pill shows OFFLINE
            }
            finally
            {
                Interlocked.Exchange(ref _pollBusy, 0);
            }
        }

        // =====================================================================
        //  Snapshot model + parser (runs on the timer thread)
        // =====================================================================
        private sealed class Snapshot
        {
            public bool Ok;
            public string Instrument = "", Timeframe = "", Agent = "", Model = "", StrategyId = "";
            // Agent-authored strategy: source ("agent"/"custom"), the headline name/summary
            // (= the active setup), how the active setup was chosen ("declared" by the brain
            // vs "regime" fallback), and every authored setup as name|regime|summary|active.
            public string StrategySource = "", StrategyName = "", StrategySummary = "", StrategyActiveSource = "";
            public List<string[]> StrategyRows = new List<string[]>();  // name|regime|summary|active
            // Authoring telemetry (re-author observability): how many playbooks have installed
            // (count ticks => the strategy refreshed), how long ago, and why the latest fired.
            // Count < 0 means the bridge sent no telemetry (old bridge / never authored).
            public int StrategyAuthoredCount = -1, StrategyAuthoredBarsAgo = -1;
            public string StrategyAuthoredReason = "";
            // Planner / study health, so a re-author that never lands is visible on the card.
            public string PlannerStatus = "", PlannerError = "", SessionError = "";
            public double AgeS = double.NaN, LastClose = double.NaN;
            public int Position, Trades;
            public double AvgPrice = double.NaN, Realized, Unrealized;
            public bool Halted, GoalHit;
            public string HaltReason = "";
            public double GoalTarget = double.NaN, GoalLoss = double.NaN;
            public string LdTime = "", LdAction = "", LdOrder = "", LdRationale = "";
            public double LdConf = double.NaN, LdClose = double.NaN;
            public List<string[]> Rows = new List<string[]>();  // time|action|conf|close|order
            public bool HasPlan;
            public string PlanStatus = "", PlanDirection = "", PlanNote = "", PlanBarsLeft = "";
            public double PlanHigh = double.NaN, PlanLow = double.NaN;
            public DateTime PolledUtc;     // for client-side age extrapolation between polls
            public DateTime PolledLocal;   // footer "updated HH:mm:ss"
        }

        private static Snapshot ParsePanel(string text)
        {
            var p = new Snapshot { PolledUtc = DateTime.UtcNow, PolledLocal = DateTime.Now };
            foreach (var raw in text.Split('\n'))
            {
                string line = raw.TrimEnd('\r');
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string k = line.Substring(0, eq), v = line.Substring(eq + 1);
                switch (k)
                {
                    case "ok":           p.Ok = v == "1"; break;
                    case "instrument":   p.Instrument = v; break;
                    case "timeframe":    p.Timeframe = v; break;
                    case "agent":        p.Agent = v; break;
                    case "model":        p.Model = v; break;
                    case "strategy_id":      p.StrategyId = v; break;
                    case "strategy_source":         p.StrategySource = v; break;
                    case "strategy_name":           p.StrategyName = v; break;
                    case "strategy_summary":        p.StrategySummary = v; break;
                    case "strategy_active_source":  p.StrategyActiveSource = v; break;
                    case "strategy_row":
                        var sp = v.Split('|');               // name|regime|summary|active
                        if (sp.Length >= 4) p.StrategyRows.Add(sp);
                        break;
                    case "strategy_authored_count":     p.StrategyAuthoredCount = (int)NumOr(v, -1); break;
                    case "strategy_authored_bars_ago":  p.StrategyAuthoredBarsAgo = (int)NumOr(v, -1); break;
                    case "strategy_authored_reason":    p.StrategyAuthoredReason = v; break;
                    case "planner_status":              p.PlannerStatus = v; break;
                    case "planner_error":               p.PlannerError = v; break;
                    case "session_error":               p.SessionError = v; break;
                    case "age_s":        p.AgeS = Num(v); break;
                    case "last_close":   p.LastClose = Num(v); break;
                    case "position":     p.Position = (int)NumOr(v, 0); break;
                    case "avg_price":    p.AvgPrice = Num(v); break;
                    case "realized":     p.Realized = NumOr(v, 0); break;
                    case "unrealized":   p.Unrealized = NumOr(v, 0); break;
                    case "trades":       p.Trades = (int)NumOr(v, 0); break;
                    case "halted":       p.Halted = v == "1"; break;
                    case "halt_reason":  p.HaltReason = v; break;
                    case "goal_hit":     p.GoalHit = v == "1"; break;
                    case "goal_target":  p.GoalTarget = Num(v); break;
                    case "goal_loss":    p.GoalLoss = Num(v); break;
                    case "ld_time":      p.LdTime = v; break;
                    case "ld_action":    p.LdAction = v; break;
                    case "ld_conf":      p.LdConf = Num(v); break;
                    case "ld_close":     p.LdClose = Num(v); break;
                    case "ld_order":     p.LdOrder = v; break;
                    case "ld_rationale": p.LdRationale = v; break;
                    case "row":
                        var parts = v.Split('|');
                        if (parts.Length >= 5) p.Rows.Add(parts);
                        break;
                    case "plan_status":     p.HasPlan = true; p.PlanStatus = v; break;
                    case "plan_direction":  p.HasPlan = true; p.PlanDirection = v; break;
                    case "plan_entry_high": p.HasPlan = true; p.PlanHigh = Num(v); break;
                    case "plan_entry_low":  p.HasPlan = true; p.PlanLow = Num(v); break;
                    case "plan_bars_left":  p.HasPlan = true; p.PlanBarsLeft = v; break;
                    case "plan_note":       p.HasPlan = true; p.PlanNote = v; break;
                }
            }
            return p;
        }

        private static double Num(string v)
        {
            double d;
            if (double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
                return d;
            return double.NaN;
        }

        private static double NumOr(string v, double fallback)
        {
            double d = Num(v);
            return double.IsNaN(d) ? fallback : d;
        }

        private void DrawAgentLevels()
        {
            if (!ShowLevels)
            {
                RemoveLevelDrawings();
                return;
            }
            string lv = _levelsText;
            double r = GetLevel(lv, "swing_high");
            double s = GetLevel(lv, "swing_low");
            if (!double.IsNaN(r))
            {
                Draw.HorizontalLine(this, "HermesResistance", r, Brushes.Red);
                Draw.Text(this, "HermesResLabel", "R (agent)", 0, r, Brushes.Red);
            }
            else { RemoveDrawObject("HermesResistance"); RemoveDrawObject("HermesResLabel"); }
            if (!double.IsNaN(s))
            {
                Draw.HorizontalLine(this, "HermesSupport", s, Brushes.LimeGreen);
                Draw.Text(this, "HermesSupLabel", "S (agent)", 0, s, Brushes.LimeGreen);
            }
            else { RemoveDrawObject("HermesSupport"); RemoveDrawObject("HermesSupLabel"); }
        }

        // Remove all four agent S/R draw objects (resistance + support, line + label).
        // Idempotent: RemoveDrawObject is a no-op when the tag isn't on the chart.
        private void RemoveLevelDrawings()
        {
            RemoveDrawObject("HermesResistance"); RemoveDrawObject("HermesResLabel");
            RemoveDrawObject("HermesSupport");    RemoveDrawObject("HermesSupLabel");
        }

        private double GetLevel(string text, string key)
        {
            if (string.IsNullOrEmpty(text)) return double.NaN;
            foreach (var raw in text.Split('\n'))
            {
                string line = raw.Trim();
                int eq = line.IndexOf('=');
                if (eq > 0 && line.Substring(0, eq) == key)
                {
                    double v;
                    if (double.TryParse(line.Substring(eq + 1), NumberStyles.Float,
                        CultureInfo.InvariantCulture, out v))
                        return v;
                }
            }
            return double.NaN;
        }

        // =====================================================================
        //  Card dragging — Preview mouse events on the ChartControl. Hook/unhook
        //  hop to the UI thread ASYNC (a sync Invoke from a lifecycle path can
        //  ABBA-deadlock with F5 reload's Clone — dump-verified 2026-06-10).
        //  Handlers run on the UI thread: guard _terminated, try/catch all
        //  bodies (#21), touch only plain fields, never Draw.* (#22).
        // =====================================================================
        private void HookMouse(ChartControl cc)
        {
            try
            {
                if (cc == null || _mouseHooked || _hookQueued || _terminated) return;
                _hookQueued = true;   // one InvokeAsync in flight at most
                cc.Dispatcher.InvokeAsync(() =>
                {
                    try
                    {
                        if (_terminated || _mouseHooked) return;
                        cc.PreviewMouseLeftButtonDown += OnChartMouseDown;
                        cc.PreviewMouseMove += OnChartMouseMove;
                        cc.PreviewMouseLeftButtonUp += OnChartMouseUp;
                        _mouseChart = cc;
                        _mouseHooked = true;
                        Print("[HermesDashboard] card drag enabled (mouse hook installed)");
                    }
                    catch (Exception ex) { _hookQueued = false; MouseError(ex); }   // retry next frame
                });
            }
            catch (Exception ex) { _hookQueued = false; MouseError(ex); }
        }

        private void UnhookMouse()
        {
            try
            {
                var cc = _mouseChart;
                if (cc == null) return;
                if (cc.Dispatcher.CheckAccess()) UnhookMouse(cc);
                else cc.Dispatcher.InvokeAsync(() => UnhookMouse(cc));
            }
            catch { }   // Terminated path — must never throw
        }

        private void UnhookMouse(ChartControl cc)
        {
            try
            {
                cc.PreviewMouseLeftButtonDown -= OnChartMouseDown;
                cc.PreviewMouseMove -= OnChartMouseMove;
                cc.PreviewMouseLeftButtonUp -= OnChartMouseUp;
            }
            catch { }
            _mouseHooked = false;
            _mouseChart = null;
        }

        private void OnChartMouseDown(object sender, MouseButtonEventArgs e)
        {
            try
            {
                if (_terminated || !_hasCard) return;
                var cc = _mouseChart;
                if (cc == null) return;
                float mx = MouseX(cc, e), my = MouseY(cc, e);
                if (!HitCard(mx, my)) return;
                if (HitFold(mx, my))
                {
                    if (e.ClickCount == 1)   // swallow the 2nd click of a fast double
                    {
                        CardFolded = !CardFolded;
                        _rebuildAsap = true;
                        cc.InvalidateVisual();
                    }
                    _dragging = false;
                    e.Handled = true;
                    return;
                }
                if (e.ClickCount == 2)   // double-click: snap back to the default corner
                {
                    CardOffsetX = 0f; CardOffsetY = 0f;
                    _dragging = false;
                    e.Handled = true;
                    cc.InvalidateVisual();
                    return;
                }
                _dragging = true;
                _dragGrabX = mx - _offX;
                _dragGrabY = my - _offY;
                e.Handled = true;   // keep the chart from panning/crosshairing inside the card
            }
            catch (Exception ex) { MouseError(ex); }
        }

        private void OnChartMouseMove(object sender, MouseEventArgs e)
        {
            try
            {
                if (_terminated || !_dragging) return;
                if (e.LeftButton != MouseButtonState.Pressed)
                {
                    _dragging = false;   // the Up happened off-chart — self-heal
                    return;
                }
                var cc = _mouseChart;
                if (cc == null) return;
                CardOffsetX = MouseX(cc, e) - _dragGrabX;
                CardOffsetY = MouseY(cc, e) - _dragGrabY;
                e.Handled = true;
                if ((DateTime.UtcNow - _lastDragRefreshUtc).TotalMilliseconds >= 33)
                {
                    _lastDragRefreshUtc = DateTime.UtcNow;
                    cc.InvalidateVisual();   // UI thread — schedules the next OnRender
                }
            }
            catch (Exception ex) { MouseError(ex); }
        }

        private void OnChartMouseUp(object sender, MouseButtonEventArgs e)
        {
            try
            {
                if (!_dragging) return;
                _dragging = false;
                e.Handled = true;
                var cc = _mouseChart;
                if (cc != null) cc.InvalidateVisual();   // final paint past the 33 ms gate
            }
            catch (Exception ex) { MouseError(ex); }
        }

        // WPF mouse coords are DIPs; ChartPanel/SharpDX are device px — convert
        // via NT8's extensions so hit-testing works at any DPI scaling.
        private static float MouseX(ChartControl cc, MouseEventArgs e)
        {
            return (float)e.GetPosition(cc).X.ConvertToHorizontalPixels(cc.PresentationSource);
        }

        private static float MouseY(ChartControl cc, MouseEventArgs e)
        {
            return (float)e.GetPosition(cc).Y.ConvertToVerticalPixels(cc.PresentationSource);
        }

        private bool HitCard(float x, float y)
        {
            return _hasCard
                && x >= _cardX + _offX && x <= _cardX + _offX + _cardW
                && y >= _cardY + _offY && y <= _cardY + _offY + _cardH;
        }

        private bool HitFold(float x, float y)
        {
            return _foldX2 > _foldX1
                && x >= _foldX1 + _offX && x <= _foldX2 + _offX
                && y >= _foldY1 + _offY && y <= _foldY2 + _offY;
        }

        // Clamp the dragged offset so the card can't leave the panel (render thread).
        private void ApplyCardOffset()
        {
            if (!_hasCard || ChartPanel == null)
            {
                _offX = CardOffsetX; _offY = CardOffsetY;
                return;
            }
            float x = _cardX + CardOffsetX, y = _cardY + CardOffsetY;
            x = Math.Max(ChartPanel.X, Math.Min(x, ChartPanel.X + ChartPanel.W - _cardW));
            y = Math.Max(ChartPanel.Y, Math.Min(y, ChartPanel.Y + ChartPanel.H - _cardH));
            _offX = x - _cardX;
            _offY = y - _cardY;
        }

        private void MouseError(Exception ex)
        {
            if (_mouseErrorCount < 5)
            {
                _mouseErrorCount++;
                Print("[HermesDashboard] mouse error #" + _mouseErrorCount + ": " + ex.Message);
            }
        }

        // =====================================================================
        //  Card rendering (SharpDX only — no Draw.* in OnRender, AGENTS.md #13)
        // =====================================================================
        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            try { base.OnRender(chartControl, chartScale); } catch { }
            if (!_mouseHooked) HookMouse(chartControl);   // retry until installed (cheap: flag-guarded)
            if (_terminated || RenderTarget == null || ChartPanel == null) return;

            try
            {
                // §5b self-heal: if NT8 swapped the target without firing
                // OnRenderTargetChanged, drawing with the old brushes faults the UI
                // thread (the "chart-then-NT8" lockup). Re-init against the live RT.
                if (!_dxInitialized || !object.ReferenceEquals(RenderTarget, _lastSeenRT))
                    InitDeviceResources();
                if (!_dxInitialized)
                    return;   // init failed (transient RT state) — skip the frame, retry next pass

                // Rebuild the display list at most ~1×/s (age counter tick) or when a new
                // snapshot/error arrives. OnRender itself can fire per tick; building
                // DirectWrite layouts that often is the GC/dispatcher pressure #17 warns about.
                Snapshot p = _snap;
                string err = _error;
                if (p != _builtFrom || err != _builtError || _rebuildAsap
                    || (DateTime.UtcNow - _lastBuildUtc).TotalMilliseconds >= 1000)
                {
                    BuildOps(p, err);
                }

                ApplyCardOffset();   // clamp the dragged offset to the panel each frame

                var oldAa = RenderTarget.AntialiasMode;
                var oldTxt = RenderTarget.TextAntialiasMode;
                var oldXf = RenderTarget.Transform;
                RenderTarget.AntialiasMode = D2D.AntialiasMode.PerPrimitive;
                RenderTarget.TextAntialiasMode = D2D.TextAntialiasMode.Grayscale;
                if (_offX != 0f || _offY != 0f)   // drag = one transform, zero rebuilds
                    RenderTarget.Transform = SharpDX.Matrix3x2.Multiply(
                        SharpDX.Matrix3x2.Translation(_offX, _offY), oldXf);
                try { ReplayOps(); }
                finally
                {
                    RenderTarget.Transform = oldXf;
                    RenderTarget.AntialiasMode = oldAa;
                    RenderTarget.TextAntialiasMode = oldTxt;
                }
            }
            catch (Exception ex)
            {
                // §5d: bounded — never let a render bug take down the chart or count
                // toward MaxRestarts.
                if (_renderErrorCount < 5)
                {
                    _renderErrorCount++;
                    Print("[HermesDashboard] OnRender error #" + _renderErrorCount + ": " + ex.Message);
                }
            }
        }

        // §5b: same-target skip + EAGER re-init. Never lazy-init brushes in the draw
        // path; never leave a frame with a half-valid palette.
        public override void OnRenderTargetChanged()
        {
            try { base.OnRenderTargetChanged(); } catch { }
            try
            {
                // No-op when NT8 fires the callback without actually swapping the
                // target (it does, e.g. around playback/state transitions).
                if (RenderTarget != null && _dxInitialized
                    && object.ReferenceEquals(RenderTarget, _lastSeenRT))
                    return;

                DisposeBrushes();                       // sets _dxInitialized = false
                if (RenderTarget == null || _terminated)
                    return;
                InitDeviceResources();                  // eager — never let a frame see null brushes
            }
            catch (Exception ex)
            {
                if (_renderErrorCount < 5)
                {
                    _renderErrorCount++;
                    Print("[HermesDashboard] OnRenderTargetChanged error #" + _renderErrorCount + ": " + ex.Message);
                }
            }
        }

        // Create the full palette against the CURRENT RenderTarget. On partial failure
        // everything is torn down so the next attempt starts clean (§5b companion rule:
        // a half-initialized palette + same-target skip would stay dead forever).
        private void InitDeviceResources()
        {
            DisposeBrushes();
            var rt = RenderTarget;
            if (rt == null) return;
            try
            {
                foreach (uint argb in Palette)
                {
                    var c = new SharpDX.Color4(
                        ((argb >> 16) & 0xFF) / 255f,
                        ((argb >> 8) & 0xFF) / 255f,
                        (argb & 0xFF) / 255f,
                        ((argb >> 24) & 0xFF) / 255f);
                    _brushes[argb] = new D2D.SolidColorBrush(rt, c);
                }
                _lastSeenRT = rt;
                _dxInitialized = true;
            }
            catch
            {
                DisposeBrushes();   // partial init → tear down so the next call retries cleanly
            }
        }

        private enum OpKind { FillRect, StrokeRect, Line, FillEllipse, Text }

        private struct DrawOp
        {
            public OpKind Kind;
            // Geometry by kind: Fill/StrokeRect = LTRB bounds · Line = x1,y1 -> x2,y2 ·
            // FillEllipse = center x1,y1 + radii x2,y2 · Text = origin x1,y1.
            public float X1, Y1, X2, Y2;
            public float Radius;         // rounded-corner radius (0 = square)
            public float Thick;          // stroke width for lines / borders
            public uint Color;
            public DW.TextLayout Layout; // Text ops own their layout; disposed on rebuild
        }

        // SharpDX 2.6.3's RectangleF ctor is (x, y, width, height) — build from LTRB.
        private static DXRect Ltrb(float l, float t, float r, float b)
        {
            return new DXRect(l, t, r - l, b - t);
        }

        private void ReplayOps()
        {
            if (_hasCard)
            {
                var bg = BrushFor(ColCardBg);
                var edge = BrushFor(ColCardEdge);
                var rr = new D2D.RoundedRectangle
                {
                    Rect = new DXRect(_cardX, _cardY, _cardW, _cardH),
                    RadiusX = 10f * _s,
                    RadiusY = 10f * _s,
                };
                if (bg != null) RenderTarget.FillRoundedRectangle(rr, bg);
                if (edge != null) RenderTarget.DrawRoundedRectangle(rr, edge, 1f);
            }
            foreach (var op in _ops)
            {
                var b = BrushFor(op.Color);
                if (b == null) continue;
                switch (op.Kind)
                {
                    case OpKind.FillRect:
                        if (op.Radius > 0f)
                            RenderTarget.FillRoundedRectangle(new D2D.RoundedRectangle
                            {
                                Rect = Ltrb(op.X1, op.Y1, op.X2, op.Y2),
                                RadiusX = op.Radius,
                                RadiusY = op.Radius,
                            }, b);
                        else
                            RenderTarget.FillRectangle(Ltrb(op.X1, op.Y1, op.X2, op.Y2), b);
                        break;
                    case OpKind.StrokeRect:
                        if (op.Radius > 0f)
                            RenderTarget.DrawRoundedRectangle(new D2D.RoundedRectangle
                            {
                                Rect = Ltrb(op.X1, op.Y1, op.X2, op.Y2),
                                RadiusX = op.Radius,
                                RadiusY = op.Radius,
                            }, b, op.Thick);
                        else
                            RenderTarget.DrawRectangle(Ltrb(op.X1, op.Y1, op.X2, op.Y2), b, op.Thick);
                        break;
                    case OpKind.Line:
                        RenderTarget.DrawLine(new DXVec(op.X1, op.Y1), new DXVec(op.X2, op.Y2),
                            b, op.Thick);
                        break;
                    case OpKind.FillEllipse:
                        RenderTarget.FillEllipse(
                            new D2D.Ellipse(new DXVec(op.X1, op.Y1), op.X2, op.Y2), b);
                        break;
                    case OpKind.Text:
                        if (op.Layout != null && !op.Layout.IsDisposed)
                            RenderTarget.DrawTextLayout(new DXVec(op.X1, op.Y1), op.Layout, b);
                        break;
                }
            }
        }

        // ---- display-list construction ------------------------------------------
        private void BuildOps(Snapshot p, string err)
        {
            _builtFrom = p;
            _builtError = err;
            _lastBuildUtc = DateTime.UtcNow;
            _rebuildAsap = false;
            _foldX1 = _foldY1 = _foldX2 = _foldY2 = 0f;   // no fold toggle until placed
            DisposeOps();
            EnsureFormats();

            float s = Math.Max(0.5f, FontSize / 12f);
            _s = s;
            float x0 = ChartPanel.X + 12f, y0 = ChartPanel.Y + 12f;
            float w = 420f * s, pad = 14f * s;
            float xL = x0 + pad, xR = x0 + w - pad, innerW = xR - xL;
            float y = y0 + pad;

            if (p == null)   // never reached the bridge yet
            {
                AddText("HERMES", _fHead, xL, y, ColText);
                AddPill(err == null ? "CONNECTING" : "OFFLINE",
                        err == null ? ColAmberBg : ColRedBg,
                        err == null ? ColAmber : ColRed, xR, y + 9f * s);
                y += 26f * s;
                AddText("bridge " + BridgeHost + ":" + BridgePort, _fSub, xL, y, ColDim);
                y += 18f * s;
                SetCard(x0, y0, w, y + pad - 4f * s - y0);
                return;
            }

            // ---- status pill ----------------------------------------------------
            double age = double.IsNaN(p.AgeS)
                ? double.NaN
                : p.AgeS + (DateTime.UtcNow - p.PolledUtc).TotalSeconds;
            int staleAfter = TimeframeSeconds(p.Timeframe) * 2 + 30;
            string pill; uint pillBg, pillFg;
            if (err != null) { pill = "OFFLINE"; pillBg = ColRedBg; pillFg = ColRed; }
            else if (!p.Ok) { pill = "NO DATA"; pillBg = ColAmberBg; pillFg = ColAmber; }
            else if (double.IsNaN(age)) { pill = "WAITING"; pillBg = ColAmberBg; pillFg = ColAmber; }
            else if (age > staleAfter) { pill = "STALE " + FmtAge(age); pillBg = ColAmberBg; pillFg = ColAmber; }
            else { pill = "LIVE " + FmtAge(age); pillBg = ColGreenBg; pillFg = ColGreen; }

            // ---- header ----------------------------------------------------------
            AddEllipse(xL + 4f * s, y + 9f * s, 4f * s, pillFg);
            var hHead = AddText("HERMES", _fHead, xL + 14f * s, y, ColText);
            AddText(Join(" · ", p.Instrument, p.Timeframe), _fSub,
                xL + 14f * s + hHead.Metrics.WidthIncludingTrailingWhitespace + 8f * s,
                y + 3.5f * s, ColMuted);
            AddPill(pill, pillBg, pillFg, xR - 18f * s, y + 9f * s);
            // fold toggle (▾ open / ▸ folded) — generous hit box, tested on mouse-down
            AddTextRight(CardFolded ? "▸" : "▾", _fBody, xR + 2f * s, y + 3f * s, ColMuted);
            _foldX1 = xR - 14f * s; _foldY1 = y - 2f * s;
            _foldX2 = x0 + w;       _foldY2 = y + 20f * s;
            y += 24f * s;

            if (CardFolded)   // compact card: header + position/P&L strip
            {
                double tot = p.Realized + p.Unrealized;
                AddText(PosText(p), _fBodyB, xL, y, PosColor(p));
                AddTextRight(FmtMoney(tot) + " today", _fSub, xR, y + 1.5f * s, MoneyColor(tot));
                y += 20f * s;
                SetCard(x0, y0, w, y + pad - 5f * s - y0);
                return;
            }

            AddText(Join(" · ", p.Agent, p.Model, p.StrategyId), _fSub, xL, y, ColDim);
            y += 20f * s;

            // ---- agent-authored strategies (every setup; the live-regime one highlighted) ---
            AddText("STRATEGY", _fLabel, xL, y, ColDim);
            y += 17f * s;
            if (p.StrategyRows.Count > 0)
            {
                foreach (var sr in p.StrategyRows)   // name|regime|summary|active
                {
                    string nm = sr[0], regime = sr[1], summary = sr[2];
                    bool active = sr.Length > 3 && sr[3] == "1";
                    string head = (active ? "▸ " : "· ") + nm
                        + (regime.Length > 0 ? "  (" + regime + ")" : "");
                    AddText(head, _fBodyB, xL, y, active ? ColGreen : ColText);
                    if (active)   // "TRADING" when the brain named this setup; else regime match
                        AddTextRight(p.StrategyActiveSource == "declared" ? "TRADING" : "ACTIVE",
                            _fLabel, xR, y + 1f * s, ColGreen);
                    y += 16f * s;
                    if (summary.Length > 0)
                    {
                        AddText(summary, _fRat, xL + 12f * s, y, ColMuted, innerW - 12f * s);
                        y += 15f * s;   // one ellipsized line per setup
                    }
                }
            }
            else
            {
                string fb = p.StrategySource == "agent" ? "authoring…"
                          : p.StrategySource == "custom" ? "custom playbooks" : "—";
                AddText(fb, _fBody, xL, y, ColMuted);
                y += 16f * s;
            }
            // Authoring telemetry + study health: watch the count tick to confirm the playbook
            // is refreshing; an analyzing_session status or a red session error shows a
            // re-author in flight / failed (previously the card had no signal for either).
            if (p.StrategyAuthoredCount >= 0)
            {
                string ago = p.StrategyAuthoredBarsAgo >= 0
                    ? p.StrategyAuthoredBarsAgo + "b ago" : "just now";
                AddText("authored " + p.StrategyAuthoredCount + "x · " + ago, _fSub, xL, y, ColDim);
                if (p.PlannerStatus.Length > 0)
                    AddTextRight(p.PlannerStatus, _fSub, xR, y,
                        p.PlannerStatus == "analyzing_session" ? ColAmber : ColDim);
                y += 14f * s;
                string why = p.SessionError.Length > 0 ? p.SessionError
                           : p.StrategyAuthoredReason;
                if (why.Length > 0)
                {
                    AddText(why, _fRat, xL + 12f * s, y,
                        p.SessionError.Length > 0 ? ColRed : ColMuted, innerW - 12f * s);
                    y += 14f * s;
                }
            }
            Divider(xL, xR, ref y);

            // ---- position + last price ------------------------------------------
            AddText(PosText(p), _fBig, xL, y, PosColor(p));
            AddTextRight("last " + FmtPrice(p.LastClose), _fSub, xR, y + 5f * s, ColMuted);
            y += 26f * s;

            // ---- stat boxes ------------------------------------------------------
            double total = p.Realized + p.Unrealized;
            string[] labels = { "REALIZED", "UNREALIZED", "TOTAL", "TRADES" };
            string[] vals = { FmtMoney(p.Realized), FmtMoney(p.Unrealized), FmtMoney(total),
                              p.Trades.ToString(CultureInfo.InvariantCulture) };
            uint[] vcols = { MoneyColor(p.Realized), MoneyColor(p.Unrealized), MoneyColor(total), ColText };
            float gap = 8f * s, bw = (innerW - gap * 3f) / 4f, bh = 38f * s;
            for (int i = 0; i < 4; i++)
            {
                float bx = xL + i * (bw + gap);
                AddRect(bx, y, bx + bw, y + bh, 6f * s, ColBoxBg, ColBoxEdge);
                AddText(labels[i], _fLabel, bx + 7f * s, y + 5f * s, ColDim);
                AddText(vals[i], _fBodyB, bx + 7f * s, y + 17f * s, vcols[i]);
            }
            y += bh + 12f * s;

            // ---- daily goal ------------------------------------------------------
            AddText("DAILY GOAL", _fLabel, xL, y, ColDim);
            string goalRight; uint goalRightCol;
            if (p.Halted)
            {
                goalRight = "HALTED" + (p.HaltReason.Length > 0 ? ": " + p.HaltReason : "");
                goalRightCol = ColAmber;
            }
            else if (p.GoalHit) { goalRight = "GOAL HIT"; goalRightCol = ColGreen; }
            else
            {
                goalRight = "stop -" + FmtPrice(p.GoalLoss) + "  target +" + FmtPrice(p.GoalTarget);
                goalRightCol = ColMuted;
            }
            AddTextRight(goalRight, _fSub, xR, y - 1.5f * s, goalRightCol);
            y += 15f * s;
            AddRect(xL, y, xR, y + 4f * s, 2f * s, ColTrack, 0);
            double span = p.GoalLoss + p.GoalTarget;
            double frac = (double.IsNaN(span) || span <= 0) ? 0.5
                : Math.Min(1.0, Math.Max(0.0, (total + p.GoalLoss) / span));
            float mx = xL + (float)(innerW * frac);
            AddRect(mx - 1.5f * s, y - 3f * s, mx + 1.5f * s, y + 7f * s, 1.5f * s,
                total < 0 ? ColRed : ColGreen, 0);
            y += 14f * s;
            Divider(xL, xR, ref y);

            // ---- armed plan (forward-compat with the analysis/execution split) ---
            AddText("ARMED PLAN", _fLabel, xL, y, ColDim);
            if (p.PlanStatus.Length > 0)
            {
                string st = p.PlanStatus.ToUpperInvariant();
                uint bg = st == "ARMED" ? ColGreenBg : st == "ANALYZING" ? ColBlueBg : ColBoxBg;
                uint fg = st == "ARMED" ? ColGreen : st == "ANALYZING" ? ColBlue : ColMuted;
                AddPill(st, bg, fg, xR, y + 5f * s);
            }
            y += 17f * s;
            string dir = p.PlanDirection.Length > 0 ? p.PlanDirection.ToUpperInvariant() : "NEUTRAL";
            uint dirCol = dir.Contains("LONG") ? ColGreen : dir.Contains("SHORT") ? ColRed : ColText;
            var hDir = AddText("◆ " + dir, _fBodyB, xL, y, dirCol);
            string note = p.PlanNote.Length > 0 ? p.PlanNote
                : p.HasPlan ? "seeking entry" : "per-bar mode";
            AddText(note, _fSub,
                xL + hDir.Metrics.WidthIncludingTrailingWhitespace + 8f * s, y + 1f * s, ColMuted);
            y += 18f * s;
            if (!double.IsNaN(p.PlanLow) && !double.IsNaN(p.PlanHigh))
            {
                string rng = FmtPrice(p.PlanLow) + " – " + FmtPrice(p.PlanHigh)
                    + (p.PlanBarsLeft.Length > 0 ? "  ·  " + p.PlanBarsLeft + " bars left" : "");
                AddText(rng, _fBody, xL, y, ColText);
            }
            else
                AddText("none armed", _fBody, xL, y, ColMuted);
            y += 20f * s;
            Divider(xL, xR, ref y);

            // ---- last decision ---------------------------------------------------
            AddText("LAST DECISION", _fLabel, xL, y, ColDim);
            AddTextRight(p.LdTime, _fSub, xR, y - 1.5f * s, ColMuted);
            y += 17f * s;
            string act = p.LdAction.Length > 0 ? DisplayAction(p.LdAction) : "—";
            var hAct = AddText(act, _fBig, xL, y, ActionColor(p.LdAction));
            float ax = xL + hAct.Metrics.WidthIncludingTrailingWhitespace + 10f * s;
            var hConf = AddText("conf " + FmtConf(p.LdConf), _fSub, ax, y + 5f * s, ColMuted);
            AddText("@ " + FmtPrice(p.LdClose), _fBodyB,
                ax + hConf.Metrics.WidthIncludingTrailingWhitespace + 10f * s, y + 4f * s, ColText);
            if (p.LdOrder.Length > 0)   // an order actually left the bridge for this decision
                AddTextRight("→ " + DisplayAction(p.LdOrder), _fBodyB, xR, y + 4f * s,
                    ActionColor(p.LdOrder));
            y += 23f * s;
            if (p.LdRationale.Length > 0)
            {
                AddText(p.LdRationale, _fRat, xL, y, ColMuted, innerW);
                y += 17f * s;
            }
            Divider(xL, xR, ref y);

            // ---- recent decisions table -----------------------------------------
            // Columns are anchored to innerW (fractions, not fixed px) so the ORDER
            // column — drawn left-aligned and unbounded — always lands inside the card
            // and can't run off the right edge if the card width changes again.
            float cTime = xL, cAct = xL + innerW * 0.17f, cConf = xL + innerW * 0.44f,
                  cClose = xL + innerW * 0.57f, cOrd = xL + innerW * 0.79f;
            AddText("TIME", _fLabel, cTime, y, ColDim);
            AddText("ACTION", _fLabel, cAct, y, ColDim);
            AddText("CONF", _fLabel, cConf, y, ColDim);
            AddText("CLOSE", _fLabel, cClose, y, ColDim);
            AddText("ORDER", _fLabel, cOrd, y, ColDim);
            y += 15f * s;
            int n = Math.Min(p.Rows.Count, Math.Max(1, Math.Min(8, RecentRows)));
            if (p.Rows.Count == 0)
            {
                AddText("no decisions yet", _fSub, xL, y, ColDim);
                y += 15f * s;
            }
            for (int i = 0; i < n; i++)
            {
                var r = p.Rows[i];
                string ord = r[4].Length > 0 ? DisplayAction(r[4]) : "—";
                AddText(r[0], _fSub, cTime, y, ColMuted);
                AddText(DisplayAction(r[1]), _fSub, cAct, y, ActionColor(r[1]));
                AddText(r[2], _fSub, cConf, y, ColMuted);
                AddText(r[3], _fSub, cClose, y, ColText);
                AddText(ord, _fSub, cOrd, y, r[4].Length > 0 ? ActionColor(r[4]) : ColDim);
                y += 15f * s;
            }
            y += 3f * s;
            Divider(xL, xR, ref y);

            // ---- footer ----------------------------------------------------------
            AddText("bridge " + BridgeHost + ":" + BridgePort, _fSub, xL, y, ColDim);
            AddTextRight(err != null ? "retrying…" : "updated " + p.PolledLocal.ToString("HH:mm:ss"),
                _fSub, xR, y, ColDim);
            y += 15f * s;

            SetCard(x0, y0, w, y + pad - 5f * s - y0);
        }

        private void SetCard(float x, float y, float w, float h)
        {
            _cardX = x; _cardY = y; _cardW = w; _cardH = h;
            _hasCard = true;
        }

        // ---- small build helpers (render thread only) ----------------------------
        private DW.TextLayout AddText(string text, DW.TextFormat fmt, float x, float y,
            uint color, float maxW = 4000f)
        {
            var tl = new DW.TextLayout(NinjaTrader.Core.Globals.DirectWriteFactory,
                text ?? "", fmt, maxW, 200f);
            _ops.Add(new DrawOp
            {
                Kind = OpKind.Text,
                Layout = tl,
                X1 = x, Y1 = y,
                Color = color,
            });
            return tl;
        }

        private void AddTextRight(string text, DW.TextFormat fmt, float right, float y, uint color)
        {
            var tl = new DW.TextLayout(NinjaTrader.Core.Globals.DirectWriteFactory,
                text ?? "", fmt, 4000f, 200f);
            _ops.Add(new DrawOp
            {
                Kind = OpKind.Text,
                Layout = tl,
                X1 = right - tl.Metrics.Width, Y1 = y,
                Color = color,
            });
        }

        private void AddPill(string text, uint bg, uint fg, float rightEdge, float cy)
        {
            var tl = new DW.TextLayout(NinjaTrader.Core.Globals.DirectWriteFactory,
                text ?? "", _fLabel, 4000f, 200f);
            float tw = tl.Metrics.Width, th = tl.Metrics.Height;
            float padX = 8f * _s, padY = 3f * _s;
            float left = rightEdge - tw - padX * 2f;
            _ops.Add(new DrawOp
            {
                Kind = OpKind.FillRect,
                X1 = left, Y1 = cy - th / 2f - padY, X2 = rightEdge, Y2 = cy + th / 2f + padY,
                Radius = th / 2f + padY,
                Color = bg,
            });
            _ops.Add(new DrawOp
            {
                Kind = OpKind.Text,
                Layout = tl,
                X1 = left + padX, Y1 = cy - th / 2f,
                Color = fg,
            });
        }

        private void AddRect(float l, float t, float r, float b, float radius, uint fill, uint stroke)
        {
            _ops.Add(new DrawOp
            {
                Kind = OpKind.FillRect,
                X1 = l, Y1 = t, X2 = r, Y2 = b,
                Radius = radius,
                Color = fill,
            });
            if (stroke != 0)
                _ops.Add(new DrawOp
                {
                    Kind = OpKind.StrokeRect,
                    X1 = l, Y1 = t, X2 = r, Y2 = b,
                    Radius = radius,
                    Thick = 1f,
                    Color = stroke,
                });
        }

        private void AddEllipse(float cx, float cy, float radius, uint color)
        {
            _ops.Add(new DrawOp
            {
                Kind = OpKind.FillEllipse,
                X1 = cx, Y1 = cy, X2 = radius, Y2 = radius,
                Color = color,
            });
        }

        private void Divider(float xL, float xR, ref float y)
        {
            _ops.Add(new DrawOp
            {
                Kind = OpKind.Line,
                X1 = xL, Y1 = y, X2 = xR, Y2 = y,
                Thick = 1f,
                Color = ColDivider,
            });
            y += 11f * _s;
        }

        // ---- formatting helpers ---------------------------------------------------
        private static string Join(string sep, params string[] parts)
        {
            var keep = new List<string>();
            foreach (var part in parts)
                if (!string.IsNullOrEmpty(part)) keep.Add(part);
            return string.Join(sep, keep);
        }

        private static string FmtPrice(double v)
        {
            return double.IsNaN(v) ? "—" : v.ToString("0.##", CultureInfo.InvariantCulture);
        }

        private static string FmtMoney(double v)
        {
            return double.IsNaN(v) ? "—" : v.ToString("+0.00;-0.00;+0.00", CultureInfo.InvariantCulture);
        }

        private static string FmtConf(double v)
        {
            return double.IsNaN(v) ? "—" : v.ToString("0.00", CultureInfo.InvariantCulture);
        }

        private static string FmtAge(double seconds)
        {
            if (seconds < 0) seconds = 0;
            if (seconds < 100) return ((int)seconds) + "s";
            return ((int)(seconds / 60)) + "m";
        }

        private static int TimeframeSeconds(string tf)
        {
            if (string.IsNullOrEmpty(tf)) return 120;
            int i = 0;
            while (i < tf.Length && char.IsDigit(tf[i])) i++;
            int n;
            if (i == 0 || !int.TryParse(tf.Substring(0, i), out n)) return 120;
            char unit = i < tf.Length ? char.ToLowerInvariant(tf[i]) : 'm';
            if (unit == 's') return n;
            if (unit == 'h') return n * 3600;
            return n * 60;
        }

        private static string DisplayAction(string a)
        {
            return (a ?? "").Replace("ENTER_", "");
        }

        private uint ActionColor(string a)
        {
            string u = (a ?? "").ToUpperInvariant();
            if (u.Contains("LONG") || u.Contains("BUY")) return ColGreen;
            if (u.Contains("SHORT") || u.Contains("SELL")) return ColRed;
            if (u.Contains("FLAT")) return ColAmber;
            if (u.Contains("WAIT")) return ColMuted;
            if (u.Contains("HOLD")) return ColBlue;
            return ColText;
        }

        private static string PosText(Snapshot p)
        {
            if (p.Position > 0) return "LONG " + p.Position + " @ " + FmtPrice(p.AvgPrice);
            if (p.Position < 0) return "SHORT " + (-p.Position) + " @ " + FmtPrice(p.AvgPrice);
            return "FLAT";
        }

        private static uint PosColor(Snapshot p)
        {
            return p.Position > 0 ? ColGreen : p.Position < 0 ? ColRed : ColText;
        }

        private uint MoneyColor(double v)
        {
            if (double.IsNaN(v)) return ColText;
            if (v > 0.005) return ColGreen;
            if (v < -0.005) return ColRed;
            return ColText;
        }

        // ---- resource lifecycle -----------------------------------------------------
        private void EnsureFormats()
        {
            if (_fLabel != null) return;
            float s = Math.Max(0.5f, FontSize / 12f);
            var fac = NinjaTrader.Core.Globals.DirectWriteFactory;
            _fLabel = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.SemiBold, DW.FontStyle.Normal, 9f * s);
            _fSub   = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Normal,   DW.FontStyle.Normal, 11f * s);
            _fBody  = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Normal,   DW.FontStyle.Normal, 12f * s);
            _fBodyB = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Bold,     DW.FontStyle.Normal, 12f * s);
            _fHead  = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Bold,     DW.FontStyle.Normal, 15f * s);
            _fBig   = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Bold,     DW.FontStyle.Normal, 16f * s);
            _fRat   = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Normal,   DW.FontStyle.Normal, 10.5f * s);
            _fRat.WordWrapping = DW.WordWrapping.NoWrap;
            _ratEllipsis = new DW.EllipsisTrimming(fac, _fRat);
            _fRat.SetTrimming(new DW.Trimming { Granularity = DW.TrimmingGranularity.Character },
                _ratEllipsis);
        }

        private void DisposeOps()
        {
            foreach (var op in _ops)
                if (op.Layout != null && !op.Layout.IsDisposed) op.Layout.Dispose();
            _ops.Clear();
            _hasCard = false;
        }

        private void DisposeBrushes()
        {
            _dxInitialized = false;
            _lastSeenRT = null;
            foreach (var b in _brushes.Values)
                if (b != null && !b.IsDisposed) b.Dispose();
            _brushes.Clear();
        }

        private void DisposeFormats()
        {
            if (_ratEllipsis != null && !_ratEllipsis.IsDisposed) _ratEllipsis.Dispose();
            _ratEllipsis = null;
            var formats = new[] { _fLabel, _fSub, _fBody, _fBodyB, _fHead, _fBig, _fRat };
            foreach (var f in formats)
                if (f != null && !f.IsDisposed) f.Dispose();
            _fLabel = _fSub = _fBody = _fBodyB = _fHead = _fBig = _fRat = null;
        }

        // Lookup ONLY — the palette is created eagerly in InitDeviceResources (§5b).
        private D2D.SolidColorBrush BrushFor(uint argb)
        {
            D2D.SolidColorBrush b;
            if (_brushes.TryGetValue(argb, out b) && b != null && !b.IsDisposed)
                return b;
            return null;
        }
    }
}
