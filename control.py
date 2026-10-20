#!/usr/bin/env python3
"""Control panel for the bot: start/stop paper trading, replay from a past date, see P&L.
Standard library only. Run from the venv:  venv/bin/python control.py
Set CONTROL_PASSWORD (required for any non-local --host)."""
import argparse, base64, hmac, json, os, re, signal, sqlite3, subprocess, sys, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "config.toml")
DB = os.path.join(ROOT, "data", "bot.db")
LOG = os.path.join(ROOT, "data", "control.log")
PIDF = os.path.join(ROOT, "data", "control.pid")
lock = threading.Lock()
proc = None
live = {"price": None, "ts": None, "err": None, "sym": ""}


def cfgval(cfg, k):
    m = re.search(rf'^\s*{k}\s*=\s*"?([^"#\n]*?)"?\s*(#.*)?$', cfg, re.M)
    return m.group(1) if m else ""


def fmt(n):
    return str(int(n)) if float(n) == int(n) else str(round(float(n), 2))


def poll_live():
    """Background thread: keeps the latest SPY (or configured symbol) price fresh."""
    while True:
        wait = 15
        try:
            import yfinance as yf
            sym = cfgval(open(CFG).read(), "symbol") or "SPY"
            t = yf.Ticker(sym)
            try:
                price = float(t.fast_info["last_price"])
            except Exception:
                price = float(t.history(period="1d", interval="1m")["Close"].iloc[-1])
            live.update(price=price, ts=datetime.now().strftime("%H:%M:%S"), err=None, sym=sym)
        except Exception as e:
            live["err"] = str(e)[:120]
            wait = 60
        time.sleep(wait)


def alive():
    """pid of the running bot process, or None."""
    if proc is not None and proc.poll() is None:
        return proc.pid
    if proc is not None:
        return None
    try:  # server restarted: look at the pidfile
        pid = int(open(PIDF).read().split()[0])
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def set_key(text, section, key, value):
    out, cur, done = [], None, False
    for line in text.split("\n"):
        m = re.match(r"\s*\[(\w+)\]", line)
        if m:
            cur = m.group(1)
        if cur == section and not done and re.match(rf"\s*{key}\s*=", line):
            cm = re.search(r"\s+#.*$", line)
            line = f"{key} = {value}" + (cm.group(0) if cm else "")
            done = True
        out.append(line)
    if not done:
        raise ValueError(f"[{section}] {key} not found in config.toml")
    return "\n".join(out)


def launch(mode, cleanup=None):
    global proc
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "run_bot.py"), mode],
        cwd=ROOT, stdout=open(LOG, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    open(PIDF, "w").write(f"{proc.pid} {mode}")
    if cleanup:
        p = proc
        threading.Thread(target=lambda: (p.wait(), cleanup()), daemon=True).start()


def start(req):
    with lock:
        if alive():
            return "Already running. Stop it first."
        orig = open(CFG).read()
        txt, note, mode, cash = orig, "", req.get("mode"), req.get("cash")
        if cash not in (None, ""):
            cash = float(cash)
            if not 0 < cash < 1e9:
                return "Starting cash must be a positive number."
            if mode == "paper" and not req.get("fresh") and cash != float(cfgval(orig, "starting_cash") or 0):
                return "A new starting cash only applies to a fresh account. Tick 'start a fresh account'."
            txt = set_key(txt, "sim", "starting_cash", fmt(cash))
            mpv = float(cfgval(orig, "max_position_value") or 0)
            if 0 < mpv < cash:
                txt = set_key(txt, "risk", "max_position_value", fmt(cash))
                note = " Raised max_position_value to match."
            mos = float(cfgval(orig, "max_order_shares") or 0)
            if mos and live.get("price") and cash / live["price"] > mos:
                note += " Orders may be capped by max_order_shares."
        if mode == "paper":
            if req.get("fresh"):
                name = "paper-" + datetime.now().strftime("%Y%m%d-%H%M")
                txt = set_key(txt, "run", "name", json.dumps(name))
            if txt != orig:
                open(CFG, "w").write(txt)
            launch("paper")
            return "Paper trading started." + note
        if mode == "replay":
            since = str(req.get("since", ""))
            datetime.strptime(since, "%Y-%m-%d")
            txt = set_key(txt, "data", "start", json.dumps(since))
            if req.get("nohalt"):
                txt = set_key(txt, "risk", "max_daily_loss_pct", "0")
            open(CFG, "w").write(txt)
            launch("replay", cleanup=lambda: open(CFG, "w").write(orig))
            return f"Replay started from {since}." + note
        return "Unknown mode."


def stop():
    with lock:
        pid = alive()
        if not pid:
            return "Nothing is running."
        os.kill(pid, signal.SIGTERM)
        return "Stop signal sent."


def pick_run(c, name):
    """Run id for a run name, using whichever table holds run names (None if not found)."""
    for (t,) in c.execute("select name from sqlite_master where type='table'").fetchall():
        cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]
        low = [x.lower() for x in cols]
        idc = next((x for x in cols if x.lower() in ("id", "run_id")), None)
        nc = next((x for x in cols if x.lower() in ("name", "run_name")), None)
        if idc and nc and "equity" not in low:
            r = c.execute(f'select "{idc}" from "{t}" where "{nc}"=? order by "{idc}" desc limit 1', (name,)).fetchone()
            if r:
                return r[0]
    return None


def pnl(cash0, name, mode):
    """Best effort: find the table with an equity column and read the run being shown
    (the paper account when paper was last started, otherwise the latest run)."""
    if not os.path.exists(DB):
        return None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rid = pick_run(c, name) if mode == "paper" else None
        seen = {}
        for (t,) in c.execute("select name from sqlite_master where type='table'").fetchall():
            cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]
            seen[t] = cols
            pick = lambda names: next((x for x in cols if x.lower() in names), None)
            eq = pick(("equity", "total_equity", "nav"))
            if not eq:
                continue
            tc = pick(("ts", "date", "time", "timestamp", "bar_date", "dt")) or "rowid"
            cash_c, close_c = pick(("cash", "cash_balance")), pick(("close", "price", "last_price", "px"))
            extra = [x for x in (cash_c, close_c) if x]
            sel = ", ".join([tc, f'"{eq}"'] + [f'"{x}"' for x in extra])
            if "run_id" in cols:
                where = f"where run_id={int(rid)}" if rid is not None else f'where run_id=(select max(run_id) from "{t}")'
            else:
                where = ""
            rows = c.execute(f'select {sel} from "{t}" {where} order by rowid').fetchall()
            if not rows:
                if rid is not None:  # paper account exists but has no bars yet: all cash
                    return {"equity": cash0, "base": cash0, "pnl": 0, "pct": 0, "as_of": "no bars yet",
                            "points": [], "cols": cols, "cash_now": cash0, "close": 1.0}
                continue
            step = max(1, len(rows) // 300)
            pts = rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])
            last, base = rows[-1], rows[0][1]
            vals = dict(zip(extra, last[2:]))
            return {"equity": last[1], "base": base, "pnl": last[1] - base,
                    "pct": (last[1] / base - 1) * 100 if base else 0, "as_of": str(last[0]),
                    "points": [[str(r[0]), r[1]] for r in pts], "cols": cols,
                    "cash_now": vals.get(cash_c), "close": vals.get(close_c)}
        return {"error": "No equity table recognised.", "tables": seen}
    except Exception as e:
        return {"error": str(e)}
    finally:
        c.close()


def state():
    cfg = open(CFG).read()
    mode = None
    try:
        mode = open(PIDF).read().split()[1]
    except Exception:
        pass
    try:
        log = "\n".join(open(LOG, errors="replace").read().split("\n")[-60:])
    except Exception:
        log = ""
    try:
        cash = float(cfgval(cfg, "starting_cash") or 0)
    except ValueError:
        cash = 0
    p = pnl(cash, cfgval(cfg, "name"), mode)
    if (mode == "paper" and isinstance(p, dict) and not p.get("error") and live.get("price")
            and p.get("cash_now") is not None and p.get("close")):
        # position value at the last close, re-marked to the current price
        p["live_equity"] = p["cash_now"] + (p["equity"] - p["cash_now"]) / p["close"] * live["price"]
        p["live_pnl"] = p["live_equity"] - p["base"]
        p["live_pct"] = (p["live_equity"] / p["base"] - 1) * 100 if p["base"] else 0
    return {"running": bool(alive()), "mode": mode, "name": cfgval(cfg, "name"),
            "start": cfgval(cfg, "start"), "cash": cash, "log": log, "live": live, "pnl": p}


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bot control</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--card:#f4f4f2;--up:#0a7d3b;--dn:#c0392b;--bd:#ddd}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#999;--card:#222;--up:#4cd07d;--dn:#ff6b5e;--bd:#333}}
body{font:15px system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:820px;margin:0 auto;padding:16px}
.card{background:var(--card);border-radius:10px;padding:14px;margin-bottom:12px}
h2{margin:0 0 8px;font-size:15px;color:var(--mut);font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--fg);cursor:pointer}
button.go{background:var(--up);color:#fff;border-color:var(--up)}button.stop{background:var(--dn);color:#fff;border-color:var(--dn)}
input[type=date],input[type=number]{font:inherit;padding:6px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--fg)}
.big{font-size:28px;font-weight:700}.mut{color:var(--mut);font-size:13px}
#badge{padding:3px 10px;border-radius:99px;font-size:13px;border:1px solid var(--bd)}
pre{margin:0;max-height:260px;overflow:auto;font-size:12px;white-space:pre-wrap}
canvas{width:100%;height:160px}
</style>
<div class=card><div class=row><b>Bot control</b><span id=badge>...</span><span class=mut id=info></span>
<span style="flex:1"></span><button class=stop onclick="act('/api/stop',{})">Stop</button></div></div>
<div class=card><h2>Profit / loss (latest run) <span class=mut id=live></span></h2><div class=row>
<div><div class=big id=pl>-</div><div class=mut id=plpct></div></div>
<div><div class=big id=eq>-</div><div class=mut id=asof>equity</div></div></div>
<canvas id=cv width=800 height=160></canvas><div class=mut id=perr></div></div>
<div class=card><h2>Starting cash</h2><div class=row><input type=number id=cash min=1 step=1000 oninput="this.dataset.d=1"><span class=mut>Used for a fresh paper account and for replays. Resuming an account keeps its cash.</span></div></div>
<div class=card><h2>Paper trading (live data, forward test)</h2><div class=row>
<button class=go onclick="act('/api/start',{mode:'paper',fresh:$('fresh').checked,cash:$('cash').value})">Start paper</button>
<label><input type=checkbox id=fresh> start a fresh account</label></div></div>
<div class=card><h2>Replay from a past date (backtest to today)</h2><div class=row>
<input type=date id=since value="2020-01-02">
<button class=go onclick="act('/api/start',{mode:'replay',since:$('since').value,nohalt:$('nohalt').checked,cash:$('cash').value})">Run replay</button>
<label><input type=checkbox id=nohalt> ignore one-bar loss halt (test only)</label></div>
<div class=mut style="margin-top:6px">The first 200 bars after the date are indicator warm-up, so trades begin later.</div></div>
<div class=card><h2>Log <span id=msg class=mut></span></h2><pre id=log></pre></div>
<script>
const $=id=>document.getElementById(id);
const money=n=>(n<0?"-":"+")+"$"+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:2});
async function api(p,b){const r=await fetch(p,{method:b?"POST":"GET",headers:{"X-Ctl":"1","Content-Type":"application/json"},body:b?JSON.stringify(b):undefined});return r.json()}
async function act(p,b){$("msg").textContent="...";const r=await api(p,b);$("msg").textContent=r.msg||"";refresh()}
function draw(pts){const c=$("cv"),x=c.getContext("2d"),W=c.width,H=c.height;x.clearRect(0,0,W,H);
 if(!pts||pts.length<2)return;const v=pts.map(p=>p[1]),lo=Math.min(...v),hi=Math.max(...v),s=getComputedStyle(document.body);
 x.strokeStyle=v[v.length-1]>=v[0]?s.getPropertyValue("--up"):s.getPropertyValue("--dn");x.lineWidth=2;x.beginPath();
 v.forEach((y,i)=>{const px=i/(v.length-1)*W,py=H-6-(y-lo)/((hi-lo)||1)*(H-12);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}
async function refresh(){const s=await api("/api/state"),p=s.pnl,ok=p&&!p.error,L=s.live;
 $("badge").textContent=s.running?"RUNNING ("+s.mode+")":"stopped";
 $("info").textContent="account: "+s.name+" | started with $"+(ok?p.base:s.cash).toLocaleString();
 if(!$("cash").dataset.d&&!$("cash").value)$("cash").value=s.cash;
 $("live").textContent=L.price?(L.sym+" $"+L.price.toFixed(2)+" @ "+L.ts):(L.err?"price feed: "+L.err:"price feed starting...");
 $("perr").textContent="";
 if(ok){const lv=p.live_equity!=null,pl=lv?p.live_pnl:p.pnl,pc=lv?p.live_pct:p.pct,eq=lv?p.live_equity:p.equity;
  $("pl").textContent=money(pl);$("pl").style.color=pl>=0?"var(--up)":"var(--dn)";
  $("plpct").textContent=pc.toFixed(2)+"% vs starting cash";$("eq").textContent="$"+eq.toLocaleString(undefined,{maximumFractionDigits:2});
  $("asof").textContent=lv?"live estimate (marked to current price)":"equity as of "+p.as_of;
  const pts=p.points.slice();if(lv)pts.push(["live",eq]);draw(pts);
  if(s.mode=="paper"&&!lv)$("perr").textContent="Live value unavailable (equity table columns: "+p.cols.join(", ")+")"}
 else{$("perr").textContent=p?(p.error+" "+JSON.stringify(p.tables||"")):"No data yet. Run a replay or start paper."}
 const l=$("log"),bottom=l.scrollTop+l.clientHeight>=l.scrollHeight-20;l.textContent=s.log;if(bottom)l.scrollTop=l.scrollHeight}
refresh();setInterval(refresh,2000);
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        pw = os.environ.get("CONTROL_PASSWORD", "")
        if not pw:
            return True
        try:
            given = base64.b64decode(self.headers.get("Authorization", "").split(" ", 1)[1]).decode().split(":", 1)[1]
            if hmac.compare_digest(given.encode(), pw.encode()):
                return True
        except Exception:
            pass
        self.send(401, "", extra={"WWW-Authenticate": 'Basic realm="bot"'})
        return False

    def do_GET(self):
        if not self.authed():
            return
        if self.path == "/api/state":
            self.send(200, json.dumps(state()))
        elif self.path in ("/", "/index.html"):
            self.send(200, PAGE, "text/html; charset=utf-8")
        else:
            self.send(404, "{}")

    def do_POST(self):
        if not self.authed():
            return
        if self.headers.get("X-Ctl") != "1":
            return self.send(403, "{}")
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            msg = {"/api/start": start, "/api/stop": lambda r: stop()}[self.path](req)
        except KeyError:
            return self.send(404, "{}")
        except Exception as e:
            msg = f"error: {e}"
        self.send(200, json.dumps({"msg": msg}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--autostart", action="store_true", help="resume paper trading when this server starts")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("CONTROL_PASSWORD"):
        sys.exit("Refusing to listen on a non-local address without CONTROL_PASSWORD.")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    print(f"control panel on http://{a.host}:{a.port}")
    threading.Thread(target=poll_live, daemon=True).start()
    if a.autostart and not alive():
        launch("paper")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
