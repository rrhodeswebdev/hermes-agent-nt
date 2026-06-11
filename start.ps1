#requires -Version 5
<#
  Hermes x NinjaTrader 8 - one-command startup (Windows side).
  Starts the Python bridge (FastAPI server + dashboard). Reads config/trading.yaml,
  creates the bridge venv on first run, validates the decision brain, prints the
  NinjaTrader connection info, then runs the server. Ctrl-C stops it.

  Usage:
    .\start.ps1                 # serve using config/trading.yaml
    .\start.ps1 -Mock           # force the deterministic mock brain (no LLM)
    .\start.ps1 -CheckClaude    # also ping claude once (one real model call) before serving
    .\start.ps1 -Port 9000      # override the port
#>
[CmdletBinding()]
param(
  [switch]$Mock,
  [switch]$CheckClaude,
  [int]$Port
)

$ErrorActionPreference = 'Stop'

$RepoRoot  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgeDir = Join-Path $RepoRoot 'bridge'
$Config    = Join-Path $RepoRoot 'config\trading.yaml'
$Venv      = Join-Path $BridgeDir '.venv'
$VenvPy    = Join-Path $Venv 'Scripts\python.exe'
$VenvCli   = Join-Path $Venv 'Scripts\hermes-bridge.exe'

function Say  ($m) { Write-Host "> $m"     -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "[ok] $m"  -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m"   -ForegroundColor Yellow }
function Die  ($m) { Write-Host "[x] $m"   -ForegroundColor Red; exit 1 }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Die "uv not found - install it: https://docs.astral.sh/uv/"
}

# 1. Bridge venv (create on first run).
if (-not (Test-Path $VenvCli)) {
  Say "Setting up the bridge venv (first run)..."
  Push-Location $BridgeDir
  try {
    uv venv --python 3.11 .venv
    uv pip install --python .venv -e ".[dev]"
  } finally { Pop-Location }
  Ok "Bridge venv ready."
}

# 2. Read the resolved config authoritatively (same loader the server uses).
if ($Mock) { $env:HERMES_BRIDGE_AGENT = 'mock' }
if ($Port) { $env:HERMES_BRIDGE_PORT = "$Port" }
$pyReader = @'
import sys
from hermes_bridge.config import load_config
c = load_config(sys.argv[1])
print(f"CLIENT={c.agent.client}")
print(f"HOST={c.server.host}")
print(f"PORT={c.server.port}")
print(f"SID={c.strategy_id}")
print(f"SYM={c.instrument.symbol}")
print(f"TF={c.instrument.timeframe}")
print(f"ACCT={c.execution.account}")
print(f"LIVE={'1' if c.execution.allow_live else '0'}")
print(f"CLAUDEBIN={c.agent.claude.claude_bin}")
print(f"CLAUDEMODEL={c.agent.claude.model}")
'@
$resolvedText = $pyReader | & $VenvPy - $Config
if ($LASTEXITCODE -ne 0) { Die "Could not read $Config" }
$cfg = @{}
foreach ($line in $resolvedText) {
  if ($line -match '^\s*([^=]+)=(.*)$') { $cfg[$matches[1].Trim()] = $matches[2] }
}

$healthHost = $cfg.HOST; if ($healthHost -eq '0.0.0.0') { $healthHost = '127.0.0.1' }
$baseUrl = "http://${healthHost}:$($cfg.PORT)"

Say "Config:   agent=$($cfg.CLIENT)  instrument=$($cfg.SYM) $($cfg.TF)  account=$($cfg.ACCT)  strategy_id=$($cfg.SID)"

# 3. Validate the decision brain.
switch ($cfg.CLIENT) {
  'claude' {
    $cb = Get-Command $cfg.CLAUDEBIN -ErrorAction SilentlyContinue
    if (-not $cb) { Die "claude not found on PATH ('$($cfg.CLAUDEBIN)'). Install Claude Code, or run .\start.ps1 -Mock." }
    Ok "Brain: Claude oneshot (claude -p --safe-mode, model=$($cfg.CLAUDEMODEL)) -> $($cb.Source)"
    if ($CheckClaude) {
      Say "Pinging Claude (one real model call on your subscription)..."
      $pong = & $cfg.CLAUDEBIN -p --safe-mode --model $cfg.CLAUDEMODEL "Reply with exactly one word: pong"
      if ("$pong" -match 'pong') { Ok "Claude responded." }
      else { Warn "Claude ping did not return 'pong' - the bridge falls back to WAIT on errors." }
    }
  }
  'hermes' { Ok "Brain: Hermes (see config/trading.yaml agent.hermes)." }
  default  { Ok "Brain: deterministic mock rules (no LLM)." }
}

if ($cfg.LIVE -eq '1') { Warn "execution.allow_live is TRUE - this can place REAL orders. Ctrl-C now if unintended." }

# 4. Print the connection banner, then run the server (Ctrl-C stops it).
$lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "  -- Hermes bridge starting --------------------------------" -ForegroundColor White
Write-Host "     Dashboard       $baseUrl/"
Write-Host "     Health          $baseUrl/health"
Write-Host "     Session status  $baseUrl/session/status"
Write-Host ""
Write-Host "  NinjaTrader (Sim) -> set on HermesBridgeStrategy:"
Write-Host "     BridgePort  = $($cfg.PORT)"
Write-Host "     StrategyId  = $($cfg.SID)   (must match exactly)"
Write-Host "     Account     = $($cfg.ACCT)  AllowLive = false"
if ($lan) {
  Write-Host "     BridgeHost  = 127.0.0.1   (same machine) or $lan from another box on the LAN"
} else {
  Write-Host "     BridgeHost  = 127.0.0.1   (NinjaTrader on this machine)"
}
Write-Host ""
Write-Host "  Kill switch:  Invoke-RestMethod -Method Post $baseUrl/control/flatten"
Write-Host "  Stop:         Ctrl-C"
Write-Host ""

Set-Location $RepoRoot
& $VenvCli serve --config $Config
