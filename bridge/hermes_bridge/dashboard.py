"""Dashboard rendering: a plain-text panel (for terminal / CLI viewing via
/dashboard.txt + /panel.txt) and a self-contained auto-refreshing HTML page (the
primary dashboard, opened in a browser or the NinjaTrader WebView2 window). Both
consume the dict produced by the /dashboard endpoint, so the format lives in one place.
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
    return f"{side} {abs(position)} @ {avg_price:.10g}"


def _strategy_view(d: dict) -> tuple[str, list[dict]]:
    """(label, setups) for the strategy section: ``label`` is a short status word and
    ``setups`` is the list of ``{name, regime, summary, active}`` the agent authored.
    Falls back to "authoring…" (agent mode, none yet) / "custom playbooks" / source when
    the list is empty."""
    strat = d.get("strategy") or {}
    source = strat.get("source") or d.get("strategy_source") or "?"
    setups = strat.get("list") or []
    if setups:
        return source, setups
    if source == "agent":
        return "authoring…", []
    if source == "custom":
        return "custom playbooks", []
    return source, []


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

    slabel, setups = _strategy_view(d)
    lines = [
        f"HERMES  {d['instrument']} {d['timeframe']}  ({d['agent']}/{d['brain']})",
        f"account: {d.get('account', '?')}   strategy: {slabel}",
    ]
    for it in setups:  # list every authored setup; ▸ marks the one active for the regime
        mark = "▸" if it.get("active") else "·"
        reg = f" [{it['regime']}]" if it.get("regime") else ""
        lines.append(f"  {mark} {it.get('name', '')}{reg}"[:58])
    _active = next((it for it in setups if it.get("active")), None)
    _sum_of = _active or (setups[0] if len(setups) == 1 else None)
    if _sum_of and _sum_of.get("summary"):
        lines.append(f"    ↳ {_sum_of['summary']}"[:58])
    authored = (d.get("strategy") or {}).get("authored")
    if authored:  # watch the count tick to confirm the playbook is actually re-authoring
        ago = authored.get("bars_ago")
        ago_str = f"{ago}b ago" if ago is not None else "just now"
        line = f"  authored {authored.get('count', 0)}× · {ago_str}"
        if authored.get("reason"):
            line += f" · {authored['reason']}"
        lines.append(line[:58])
    lines += [
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
    nw = d.get("news")
    if nw and nw.get("enabled"):
        if nw.get("blackout_active"):
            lines.append(f"news: BLACKOUT {nw.get('active_event', '')}"[:60])
        elif not nw.get("ok"):
            lines.append("news: feed down (trading)"[:60])
        else:
            nxt = nw.get("next_event")
            tail = f" · next {nxt['title']} {_hhmmss(nxt['ts'])}" if nxt else ""
            lines.append(f"news: clear{tail}"[:60])
    lines.append("-" * 40)
    ld = d.get("last_decision")
    if ld:
        lines.append(f"LAST: {ld['action']}  conf {ld['confidence']:.2f}  @ {ld['close']:.10g}")
        rat = ld.get("rationale", "")
        for chunk in _wrap(rat, 44):
            lines.append(f"  {chunk}")
    lines.append("recent:")
    for r in d.get("recent_decisions", [])[:6]:
        lines.append(
            f"  {_hhmmss(r['ts'])}  {r['action']:<11} {r['confidence']:.2f}  @{r['close']:.10g}"
        )
    return "\n".join(lines)


def _oneline(value: object) -> str:
    """Collapse whitespace/newlines so a value stays on its key=value line."""
    return " ".join(str(value).split())


def render_panel(d: dict | None) -> str:
    """Structured key=value snapshot for the HermesDashboard card indicator.

    Same philosophy as /levels.txt: the NinjaScript side stays a dumb line parser —
    no JSON in C#. One `key=value` per line; recent decisions are `row=` lines with
    pipe-separated fields. `plan_*` keys are emitted only when the payload carries an
    armed-plan object (forward-compat for the analysis/execution-split bridge).
    """
    if not d:
        return "ok=0"
    s = d["session"]
    goal = d["goal"]
    age = d.get("data_age_seconds")
    lb = d.get("last_bar")
    lines = [
        "ok=1",
        f"instrument={d['instrument']}",
        f"timeframe={d['timeframe']}",
        f"agent={d['agent']}",
        f"model={d.get('model', '')}",
        f"strategy_id={d['strategy_id']}",
        f"strategy_source={d.get('strategy_source', '')}",
        f"account={d.get('account', '')}",
        f"age_s={age:.0f}" if age is not None else "age_s=",
        # NB: prices use :.10g, not :g — :g truncates to 6 significant digits, which
        # is wrong by a tick+ at MNQ levels (f"{21512.75:g}" -> "21512.8").
        f"last_close={lb['close']:.10g}" if lb else "last_close=",
        f"position={s['position']}",
        f"avg_price={s['avg_price']:.10g}",
        f"realized={s['realized_pnl']:.2f}",
        f"unrealized={s['unrealized_pnl']:.2f}",
        f"trades={s['trades_today']}",
        f"halted={1 if s['halted'] else 0}",
        f"halt_reason={_oneline(s['halt_reason'])}",
        f"goal_hit={1 if s['daily_goal_hit'] else 0}",
        f"goal_target={goal['profit_target']:.0f}",
        f"goal_loss={goal['max_daily_loss']:.0f}",
        f"stale_drops={d.get('stale_drops', 0)}",
    ]
    # Agent-authored strategy. The headline name/summary (= the active setup) feed the
    # folded card / legacy fallback; one `strategy_row=` line per setup feeds the card's
    # list (pipe-separated name|regime|summary|active, `|` stripped from the free text).
    strat = d.get("strategy") or {}
    if strat.get("name"):
        lines.append(f"strategy_name={_oneline(strat['name'])}")
    if strat.get("summary"):
        lines.append(f"strategy_summary={_oneline(strat['summary'])}")
    if strat.get("active_source"):
        # "declared" = the brain named this setup in its plan; "regime" = regime fallback.
        lines.append(f"strategy_active_source={strat['active_source']}")
    # Authoring telemetry: watch authored_count tick to confirm the playbook is refreshing;
    # authored_bars_ago / authored_reason say how long ago and why the latest one was authored.
    authored = strat.get("authored")
    if authored:
        lines.append(f"strategy_authored_count={authored.get('count', 0)}")
        if authored.get("bars_ago") is not None:
            lines.append(f"strategy_authored_bars_ago={authored['bars_ago']}")
        if authored.get("reason"):
            lines.append(f"strategy_authored_reason={_oneline(authored['reason'])}")
    # Planner / authoring health — the card had no signal for "analyzing_session" or a failed
    # study, so a re-author that never landed was invisible. Surface it here.
    pl = d.get("planner") or {}
    if pl.get("status"):
        lines.append(f"planner_status={_oneline(pl['status'])}")
    if pl.get("last_error"):
        lines.append(f"planner_error={_oneline(pl['last_error'])}")
    if pl.get("session_error"):
        lines.append(f"session_error={_oneline(pl['session_error'])}")
    # Major-news blackout (emitted only when the feature is on).
    nw = d.get("news") or {}
    if nw.get("enabled"):
        lines.append("news_enabled=1")
        lines.append(f"news_ok={1 if nw.get('ok') else 0}")
        lines.append(f"news_blackout={1 if nw.get('blackout_active') else 0}")
        if nw.get("active_event"):
            lines.append(f"news_active={_oneline(nw['active_event'])}")
        nxt = nw.get("next_event")
        if nxt:
            lines.append(f"news_next={_oneline(nxt.get('title', ''))}")
    for it in (strat.get("list") or []):
        name = _oneline(it.get("name", "")).replace("|", "/")
        regime = _oneline(it.get("regime", "")).replace("|", "/")
        summary = _oneline(it.get("summary", "")).replace("|", "/")
        active = "1" if it.get("active") else "0"
        lines.append(f"strategy_row={name}|{regime}|{summary}|{active}")
    ld = d.get("last_decision")
    if ld:
        lines += [
            f"ld_time={_hhmmss(ld['ts'])}",
            f"ld_action={ld['action']}",
            f"ld_conf={ld['confidence']:.2f}",
            f"ld_close={ld['close']:.10g}",
            f"ld_order={ld.get('queued') or ''}",
            f"ld_rationale={_oneline(ld.get('rationale', ''))}",
        ]
    for r in d.get("recent_decisions", [])[:8]:
        lines.append(
            f"row={_hhmmss(r['ts'])}|{r['action']}|{r['confidence']:.2f}"
            f"|{r['close']:.10g}|{r.get('queued') or ''}"
        )
    plan = d.get("plan")
    if isinstance(plan, dict):
        for key in ("status", "direction", "entry_high", "entry_low", "bars_left", "note"):
            value = plan.get(key)
            if value is not None:
                lines.append(f"plan_{key}={_oneline(value)}")
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
  .strat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:12px 14px;margin-bottom:16px}
  .strat .shead{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .strat .slabel{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
  .sbtn{font:inherit;font-size:11px;color:var(--dim);background:#21262d;border:1px solid var(--line);
    border-radius:6px;padding:3px 9px;cursor:pointer}
  .sbtn:hover{color:var(--fg);border-color:var(--blue)}
  .sbtn:disabled{opacity:.5;cursor:default}
  .srow{border:1px solid var(--line);border-left:3px solid var(--line);border-radius:6px;
    padding:8px 10px;margin-top:6px}
  .srow.active{border-left-color:var(--grn);background:rgba(63,185,80,.08)}
  .srow .sname{font-size:15px;font-weight:600;color:var(--fg)}
  .srow.active .sname{color:var(--grn)}
  .srow .ssum{color:var(--dim);margin-top:3px;line-height:1.35;font-size:13px}
  .schip{display:inline-block;padding:0 7px;border-radius:10px;font-size:10px;font-weight:600;
    margin-left:8px;background:#21262d;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
  .srow.active .schip{background:rgba(63,185,80,.18);color:var(--grn)}
  .news{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--line);
    border-radius:8px;padding:10px 14px;margin-bottom:16px}
  .news.blk{border-color:var(--red);background:rgba(248,81,73,.10)}
  .news .nlabel{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
</style></head>
<body>
<header><h1>HERMES · <span id="inst">—</span></h1><div id="conn">connecting…</div></header>
<div class="wrap">
  <div class="strat" id="strat" style="display:none">
    <div class="shead">
      <span class="slabel" id="slabel">Agent strategies</span>
      <button class="sbtn" id="reauthor" title="Discard the current playbook and author a fresh one from history" style="display:none">Re-author</button>
    </div>
    <div id="slist"></div>
  </div>
  <div class="cards">
    <div class="card"><div class="label">Position</div><div class="val" id="pos">—</div></div>
    <div class="card"><div class="label">Realized P&L</div><div class="val" id="rpnl">—</div></div>
    <div class="card"><div class="label">Trades / Goal</div><div class="val" id="trades">—</div></div>
    <div class="card"><div class="label">Data age</div><div class="val" id="age">—</div></div>
  </div>
  <div class="news" id="news" style="display:none">
    <span class="nlabel">News</span><span id="nstatus" class="dim"></span>
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
    document.getElementById('conn').textContent='● live · '+(d.agent)+'/'+(d.brain)+(d.account?' · '+d.account:'')+(d.strategy_source?' · '+d.strategy_source:'');
    document.getElementById('conn').className='grn';
    document.getElementById('inst').textContent=d.instrument+' '+d.timeframe;
    // Agent-authored strategies: list every setup the agent wrote, highlight the one
    // whose regime matches the live market. Built with textContent (agent-authored text).
    const strat=d.strategy||{}; const se=document.getElementById('strat');
    const setups=strat.list||[]; const sl=document.getElementById('slist');
    document.getElementById('reauthor').style.display=strat.source==='agent'?'':'none';
    if(setups.length){
      // The active setup is the one the brain declared in its plan (active_source
      // 'declared') or, failing that, the one matching the live regime ('regime').
      const declared=strat.active_source==='declared';
      const aname=(strat.active_index!=null&&setups[strat.active_index])?setups[strat.active_index].name:null;
      const ctx=declared&&aname?' · trading '+aname:(strat.regime?' · '+strat.regime+' now':'');
      const au=strat.authored;
      const aud=au?(' · authored '+au.count+'×'+(au.bars_ago!=null?' ('+au.bars_ago+'b ago)':'')):'';
      document.getElementById('slabel').textContent='Agent strategies'+ctx+aud;
      sl.innerHTML='';
      setups.forEach(it=>{
        const row=document.createElement('div'); row.className='srow'+(it.active?' active':'');
        const head=document.createElement('div');
        const nm=document.createElement('span'); nm.className='sname'; nm.textContent=it.name||''; head.appendChild(nm);
        if(it.regime){const c=document.createElement('span'); c.className='schip'; c.textContent=it.regime; head.appendChild(c);}
        if(it.active){const a=document.createElement('span'); a.className='schip'; a.textContent=declared?'trading':'active'; head.appendChild(a);}
        row.appendChild(head);
        if(it.summary){const su=document.createElement('div'); su.className='ssum'; su.textContent=it.summary; row.appendChild(su);}
        sl.appendChild(row);
      });
      se.style.display='';
    }else if(strat.source==='agent'){
      document.getElementById('slabel').textContent='Agent strategies';
      sl.innerHTML='<div class="srow"><div class="sname dim">authoring…</div>'
        +'<div class="ssum">waiting for the pre-session study to write the playbook</div></div>';
      se.style.display='';
    }else{ se.style.display='none'; }
    const s=d.session;
    const pos=s.position===0?'FLAT':(s.position>0?'LONG ':'SHORT ')+Math.abs(s.position)+' @'+s.avg_price;
    const pe=document.getElementById('pos'); pe.textContent=pos;
    pe.className='val '+(s.position>0?'grn':s.position<0?'red':'dim');
    const re=document.getElementById('rpnl'); re.textContent=fmt(s.realized_pnl); re.className='val '+cls(s.realized_pnl);
    document.getElementById('trades').textContent=s.trades_today+' · +'+d.goal.profit_target+'/-'+d.goal.max_daily_loss;
    const age=d.data_age_seconds; const ae=document.getElementById('age');
    ae.textContent=(age==null?'?':Math.round(age)+'s'); ae.className='val '+(age==null?'dim':age>120?'red':'grn');
    // Major-news blackout. textContent only (feed titles are third-party) → no HTML injection.
    const nw=d.news; const ne=document.getElementById('news'); const ns=document.getElementById('nstatus');
    if(nw&&nw.enabled){
      ne.style.display='';
      if(nw.blackout_active){
        ne.className='news blk'; ns.className='red';
        ns.textContent='⛔ BLACKOUT · '+(nw.active_event||'high-impact event');
      }else if(!nw.ok){
        ne.className='news'; ns.className='amber';
        ns.textContent='feed unavailable — trading (fail-open)'+(nw.error?' · '+nw.error:'');
      }else{
        ne.className='news'; ns.className='grn';
        let t='clear ('+(nw.event_count||0)+' today)';
        if(nw.next_event){const nt=new Date(nw.next_event.ts*1000).toLocaleTimeString();
          t+=' · next: '+nw.next_event.currency+' '+nw.next_event.title+' @ '+nt;}
        ns.textContent=t;
      }
    }else{ ne.style.display='none'; }
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
document.getElementById('reauthor').addEventListener('click',async function(){
  const b=this; if(!confirm('Discard the current playbook and author a fresh one from history?'))return;
  b.disabled=true; const was=b.textContent; b.textContent='re-authoring…';
  try{
    const r=await (await fetch('/control/reauthor',{method:'POST'})).json();
    if(!r.ok) alert('Re-author: '+(r.note||'failed'));
  }catch(e){ alert('Re-author failed: '+e); }
  finally{ setTimeout(()=>{b.disabled=false; b.textContent=was;}, 1500); tick(); }
});
tick(); setInterval(tick,3000);
</script>
</body></html>"""
