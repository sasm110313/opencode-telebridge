#!/usr/bin/env python3
import base64
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PASS_FILE = os.path.join(HERE, ".cam-pass")

HOST = "127.0.0.1"
PORT = int(os.environ.get("CAM_PORT", "8091"))
DEVICE = os.environ.get("CAM_DEVICE", "/dev/video0")
SIZE = os.environ.get("CAM_SIZE", "1280x720")
FPS = int(os.environ.get("CAM_FPS", "15"))
QUALITY = os.environ.get("CAM_QUALITY", "6")
IDLE_STOP = float(os.environ.get("CAM_IDLE_STOP", "60"))
AUTH_USER = os.environ.get("CAM_USER", "cam")


def _load_password():
    env = os.environ.get("CAM_PASS")
    if env:
        return env
    try:
        with open(PASS_FILE, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
            if val:
                return val
    except OSError:
        pass
    val = secrets.token_urlsafe(9)
    try:
        fd = os.open(PASS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(val)
    except OSError:
        pass
    return val


AUTH_PASS = _load_password()


class Camera:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs_lock = threading.Lock()
        self._subs = set()
        self._proc = None
        self._reader = None
        self._err = []
        self.started_at = None
        self.frames = 0
        self.last_jpeg = None
        self.last_error = None

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        with self._lock:
            if self.running:
                return True
            self._err = []
            self.last_error = None
            self.frames = 0
            self.last_jpeg = None
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "v4l2", "-input_format", "mjpeg",
                "-video_size", SIZE, "-framerate", str(FPS), "-i", DEVICE,
                "-an", "-c:v", "mjpeg", "-q:v", QUALITY,
                "-f", "mjpeg", "-",
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
            except OSError as exc:
                self.last_error = str(exc)
                self._proc = None
                return False
            self.started_at = time.time()
            self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
            self._reader.start()
            threading.Thread(target=self._stderr_loop, args=(self._proc,), daemon=True).start()
            return True

    def stop(self):
        with self._lock:
            proc, self._proc = self._proc, None
            self.started_at = None
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        self._broadcast(None)

    def _stderr_loop(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._err.append(line)
                self.last_error = line
                del self._err[:-20]

    def _read_loop(self, proc):
        buf = bytearray()
        fd = proc.stdout.fileno()
        while proc.poll() is None:
            try:
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    if len(buf) > 2:
                        del buf[:-1]
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buf[:start]
                    break
                frame = bytes(buf[start:end + 2])
                del buf[:end + 2]
                self.frames += 1
                self.last_jpeg = frame
                self._broadcast(frame)
        self._broadcast(None)

    def _broadcast(self, frame):
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            if frame is None:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
                continue
            try:
                q.put_nowait(frame)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(frame)
                except Exception:
                    pass

    def subscribe(self):
        q = queue.Queue(maxsize=8)
        with self._subs_lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            self._subs.discard(q)

    def viewers(self):
        with self._subs_lock:
            return len(self._subs)

    def status(self):
        running = self.running
        uptime = max(0.0, time.time() - self.started_at) if running and self.started_at else 0.0
        return {
            "running": running,
            "device": DEVICE,
            "size": SIZE,
            "fps_target": FPS,
            "fps_actual": round(self.frames / uptime, 1) if uptime > 0.5 else 0,
            "frames": self.frames,
            "uptime": round(uptime, 1),
            "viewers": self.viewers(),
            "idle_stop": IDLE_STOP,
            "error": None if running else self.last_error,
        }

    def idle_monitor(self):
        empty_since = None
        while True:
            time.sleep(2)
            if not self.running or IDLE_STOP <= 0:
                empty_since = None
                continue
            if self.viewers() == 0:
                if empty_since is None:
                    empty_since = time.time()
                elif time.time() - empty_since >= IDLE_STOP:
                    self.stop()
                    empty_since = None
            else:
                empty_since = None


cam = Camera()

PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0f1e">
<link rel="icon" href="data:,">
<title>دوربین زنده</title>
<style>
  :root{
    --bg:#0a0f1e; --card:#151d33; --card2:#1c2744; --line:#2b3760;
    --txt:#eef2ff; --muted:#8d9ac4; --ok:#4ade80; --bad:#f87171;
    --accent:#7c8cff; --accent2:#c084fc;
    --safe-t:env(safe-area-inset-top,0px); --safe-b:env(safe-area-inset-bottom,0px);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;height:100%}
  body{
    background:radial-gradient(120% 70% at 50% -10%,#1e2a5c 0%,transparent 60%),var(--bg);
    color:var(--txt);overscroll-behavior:none;
    font-family:"Vazirmatn","IRANSans",system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;
  }
  #app{
    min-height:100%;max-width:820px;margin:0 auto;
    padding:calc(var(--safe-t) + 14px) 14px calc(var(--safe-b) + 14px);
    display:flex;flex-direction:column;gap:12px;
  }
  header{display:flex;align-items:center;justify-content:space-between;gap:10px}
  h1{margin:0;font-size:19px;letter-spacing:-.3px}
  .pill{
    display:inline-flex;align-items:center;gap:7px;
    padding:7px 13px;border-radius:99px;font-size:13px;font-weight:700;
    background:var(--card2);border:1px solid var(--line);color:var(--muted);
  }
  .pill.live{color:var(--ok);border-color:rgba(74,222,128,.45);background:rgba(74,222,128,.1)}
  .pill.live .dot{background:var(--ok);animation:pulse 1.4s infinite}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  @keyframes pulse{50%{opacity:.25}}
  .view{
    position:relative;background:#000;border:1px solid var(--line);
    border-radius:18px;overflow:hidden;aspect-ratio:16/9;
    display:grid;place-items:center;
  }
  .view img{width:100%;height:100%;object-fit:contain;display:block}
  .view .off{color:var(--muted);text-align:center;font-size:14px;padding:20px;line-height:2}
  .view .off b{color:var(--txt);display:block;font-size:16px;margin-bottom:4px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  button{
    font:inherit;font-weight:700;color:var(--txt);cursor:pointer;
    min-height:54px;border-radius:16px;border:1px solid var(--line);
    background:var(--card2);transition:transform .08s,opacity .15s;
    display:inline-flex;align-items:center;justify-content:center;gap:8px;
  }
  button:active{transform:scale(.97)}
  button:disabled{opacity:.35;cursor:not-allowed}
  button.on{background:linear-gradient(135deg,var(--accent),var(--accent2));border-color:transparent;color:#0a0f1e}
  button.off{border-color:rgba(248,113,113,.5);background:rgba(248,113,113,.12);color:#fca5a5}
  .wide{grid-column:1/-1;min-height:48px;font-size:14px}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:9px 10px;text-align:center}
  .box b{display:block;font-size:16px;font-variant-numeric:tabular-nums}
  .box span{font-size:11px;color:var(--muted)}
  .note{color:var(--muted);font-size:12px;line-height:1.9;text-align:center}
  .err{
    display:none;background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.4);
    color:#fca5a5;border-radius:14px;padding:10px 12px;font-size:12px;
    word-break:break-word;direction:ltr;text-align:left;
  }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>دوربین زنده</h1>
    <span class="pill" id="pill"><span class="dot"></span><span id="pillText">خاموش</span></span>
  </header>

  <div class="view">
    <img id="live" alt="" hidden>
    <div class="off" id="off"><b>دوربین خاموشه</b>برای دیدن تصویر، روشنش کن</div>
  </div>

  <div class="row">
    <button id="start" class="on">روشن کردن</button>
    <button id="stop" class="off">خاموش کردن</button>
    <button id="snap" class="wide">گرفتن عکس</button>
  </div>

  <div class="stats">
    <div class="box"><b id="s-fps">0</b><span>فریم/ثانیه</span></div>
    <div class="box"><b id="s-time">0s</b><span>مدت</span></div>
    <div class="box"><b id="s-view">0</b><span>بیننده</span></div>
  </div>

  <div class="err" id="err"></div>

  <div class="note" id="note"></div>
</div>
<script>
const $ = s => document.querySelector(s);
let running = false, attached = false, idleStop = 0;

const fmtTime = s => s < 60 ? Math.round(s) + "s" : Math.floor(s/60) + "m " + Math.round(s%60) + "s";

async function api(path, method){
  const r = await fetch(path, { method: method || "GET", cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function attach(){
  const img = $("#live");
  img.hidden = false;
  $("#off").style.display = "none";
  img.src = "/stream?t=" + Date.now();
}

function detach(){
  const img = $("#live");
  img.removeAttribute("src");
  img.hidden = true;
  $("#off").style.display = "";
}

function render(st){
  running = st.running;
  idleStop = st.idle_stop;
  $("#pill").classList.toggle("live", running);
  $("#pillText").textContent = running ? "روشن" : "خاموش";
  $("#start").disabled = running;
  $("#stop").disabled = !running;
  $("#snap").disabled = !running;
  $("#s-fps").textContent = st.fps_actual || 0;
  $("#s-time").textContent = fmtTime(st.uptime || 0);
  $("#s-view").textContent = st.viewers || 0;
  if (running && !attached){ attached = true; attach() }
  else if (!running && attached){ attached = false; detach() }
  const e = $("#err");
  if (st.error){ e.style.display = "block"; e.textContent = st.error } else { e.style.display = "none" }
  const note = $("#note");
  if (running && idleStop > 0){
    note.textContent = "بعد از " + Math.round(idleStop) + " ثانیه بدون بیننده، دوربین خودکار خاموش می‌شود.";
  } else if (!running){
    note.textContent = "برای روشن کردن، دکمهٔ بالا را بزن.";
  } else {
    note.textContent = "";
  }
}

async function refresh(){
  try { render(await api("/api/status")) }
  catch(err){ $("#pillText").textContent = "قطع"; $("#pill").classList.remove("live") }
}

$("#start").onclick = async () => {
  $("#start").disabled = true;
  try { render(await api("/api/start", "POST")) } catch(err){}
};
$("#stop").onclick = async () => {
  $("#stop").disabled = true;
  try { render(await api("/api/stop", "POST")) } catch(err){}
};
$("#snap").onclick = () => {
  const a = document.createElement("a");
  a.href = "/api/snapshot?t=" + Date.now();
  a.download = "webcam-" + Date.now() + ".jpg";
  a.click();
};

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WebcamLive"

    def log_message(self, *args):
        pass

    def _authed(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        user, _, pw = decoded.partition(":")
        return secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pw, AUTH_PASS)

    def _deny(self):
        body = b"authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="webcam"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return self._deny()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(cam.status())
        if path == "/api/snapshot":
            if cam.running and cam.last_jpeg:
                return self._send_bytes(cam.last_jpeg, "image/jpeg")
            self.send_error(404, "no frame")
            return
        if path == "/stream":
            return self._stream()
        self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return self._deny()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
        except Exception:
            pass
        path = self.path.split("?", 1)[0]
        if path == "/api/start":
            cam.start()
            return self._json(cam.status())
        if path == "/api/stop":
            cam.stop()
            return self._json(cam.status())
        self.send_error(404)

    def _stream(self):
        q = cam.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Age", "0")
            self.end_headers()
            while True:
                try:
                    frame = q.get(timeout=15)
                except queue.Empty:
                    if not cam.running:
                        break
                    continue
                if frame is None:
                    break
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(frame)).encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            cam.unsubscribe(q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    threading.Thread(target=cam.idle_monitor, daemon=True).start()
    server = Server((HOST, PORT), Handler)
    server.daemon_threads = True

    def shutdown(*_):
        cam.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("webcam-live on http://%s:%d" % (HOST, PORT), flush=True)
    print("user: %s" % AUTH_USER, flush=True)
    print("pass: %s" % AUTH_PASS, flush=True)
    print("device: %s  size: %s  fps: %d" % (DEVICE, SIZE, FPS), flush=True)
    print("idle auto-stop: %ss (0 = off)" % IDLE_STOP, flush=True)
    try:
        server.serve_forever()
    finally:
        cam.stop()
        server.server_close()


if __name__ == "__main__":
    main()
