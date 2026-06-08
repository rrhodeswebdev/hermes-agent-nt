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
//  Shows what the Hermes agent is doing, as a fixed text panel on the chart. It
//  polls the bridge's pre-formatted panel (GET /dashboard.txt) on a timer and draws
//  it — so there is NO JSON parsing on the NinjaScript side.
//
//  Drop it on ANY chart or a dedicated chart window (it doesn't have to be the
//  trading chart). It only needs network access to the bridge.
//
//  Compile inside NinjaTrader (NinjaScript Editor → New Indicator → paste → F5).
// =============================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class HermesDashboard : Indicator
    {
        private static readonly HttpClient Http = new HttpClient();
        private Timer _timer;
        private volatile string _panelText = "HERMES — connecting…";

        [NinjaScriptProperty]
        public string BridgeHost { get; set; } = "127.0.0.1";

        [NinjaScriptProperty]
        public int BridgePort { get; set; } = 8787;

        [NinjaScriptProperty]
        public int RefreshSeconds { get; set; } = 5;

        [NinjaScriptProperty]
        public int FontSize { get; set; } = 12;

        private string BaseUrl => string.Format("http://{0}:{1}", BridgeHost, BridgePort);

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "HermesDashboard";
                Description = "Live panel of the Hermes agent's decisions / position / P&L.";
                IsOverlay = true;                 // draw on the price panel
                Calculate = Calculate.OnEachTick; // repaint as ticks arrive
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
            }
        }

        // Background poll — fetch the pre-formatted panel and cache it. Drawing happens
        // on the chart thread in OnBarUpdate; here we only update the cached text and
        // request a repaint so the panel stays current even in a quiet market.
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
                if (ChartControl != null)
                    ChartControl.Dispatcher.InvokeAsync(() => ForceRefresh());
            }
            catch (Exception ex)
            {
                _panelText = "HERMES — bridge unreachable\n" + ex.Message;
            }
        }

        protected override void OnBarUpdate()
        {
            // Color the panel by position direction if we can cheaply detect it.
            Brush text = Brushes.White;
            if (_panelText.Contains("pos: LONG")) text = Brushes.LimeGreen;
            else if (_panelText.Contains("pos: SHORT")) text = Brushes.OrangeRed;
            if (_panelText.Contains("HALTED")) text = Brushes.Gold;

            Draw.TextFixed(this, "HermesPanel", _panelText, TextPosition.TopLeft,
                text, new SimpleFont("Courier New", FontSize), Brushes.Transparent,
                Brushes.Black, 55);
        }
    }
}
