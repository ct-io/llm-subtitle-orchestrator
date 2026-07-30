import http.server, socketserver, urllib.request, json, subprocess, sys, os, tempfile, threading, glob
from urllib.parse import urlparse, parse_qs

_SI = subprocess.STARTUPINFO(); _SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW; _SI.wShowWindow = 0
sys.stdout = sys.stderr = open(os.path.expandvars(r"%USERPROFILE%\.gatekeeper\whisper.log"), "a", buffering=1, encoding="utf-8")

WHISPER      = os.path.expandvars(r"%USERPROFILE%\.gatekeeper\whisper\Engine\faster-whisper-xxl.exe")
MODEL_DIR    = os.path.expandvars(r"%USERPROFILE%\.gatekeeper\whisper\Model")
MODEL        = "large-v2"
OLLAMA       = "http://127.0.0.1:11434"
LISTEN_PORT  = 11436
THRESHOLD_MB = int(os.environ.get("GATE_THRESHOLD_MB", "8000"))
LOCK = threading.Lock()

def gpu_used_mb():
    out = subprocess.run(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=10, startupinfo=_SI)
    
    return int(out.stdout.strip().splitlines()[0])
def ollama_vram_mb():
    try:
        with urllib.request.urlopen(OLLAMA+"/api/ps", timeout=5) as r:
            return sum(m.get("size_vram",0) for m in json.load(r).get("models",[]))//(1024*1024)
    except Exception: return 0
def other_apps_mb():
    try: return max(0, gpu_used_mb()-ollama_vram_mb())
    except Exception: return 0

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body))); self.send_header("Connection","close")
        self.end_headers()
        if body: self.wfile.write(body)
    def do_GET(self):
        return self._send(200, b"ok") if self.path.startswith("/health") else self._send(404, b"POST /transcribe?lang=ru")
    def do_POST(self):
        lang = (parse_qs(urlparse(self.path).query).get("lang",["ru"])[0]).lower()
        n = int(self.headers.get("Content-Length",0))
        audio = self.rfile.read(n) if n else b""
        if not audio: return self._send(400, b"no audio")
        busy = other_apps_mb()
        sys.stderr.write(f"[whisper] other_apps={busy}MB lang={lang} bytes={len(audio)}\n")
        if busy > THRESHOLD_MB: return self._send(503, b'{"error":"gpu busy"}', "application/json")
        if not LOCK.acquire(blocking=False): return self._send(503, b'{"error":"whisper busy"}', "application/json")
        try:
            with tempfile.TemporaryDirectory() as td:
                inp = os.path.join(td,"audio.wav"); open(inp,"wb").write(audio)
                outdir = os.path.join(td,"out"); os.makedirs(outdir, exist_ok=True)
                cmd = [WHISPER, inp, "--model", MODEL, "--model_dir", MODEL_DIR,
                       "--language", lang, "--output_format", "srt", "--output_dir", outdir,
                       "--beep_off"]
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=7200,
                                   creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=_SI)
                srts = glob.glob(os.path.join(outdir,"*.srt"))
                if not srts:
                    return self._send(500, ("no srt produced\n"+(p.stderr or "")[-2000:]).encode("utf-8","replace"))
                text = open(srts[0], "rb").read().decode("utf-8-sig", errors="replace")  # strip BOM, force UTF-8
                return self._send(200, text.encode("utf-8"), "application/x-subrip; charset=utf-8")
        except subprocess.TimeoutExpired: return self._send(504, b"whisper timeout")
        except Exception as e: return self._send(500, str(e).encode())
        finally: LOCK.release()

socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("0.0.0.0", LISTEN_PORT), H) as s:
    sys.stderr.write(f"[whisper] listening on :{LISTEN_PORT}  model={MODEL}\n"); s.serve_forever()