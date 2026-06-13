"""Dashboard rendering: a plain-text panel (for the NinjaScript indicator) and a
self-contained auto-refreshing HTML page (for a browser). Both consume the dict
produced by the /dashboard endpoint, so the format lives in one place.
"""

from __future__ import annotations

from datetime import datetime


def _hhmmss(ts: float | None) -> str:
    if ts is None:
        return "--:--:--"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _pos_str(position: int, avg_price: float) -> str:
    if position == 0:
        return "FLAT"
    side = "LONG" if position > 0 else "SHORT"
    return f"{side} {abs(position)} @ {avg_price:g}"


def render_text(d: dict | None) -> str:
    """Pre-formatted monospace panel. The NinjaScript indicator draws this verbatim."""
    if not d:
        return "HERMES — no data yet"
    s = d["session"]
    goal = d["goal"]
    age = d.get("data_age_seconds")
    age_str = f"{int(age)}s" if age is not None else "?"
    delayed = "  [DELAYED]" if (age is not None and age > 120) else ""
    halt = f"HALTED:{s['halt_reason']}" if s["halted"] else "active"

    lines = [
        f"HERMES  {d['instrument']} {d['timeframe']}  ({d['agent']}/{d['brain']})",
        f"data age: {age_str}{delayed}",
        f"pos: {_pos_str(s['position'], s['avg_price'])}",
        f"realized: {s['realized_pnl']:+.2f}   unreal: {s['unrealized_pnl']:+.2f}"
        f"   trades: {s['trades_today']}",
        f"goal: +{goal['profit_target']:.0f} / -{goal['max_daily_loss']:.0f}   [{halt}]",
    ]
    pl = d.get("planner")
    if pl:
        detail = pl.get("conditions") or pl.get("last_error") or ""
        lines.append(f"plan[{pl['status']}]: {detail}"[:60])
        if pl.get("session_error"):
            # The pre-session study failed: every plan runs without the brief.
            lines.append(f"session ERROR: {pl['session_error']}"[:60])
    lines.append("-" * 40)
    ld = d.get("last_decision")
    if ld:
        lines.append(f"LAST: {ld['action']}  conf {ld['confidence']:.2f}  @ {ld['close']:g}")
        rat = ld.get("rationale", "")
        for chunk in _wrap(rat, 44):
            lines.append(f"  {chunk}")
    lines.append("recent:")
    for r in d.get("recent_decisions", [])[:6]:
        lines.append(f"  {_hhmmss(r['ts'])}  {r['action']:<11} {r['confidence']:.2f}  @{r['close']:g}")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out[:3]  # cap at 3 lines


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hermes Trading Agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
        --grn:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,monospace;font-size:14px}
  header{display:flex;justify-content:space-between;align-items:center;padding:12px 18px;
    border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:16px;margin:0;letter-spacing:.5px}
  #conn{font-size:12px;color:var(--dim)}
  .wrap{padding:16px;max-width:900px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
  .card .label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
  .card .val{font-size:20px;font-weight:600;margin-top:4px}
  .last{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:16px}
  .last .act{font-size:22px;font-weight:700}
  .last .rat{color:var(--dim);margin-top:6px;line-height:1.4}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);font-size:13px}
  th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase}
  tr:last-child td{border-bottom:none}
  .grn{color:var(--grn)} .red{color:var(--red)} .amber{color:var(--amber)} .blue{color:var(--blue)} .dim{color:var(--dim)}
  .pill{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
</style></head>
<body>
<header><h1>HERMES · <span id="inst">—</span></h1><div id="conn">connecting…</div></header>
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="label">Position</div><div class="val" id="pos">—</div></div>
    <div class="card"><div class="label">Realized P&L</div><div class="val" id="rpnl">—</div></div>
    <div class="card"><div class="label">Trades / Goal</div><div class="val" id="trades">—</div></div>
    <div class="card"><div class="label">Data age</div><div class="val" id="age">—</div></div>
  </div>
  <div class="last"><div class="act" id="lact">—</div><div class="rat" id="lrat"></div></div>
  <table><thead><tr><th>Time</th><th>Action</th><th>Conf</th><th>Close</th><th>Order</th><th>Rationale</th></tr></thead>
    <tbody id="rows"></tbody></table>
</div>
<script>
function fmt(n,s){return (n>=0?'+':'')+n.toFixed(2)+(s||'')}
function cls(n){return n>0?'grn':n<0?'red':'dim'}
async function tick(){
  try{
    const d=await (await fetch('/dashboard',{cache:'no-store'})).json();
    document.getElementById('conn').textContent='● live · '+(d.agent)+'/'+(d.brain);
    document.getElementById('conn').className='grn';
    document.getElementById('inst').textContent=d.instrument+' '+d.timeframe;
    const s=d.session;
    const pos=s.position===0?'FLAT':(s.position>0?'LONG ':'SHORT ')+Math.abs(s.position)+' @'+s.avg_price;
    const pe=document.getElementById('pos'); pe.textContent=pos;
    pe.className='val '+(s.position>0?'grn':s.position<0?'red':'dim');
    const re=document.getElementById('rpnl'); re.textContent=fmt(s.realized_pnl); re.className='val '+cls(s.realized_pnl);
    document.getElementById('trades').textContent=s.trades_today+' · +'+d.goal.profit_target+'/-'+d.goal.max_daily_loss;
    const age=d.data_age_seconds; const ae=document.getElementById('age');
    ae.textContent=(age==null?'?':Math.round(age)+'s'); ae.className='val '+(age==null?'dim':age>120?'red':'grn');
    const ld=d.last_decision;
    if(ld){const a=document.getElementById('lact');
      a.textContent=ld.action+'  ·  conf '+ld.confidence.toFixed(2)+(s.halted?'  ·  HALTED':'');
      a.className='act '+(ld.action.indexOf('LONG')>=0?'grn':ld.action.indexOf('SHORT')>=0||ld.action=='EXIT'||ld.action=='FLATTEN'?'red':'dim');
      document.getElementById('lrat').textContent=ld.rationale||'';}
    const rows=(d.recent_decisions||[]).map(r=>{
      const t=new Date(r.ts*1000).toLocaleTimeString();
      const ac=r.action.indexOf('LONG')>=0?'grn':(r.action.indexOf('SHORT')>=0||r.action=='EXIT'||r.action=='FLATTEN')?'red':'dim';
      return `<tr><td class=dim>${t}</td><td class=${ac}>${r.action}</td><td>${r.confidence.toFixed(2)}</td>`
        +`<td>${r.close}</td><td class=blue>${r.queued||''}</td><td class=dim>${r.rationale||''}</td></tr>`;
    }).join('');
    document.getElementById('rows').innerHTML=rows;
  }catch(e){const c=document.getElementById('conn');c.textContent='● disconnected';c.className='red';}
}
tick(); setInterval(tick,3000);
</script>
</body></html>"""
