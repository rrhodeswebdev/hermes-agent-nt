#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// =============================================================================
//  HermesBridgeStrategy
// -----------------------------------------------------------------------------
//  Streams chart data to the Python "hermes-bridge" and executes the risk-approved
//  orders it returns, on the SIMULATED account by default.
//
//  Lifecycle:
//    * On the historical→realtime transition it bulk-uploads ALL loaded bars to
//      POST /ingest/history (the agent's "review the history" step).
//    * On each newly CLOSED realtime bar (Calculate.OnBarClose) it POSTs the bar to
//      /ingest/bar, then GETs /commands/next and, if a command is returned, runs it
//      on the strategy thread via TriggerCustomEvent (required for order methods).
//    * Fills are reported back to /ingest/fill from OnExecutionUpdate.
//
//  Safety:
//    * AllowLive defaults to FALSE. If the selected account does not look like a
//      simulation account, the strategy logs an error and does NOT place orders.
//    * The bridge's RiskGate is the real safety authority; this strategy only
//      executes commands the bridge already approved.
//
//  NOTE: This file compiles INSIDE NinjaTrader 8 (NinjaScript editor / right-click
//  > Compile). It cannot be compiled by a standalone toolchain because it links
//  against NinjaTrader assemblies.
// =============================================================================

namespace NinjaTrader.NinjaScript.Strategies
{
    public class HermesBridgeStrategy : Strategy
    {
        private static readonly HttpClient Http = new HttpClient();
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

        // MUST exceed the agent's decision time, or the bar POST is abandoned before the
        // bridge finishes deciding and the command isn't fetched until the NEXT bar (late /
        // stale). Claude-on-subscription decisions run ~30-115s, so set this to your bridge's
        // agent timeout (config: agent.claude.timeout_s) and keep it just BELOW your bar
        // interval to avoid overlapping requests. 2m bar + 115s bridge timeout -> 115000.
        [NinjaScriptProperty]
        public int HttpTimeoutMs { get; set; } = 115000;

        private string BaseUrl => string.Format("http://{0}:{1}", BridgeHost, BridgePort);
        #endregion

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Streams bars to the Hermes bridge and executes approved orders (Sim).";
                Name = "HermesBridgeStrategy";
                Calculate = Calculate.OnBarClose;          // one decision per closed bar
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsUnmanaged = false;                        // use managed orders + brackets
                BarsRequiredToTrade = 30;                   // warm up indicators on the agent side
                IncludeCommission = true;
                StartBehavior = StartBehavior.WaitUntilFlat;
            }
            else if (State == State.Realtime)
            {
                // Transitioned from historical to realtime: ship the full history once.
                if (SendHistory && !historySent)
                {
                    historySent = true;
                    GuardAccount();
                    _ = PostHistoryAsync();
                }
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0) return;
            if (CurrentBar < BarsRequiredToTrade) return;
            // Only stream/act on realtime bars; history is bulk-uploaded separately.
            if (State != State.Realtime) return;

            string barJson = BarJson(
                EpochSeconds(Time[0]), Open[0], High[0], Low[0], Close[0], Volume[0]);
            _ = HandleBarAsync(barJson);
        }

        // ---- networking ------------------------------------------------------
        private async Task HandleBarAsync(string barJson)
        {
            // Send the bar. The bridge computes the decision server-side; this call may
            // block for the agent's full think time (LLMs ~15s), hence HttpTimeoutMs.
            try
            {
                string body = string.Format(
                    "{{\"instrument\":\"{0}\",\"timeframe\":\"{1}\",\"bar\":{2}}}",
                    Escape(Instrument.FullName), Escape(BarsPeriodString()), barJson);
                await PostAsync("/ingest/bar", body);
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

        private async Task PostHistoryAsync()
        {
            try
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
                await PostAsync("/ingest/history", sb.ToString());
                Print(string.Format("Hermes: sent {0} historical bars to {1}", last + 1, BaseUrl));
            }
            catch (Exception ex)
            {
                Print("Hermes bridge history error: " + ex.Message);
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
            // Best-effort guard. NinjaTrader's built-in simulator account is "Sim101".
            // If the live account does not look like a sim account, refuse to trade
            // unless AllowLive was explicitly enabled.
            string name = Account != null ? Account.Name : "";
            bool looksSim = name != null && name.IndexOf("Sim", StringComparison.OrdinalIgnoreCase) >= 0;
            if (!looksSim && !AllowLive)
            {
                tradingDisabled = true;
                Print("Hermes SAFETY: account '" + name + "' is not a simulation account and "
                      + "AllowLive is false. Trading DISABLED. Set AllowLive=true to override.");
            }
        }

        private int SignedPosition()
        {
            if (Position.MarketPosition == MarketPosition.Long) return Position.Quantity;
            if (Position.MarketPosition == MarketPosition.Short) return -Position.Quantity;
            return 0;
        }

        private static double EpochSeconds(DateTime t)
        {
            var epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            return Math.Floor((t.ToUniversalTime() - epoch).TotalSeconds);
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

        // Per-request timeout via CancellationToken. We never mutate Http.Timeout because
        // Http is a shared static client: once it has sent a request its Timeout is locked,
        // and setting it again (e.g. on a strategy re-enable) throws InvalidOperationException.
        private async Task PostAsync(string path, string json)
        {
            using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
            using (var cts = new CancellationTokenSource(HttpTimeoutMs))
                await Http.PostAsync(BaseUrl + path, content, cts.Token);
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
    }
}
