"""
dashboard_goldbase.py -- GoldBase A.I. dashboard (Benchmark Desk)
Port 5033. Single-instrument (Gold XAU/USD). Prominent WITH/AGAINST switch (live
reload), price, position, P&L, Lancelot, SSL signal, session. All times UTC.
"""
import csv
import os
import signal
import threading
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

import direction_switch

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"
_VER = BASE_DIR / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"

# ── Environment label (Part 2a): TEST (Dell, amber) / LIVE (K1, green). Set ENV_LABEL in .env. ──
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass
ENV_LABEL = os.getenv("ENV_LABEL", "TEST").strip().upper()
if ENV_LABEL == "LIVE":
    _ENV_BADGE = ('<span style="background:#12331b;color:#3fb950;border:1px solid #2ea043;border-radius:5px;'
                  'padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:1px;">LIVE &mdash; K1</span>')
else:
    _ENV_BADGE = ('<span style="background:#3a2f00;color:#e0b020;border:1px solid #6b5600;border-radius:5px;'
                  'padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:1px;">TEST &mdash; Dell</span>')
PORT = 5033

logging.basicConfig(level=logging.WARNING)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("dashboard")
app = Flask(__name__)
_state = {"system": "GoldBase", "version": APP_VERSION}


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>GoldBase A.I.</title>
<style>
:root{--bg:#0d0d0f;--bg2:#16161a;--bd:#2a2a30;--tx:#e6edf3;--mut:#8b949e;
--gold:#FFD700;--green:#3fb950;--red:#f85149;--amber:#d29922;}
*{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--tx);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;}
header{background:var(--bg2);border-bottom:2px solid var(--gold);padding:10px 18px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.brand{font-size:18px;font-weight:800;color:var(--gold);letter-spacing:1px;}
.brand small{color:var(--mut);font-size:11px;font-weight:400;letter-spacing:0;margin-left:8px;}
.clock{font-family:monospace;color:var(--gold);font-weight:700;}
.wrap{max-width:820px;margin:0 auto;padding:18px;}
.switch-bar{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
.switch-bar .lbl{font-size:13px;font-weight:700;letter-spacing:0.5px;color:var(--mut);text-transform:uppercase;}
.sw-btn{font-size:15px;font-weight:800;letter-spacing:1px;padding:9px 26px;border-radius:8px;cursor:pointer;background:#1e1e1e;color:#aaa;border:2px solid #444;}
.sw-btn:hover{background:#262626;}
.sw-btn.on-WITH{background:rgba(63,185,80,0.20);color:var(--green);border-color:var(--green);}
.sw-btn.on-AGAINST{background:rgba(248,81,73,0.22);color:var(--red);border-color:var(--red);}
.sw-meta{color:var(--mut);font-size:11px;margin-left:auto;}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:16px 18px;}
.price{font-size:30px;font-weight:800;color:var(--gold);}
.row{display:flex;justify-content:space-between;padding:5px 0;font-size:13px;border-bottom:1px solid rgba(255,255,255,0.04);}
.row .k{color:var(--mut);} .bull{color:var(--green);} .bear{color:var(--red);} .mut{color:var(--mut);}
.pos-long{color:var(--green);font-weight:700;} .pos-short{color:var(--red);font-weight:700;}
.lanc-clear{color:var(--green);} .lanc-block{color:var(--amber);} .lanc-trade{color:var(--gold);}
.note{color:var(--mut);font-size:10px;margin-top:14px;text-align:center;line-height:1.5;}
.nav{display:flex;align-items:center;gap:10px;}
.navbtn{font-size:12px;font-weight:700;color:var(--gold);background:rgba(255,255,255,0.06);border:1px solid var(--gold);padding:5px 12px;border-radius:6px;cursor:pointer;text-decoration:none;}
.navbtn:hover{background:rgba(255,255,255,0.12);}
table.tr{width:100%;border-collapse:collapse;font-size:12px;min-width:520px;}
table.tr th{text-align:left;color:var(--mut);border-bottom:1px solid var(--bd);padding:7px 8px;font-weight:600;}
table.tr td{padding:6px 8px;border-bottom:1px solid rgba(255,255,255,0.04);}
.win{color:var(--green);font-weight:700;} .loss{color:var(--red);font-weight:700;}
</style></head><body>
<header>
  <div class="brand">GOLD<span style="color:#fff">BENCHMARK</span> A.I.
    <small>__VER__ &middot; port 5033 &middot; Gold XAU/USD &middot; Lancelot + 3-TF SSL + switch</small></div>
  <div class="nav">
    __ENV__
    <button class="navbtn" id="toPnl" onclick="showPage(2)">P&amp;L &rarr;</button>
    <div class="clock" id="clock">--:--:-- UTC</div>
  </div>
</header>
<div class="wrap">
  <div id="page1">
  <div class="switch-bar">
    <span class="lbl">Direction Switch</span>
    <button class="sw-btn" id="swWITH" onclick="setDir('WITH')">WITH</button>
    <button class="sw-btn" id="swAGAINST" onclick="setDir('AGAINST')">AGAINST</button>
    <span class="sw-meta" id="swMeta">--</span>
  </div>
  <div class="card"><div id="body">Awaiting engine...</div></div>
  <div class="note">Benchmark Desk &mdash; pure Lancelot + 3-timeframe SSL agreement, traded WITH or AGAINST.
    No Arthur, Morgan, Guinevere or phantom logging. Paper trading only.</div>
  </div><!-- /page1 -->
  <div id="page2" style="display:none;">
    <div style="margin-bottom:14px;"><button class="navbtn" onclick="showPage(1)">&larr; Back to Dashboard</button></div>
    <div class="card">
      <div style="font-size:16px;font-weight:800;color:var(--gold);margin-bottom:12px;">Trade History &mdash; P&amp;L</div>
      <div id="pnlBody" style="overflow-x:auto;">Loading...</div>
    </div>
  </div><!-- /page2 -->
</div>
<script>
function clk(){var t=new Date();document.getElementById('clock').textContent=
  String(t.getUTCHours()).padStart(2,'0')+':'+String(t.getUTCMinutes()).padStart(2,'0')+':'+String(t.getUTCSeconds()).padStart(2,'0')+' UTC';}
setInterval(clk,1000);clk();
function row(k,v,cls){return '<div class="row"><span class="k">'+k+'</span><span class="'+(cls||'')+'">'+v+'</span></div>';}
function money(v){if(v===null||v===undefined)return '--';var n=Number(v);return (n<0?'-£':'+£')+Math.abs(n).toFixed(2);}
function renderDir(m){
  var mode=(m&&m.mode)||'WITH';
  document.getElementById('swWITH').className='sw-btn'+(mode==='WITH'?' on-WITH':'');
  document.getElementById('swAGAINST').className='sw-btn'+(mode==='AGAINST'?' on-AGAINST':'');
  document.getElementById('swMeta').textContent='Active: '+mode+(m&&m.set_at?' (set '+m.set_at+')':'');
}
function setDir(mode){
  fetch('/api/direction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode,by:'Nick'})})
    .then(function(r){return r.json();}).then(renderDir).catch(function(e){console.error(e);});
}
function poll(){
  fetch('/api/state').then(function(r){return r.json();}).then(function(d){
    var h='';
    h+='<div class="price">$'+(d.price!=null?Number(d.price).toFixed(2):'--')+'</div>';
    if(d.in_trade && d.position){
      var p=d.position;var dc=p.direction==='LONG'?'pos-long':'pos-short';
      h+=row('Position','<span class="'+dc+'">'+p.direction+'</span>');
      h+=row('Entry','$'+Number(p.entry).toFixed(2));
      h+=row('Stop / Target','$'+Number(p.stop).toFixed(2)+' / $'+Number(p.target).toFixed(2));
      h+=row('Stake','£'+Number(p.stake).toFixed(2)+'/pt'+(p.ladder_step>0?' &middot; ladder step '+p.ladder_step:''));
      h+=row('Floating P&amp;L',money(p.floating_gbp),Number(p.floating_gbp)>=0?'bull':'bear');
    } else {
      h+=row('Position','<span class="mut">FLAT</span>');
      h+=row('SSL signal (D+1h+5m)',d.signal||'--');
    }
    var lc=String(d.lancelot||'--');
    var lcls=lc.indexOf('CLEAR')===0?'lanc-clear':(lc.indexOf('IN TRADE')===0?'lanc-trade':'lanc-block');
    h+=row('Lancelot','<span class="'+lcls+'">'+lc+'</span>');
    var pf=d.portfolio||{};
    h+=row("Today's P&amp;L",money(pf.today_pnl),Number(pf.today_pnl)>=0?'bull':'bear');
    h+=row('Balance','£'+Number(pf.balance||0).toFixed(2));
    h+=row('Session',d.session||'--','mut');
    h+=row('Updated',(d.updated_utc||'--')+' UTC','mut');
    document.getElementById('body').innerHTML=h;
    if(d.mode){renderDir({mode:d.mode});}
  }).catch(function(e){});
  fetch('/api/direction').then(function(r){return r.json();}).then(renderDir).catch(function(e){});
}
poll();setInterval(poll,5000);
function showPage(n){
  document.getElementById('page1').style.display=(n===1?'':'none');
  document.getElementById('page2').style.display=(n===2?'':'none');
  if(n===2){loadTrades();}
}
function loadTrades(){
  fetch('/api/trades').then(function(r){return r.json();}).then(function(d){
    var t=d.trades||[];
    if(!t.length){document.getElementById('pnlBody').innerHTML='<div class="mut">No trades recorded yet.</div>';return;}
    var h='<table class="tr"><thead><tr><th>Date</th><th>Time</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Result</th></tr></thead><tbody>';
    for(var i=0;i<t.length;i++){var r=t[i];
      var rc=r.result==='WIN'?'win':(r.result==='LOSS'?'loss':'mut');
      h+='<tr><td>'+r.date+'</td><td>'+r.time+'</td><td>'+r.direction+'</td><td>'+r.entry+'</td><td>'+r.exit+'</td>'+
         '<td class="'+rc+'">'+money(r.pnl_gbp)+'</td><td class="'+rc+'">'+r.result+'</td></tr>';
    }
    h+='</tbody></table>';
    document.getElementById('pnlBody').innerHTML=h;
  }).catch(function(e){document.getElementById('pnlBody').innerHTML='<div class="loss">Error loading trades.</div>';});
}
</script>
</body></html>"""


TRADES_CSV = BASE_DIR / "logs" / "gold_trades.csv"


def _read_trades(path, limit=300):
    """Read a Stanley trade-history CSV into normalized rows (most recent first)."""
    rows = []
    if not path.exists():
        return rows
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pnl = r.get("pnl_gbp")
                try:
                    pnlf = round(float(pnl), 2)
                except (TypeError, ValueError):
                    pnlf = None
                rows.append({
                    "date": r.get("date", ""), "time": r.get("time", ""),
                    "direction": r.get("direction", ""),
                    "entry": r.get("entry_price_usd") or r.get("entry_price") or "",
                    "exit": r.get("exit_price_usd") or r.get("exit_price") or "",
                    "pnl_gbp": pnlf,
                    "result": ("WIN" if pnlf >= 0 else "LOSS") if pnlf is not None else "--",
                    "reason": r.get("exit_reason", ""),
                })
    except Exception:
        pass
    return rows[-limit:][::-1]


@app.route("/")
def index():
    return HTML.replace("__VER__", "v" + APP_VERSION).replace("__ENV__", _ENV_BADGE)


@app.route("/api/update", methods=["POST"])
def api_update():
    try:
        _state.update(request.get_json(force=True, silent=True) or {})
        _state["received_utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/state")
def api_state():
    return jsonify(_state)


@app.route("/api/direction", methods=["GET", "POST"])
def api_direction():
    if request.method == "GET":
        return jsonify(direction_switch.get_state())
    try:
        body = request.get_json(force=True, silent=True) or {}
        by = str(body.get("by") or "Nick").strip() or "Nick"
        return jsonify(direction_switch.set_mode(body.get("mode"), set_by=by))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/trades")
def api_trades():
    return jsonify({"trades": _read_trades(TRADES_CSV)})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Write the shutdown flag for the engine + watchdog, then kill this dashboard.
    Used by the BenchmarkRoundTable SHUTDOWN ALL fan-out (POST /api/shutdown)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SHUTDOWN_FLAG.write_text("shutdown requested\n", encoding="utf-8")
        log.info("Shutdown flag written -- engine will exit on next check")
    except Exception as e:
        log.warning("Could not write shutdown flag: %s", e)

    def _kill():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"status": "shutting_down"})


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "system": "GoldBase",
                    "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    print("GoldBase dashboard -> http://localhost:%d" % PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
