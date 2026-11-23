#!/usr/bin/env python3
"""Local web viewer for the Pixart PAC207 USB microscope (093a:2468).

Runs ffmpeg through libv4l's decoder (the camera's 'P207' format is not readable
by browsers), serves the frames as an MJPEG stream, and exposes the camera's
V4L2 controls (brightness, gain, exposure, auto gain) as sliders, plus a
snapshot button (~/Pictures/microscope/) and a video record button
(~/Videos/microscope/, H.264 .mp4).

    python3 microscope_viewer.py [--port 8642] [--device /dev/videoN] [--open]

Then open http://127.0.0.1:8642 . Listens on localhost only, and refuses requests
whose Host or Origin header is not that local address (so other websites can't
drive the camera from your browser).

Notes for this camera:
- Auto gain ramps gain/exposure to max, where the sensor reads as dark static,
  so the server switches to manual low settings whenever the camera (re)appears.
- Plug it straight into the computer: behind a USB hub it can drop to ~1 fps
  with green static (kernel: "Not enough bandwidth for altsetting").
"""
import argparse
import glob
import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# libv4l's converter decodes the camera's own pixel format; its path differs per distro
PRELOAD_CANDIDATES = ["/usr/lib/libv4l/v4l2convert.so",                     # Arch
                      "/usr/lib/x86_64-linux-gnu/libv4l/v4l2convert.so",    # Debian/Ubuntu
                      "/usr/lib/aarch64-linux-gnu/libv4l/v4l2convert.so",   # Debian/Ubuntu ARM (Raspberry Pi)
                      "/usr/lib64/libv4l/v4l2convert.so"]                   # Fedora
PRELOAD = next((p for p in PRELOAD_CANDIDATES if os.path.exists(p)), PRELOAD_CANDIDATES[0])
SNAP_DIR = Path.home() / "Pictures" / "microscope"
VIDEO_DIR = Path.home() / "Videos" / "microscope"
CTRL_ORDER = ["brightness", "gain_automatic", "gain", "exposure"]
# Settings that work under the microscope (auto gain must go off first,
# otherwise the camera ignores manual gain/exposure).
DEFAULTS = [("gain_automatic", 0), ("gain", 0), ("exposure", 4), ("brightness", 46)]


def find_device():
    for dev in sorted(glob.glob("/dev/video*")):
        r = subprocess.run(["v4l2-ctl", "-d", dev, "--info"], capture_output=True, text=True)
        if "pac207" in r.stdout:
            return dev
    return None


def device_key(dev):
    """Changes when the device node is recreated, i.e. the camera was replugged."""
    try:
        st = os.stat(dev)
        return (dev, st.st_rdev, st.st_ctime_ns)
    except OSError:
        return None


class Recorder:
    """Encodes the live JPEG frames into an H.264 .mp4 with a second ffmpeg."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.path = None
        self.started = 0.0

    @property
    def active(self):
        return self.proc is not None

    def start(self):
        with self.lock:
            if self.proc:
                raise RuntimeError("already recording")
            VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            self.path = VIDEO_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.mp4"
            # frames arrive at an uneven rate; stamp them with the wall clock and
            # resample to a steady 15 fps so the video plays back in real time
            self.proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-use_wallclock_as_timestamps", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-i", "-",
                 "-fps_mode", "cfr", "-r", "15",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-y", str(self.path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.started = time.monotonic()
            return str(self.path)

    def write(self, jpg):
        with self.lock:
            if not self.proc:
                return
            try:
                self.proc.stdin.write(jpg)
            except (BrokenPipeError, ValueError):
                self.proc = None

    def stop(self):
        with self.lock:
            proc, self.proc = self.proc, None
        if not proc:
            raise RuntimeError("not recording")
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait(timeout=30)
        if not self.path.exists() or self.path.stat().st_size == 0:
            raise RuntimeError("recording failed (no frames?)")
        return str(self.path)

    def elapsed(self):
        return round(time.monotonic() - self.started) if self.proc else 0


class Camera:
    """Keeps one ffmpeg process running and holds the latest JPEG frame."""

    def __init__(self, device):
        self.fixed_device = device
        self.device = device
        self.frame = None
        self.frame_id = 0
        self.frame_times = deque(maxlen=30)
        self.cond = threading.Condition()
        self.status = "starting"
        self.applied_for = None
        self.recorder = Recorder()
        self.awb = False      # software auto white balance (ffmpeg grayworld)
        self.proc = None
        self.restart = False  # set when ffmpeg is stopped on purpose to change filters
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            dev = self.fixed_device or find_device()
            if not dev:
                self.status = "camera not found, is it plugged in?"
                self.frame_times.clear()
                time.sleep(2)
                continue
            self.device = dev
            key = device_key(dev)
            if key != self.applied_for:
                # new or replugged camera: auto gain is back on, apply working settings
                try:
                    self.apply_defaults()
                except Exception:
                    pass
                self.applied_for = key
            self.status = "ok"
            env = dict(os.environ, LD_PRELOAD=PRELOAD)
            # grayworld: assumes the scene averages to grey and evens out R/G/B,
            # removing the green cast this sensor has (it has no white balance of its own)
            vf = ["-vf", "grayworld"] if self.awb else []
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "v4l2", "-video_size", "352x288", "-i", dev, *vf,
                 "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            self.proc = proc
            buf = b""
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                buf += chunk
                # split the stream on JPEG start (FFD8) / end (FFD9) markers
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        break
                    jpg = buf[start:end + 2]
                    buf = buf[end + 2:]
                    with self.cond:
                        self.frame = jpg
                        self.frame_id += 1
                        self.frame_times.append(time.monotonic())
                        self.cond.notify_all()
                    self.recorder.write(jpg)
            proc.wait()
            self.proc = None
            if self.restart:  # filter change: start again right away
                self.restart = False
                continue
            self.status = "stream stopped, retrying"
            self.frame_times.clear()
            time.sleep(1)

    def fps(self):
        t = list(self.frame_times)
        if len(t) < 2 or time.monotonic() - t[-1] > 3:
            return 0.0
        return round((len(t) - 1) / (t[-1] - t[0]), 1)

    def wait_frame(self, last_id, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            return self.frame_id, self.frame

    # --- V4L2 controls -------------------------------------------------
    def controls(self):
        if not self.device:
            return {}
        r = subprocess.run(["v4l2-ctl", "-d", self.device, "--list-ctrls"],
                           capture_output=True, text=True)
        out = {}
        for line in r.stdout.splitlines():
            m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)", line)
            if not m:
                continue
            name, kind, rest = m.groups()
            fields = dict(re.findall(r"(\w+)=(\S+)", rest))
            out[name] = {
                "type": kind,
                "min": int(fields.get("min", 0)),
                "max": int(fields.get("max", 1)),
                "value": int(fields.get("value", 0)),
                "inactive": "inactive" in fields.get("flags", ""),
            }
        return {k: out[k] for k in CTRL_ORDER if k in out} | out

    def set_control(self, name, value):
        if not re.fullmatch(r"\w+", name):
            raise ValueError("bad control name")
        r = subprocess.run(["v4l2-ctl", "-d", self.device, "-c", f"{name}={int(value)}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "v4l2-ctl failed")

    def set_awb(self, on):
        on = bool(on)
        if on == self.awb:
            return
        self.awb = on
        proc = self.proc
        if proc:
            self.restart = True
            proc.terminate()  # the loop restarts ffmpeg with the new filter

    def apply_defaults(self):
        for name, value in DEFAULTS:  # one at a time, auto gain first
            self.set_control(name, value)

    def snap(self):
        if not self.frame:
            raise RuntimeError("no frame yet")
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        path = SNAP_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(self.frame)
        return str(path)


class Handler(BaseHTTPRequestHandler):
    cam: Camera = None
    allowed_hosts: set = set()

    def log_message(self, *a):
        pass

    def _local_only(self):
        """Block DNS rebinding (wrong Host) and cross-site requests (foreign Origin)."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in self.allowed_hosts or (origin is not None and origin.split("://", 1)[-1] not in self.allowed_hosts):
            self.send_error(403, "local use only")
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._local_only():
            return
        path = self.path.split("?")[0]
        if path == "/":
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = -1
            try:
                while True:
                    last, jpg = self.cam.wait_frame(last)
                    if jpg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/controls":
            try:
                rec = self.cam.recorder
                self._json({"status": self.cam.status, "fps": self.cam.fps(),
                            "recording": rec.active, "rec_seconds": rec.elapsed(),
                            "awb": self.cam.awb,
                            "controls": self.cam.controls()})
            except Exception as e:
                self._json({"status": str(e), "fps": 0, "recording": False, "controls": {}})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._local_only():
            return
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/set":
                self.cam.set_control(data["name"], data["value"])
                self._json({"ok": True, "controls": self.cam.controls()})
            elif self.path == "/awb":
                self.cam.set_awb(data.get("on"))
                self._json({"ok": True, "awb": self.cam.awb})
            elif self.path == "/reset":
                self.cam.apply_defaults()
                self._json({"ok": True, "controls": self.cam.controls()})
            elif self.path == "/snap":
                self._json({"ok": True, "path": self.cam.snap()})
            elif self.path == "/record/start":
                self._json({"ok": True, "path": self.cam.recorder.start()})
            elif self.path == "/record/stop":
                self._json({"ok": True, "path": self.cam.recorder.stop()})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--device", help="video device (default: auto-detect the pac207 camera)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = ap.parse_args()

    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    Handler.cam = Camera(args.device)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    print(f"microscope viewer on {url}  (ctrl+c to stop)", flush=True)
    if args.open:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if Handler.cam.recorder.active:  # don't leave a half-written video
            Handler.cam.recorder.stop()


if __name__ == "__main__":
    main()
