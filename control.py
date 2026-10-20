#!/usr/bin/env python3
"""Control panel for the bot: start/stop paper trading, replay from a past date, see P&L.
Standard library only. Run from the venv:  venv/bin/python control.py
Set CONTROL_PASSWORD (required for any non-local --host)."""
import argparse, base64, hmac, json, os, re, signal, sqlite3, subprocess, sys, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "config.toml")
DB = os.path.join(ROOT, "data", "bot.db")
LOG = os.path.join(ROOT, "data", "control.log")
PIDF = os.path.join(ROOT, "data", "control.pid")
lock = threading.Lock()
proc = None


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
        mode = req.get("mode")
        if mode == "paper":
            if req.get("fresh"):
                name = "paper-" + datetime.now().strftime("%Y%m%d-%H%M")
                open(CFG, "w").write(set_key(orig, "run", "name", json.dumps(name)))
            launch("paper")
            return "Paper trading started."
        if mode == "replay":
            since = str(req.get("since", ""))
            datetime.strptime(since, "%Y-%m-%d")
            txt = set_key(orig, "data", "start", json.dumps(since))
            if req.get("nohalt"):
                txt = set_key(txt, "risk", "max_daily_loss_pct", "0")
            open(CFG, "w").write(txt)
            launch("replay", cleanup=lambda: open(CFG, "w").write(orig))
            return f"Replay started from {since}."
        return "Unknown mode."


def stop():
    with lock:
        pid = alive()
        if not pid:
            return "Nothing is running."
        os.kill(pid, signal.SIGTERM)
        return "Stop signal sent."


def pnl(cash):
    """Best effort: find the table with an equity column and read the latest run."""
    if not os.path.exists(DB):
        return None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        seen = {}
        for (t,) in c.execute("select name from sqlite_master where type='table'").fetchall():
            cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]
            seen[t] = cols
            eq = next((x for x in cols if x.lower() in ("equity", "total_equity", "nav")), None)
            if not eq:
                continue
            tc = next((x for x in cols if x.lower() in
                       ("ts", "date", "time", "timestamp", "bar_date", "dt")), "rowid")
            where = f'where run_id=(select max(run_id) from "{t}")' if "run_id" in cols else ""
            rows = c.execute(f'select {tc}, "{eq}" from "{t}" {where} order by rowid').fetchall()
            if not rows:
                continue
            step = max(1, len(rows) // 300)
            last = rows[-1][1]
            return {"equity": last, "pnl": last - cash, "pct": (last / cash - 1) * 100 if cash else 0,
                    "as_of": str(rows[-1][0]), "points": [[str(a), b] for a, b in rows[::step]]}
        return {"error": "No equity table recognised.", "tables": seen}
    except Exception as e:
        return {"error": str(e)}
    finally:
        c.close()


def state():
    cfg = open(CFG).read()

    def g(k):
        m = re.search(rf'^\s*{k}\s*=\s*"?([^"#\n]*?)"?\s*(#.*)?$', cfg, re.M)
        return m.group(1) if m else ""
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
        cash = float(g("starting_cash") or 0)
    except ValueError:
        cash = 0
    return {"running": bool(alive()), "mode": mode, "name": g("name"), "start": g("start"),
            "cash": cash, "log": log, "pnl": pnl(cash)}


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
input[type=date]{font:inherit;padding:6px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--fg)}
.big{font-size:28px;font-weight:700}.mut{color:var(--mut);font-size:13px}
#badge{padding:3px 10px;border-radius:99px;font-size:13px;border:1px solid var(--bd)}
pre{margin:0;max-height:260px;overflow:auto;font-size:12px;white-space:pre-wrap}
canvas{width:100%;height:160px}
</style>
<div class=card><div class=row><b>Bot control</b><span id=badge>...</span><span class=mut id=info></span>
<span style="flex:1"></span><button class=stop onclick="act('/api/stop',{})">Stop</button></div></div>
<div class=card><h2>Profit / loss (latest run)</h2><div class=row>
<div><div class=big id=pl>-</div><div class=mut id=plpct></div></div>
<div><div class=big id=eq>-</div><div class=mut id=asof>equity</div></div></div>
<canvas id=cv width=800 height=160></canvas><div class=mut id=perr></div></div>
<div class=card><h2>Paper trading (live data, forward test)</h2><div class=row>
<button class=go onclick="act('/api/start',{mode:'paper',fresh:$('fresh').checked})">Start paper</button>
<label><input type=checkbox id=fresh> start a fresh account</label></div></div>
<div class=card><h2>Replay from a past date (backtest to today)</h2><div class=row>
<input type=date id=since value="2020-01-02">
<button class=go onclick="act('/api/start',{mode:'replay',since:$('since').value,nohalt:$('nohalt').checked})">Run replay</button>
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
async function refresh(){const s=await api("/api/state");
 $("badge").textContent=s.running?"RUNNING ("+s.mode+")":"stopped";
 $("info").textContent="account: "+s.name+" | start $"+s.cash.toLocaleString();
 const p=s.pnl;$("perr").textContent="";
 if(p&&!p.error){$("pl").textContent=money(p.pnl);$("pl").style.color=p.pnl>=0?"var(--up)":"var(--dn)";
  $("plpct").textContent=p.pct.toFixed(2)+"% vs starting cash";$("eq").textContent="$"+p.equity.toLocaleString(undefined,{maximumFractionDigits:2});
  $("asof").textContent="equity as of "+p.as_of;draw(p.points)}
 else{$("perr").textContent=p?(p.error+" "+JSON.stringify(p.tables||"")):"No data yet. Run a replay or start paper."}
 const l=$("log"),bottom=l.scrollTop+l.clientHeight>=l.scrollHeight-20;l.textContent=s.log;if(bottom)l.scrollTop=l.scrollHeight}
refresh();setInterval(refresh,3000);
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
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("CONTROL_PASSWORD"):
        sys.exit("Refusing to listen on a non-local address without CONTROL_PASSWORD.")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    print(f"control panel on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
