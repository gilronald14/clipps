#!/usr/bin/env python3
"""Shortsmith web UI — a local server, opened in your own browser.

    python webapp.py

Then it opens http://127.0.0.1:8765/ for you. Nothing is uploaded anywhere:
the page is served from this machine, the video never leaves it, and the whole
thing runs offline. There is no account, no key and nothing to install beyond
the libraries setup.bat already handles.

Deliberately stdlib-only. The whole point is that somebody can download this
folder from GitHub and run it on a PC that has never seen this program, with no
package manager involved — so no Flask, no FastAPI, nothing to pip install for
the server itself.

The browser is the front end, so it cannot browse this PC's disk by itself.
Everything that needs real filesystem access — choosing the video, choosing
where the clips go — goes through the small JSON API below, which is why there
is a server-side folder browser rather than a native file dialog.

Security: binds to 127.0.0.1 only. The browse API exposes this machine's
filesystem, so it must not be reachable from the network.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

HOST = "127.0.0.1"
PORT = int(os.environ.get("SHORTSMITH_PORT", "8765"))
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv")
UPLOAD_DIR = os.path.join(_HERE, "uploads")

# One render at a time, matching the pipeline: transcoding is CPU-bound, and
# two jobs would fight over the same cores while both reported progress.
class Job:
    def __init__(self):
        self.proc = None
        self.lines = []
        self.state = "idle"          # idle | running | done | failed | stopped
        self.out_dir = ""
        self.clips = []
        self.started = 0.0
        self.lock = threading.Lock()

    def log(self, line):
        with self.lock:
            self.lines.append(line)

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self, from_index):
        with self.lock:
            return self.lines[from_index:], len(self.lines), self.state


JOB = Job()


# ---------------------------------------------------------------- filesystem
def list_drives():
    """Drive roots, so the browser can show the actual storage on this PC."""
    out = []
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if os.path.exists(root):
                out.append({"name": f"{letter}:", "path": root})
    else:
        out.append({"name": "/", "path": "/"})
        for mnt in ("/mnt", "/media", "/Volumes"):
            if os.path.isdir(mnt):
                for e in sorted(os.listdir(mnt)):
                    p = os.path.join(mnt, e)
                    if os.path.isdir(p):
                        out.append({"name": e, "path": p})
    return out


def browse(path):
    """Contents of a folder: sub-folders, plus any video files sitting in it."""
    if not path:
        return {"path": "", "parent": None, "dirs": list_drives(), "videos": []}
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"error": f"not a folder: {path}"}
    dirs, videos = [], []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full})
                elif name.lower().endswith(VIDEO_EXTS):
                    videos.append({"name": name, "path": full,
                                   "size": os.path.getsize(full)})
            except OSError:
                continue                      # unreadable entry, skip it
    except PermissionError:
        return {"error": f"no permission to read {path}"}
    parent = os.path.dirname(path.rstrip("\\/")) or None
    if parent == path:
        parent = None
    return {"path": path, "parent": parent, "dirs": dirs, "videos": videos}


def human(n):
    """A byte count as something readable."""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# ------------------------------------------------------------------- render
def _pump(proc, job):
    """Feed the child's output into the job log, line by line."""
    for raw in proc.stdout:
        job.log(raw.rstrip("\n"))
    proc.stdout.close()
    rc = proc.wait()

    with job.lock:
        stopped = job.state == "stopped"      # the Stop button already said so
        out = job.out_dir

    # Collect the clips BEFORE publishing the finished state. The browser
    # stops listening the moment it sees "done", so publishing first would
    # hand it an empty list and the results would never appear.
    clips = []
    if rc == 0 and out:
        for root, _dirs, files in os.walk(out):
            for f in files:
                if f.lower().endswith(".mp4"):
                    p = os.path.join(root, f)
                    try:
                        clips.append({"name": f, "path": p,
                                      "size": os.path.getsize(p)})
                    except OSError:
                        pass
        clips.sort(key=lambda c: c["name"])

    with job.lock:
        job.clips = clips                     # clips first...
        if not stopped:
            job.state = "done" if rc == 0 else "failed"    # ...then the state


def start_job(opts):
    with JOB.lock:
        if JOB.state == "running":
            return {"error": "a render is already running"}
        JOB.lines.clear()
        JOB.clips = []
        JOB.state = "running"
        JOB.started = time.time()

    src = (opts.get("source") or "").strip()
    out = (opts.get("out") or "").strip()
    if not src or not os.path.isfile(src):
        JOB.set(state="failed")
        JOB.log(f"source video not found: {src}")
        return {"error": f"source video not found: {src}"}
    if not out:
        JOB.set(state="failed")
        JOB.log("no output folder chosen")
        return {"error": "no output folder chosen"}
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        JOB.set(state="failed")
        JOB.log(f"cannot create output folder: {e}")
        return {"error": f"cannot create output folder: {e}"}

    cmd = [sys.executable, os.path.join(_HERE, "make_shorts.py"), src,
           "--out", out,
           "--clips", str(int(opts.get("clips", 7))),
           "--min", str(float(opts.get("min_sec", 20.0))),
           "--max", str(float(opts.get("max_sec", 45.0))),
           "--whisper", str(opts.get("whisper", "base")),
           "--work", os.path.join(_HERE, "work")]
    if opts.get("llm"):
        cmd.append("--llm")

    JOB.set(out_dir=out)
    JOB.log(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    JOB.log("")
    proc = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    JOB.set(proc=proc)
    threading.Thread(target=_pump, args=(proc, JOB), daemon=True).start()
    return {"ok": True}


def stop_job():
    with JOB.lock:
        proc = JOB.proc
        if not proc or proc.poll() is not None:
            return {"error": "nothing is running"}
        JOB.state = "stopped"
        JOB.lines.append("")
        JOB.lines.append("Stopping — closing the render and anything it started…")
    # Kill the tree: make_shorts.py spawns an ffmpeg per stage, and killing
    # only the parent leaves encoders running with nobody to report to.
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    return {"ok": True}


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    server_version = "Shortsmith"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                                   # the page has its own log

    # -- helpers
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    # -- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(_HERE, "webui.html"), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, f"webui.html is missing: {e}", "text/plain")
            return

        if path == "/api/browse":
            self._send(200, browse((q.get("path") or [""])[0]))
            return

        if path == "/api/state":
            with JOB.lock:
                self._send(200, {"state": JOB.state, "clips": JOB.clips,
                                 "out_dir": JOB.out_dir})
            return

        if path == "/api/stream":
            self._stream()
            return

        if path == "/api/file":
            self._serve_file((q.get("path") or [""])[0])
            return

        self._send(404, {"error": "not found"})

    # -- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/api/start":
            self._send(200, start_job(self._json_body()))
            return

        if u.path == "/api/stop":
            self._send(200, stop_job())
            return

        if u.path == "/api/upload":
            self._upload(q)
            return

        if u.path == "/api/open":
            p = (q.get("path") or [""])[0]
            if p and os.path.isdir(p):
                try:
                    if os.name == "nt":
                        os.startfile(p)              # noqa: S606
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", p])
                    else:
                        subprocess.Popen(["xdg-open", p])
                    self._send(200, {"ok": True})
                except OSError as e:
                    self._send(200, {"error": str(e)})
            else:
                self._send(200, {"error": "that folder does not exist yet"})
            return

        self._send(404, {"error": "not found"})

    # -- upload: the raw body IS the file, so there is no multipart to parse.
    #    (Python 3.13 removed the cgi module that used to do this.)
    def _upload(self, q):
        name = os.path.basename((q.get("name") or ["video.mp4"])[0])
        safe = re.sub(r"[^\w.\-]", "_", name) or "video.mp4"
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, safe)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            self._send(400, {"error": "empty upload"})
            return
        got = 0
        try:
            with open(dest, "wb") as f:
                while got < n:
                    chunk = self.rfile.read(min(1024 * 1024, n - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
        except OSError as e:
            self._send(500, {"error": f"could not save the upload: {e}"})
            return
        JOB.log(f"uploaded {safe} ({human(got)})")
        self._send(200, {"ok": True, "path": dest, "size": got})

    # -- server-sent events: one line per tick, no websocket needed
    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        idx, last_state = 0, None
        try:
            while True:
                new, idx, state = JOB.snapshot(idx)
                for line in new:
                    self._event("log", line)
                if state != last_state:
                    self._event("state", state)
                    last_state = state
                    if state in ("done", "failed", "stopped"):
                        with JOB.lock:
                            self._event("clips", JOB.clips)
                        break
                if not new:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _event(self, name, data):
        payload = json.dumps(data)
        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- serve a produced clip, with byte-range so the video element can seek
    def _serve_file(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, {"error": "no such file"})
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
        length = end - start + 1
        ctype = "video/mp4" if path.lower().endswith((".mp4", ".m4v")) else \
                "application/octet-stream"
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 512, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print("=" * 66)
    print("  Shortsmith — local web UI")
    print("=" * 66)
    print(f"  open:  {url}")
    print("  stop:  Ctrl+C in this window")
    print()
    print("  Nothing is uploaded. The video stays on this PC and the whole")
    print("  thing works offline.")
    print("=" * 66)
    try:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:                                    # noqa: BLE001
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        if JOB.proc and JOB.proc.poll() is None:
            stop_job()
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
