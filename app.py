#!/usr/bin/env python3
import os, re, json, secrets, subprocess, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST = os.environ.get("WSHELL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("WSHELL_PORT", "5000")))
MAX_OUT = 200_000

class ShellSession:
    def __init__(self):
        self.sid = secrets.token_hex(8)
        self.cwd = os.path.expanduser("~")
        self.env = dict(os.environ, TERM="xterm-256color")
        self.lock = threading.Lock()

    def run(self, cmd: str) -> dict:
        cmd = cmd.rstrip("\n")
        if not cmd.strip():
            return {"stdout": "", "stderr": "", "cwd": self.cwd}
        with self.lock:
            try:
                p = subprocess.run(["bash", "-c", cmd], cwd=self.cwd, env=self.env,
                                   capture_output=True, text=True, timeout=600)
                out, err = p.stdout, p.stderr
            except subprocess.TimeoutExpired:
                return {"stdout": "", "stderr": "команда прервана по таймауту (10 мин)", "cwd": self.cwd}
            m = re.match(r"^\s*cd\s*(.*?)\s*(?:&&|;|$)", cmd)
            if m:
                target = m.group(1).strip().strip("'\"") or os.path.expanduser("~")
                target = os.path.expanduser(target)
                new = target if os.path.isabs(target) else os.path.abspath(os.path.join(self.cwd, target))
                if os.path.isdir(new):
                    self.cwd = new
                elif not err:
                    err = f"bash: cd: {target}: Нет такого файла или каталога"
            return {"stdout": out[:MAX_OUT], "stderr": err[:MAX_OUT], "cwd": self.cwd}

SESSIONS = {}
LOCK = threading.Lock()

def get_session(sid):
    with LOCK:
        s = SESSIONS.get(sid) if sid else None
        if s is None:
            s = ShellSession()
            SESSIONS[s.sid] = s
        return s

class Handler(BaseHTTPRequestHandler):
    server_version = "webshell/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        print("[req]", self.address_string(), fmt % a, flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/healthz":
            return self._send(200, '{"ok":true}')
        if p.startswith("/api/"):
            return self._send(404, '{"error":"not found"}')
        return self._send(200, HTML, "text/html; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            return self._send(413, '{"error":"too large"}')
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, '{"error":"bad json"}')
        sid = data.get("sid") if isinstance(data.get("sid"), str) else None

        if path == "/api/exec":
            cmd = data.get("cmd", "")
            if not isinstance(cmd, str) or len(cmd) > 100_000:
                return self._send(400, '{"error":"bad cmd"}')
            s = get_session(sid)
            r = s.run(cmd)
            r["sid"] = s.sid
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if path == "/api/reset":
            s = get_session(sid)
            s.cwd = os.path.expanduser("~")
            return self._send(200, json.dumps({"ok": True, "cwd": s.cwd}))
        self._send(404, '{"error":"not found"}')

HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>webshell</title>
<style>
  html,body{margin:0;height:100%;background:#0d1117;color:#c9d1d9;
    font:14px/1.45 ui-monospace,Menlo,Consolas,monospace}
  #log{position:fixed;inset:0;overflow-y:auto;padding:10px 12px 48px;
      white-space:pre-wrap;word-break:break-all}
  .p{color:#58a6ff;user-select:none}
  .e{color:#f85149}
  .d{color:#8b949e}
  #inp{position:fixed;left:0;bottom:0;width:100%;background:#010409;border:0;
    outline:0;color:#c9d1d9;font:inherit;padding:10px 12px;box-sizing:border-box}
  #bar{position:fixed;top:0;right:0;padding:6px 10px;color:#8b949e;font-size:12px;
    background:#161b22;border-bottom-left-radius:8px}
</style>
</head>
<body>
<div id="log"></div>
<div id="bar"></div>
<input id="inp" autocomplete="off" autocapitalize="off" spellcheck="false" autofocus placeholder="команда…">
<script>
const inp = document.getElementById('inp');
const log = document.getElementById('log');
const bar = document.getElementById('bar');
let sid = null, hist = [], hi = -1;

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function add(html){log.insertAdjacentHTML('beforeend',html);log.scrollTop=log.scrollHeight;}
function prompt(){const c=bar.textContent||'';
  return '<span class="p">'+esc(c.replace(/^\/home\/[^/]+/,'~'))+'$ </span>';}

add('<span class="d">webshell готов. Помни: сюда может зайти кто угодно.</span>\n');

async function api(path, body){
  const r = await fetch(path, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({...body, sid})});
  if (r.status === 413){ add('<span class="e">слишком большой запрос</span>\n'); throw 0; }
  return r.json();
}

async function run(cmd){
  add(prompt() + esc(cmd) + '\n');
  try{
    const d = await api('/api/exec', {cmd});
    sid = d.sid; bar.textContent = d.cwd;
    if (d.stdout) add(esc(d.stdout));
    if (d.stderr) add('<span class="e">'+esc(d.stderr)+'</span>');
  }catch(e){}
  log.scrollTop = log.scrollHeight;
}

inp.addEventListener('keydown', async e => {
  if (e.key === 'Enter'){
    const cmd = inp.value; inp.value=''; hi = -1;
    if (cmd.trim()){ hist.push(cmd); }
    if (cmd.trim() === 'clear'){ log.innerHTML=''; return; }
    await run(cmd);
  } else if (e.key === 'ArrowUp'){
    e.preventDefault();
    if (hist.length){ hi = Math.min(hi+1, hist.length-1); inp.value = hist[hist.length-1-hi]; }
  } else if (e.key === 'ArrowDown'){
    e.preventDefault();
    hi = Math.max(hi-1, -1); inp.value = hi<0 ? '' : hist[hist.length-1-hi];
  } else if (e.key === 'c' && e.ctrlKey){
    add('^C\n'); inp.value='';
  }
});
document.addEventListener('click', ()=>inp.focus());
</script>
</body>
</html>"""

if __name__ == "__main__":
    print(f"\n  webshell: http://{HOST}:{PORT}/\n", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
