import http.server, socketserver, urllib.request, urllib.error, json, subprocess, sys, os, time

_SI = subprocess.STARTUPINFO(); _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW; _SI.wShowWindow = 0
sys.stdout = sys.stderr = open(os.path.expandvars(r"%USERPROFILE%\.gatekeeper\gatekeeper.log"), "a", buffering=1, encoding="utf-8")

OLLAMA       = "http://127.0.0.1:11434"
LISTEN_PORT  = 11435
THRESHOLD_MB = int(os.environ.get("GATE_THRESHOLD_MB", "7000"))   # non-Ollama VRAM (MB) that means "busy"
UTIL_THRESH  = int(os.environ.get("GATE_UTIL", "35"))             # GPU utilization % that means "in use (game)"
GATED        = ("chat", "generate", "completion")                  # only gate generation calls

def nvsmi(field):
    out = subprocess.run(["nvidia-smi","--query-gpu="+field,"--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10,
                         creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=_SI)
    return int(out.stdout.strip().splitlines()[0])
def gpu_used_mb(): return nvsmi("memory.used")
def gpu_util():    return nvsmi("utilization.gpu")

def ollama_vram_mb():
    try:
        with urllib.request.urlopen(OLLAMA+"/api/ps", timeout=5) as r:
            return sum(m.get("size_vram",0) for m in json.load(r).get("models",[])) // (1024*1024)
    except Exception:
        return 0

def other_apps_mb():
    try:
        return max(0, gpu_used_mb() - ollama_vram_mb())
    except Exception as e:
        sys.stderr.write(f"[gate] vram check failed, allowing: {e}\n")
        return 0   # fail-open

def status():
    # median of 3 utilization samples so a game's sustained load trips it but momentary blips don't
    us = []
    for _ in range(3):
        try: us.append(gpu_util())
        except Exception: us.append(0)
        time.sleep(0.4)
    util = sorted(us)[1]
    other = other_apps_mb()
    busy = other > THRESHOLD_MB or util > UTIL_THRESH
    return {"util": util, "other_apps_mb": other, "threshold_mb": THRESHOLD_MB, "util_thresh": UTIL_THRESH, "busy": busy}

def unload():
    try:
        with urllib.request.urlopen(OLLAMA+"/api/ps", timeout=5) as r:
            models = json.load(r).get("models",[])
        for m in models:
            body = json.dumps({"model":m["name"],"keep_alive":0}).encode()
            urllib.request.urlopen(urllib.request.Request(OLLAMA+"/api/generate", data=body,
                headers={"Content-Type":"application/json"}, method="POST"), timeout=10).read()
    except Exception:
        pass

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.send_header("Connection","close"); self.end_headers()
        self.wfile.write(b)
    def proxy(self):
        if self.command == "GET" and self.path.startswith("/gatestatus"):
            s = status()
            sys.stderr.write(f"[gate] status util={s['util']}% other={s['other_apps_mb']}MB busy={s['busy']}\n")
            return self._json(200, s)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else None
        if self.command == "POST" and any(g in self.path.lower() for g in GATED):
            busy = other_apps_mb()
            sys.stderr.write(f"[gate] other_apps={busy}MB / threshold={THRESHOLD_MB}MB  {self.path}\n")
            if busy > THRESHOLD_MB:
                unload()
                return self._json(503, {"error":"gpu busy, requeue"})
        req = urllib.request.Request(OLLAMA+self.path, data=body, method=self.command)
        for k,v in self.headers.items():
            if k.lower() not in ("host","content-length","connection"): req.add_header(k,v)
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            self.send_response(502); self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(resp.status)
        for k,v in resp.getheaders():
            if k.lower() not in ("transfer-encoding","connection","content-length"): self.send_header(k,v)
        self.send_header("Connection","close"); self.end_headers()
        while True:
            chunk = resp.read(8192)
            if not chunk: break
            self.wfile.write(chunk)
    do_GET = proxy
    do_POST = proxy
    def log_message(self,*a): pass

socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("0.0.0.0", LISTEN_PORT), H) as s:
    sys.stderr.write(f"[gate] listening on :{LISTEN_PORT} -> {OLLAMA}  (busy if other-VRAM>{THRESHOLD_MB}MB or util>{UTIL_THRESH}%)\n")
    s.serve_forever()
