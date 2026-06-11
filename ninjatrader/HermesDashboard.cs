#region Using declarations
using System;
using System.Net.Http;
using System.Threading;
using System.Windows.Media;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// =============================================================================
//  HermesDashboard (Indicator)
// -----------------------------------------------------------------------------
//  Shows what the Hermes agent is doing as a fixed text panel, and plots the
//  agent's support/resistance (swing_low/swing_high) as horizontal lines.
//
//  THREADING / LOCKUP SAFETY (see E:\Coding\NinjaTrader\AGENTS.md gotcha #17,
//  "UI dispatcher saturation"):
//   * The background timer ONLY fetches + caches text. It makes NO ChartControl /
//     Dispatcher / Draw calls — touching ChartControl.Dispatcher from the timer
//     thread queues UI jobs faster than they drain and locks the chart.
//   * ALL drawing happens in OnBarUpdate and is WALL-CLOCK THROTTLED (~5 Hz), so a
//     fast tick feed on Calculate.OnEachTick cannot flood WPF.
//
//  Put it on the MNQ 2m TRADING chart so the S/R lines sit at the right prices.
//  Compile inside NinjaTrader (NinjaScript Editor → New Indicator → paste → F5).
// =============================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class HermesDashboard : Indicator
    {
        private static readonly HttpClient Http = new HttpClient();
        private Timer _timer;
        private volatile string _panelText = "HERMES — connecting…";
        private volatile string _levelsText = "";   // raw /levels.txt, parsed on the chart thread
        private DateTime _lastDrawUtc = DateTime.MinValue;  // wall-clock throttle for ALL drawing

        [NinjaScriptProperty]
        public string BridgeHost { get; set; } = "127.0.0.1";

        [NinjaScriptProperty]
        public int BridgePort { get; set; } = 8787;

        [NinjaScriptProperty]
        public int RefreshSeconds { get; set; } = 5;

        [NinjaScriptProperty]
        public int FontSize { get; set; } = 12;

        [NinjaScriptProperty]
        public bool ShowLevels { get; set; } = true;   // plot the agent's S/R (swing) lines

        private string BaseUrl => string.Format("http://{0}:{1}", BridgeHost, BridgePort);

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "HermesDashboard";
                Description = "Hermes agent panel (decisions / position / P&L) + its S/R levels.";
                IsOverlay = true;                 // draw on the price panel
                Calculate = Calculate.OnEachTick; // tick-driven, but drawing is wall-clock throttled
                DisplayInDataBox = false;
                PaintPriceMarkers = false;
                IsSuspendedWhileInactive = false;
            }
            else if (State == State.DataLoaded)
            {
                if (_timer == null)
                    _timer = new Timer(Poll, null, 0, Math.Max(1, RefreshSeconds) * 1000);
            }
            else if (State == State.Terminated)
            {
                if (_timer != null) { _timer.Dispose(); _timer = null; }
                // Synchronous cleanup of our draw objects so enable/disable cycles don't leak.
                RemoveDrawObject("HermesResistance"); RemoveDrawObject("HermesResLabel");
                RemoveDrawObject("HermesSupport");    RemoveDrawObject("HermesSupLabel");
            }
        }

        // Background poll — ONLY fetch and cache the bridge's text. No chart / UI / dispatcher
        // calls here (that saturates the WPF dispatcher and locks the chart, AGENTS.md #17).
        // Drawing happens on the chart thread in OnBarUpdate, which runs on every tick.
        private async void Poll(object state)
        {
            try
            {
                using (var cts = new CancellationTokenSource(Math.Max(2, RefreshSeconds) * 1000))
                using (var resp = await Http.GetAsync(BaseUrl + "/dashboard.txt", cts.Token))
                {
                    string text = await resp.Content.ReadAsStringAsync();
                    if (!string.IsNullOrWhiteSpace(text))
                        _panelText = text;
                }
                using (var cts2 = new CancellationTokenSource(Math.Max(2, RefreshSeconds) * 1000))
                using (var resp2 = await Http.GetAsync(BaseUrl + "/levels.txt", cts2.Token))
                {
                    _levelsText = await resp2.Content.ReadAsStringAsync() ?? "";
                }
            }
            catch (Exception ex)
            {
                _panelText = "HERMES — bridge unreachable\n" + ex.Message;
            }
        }

        protected override void OnBarUpdate()
        {
            // WALL-CLOCK THROTTLE: cap ALL drawing at ~5 Hz regardless of tick rate. Per-tick
            // Draw.* on Calculate.OnEachTick floods WPF and locks the chart (AGENTS.md #17).
            if ((DateTime.UtcNow - _lastDrawUtc).TotalMilliseconds < 200)
                return;
            _lastDrawUtc = DateTime.UtcNow;

            // Color the panel by position direction if we can cheaply detect it.
            Brush text = Brushes.White;
            if (_panelText.Contains("pos: LONG")) text = Brushes.LimeGreen;
            else if (_panelText.Contains("pos: SHORT")) text = Brushes.OrangeRed;
            if (_panelText.Contains("HALTED")) text = Brushes.Gold;

            Draw.TextFixed(this, "HermesPanel", _panelText, TextPosition.TopLeft,
                text, new SimpleFont("Courier New", FontSize), Brushes.Transparent,
                Brushes.Black, 55);

            DrawAgentLevels();
        }

        // Draw the agent's current support/resistance (swing_low/swing_high) as horizontal
        // lines. Reuses fixed tags so nothing accumulates. Called only from the throttled
        // OnBarUpdate above — never per tick, never from the timer thread.
        private void DrawAgentLevels()
        {
            if (!ShowLevels)
            {
                RemoveDrawObject("HermesResistance"); RemoveDrawObject("HermesResLabel");
                RemoveDrawObject("HermesSupport");    RemoveDrawObject("HermesSupLabel");
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

        // Pull a "key=value" line out of the cached /levels.txt; NaN if the key is missing.
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
                    if (double.TryParse(line.Substring(eq + 1),
                        System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out v))
                        return v;
                }
            }
            return double.NaN;
        }
    }
}
