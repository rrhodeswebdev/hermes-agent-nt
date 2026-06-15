#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.Net.Http;
using System.Reflection;
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
//  HermesBridgeStrategy  (strategy + on-chart dashboard button)
// -----------------------------------------------------------------------------
//  Streams chart data to the Python "hermes-bridge", executes the risk-approved
//  orders it returns (Sim by default), and shows a small on-chart
//  "HERMES — DASHBOARD" button. Clicking the button
//  opens the bridge's full HTML dashboard (http://<host>:<port>/) inside a
//  NinjaTrader window via an embedded WebView2 (Chromium); if WebView2 isn't
//  available it falls back to the system default browser. There is no longer an
//  on-chart card — the rich panel lives entirely in the HTML dashboard now.
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
//  ON-CHART BUTTON + DASHBOARD:
//    * A background timer polls /health (reachability) — NO chart/UI/dispatcher
//      calls from the timer (AGENTS.md #17).
//    * The button is drawn in OnRender (SharpDX only, no Draw.* there — #13); a
//      small dot shows bridge reachability (green ok / amber connecting / red
//      offline). Brushes are device resources created eagerly in
//      OnRenderTargetChanged with a self-heal in OnRender (§5b). _terminated
//      (set first in Terminated) gates DX teardown.
//    * The button is click-draggable (double-click resets; a clean click opens the
//      dashboard). Its position persists via serialized ButtonOffsetX/Y.
//    * The dashboard window is embedded WebView2, loaded by reflection so the
//      strategy still COMPILES without the WebView2 DLLs referenced — when they're
//      absent (or the runtime is missing) the click opens the default browser.
//
//  NOTE: compiles INSIDE NinjaTrader 8 (NinjaScript editor / F5) — it links against
//  NinjaTrader + SharpDX assemblies and cannot be built by a standalone toolchain.
//  OnRender/OnRenderTargetChanged/RenderTarget/ChartPanel are used from a Strategy
//  (supported in NT8); the strategy must be applied directly to a chart to render.
//  To embed (optional): NinjaScript Editor → References… → add Microsoft.Web.WebView2.Core.dll
//  + Microsoft.Web.WebView2.Wpf.dll (NuGet) and ensure the WebView2 Runtime is installed.
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

        // ---- prop-firm account selection (single dropdown) ----------------------
        // ONE dropdown of valid firm/type/size combos (see the PropFirmAccount enum +
        // PropFirmAccounts.Map at the bottom of this file). Reported to the bridge
        // (/ingest/account); the bridge loads that firm's context file into the brain AND
        // enforces the account's limits (daily loss, max contracts). Leave on "(none)" to
        // select no account (nothing applied). The enum mirrors config/prop-firms.yaml —
        // the bridge owns the numbers and validates the combo.
        [NinjaScriptProperty]
        [Display(Name = "Prop firm account", GroupName = "Prop firm account", Order = 1)]
        public PropFirmAccount PropAccount { get; set; } = PropFirmAccount.None;

        private string BaseUrl => string.Format("http://{0}:{1}", BridgeHost, BridgePort);
        #endregion

        // ---- bridge poll state ---------------------------------------------
        private Timer _timer;
        private int _pollBusy;   // Interlocked gate: timer ticks must not overlap

        private volatile string _error;             // last poll failure (null when healthy)
        private volatile bool _everPolled;          // a /health poll has succeeded at least once

        // ---- dashboard window (embedded WebView2, opened by reflection) ----------
        private System.Windows.Window _dashWin;      // NT NTWindow when resolvable, else a plain WPF window; null once closed
        private static Type _ntWindowType;           // resolved-once NTWindow type (null = not found / use plain Window)
        private static bool _ntWindowSearched;

        // ---- button dragging (handlers run on the UI thread; OnRender only reads
        //      the float offsets — 32-bit reads/writes are atomic) ---------------
        private ChartControl _mouseChart;     // chart whose Preview events we hooked
        private bool _mouseHooked;
        private volatile bool _hookQueued;    // an InvokeAsync hookup is in flight
        private bool _armed;                  // a press started on the button (click or drag pending)
        private bool _moved;                  // pointer passed the drag threshold since press
        private float _pressX, _pressY;       // where the press began (device px)
        private float _dragGrabX, _dragGrabY; // mouse-to-button-origin grab offset
        private DateTime _lastDragRefreshUtc = DateTime.MinValue;
        private int _mouseErrorCount;
        private float _offX, _offY;           // clamped offset actually applied at replay
        private volatile bool _rebuildAsap;   // UI thread requests a display-list rebuild

        // ---- display list (built/replayed only on the render thread) ------------
        private readonly List<DrawOp> _ops = new List<DrawOp>();
        private readonly Dictionary<uint, D2D.SolidColorBrush> _brushes =
            new Dictionary<uint, D2D.SolidColorBrush>();
        private bool _builtOk;                      // bridge-reachable state the button was built for
        private string _builtError;                 // _error the button was built for
        private DateTime _lastBuildUtc = DateTime.MinValue;
        private float _btnX, _btnY, _btnW, _btnH;
        private bool _hasButton;

        // Device-resource lifecycle (AGENTS.md §5b) + teardown guard (§5d).
        private volatile bool _terminated;          // set FIRST in Terminated; OnRender bails on it
        private D2D.RenderTarget _lastSeenRT;       // target the brush palette was created against
        private bool _dxInitialized;                // palette valid for _lastSeenRT
        private int _renderErrorCount;              // bounded error prints
        private int _stateErrorCount;

        private DW.TextFormat _fHead, _fSub;

        [NinjaScriptProperty]
        public int RefreshSeconds { get; set; } = 5;

        // Master size knob: the button scales by FontSize/12 — text AND padding in
        // both BuildButton and EnsureFormats. 12 = original size, 18 = 1.5×. Bump
        // this if the button reads too small on a high-DPI / 4K chart.
        [NinjaScriptProperty]
        public int FontSize { get; set; } = 18;

        // Dragged button position (px offset from the default top-left anchor). Not
        // browsable — set by dragging; serialized so it persists with the workspace.
        [Browsable(false)]
        public float ButtonOffsetX { get; set; }

        [Browsable(false)]
        public float ButtonOffsetY { get; set; }

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
                    + "and shows an on-chart button that opens the "
                    + "HTML dashboard in a NinjaTrader (WebView2) window.";
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
                // Background poll (/health for the button's status dot). Cheap; the
                // chart render path reads only cached fields.
                if (_timer == null)
                    _timer = new Timer(Poll, null, 0, Math.Max(1, RefreshSeconds) * 1000);
            }
            else if (State == State.Historical)
            {
                // Earliest chance to hook the button-drag mouse events; ChartControl can
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
                try { DisposeOps(); } catch { }
                try { DisposeBrushes(); } catch { }
                try { DisposeFormats(); } catch { }
            }
        }

        protected override void OnBarUpdate()
        {
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
                // Map the selected account combo -> bridge report fields. "(none)" sends a
                // blank prop_firm + null size, which the bridge treats as "unspecified" and
                // leaves the current selection unchanged. account_size is a clean integer
                // string (a valid JSON number).
                string pf = "", at = "", sizeTok = "null";
                string[] sel;
                if (PropFirmAccounts.Map.TryGetValue(PropAccount, out sel))
                {
                    pf = sel[0]; at = sel[1]; sizeTok = sel[2];
                }
                string body = string.Format(
                    "{{\"account\":\"{0}\",\"allow_live\":{1},\"use_agent_strategies\":{2},"
                    + "\"prop_firm\":\"{3}\",\"account_type\":\"{4}\",\"account_size\":{5}}}",
                    Escape(name), AllowLive ? "true" : "false",
                    UseAgentStrategies ? "true" : "false",
                    Escape(pf), Escape(at), sizeTok);
                await PostAsync("/ingest/account", body);
                Print("Hermes: reported account '" + name + "' to bridge (agent_strategies="
                    + (UseAgentStrategies ? "on" : "off")
                    + (pf == "" ? "" : ", prop_firm=" + pf + "/" + at + "/" + sizeTok)
                    + ").");
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
        //  Background poll — ONLY fetch + cache. No chart / UI / dispatcher calls
        //  here (AGENTS.md #17). /health drives the button's status dot. The rich
        //  panel now lives in the HTML dash.
        // =====================================================================
        private async void Poll(object state)
        {
            if (Interlocked.Exchange(ref _pollBusy, 1) == 1)
                return;   // previous tick still in flight (slow bridge) — skip, don't stack
            try
            {
                // Reachability first — this alone drives the button's status dot.
                using (var cts = new CancellationTokenSource(Math.Max(2, RefreshSeconds) * 1000))
                using (var resp = await Http.GetAsync(BaseUrl + "/health", cts.Token))
                {
                    if (!resp.IsSuccessStatusCode)
                    {
                        _error = "HTTP " + (int)resp.StatusCode + " /health";
                        return;
                    }
                    await resp.Content.ReadAsStringAsync();   // drain so the connection is reused cleanly
                }
                _error = null;
                _everPolled = true;
            }
            catch (Exception ex)
            {
                _error = ex.Message;   // the button's status dot turns red until the bridge answers
            }
            finally
            {
                Interlocked.Exchange(ref _pollBusy, 0);
            }
        }

        // =====================================================================
        //  Button mouse handling — Preview mouse events on the ChartControl. A clean
        //  click opens the dashboard; a drag past the threshold moves the button;
        //  double-click snaps it home. Hook/unhook hop to the UI thread ASYNC (a
        //  sync Invoke from a lifecycle path can ABBA-deadlock with F5 reload's
        //  Clone — dump-verified 2026-06-10). Handlers run on the UI thread: guard
        //  _terminated, try/catch all bodies (#21), touch only plain fields, never
        //  Draw.* (#22).
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
                        Print("[HermesDashboard] dashboard button enabled (mouse hook installed)");
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

        private const float DragThresholdPx = 4f;   // movement under this counts as a click, not a drag

        private void OnChartMouseDown(object sender, MouseButtonEventArgs e)
        {
            try
            {
                _armed = false; _moved = false;
                if (_terminated || !_hasButton) return;
                var cc = _mouseChart;
                if (cc == null) return;
                float mx = MouseX(cc, e), my = MouseY(cc, e);
                if (!HitButton(mx, my)) return;
                if (e.ClickCount == 2)   // double-click: snap back to the default corner
                {
                    ButtonOffsetX = 0f; ButtonOffsetY = 0f;
                    e.Handled = true;
                    cc.InvalidateVisual();
                    return;
                }
                // Arm a press: it becomes a drag if the pointer passes the threshold,
                // otherwise the mouse-up fires it as a click (opens the dashboard).
                _armed = true;
                _pressX = mx; _pressY = my;
                _dragGrabX = mx - _offX;
                _dragGrabY = my - _offY;
                e.Handled = true;   // keep the chart from panning/crosshairing inside the button
            }
            catch (Exception ex) { MouseError(ex); }
        }

        private void OnChartMouseMove(object sender, MouseEventArgs e)
        {
            try
            {
                if (_terminated || !_armed) return;
                if (e.LeftButton != MouseButtonState.Pressed)
                {
                    _armed = false;   // the Up happened off-chart — self-heal (no click)
                    return;
                }
                var cc = _mouseChart;
                if (cc == null) return;
                float mx = MouseX(cc, e), my = MouseY(cc, e);
                if (!_moved
                    && Math.Abs(mx - _pressX) < DragThresholdPx
                    && Math.Abs(my - _pressY) < DragThresholdPx)
                    return;   // still within the click slop — don't start dragging yet
                _moved = true;
                ButtonOffsetX = mx - _dragGrabX;
                ButtonOffsetY = my - _dragGrabY;
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
                if (!_armed) return;
                _armed = false;
                if (_terminated) return;   // tearing down — don't open mid-disable
                e.Handled = true;
                var cc = _mouseChart;
                if (_moved)
                {
                    if (cc != null) cc.InvalidateVisual();   // final paint past the 33 ms gate
                }
                else
                {
                    OpenDashboard();   // a clean click (no drag) — open the HTML dashboard
                }
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

        private bool HitButton(float x, float y)
        {
            return _hasButton
                && x >= _btnX + _offX && x <= _btnX + _offX + _btnW
                && y >= _btnY + _offY && y <= _btnY + _offY + _btnH;
        }

        // Clamp the dragged offset so the button can't leave the panel (render thread).
        private void ApplyButtonOffset()
        {
            if (!_hasButton || ChartPanel == null)
            {
                _offX = ButtonOffsetX; _offY = ButtonOffsetY;
                return;
            }
            float x = _btnX + ButtonOffsetX, y = _btnY + ButtonOffsetY;
            x = Math.Max(ChartPanel.X, Math.Min(x, ChartPanel.X + ChartPanel.W - _btnW));
            y = Math.Max(ChartPanel.Y, Math.Min(y, ChartPanel.Y + ChartPanel.H - _btnH));
            _offX = x - _btnX;
            _offY = y - _btnY;
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
        //  Button rendering (SharpDX only — no Draw.* in OnRender, AGENTS.md #13)
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

                // Rebuild the button only when its reachability state changes (or once,
                // or on an explicit request). The button is tiny, but rebuilding
                // DirectWrite layouts every tick is the GC/dispatcher pressure #17 warns about.
                string err = _error;
                bool ok = err == null && _everPolled;
                if (!_hasButton || ok != _builtOk || err != _builtError || _rebuildAsap)
                    BuildButton(ok);

                ApplyButtonOffset();   // clamp the dragged offset to the panel each frame

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
            // The button background is the first op BuildButton emits, so a plain
            // in-order replay draws it behind the dot + text. No special-casing here.
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

        // ---- on-chart button + dashboard window --------------------------------
        private void BuildButton(bool bridgeOk)
        {
            _builtOk = bridgeOk;
            _builtError = _error;
            _lastBuildUtc = DateTime.UtcNow;
            _rebuildAsap = false;
            DisposeOps();
            EnsureFormats();

            float s = Math.Max(0.5f, FontSize / 12f);
            var fac = NinjaTrader.Core.Globals.DirectWriteFactory;

            // Measure the two text lines so the pill hugs its content.
            var tlLabel = new DW.TextLayout(fac, "HERMES", _fHead, 4000f, 200f);
            var tlSub   = new DW.TextLayout(fac, "DASHBOARD ↗", _fSub, 4000f, 200f);
            float labW = tlLabel.Metrics.Width, labH = tlLabel.Metrics.Height;
            float subW = tlSub.Metrics.Width,  subH = tlSub.Metrics.Height;

            float padX = 12f * s, padY = 8f * s, gap = 7f * s, dotR = 4f * s;
            float textW = Math.Max(labW, subW);
            float w = padX * 2f + dotR * 2f + gap + textW;
            float h = padY * 2f + labH + 3f * s + subH;

            float x0 = ChartPanel.X + 12f, y0 = ChartPanel.Y + 12f;

            // Background first (drawn behind the dot + text on in-order replay). The
            // border tints with reachability: edge = ok, amber = connecting, red = offline.
            uint border = _error != null ? ColRed : (bridgeOk ? ColCardEdge : ColAmber);
            AddRect(x0, y0, x0 + w, y0 + h, 9f * s, ColCardBg, border);

            // Reachability dot, centered on the label line.
            float dotCx = x0 + padX + dotR;
            float dotCy = y0 + padY + labH / 2f;
            uint dotColor = _error != null ? ColRed : (bridgeOk ? ColGreen : ColAmber);
            AddEllipse(dotCx, dotCy, dotR, dotColor);

            // Text lines.
            float textX = dotCx + dotR + gap;
            PlaceLayout(tlLabel, textX, y0 + padY, ColText);
            PlaceLayout(tlSub,   textX, y0 + padY + labH + 3f * s, ColMuted);

            SetButton(x0, y0, w, h);
        }

        private void SetButton(float x, float y, float w, float h)
        {
            _btnX = x; _btnY = y; _btnW = w; _btnH = h;
            _hasButton = true;
        }

        // Add a Text op for an already-created (and measured) layout. The op owns the
        // layout from here — DisposeOps disposes it on the next rebuild.
        private void PlaceLayout(DW.TextLayout tl, float x, float y, uint color)
        {
            _ops.Add(new DrawOp { Kind = OpKind.Text, Layout = tl, X1 = x, Y1 = y, Color = color });
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

        // =====================================================================
        //  Open the HTML dashboard. Prefer an embedded WebView2 inside a NinjaTrader
        //  window; if the WebView2 DLLs aren't referenced or the runtime is missing,
        //  fall back to the system default browser. Reflection keeps the strategy
        //  compiling WITHOUT the WebView2 assemblies present.
        // =====================================================================
        private void OpenDashboard()
        {
            string url = string.Format("http://{0}:{1}/", BridgeHost, BridgePort);
            var cc = _mouseChart;
            Action open = delegate
            {
                try { if (TryShowWebViewWindow(url)) return; }
                catch (Exception ex) { Print("[HermesDashboard] WebView2 open failed: " + ex.Message); }
                OpenInBrowser(url);
            };
            try
            {
                if (cc != null && !cc.Dispatcher.CheckAccess())
                    cc.Dispatcher.InvokeAsync(open);
                else
                    open();
            }
            catch (Exception ex) { Print("[HermesDashboard] open dispatch failed: " + ex.Message); }
        }

        // Returns true if an embedded WebView2 window was shown; false means "not
        // available — caller should fall back to the browser." Must run on the UI thread.
        private bool TryShowWebViewWindow(string url)
        {
            // Core assembly present? (Added via NinjaScript Editor -> References...)
            Type envType = Type.GetType(
                "Microsoft.Web.WebView2.Core.CoreWebView2Environment, Microsoft.Web.WebView2.Core");
            if (envType == null) return false;

            // Runtime installed? GetAvailableBrowserVersionString(null) throws if not.
            MethodInfo getVer = envType.GetMethod("GetAvailableBrowserVersionString",
                new Type[] { typeof(string) });
            if (getVer == null) return false;
            string ver;
            try { ver = (string)getVer.Invoke(null, new object[] { null }); }
            catch { return false; }   // WebView2RuntimeNotFoundException -> browser fallback
            if (string.IsNullOrEmpty(ver)) return false;

            Type wvType = Type.GetType(
                "Microsoft.Web.WebView2.Wpf.WebView2, Microsoft.Web.WebView2.Wpf");
            if (wvType == null) return false;

            // Implicit init writes a user-data folder; point it somewhere writable
            // (NinjaTrader's install dir often isn't) before the control initializes.
            try
            {
                string udf = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "HermesDashboard", "WebView2");
                System.IO.Directory.CreateDirectory(udf);
                Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", udf);
            }
            catch { }

            // Reuse the window if it's already open.
            if (_dashWin != null)
            {
                try { _dashWin.Activate(); return true; }
                catch { _dashWin = null; }
            }

            object wv = Activator.CreateInstance(wvType);
            PropertyInfo srcProp = wvType.GetProperty("Source");
            if (srcProp == null) return false;
            srcProp.SetValue(wv, new Uri(url), null);   // setting Source triggers implicit init

            var win = CreateDashboardWindow();
            win.Title = "Hermes Dashboard";
            // NTWindow labels its chrome via a "Caption" property (not Window.Title) — set it
            // when present so the NT-themed title bar reads correctly. Reflection: NTWindow is
            // not referenced at compile time (its namespace differs across NT builds).
            try
            {
                PropertyInfo capProp = win.GetType().GetProperty("Caption");
                if (capProp != null && capProp.CanWrite)
                    capProp.SetValue(win, "Hermes Dashboard", null);
            }
            catch { }
            win.Width = 1180;
            win.Height = 820;
            win.Content = (System.Windows.UIElement)wv;
            win.Closed += delegate { _dashWin = null; };
            win.Show();
            _dashWin = win;
            return true;
        }

        // Host window for the embedded WebView2: a NinjaTrader NTWindow (native chrome +
        // theming) when its type can be resolved at runtime, otherwise a plain WPF window.
        // NTWindow is found by reflection so the strategy never hard-depends on its
        // namespace/assembly (which the NinjaScript Strategy compile doesn't expose by name).
        private System.Windows.Window CreateDashboardWindow()
        {
            if (!_ntWindowSearched)
            {
                _ntWindowSearched = true;
                _ntWindowType = ResolveNtWindowType();
            }
            if (_ntWindowType != null)
            {
                try { return (System.Windows.Window)Activator.CreateInstance(_ntWindowType); }
                catch { }   // ctor not accessible / threw — drop to a plain WPF window
            }
            return new System.Windows.Window();
        }

        // Find a Window-derived type named "NTWindow" in the loaded NinjaTrader assemblies.
        private static Type ResolveNtWindowType()
        {
            var asms = AppDomain.CurrentDomain.GetAssemblies();
            // Fast path: the two namespaces NTWindow has shipped under.
            foreach (var asm in asms)
            {
                try
                {
                    Type t = asm.GetType("NinjaTrader.Gui.NTWindow")
                          ?? asm.GetType("NinjaTrader.Gui.Tools.NTWindow");
                    if (t != null && typeof(System.Windows.Window).IsAssignableFrom(t))
                        return t;
                }
                catch { }
            }
            // Fallback: scan NinjaTrader assemblies for any Window-derived "NTWindow".
            foreach (var asm in asms)
            {
                if (asm.FullName == null
                    || asm.FullName.IndexOf("NinjaTrader", StringComparison.OrdinalIgnoreCase) < 0)
                    continue;
                Type[] types;
                try { types = asm.GetTypes(); }
                catch (ReflectionTypeLoadException ex) { types = ex.Types; }
                catch { continue; }
                foreach (var t in types)
                    if (t != null && t.Name == "NTWindow"
                        && typeof(System.Windows.Window).IsAssignableFrom(t))
                        return t;
            }
            return null;
        }

        private void OpenInBrowser(string url)
        {
            try
            {
                System.Diagnostics.Process.Start(
                    new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex) { Print("[HermesDashboard] could not open browser: " + ex.Message); }
        }

        // ---- resource lifecycle -----------------------------------------------------
        private void EnsureFormats()
        {
            if (_fHead != null) return;
            float s = Math.Max(0.5f, FontSize / 12f);
            var fac = NinjaTrader.Core.Globals.DirectWriteFactory;
            _fHead = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Bold,   DW.FontStyle.Normal, 15f * s);
            _fSub  = new DW.TextFormat(fac, "Segoe UI", DW.FontWeight.Normal, DW.FontStyle.Normal, 11f * s);
        }

        private void DisposeOps()
        {
            foreach (var op in _ops)
                if (op.Layout != null && !op.Layout.IsDisposed) op.Layout.Dispose();
            _ops.Clear();
            _hasButton = false;
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
            var formats = new[] { _fHead, _fSub };
            foreach (var f in formats)
                if (f != null && !f.IsDisposed) f.Dispose();
            _fHead = _fSub = null;
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

    // =========================================================================
    //  Prop-firm account selection — ONE flat dropdown of valid firm/type/size combos.
    //  NinjaTrader's grid renders an enum as a native dropdown with zero TypeConverter /
    //  context.Instance plumbing (the cascading-dropdown approach was unreliable in the
    //  grid — dependent lists came back empty). Each member is one VALID account; the
    //  strategy maps it to the bridge report fields via PropFirmAccounts.Map, which
    //  MIRRORS config/prop-firms.yaml (the bridge owns the numbers + validates the combo).
    //  Adding/renaming an account means editing BOTH this enum+map and the YAML.
    // =========================================================================
    public enum PropFirmAccount
    {
        [Display(Name = "(none)")] None,
        [Display(Name = "Lucid Trading - LucidPro - 25K")] LucidPro_25K,
        [Display(Name = "Lucid Trading - LucidPro - 50K")] LucidPro_50K,
        [Display(Name = "Lucid Trading - LucidPro - 100K")] LucidPro_100K,
        [Display(Name = "Lucid Trading - LucidPro - 150K")] LucidPro_150K,
        [Display(Name = "Lucid Trading - LucidFlex - 25K")] LucidFlex_25K,
        [Display(Name = "Lucid Trading - LucidFlex - 50K")] LucidFlex_50K,
        [Display(Name = "Lucid Trading - LucidFlex - 100K")] LucidFlex_100K,
        [Display(Name = "Lucid Trading - LucidFlex - 150K")] LucidFlex_150K,
        [Display(Name = "Lucid Trading - LucidDirect - 50K")] LucidDirect_50K,
        [Display(Name = "Lucid Trading - LucidDirect - 100K")] LucidDirect_100K,
        [Display(Name = "Lucid Trading - LucidDirect - 150K")] LucidDirect_150K,
    }

    internal static class PropFirmAccounts
    {
        // enum -> { prop_firm, account_type, account_size }. The size is a clean integer
        // string (a valid JSON number for the report). MIRRORS config/prop-firms.yaml.
        public static readonly Dictionary<PropFirmAccount, string[]> Map =
            new Dictionary<PropFirmAccount, string[]>
            {
                { PropFirmAccount.LucidPro_25K,     new[] { "Lucid Trading", "LucidPro",    "25000"  } },
                { PropFirmAccount.LucidPro_50K,     new[] { "Lucid Trading", "LucidPro",    "50000"  } },
                { PropFirmAccount.LucidPro_100K,    new[] { "Lucid Trading", "LucidPro",    "100000" } },
                { PropFirmAccount.LucidPro_150K,    new[] { "Lucid Trading", "LucidPro",    "150000" } },
                { PropFirmAccount.LucidFlex_25K,    new[] { "Lucid Trading", "LucidFlex",   "25000"  } },
                { PropFirmAccount.LucidFlex_50K,    new[] { "Lucid Trading", "LucidFlex",   "50000"  } },
                { PropFirmAccount.LucidFlex_100K,   new[] { "Lucid Trading", "LucidFlex",   "100000" } },
                { PropFirmAccount.LucidFlex_150K,   new[] { "Lucid Trading", "LucidFlex",   "150000" } },
                { PropFirmAccount.LucidDirect_50K,  new[] { "Lucid Trading", "LucidDirect", "50000"  } },
                { PropFirmAccount.LucidDirect_100K, new[] { "Lucid Trading", "LucidDirect", "100000" } },
                { PropFirmAccount.LucidDirect_150K, new[] { "Lucid Trading", "LucidDirect", "150000" } },
            };
    }
}
