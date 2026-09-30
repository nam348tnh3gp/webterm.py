#!/usr/bin/env python3
"""
webterm.py v11 - Terminal + màn hình ảo (noVNC) cho Termux.

BẢN FULL UI (FIXED) + NHIỀU GIAO DIỆN:
  * Dùng noVNC vnc.html (full) thay vì vnc_lite.html → có Settings panel.
  * FIX: path phải có '/' đầu: path=/novnc/ws/<token>
  * ~35 theme: Dracula, Nord, Gruvbox, Catppuccin (4), Tokyo Night, Solarized,
    One Dark, Monokai, Material, Ayu, Night Owl, Cobalt2, Rosé Pine (2)…
  * Nút ⚙ noVNC mở full UI trong tab mới.
  * Thêm tham số URL: quality/compression/view_only/shared/clipboard/bell…

Chạy:
    python webterm.py --vnc
    python webterm.py --vnc --display 1080x1920   # dọc cho điện thoại
    python webterm.py --vnc --host 0.0.0.0 --lan
"""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import io
import json
import os
import pty
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import termios
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONF_DIR = os.path.expanduser("~/.config/webterm")
SAVED_BG = os.path.join(CONF_DIR, "bg")
TOKEN_FILE = os.path.join(CONF_DIR, "token")
SCROLLBACK = 256 * 1024
MAX_SESSIONS = 16
MAX_UPLOAD = 25 * 1024 * 1024
MAX_FRAME = 4 * 1024 * 1024
VNC_LOG = "/tmp/webterm-vnc.log"

TOKEN = ""
CFG = {
    "image": None, "port": 8080, "shell": None, "font": 14,
    "dim": 0.55, "blur": 0, "virtual": None,
    "host": "127.0.0.1", "allow_lan": False,
    "vnc": {
        "enabled": False,
        "display": ":99",
        "geometry": "1280x720",
        "depth": 24,
        "vnc_port": 5900,
        "wm": "auto",
        "resize_mode": "scale",   # "scale" | "off"
    }
}
SESSIONS = {}
WM_CANDIDATES = ["startxfce4", "xfce4-session", "startlxde", "lxsession",
                 "startlxqt", "startplasma-x11", "mate-session",
                 "openbox-session", "fluxbox", "icewm-session", "i3"]


# ----------------------------------------------------- Virtual Display (VNC) ---

class VirtualDisplay:
    def __init__(self):
        self.xvfb_proc = None
        self.x11vnc_proc = None
        self.wm_proc = None
        self.display = CFG["vnc"]["display"]
        self.geometry = CFG["vnc"]["geometry"]
        self.depth = CFG["vnc"]["depth"]
        self.vnc_port = CFG["vnc"]["vnc_port"]
        self.wm_name = CFG["vnc"]["wm"]
        self.last_error = ""

    def _which(self, cmd):
        for p in [shutil.which(cmd),
                  f"/data/data/com.termux/files/usr/bin/{cmd}",
                  f"/usr/bin/{cmd}", f"/usr/local/bin/{cmd}"]:
            if p and os.path.exists(p):
                return p
        return None

    def _log(self, tag, msg):
        try:
            with open(VNC_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}\n")
        except Exception:
            pass

    def _open_log(self, tag):
        try:
            f = open(VNC_LOG, "a", buffering=1, encoding="utf-8")
            f.write(f"\n[{time.strftime('%H:%M:%S')}] === {tag} ===\n")
            return f
        except Exception:
            return subprocess.DEVNULL

    def _resolve_wm(self):
        if self.wm_name in (None, "none", ""):
            return None
        if self.wm_name == "auto":
            for cand in WM_CANDIDATES:
                path = self._which(cand)
                if path:
                    return path, cand
            return None
        path = self._which(self.wm_name)
        return (path, self.wm_name) if path else None

    def _wait_for_x_socket(self, timeout=5.0):
        n = self.display.lstrip(":")
        cands = [f"/tmp/.X11-unix/X{n}",
                 f"/data/data/com.termux/files/usr/tmp/.X11-unix/X{n}"]
        t0 = time.time()
        while time.time() - t0 < timeout:
            for sock in cands:
                if os.path.exists(sock):
                    return True
            time.sleep(0.1)
        return False

    def _xvfb_alive(self):
        return self.xvfb_proc is not None and self.xvfb_proc.poll() is None

    def _x11vnc_alive(self):
        return self.x11vnc_proc is not None and self.x11vnc_proc.poll() is None

    def _wm_alive(self):
        return self.wm_proc is not None and self.wm_proc.poll() is None

    def _test_vnc_banner(self, timeout=3.0):
        import socket
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                s = socket.create_connection(("127.0.0.1", self.vnc_port), timeout=1.0)
                s.settimeout(1.5)
                data = s.recv(12)
                s.close()
                if data:
                    return data
            except ConnectionRefusedError:
                time.sleep(0.1)
            except Exception:
                time.sleep(0.2)
        return None

    def start(self):
        if self._xvfb_alive():
            self._ensure_wm()
            return True

        self.last_error = ""
        self._log("start", f"display={self.display} {self.geometry}x{self.depth}")

        xvfb = self._which("Xvfb")
        x11vnc = self._which("x11vnc")
        if not xvfb:
            self.last_error = "Không tìm thấy Xvfb. pkg install xorg-server-xvfb"
            print("❌", self.last_error); return False
        if not x11vnc:
            self.last_error = "Không tìm thấy x11vnc. pkg install x11vnc"
            print("❌", self.last_error); return False

        try:
            n = self.display.lstrip(":")
            for p in (f"/tmp/.X{n}-lock",
                      f"/tmp/.X11-unix/X{n}",
                      f"/data/data/com.termux/files/usr/tmp/.X{n}-lock",
                      f"/data/data/com.termux/files/usr/tmp/.X11-unix/X{n}"):
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except Exception:
                    pass

            self.xvfb_proc = subprocess.Popen(
                [xvfb, self.display, "-screen", "0",
                 f"{self.geometry}x{self.depth}",
                 "-nolisten", "tcp", "-ac", "-noreset"],
                stdout=self._open_log("xvfb"), stderr=subprocess.STDOUT)
            print(f"✅ Xvfb PID {self.xvfb_proc.pid} {self.display} {self.geometry}x{self.depth}")
            self._wait_for_x_socket(5.0)

            x11vnc_cmd = [
                x11vnc,
                "-display", self.display,
                "-forever", "-shared",
                "-rfbport", str(self.vnc_port),
                "-listen", "127.0.0.1",
                "-nopw", "-quiet",
                "-noshm", "-noxdamage",
            ]
            self.x11vnc_proc = subprocess.Popen(
                x11vnc_cmd,
                stdout=self._open_log("x11vnc"), stderr=subprocess.STDOUT)
            print(f"✅ x11vnc PID {self.x11vnc_proc.pid} cổng {self.vnc_port}")

            banner = self._test_vnc_banner(3.0)
            if banner:
                self._log("vnc-test", f"banner OK: {banner!r}")
            else:
                self._log("vnc-test", "⚠️  không đọc được banner RFB")

            self._ensure_wm()
            return True
        except Exception as e:
            self.last_error = f"Lỗi khởi động: {e}"
            self._log("start", self.last_error)
            self.stop(); return False

    def _ensure_wm(self):
        if self._wm_alive():
            return True
        resolved = self._resolve_wm()
        if not resolved:
            return False
        wm_path, wm_label = resolved
        env = dict(os.environ, DISPLAY=self.display)
        dbus = self._which("dbus-run-session")
        cmd = [dbus, "--", wm_path] if dbus else [wm_path]
        try:
            self.wm_proc = subprocess.Popen(
                cmd, env=env, stdout=self._open_log("wm"),
                stderr=subprocess.STDOUT, start_new_session=True)
            print(f"✅ Desktop {wm_label} PID {self.wm_proc.pid}")
            return True
        except Exception as e:
            self.last_error = f"WM {wm_label}: {e}"
            return False

    def start_wm(self):
        return self._ensure_wm()

    def stop_wm(self):
        if self.wm_proc:
            try:
                self.wm_proc.terminate(); self.wm_proc.wait(timeout=3)
            except Exception:
                try: self.wm_proc.kill()
                except Exception: pass
            self.wm_proc = None
            print("🛑 Đã dừng desktop")

    def stop(self):
        self.stop_wm()
        for proc in [self.x11vnc_proc, self.xvfb_proc]:
            if proc:
                try:
                    proc.terminate(); proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        self.xvfb_proc = self.x11vnc_proc = None

    def resize(self, w, h, depth=None):
        w = int(w); h = int(h)
        if not (200 <= w <= 3840 and 200 <= h <= 2160):
            self.last_error = f"Kích thước không hợp lệ: {w}x{h}"
            return False
        new_geom = f"{w}x{h}"
        self._log("resize", f"{self.geometry} → {new_geom}")
        try:
            self.stop_wm()
        except Exception:
            pass
        for proc in [self.x11vnc_proc, self.xvfb_proc]:
            if proc:
                try:
                    proc.terminate(); proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        self.xvfb_proc = self.x11vnc_proc = None
        self.geometry = new_geom
        if depth is not None:
            self.depth = int(depth)
        CFG["vnc"]["geometry"] = self.geometry
        CFG["vnc"]["depth"] = self.depth
        time.sleep(0.5)
        return self.start()

    def is_running(self):
        return self._xvfb_alive() and self._x11vnc_alive()

    def wm_running(self):
        return self._wm_alive()

    def _check_port(self):
        try:
            with open("/proc/net/tcp") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) < 4: continue
                    local = parts[1]
                    if ":" not in local: continue
                    port_hex = local.split(":")[1]
                    if int(port_hex, 16) == self.vnc_port and parts[3] == "0A":
                        return True
            return False
        except Exception:
            return None

    def status(self):
        tail = ""
        try:
            with open(VNC_LOG, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-80:])
        except Exception:
            tail = "(no log)"
        return {
            "xvfb_running": self._xvfb_alive(),
            "xvfb_pid": self.xvfb_proc.pid if self.xvfb_proc else None,
            "x11vnc_running": self._x11vnc_alive(),
            "x11vnc_pid": self.x11vnc_proc.pid if self.x11vnc_proc else None,
            "wm_running": self._wm_alive(),
            "wm_pid": self.wm_proc.pid if self.wm_proc else None,
            "wm_name": self.wm_name,
            "display": self.display,
            "geometry": self.geometry,
            "depth": self.depth,
            "vnc_port": self.vnc_port,
            "vnc_port_listening": self._check_port(),
            "resize_mode": CFG["vnc"]["resize_mode"],
            "novnc_installed": novnc_ready(),
            "novnc_viewer": pick_viewer(),
            "bind_host": CFG["host"], "allow_lan": CFG["allow_lan"],
            "last_error": self.last_error, "log_tail": tail,
        }


VDISPLAY = VirtualDisplay()


# ------------------------------------------------------------- noVNC local ---

NOVNC_VERSION = "1.6.0"
NOVNC_URL = f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz"
NOVNC_DIR = os.path.join(CONF_DIR, "novnc")
NOVNC_KEEP_DIRS = {"core", "vendor", "app"}
NOVNC_KEEP_FILES = {"vnc.html", "vnc_lite.html", "package.json", "LICENSE.txt",
                    "AUTHORS", "README.md"}

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm", ".png": "image/png",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def mime_of(path):
    return MIME_MAP.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def novnc_ready():
    rfb = os.path.join(NOVNC_DIR, "core", "rfb.js")
    if not os.path.isfile(rfb) or os.path.getsize(rfb) < 1000:
        return False
    return os.path.isfile(os.path.join(NOVNC_DIR, "vnc.html"))


def pick_viewer():
    for n in ("vnc.html", "vnc_lite.html"):
        if os.path.isfile(os.path.join(NOVNC_DIR, n)):
            return n
    return None


def ensure_novnc():
    if novnc_ready():
        return True
    print(f"📥 Đang tải noVNC v{NOVNC_VERSION} từ GitHub (chỉ 1 lần)…")
    os.makedirs(CONF_DIR, exist_ok=True)
    try:
        req = urllib.request.Request(NOVNC_URL, headers={"User-Agent": "webterm/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            tar_data = resp.read()
        print(f"   ✅ Đã tải {len(tar_data):,} bytes")
    except Exception as e:
        print(f"⚠️  Không tải được: {type(e).__name__}: {e}")
        print(f"   Tải thủ công: curl -L -o /tmp/novnc.tgz {NOVNC_URL}")
        return False
    tmp = os.path.join(CONF_DIR, "_novnc_tmp")
    try:
        if os.path.isdir(tmp): shutil.rmtree(tmp)
        os.makedirs(tmp)
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:gz") as tar:
            members = []
            for m in tar.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) < 2: continue
                sub = parts[1]
                norm = os.path.normpath(sub)
                if norm.startswith("..") or os.path.isabs(norm): continue
                top = sub.split("/")[0]
                if top in NOVNC_KEEP_DIRS or top in NOVNC_KEEP_FILES:
                    members.append(m)
            if not members:
                raise RuntimeError("Tarball không có gì để extract")
            tar.extractall(path=tmp, members=members)
        roots = os.listdir(tmp)
        if not roots:
            raise RuntimeError("Thư mục giải nén trống")
        src = os.path.join(tmp, roots[0])
        if not os.path.isfile(os.path.join(src, "core", "rfb.js")):
            raise RuntimeError("Thiếu core/rfb.js")
        if os.path.isdir(NOVNC_DIR): shutil.rmtree(NOVNC_DIR)
        try:
            os.rename(src, NOVNC_DIR)
            shutil.rmtree(tmp)
        except OSError:
            shutil.copytree(src, NOVNC_DIR)
            shutil.rmtree(tmp)
        size = os.path.getsize(os.path.join(NOVNC_DIR, "core", "rfb.js"))
        print(f"✅ noVNC v{NOVNC_VERSION} OK (rfb.js={size:,}B, viewer={pick_viewer()})")
        return True
    except Exception as e:
        print(f"⚠️  Lỗi giải nén: {type(e).__name__}: {e}")
        try:
            if os.path.isdir(tmp): shutil.rmtree(tmp)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- Host check ---

def host_allowed(host, allow_lan):
    if not host:
        return False
    h = host.strip()
    if h.startswith("["):
        end = h.find("]")
        if end < 0: return False
        h = h[1:end]
    else:
        h = h.rsplit(":", 1)[0]
    if h in ("127.0.0.1", "localhost", "::1"):
        return True
    if allow_lan:
        try:
            o = [int(p) for p in h.split(".")]
            if len(o) == 4:
                if o[0] == 10: return True
                if o[0] == 172 and 16 <= o[1] <= 31: return True
                if o[0] == 192 and o[1] == 168: return True
                if o[0] == 169 and o[1] == 254: return True
                if o[0] == 127: return True
        except (ValueError, IndexError):
            pass
        if h.startswith(("fc", "fd", "fe8", "fe9", "fea", "feb")):
            return True
    return False


# ---------------------------------------------------------------- WebSocket ---

def frame(op, payload=b""):
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | op, n)
    elif n < 65536:
        head = struct.pack(">BBH", 0x80 | op, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | op, 127, n)
    return head + payload


async def read_frame(r):
    b0, b1 = await r.readexactly(2)
    fin, op = b0 & 0x80, b0 & 0x0F
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await r.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await r.readexactly(8))[0]
    if n > MAX_FRAME:
        raise ValueError("frame too large")
    mask = await r.readexactly(4) if b1 & 0x80 else None
    data = await r.readexactly(n)
    if mask and n:
        key = (mask * (n // 4 + 1))[:n]
        data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
    return fin, op, data


def pick_subprotocol(hdr):
    raw = hdr.get("sec-websocket-protocol", "").strip()
    if not raw:
        return ""
    offered = [p.strip() for p in raw.split(",") if p.strip()]
    if not offered:
        return ""
    for pref in ("binary", "base64"):
        if pref in offered:
            return pref
    return offered[0]


def ws_handshake_response(hdr):
    key = hdr.get("sec-websocket-key", "")
    if not key:
        return None
    acc = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
    sub = pick_subprotocol(hdr)
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {acc}",
    ]
    if sub:
        lines.append(f"Sec-WebSocket-Protocol: {sub}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class Client:
    def __init__(self, w):
        self.w = w
        self.sess = None

    def write(self, fr):
        if not self.w.is_closing():
            self.w.write(fr)

    def text(self, obj):
        self.write(frame(1, json.dumps(obj).encode()))


async def recv_msg(r, cl):
    op0, parts, size = None, [], 0
    while True:
        fin, op, data = await read_frame(r)
        if op == 8:
            return None, None
        if op == 9:
            cl.write(frame(10, data))
            continue
        if op == 10:
            continue
        if op in (1, 2):
            op0, parts, size = op, [data], len(data)
        elif op == 0 and op0 is not None:
            parts.append(data)
            size += len(data)
            if size > MAX_FRAME:
                raise ValueError("message too large")
        else:
            return None, None
        if fin:
            return op0, b"".join(parts)


# ------------------------------------------------------------------ Session ---

def reap(pid, tries=10):
    try:
        done = os.waitpid(pid, os.WNOHANG)[0]
    except ChildProcessError:
        return
    if done:
        return
    if tries:
        asyncio.get_running_loop().call_later(0.5, reap, pid, tries - 1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass


def find_shell():
    for cand in (CFG["shell"], os.environ.get("SHELL"),
                 "/data/data/com.termux/files/usr/bin/bash", "/bin/bash", "/bin/sh"):
        if cand and os.path.exists(cand):
            return cand
    return "/bin/sh"


class Session:
    _next = 1

    def __init__(self, cols, rows):
        self.id = Session._next
        Session._next += 1
        self.buf = bytearray()
        self.out = bytearray()
        self.clients = set()
        self.dead = False
        shell = find_shell()
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        home = env.get("HOME") or os.path.expanduser("~")
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.closerange(3, 256)
                os.chdir(home)
                os.execvpe(shell, [shell, "-l"], env)
            finally:
                os._exit(1)
        self.pid, self.fd = pid, fd
        os.set_blocking(fd, False)
        self.resize(cols, rows)
        asyncio.get_running_loop().add_reader(fd, self.on_read)
        SESSIONS[self.id] = self

    def resize(self, cols, rows):
        if 1 <= cols <= 500 and 1 <= rows <= 300:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    def on_read(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            return self.close()
        self.buf += data
        extra = len(self.buf) - SCROLLBACK
        if extra > 0:
            i = self.buf.find(b"\n", extra)
            del self.buf[: (i + 1 if i != -1 else extra)]
        fr = frame(2, data)
        for c in list(self.clients):
            c.write(fr)

    def write(self, data):
        if self.dead or len(self.out) > 1024 * 1024:
            return
        self.out += data
        self.flush()

    def flush(self):
        loop = asyncio.get_running_loop()
        while self.out and not self.dead:
            try:
                n = os.write(self.fd, self.out)
            except BlockingIOError:
                loop.add_writer(self.fd, self.flush)
                return
            except OSError:
                break
            del self.out[:n]
        if not self.dead:
            loop.remove_writer(self.fd)

    def close(self):
        if self.dead:
            return
        self.dead = True
        SESSIONS.pop(self.id, None)
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.fd); loop.remove_writer(self.fd)
        try:
            os.close(self.fd)
        except OSError:
            pass
        for c in list(self.clients):
            c.text({"t": "exit"}); c.w.close()
        self.clients.clear()
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass
        reap(self.pid)


# --------------------------------------------------------------------- HTTP ---

REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           413: "Payload Too Large", 415: "Unsupported Media Type",
           500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}


def respond(w, code, body=b"", ctype="text/plain; charset=utf-8", cache="no-store"):
    if isinstance(body, str):
        body = body.encode("utf-8")
    w.write(("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
             "Cache-Control: %s\r\nConnection: close\r\n\r\n"
             % (code, REASONS.get(code, "Error"), ctype, len(body), cache)).encode() + body)
    w.close()


def sniff(b):
    if b.startswith(b"\x89PNG"): return "image/png"
    if b.startswith(b"\xff\xd8"): return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b[:3] == b"GIF": return "image/gif"
    return None


async def handle(r, w):
    peer = None
    try:
        peer = w.get_extra_info("peername")
    except Exception:
        pass

    try:
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        hdr = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdr[k.strip().lower()] = v.strip()
    except Exception:
        return w.close()

    try:
        url = urlparse(target)
        q = parse_qs(url.query)
        host = hdr.get("host", "")
        origin = hdr.get("origin")
        is_ws = hdr.get("upgrade", "").lower() == "websocket"

        if not host_allowed(host, CFG["allow_lan"]):
            VDISPLAY._log("http", f"403 Bad host={host!r} from {peer}")
            return respond(w, 403, f"Bad host: {host}")
        if origin:
            try:
                if urlparse(origin).netloc != host:
                    VDISPLAY._log("http", f"403 Bad origin={origin!r} host={host!r}")
                    return respond(w, 403, "Bad origin")
            except Exception:
                pass

        # WebSocket VNC bridge
        if is_ws and (url.path == "/novnc/ws" or url.path.startswith("/novnc/ws/")):
            token_path = url.path[len("/novnc/ws"):].lstrip("/")
            token_query = q.get("k", [""])[0]
            ok = (token_path and secrets.compare_digest(token_path, TOKEN)) or \
                 (token_query and secrets.compare_digest(token_query, TOKEN))
            if not ok:
                VDISPLAY._log("http", f"403 Bad ws token path={token_path!r} query={token_query!r} from {peer}")
                return respond(w, 403, "Forbidden: bad ws token")
            return await ws_proxy_to_vnc(r, w, hdr)

        # File tĩnh noVNC (không cần token)
        if url.path == "/novnc" or url.path.startswith("/novnc/"):
            if not novnc_ready():
                ensure_novnc()
            if not novnc_ready():
                return respond(w, 503, "noVNC chưa sẵn sàng. Xem /tmp/webterm-vnc.log.")
            rel = url.path[len("/novnc"):].lstrip("/")
            if not rel:
                rel = pick_viewer() or "vnc.html"
            parts = rel.split("/")
            if ".." in parts or rel.startswith("/"):
                return respond(w, 403, "Bad path")
            fp = os.path.normpath(os.path.join(NOVNC_DIR, rel))
            root = os.path.normpath(NOVNC_DIR)
            if not (fp == root or fp.startswith(root + os.sep)):
                return respond(w, 403, "Bad path")
            if os.path.isdir(fp):
                for idx in ("vnc.html", "vnc_lite.html", "index.html"):
                    cand = os.path.join(fp, idx)
                    if os.path.isfile(cand):
                        fp = cand; break
                else:
                    return respond(w, 404, "Not found: " + rel)
            if not os.path.isfile(fp):
                return respond(w, 404, "Not found: " + rel)
            with open(fp, "rb") as f:
                data = f.read()
            cache = "no-store" if fp.endswith(".html") else "public, max-age=3600"
            return respond(w, 200, data, mime_of(fp), cache=cache)

        # Token query từ đây
        if not secrets.compare_digest(q.get("k", [""])[0], TOKEN):
            VDISPLAY._log("http", f"403 Bad query token from {peer} path={url.path}")
            return respond(w, 403, "Forbidden")

        # Terminal WebSocket
        if is_ws and url.path == "/ws":
            resp = ws_handshake_response(hdr)
            if not resp:
                return respond(w, 400, "Bad websocket request")
            w.write(resp)
            return await ws_session(r, w)

        # Trang chính
        if method == "GET" and url.path == "/":
            defaults = json.dumps({
                "font": CFG["font"], "dim": CFG["dim"], "blur": CFG["blur"],
                "theme": "default", "fit": "cover", "wake": False,
                "virt": False, "vcols": 120, "vrows": 40, "vfit": True,
                "vnc_enabled": CFG["vnc"]["enabled"],
                "vnc_geom": CFG["vnc"]["geometry"],
                "vnc_depth": CFG["vnc"]["depth"],
                "vnc_wm": CFG["vnc"]["wm"],
                "vnc_resize": CFG["vnc"]["resize_mode"],
            })
            force = {}
            if CFG["virtual"]:
                force = {"virt": True, "vcols": CFG["virtual"][0], "vrows": CFG["virtual"][1]}
            html = (INDEX_HTML.replace("__TOKEN__", TOKEN)
                    .replace("__DEFAULTS__", defaults)
                    .replace("__FORCE__", json.dumps(force)))
            return respond(w, 200, html, "text/html; charset=utf-8")

        # Chẩn đoán
        if url.path == "/vnc/status" and method == "GET":
            st = VDISPLAY.status()
            return respond(w, 200, json.dumps(st, indent=2, ensure_ascii=False),
                           "application/json; charset=utf-8")
        if url.path == "/vnc/novnc-check" and method == "GET":
            rfb = os.path.join(NOVNC_DIR, "core", "rfb.js")
            info = {
                "novnc_dir": NOVNC_DIR,
                "dir_exists": os.path.isdir(NOVNC_DIR),
                "novnc_ready": novnc_ready(),
                "viewer": pick_viewer(),
                "rfb_exists": os.path.isfile(rfb),
                "rfb_size": os.path.getsize(rfb) if os.path.isfile(rfb) else 0,
                "top_level": sorted(os.listdir(NOVNC_DIR)) if os.path.isdir(NOVNC_DIR) else [],
            }
            return respond(w, 200, json.dumps(info, indent=2, ensure_ascii=False),
                           "application/json; charset=utf-8")
        if url.path == "/vnc/novnc-redownload" and method == "GET":
            if os.path.isdir(NOVNC_DIR):
                shutil.rmtree(NOVNC_DIR)
            ok = ensure_novnc()
            return respond(w, 200 if ok else 500, "ok" if ok else "fail")

        # Resize runtime
        if url.path == "/vnc/resize" and method == "GET":
            try:
                ww = int(q.get("w", ["0"])[0])
                hh = int(q.get("h", ["0"])[0])
            except (ValueError, IndexError):
                return respond(w, 400, "Bad size")
            depth = None
            if "d" in q:
                try: depth = int(q["d"][0])
                except Exception: pass
            print(f"🔧 Resize VNC: {ww}x{hh}" + (f"x{depth}" if depth else ""))
            ok = VDISPLAY.resize(ww, hh, depth)
            return respond(w, 200 if ok else 500, "ok" if ok else "fail")

        # Cấu hình runtime (WM, resize_mode)
        if url.path == "/vnc/config" and method == "GET":
            wm = q.get("wm", [None])[0]
            if wm:
                CFG["vnc"]["wm"] = wm
                VDISPLAY.wm_name = wm
                print(f"⚙️  Đổi WM runtime: {wm}")
            rm = q.get("resize", [None])[0]
            if rm in ("scale", "off"):
                CFG["vnc"]["resize_mode"] = rm
                print(f"⚙️  Đổi resize mode: {rm}")
            return respond(w, 200, "ok")

        # Start/stop
        if url.path == "/vnc/start" and method == "GET":
            ensure_novnc()
            ok = VDISPLAY.start()
            return respond(w, 200 if ok else 500, "ok" if ok else "fail")
        if url.path == "/vnc/stop" and method == "GET":
            VDISPLAY.stop(); return respond(w, 200, "ok")
        if url.path == "/vnc/wm/start" and method == "GET":
            ok = VDISPLAY.start_wm()
            return respond(w, 200 if ok else 500, "ok" if ok else "fail")
        if url.path == "/vnc/wm/stop" and method == "GET":
            VDISPLAY.stop_wm(); return respond(w, 200, "ok")

        # Ảnh nền
        if url.path == "/bg" and method == "GET":
            path = CFG["image"]
            if not path or not os.path.isfile(path):
                return respond(w, 404, "No image")
            with open(path, "rb") as f:
                data = f.read()
            return respond(w, 200, data, sniff(data) or "application/octet-stream")
        if url.path == "/bg" and method == "POST":
            n = int(hdr.get("content-length", "0"))
            if n <= 0 or n > MAX_UPLOAD:
                return respond(w, 413, "Bad size")
            data = await asyncio.wait_for(r.readexactly(n), 60)
            if not sniff(data):
                return respond(w, 415, "Not an image")
            os.makedirs(CONF_DIR, exist_ok=True)
            tmp = SAVED_BG + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, SAVED_BG)
            CFG["image"] = SAVED_BG
            return respond(w, 200, "ok")

        respond(w, 404, "Not found")
    except Exception as e:
        VDISPLAY._log("http", f"handle error: {type(e).__name__}: {e}")
        try:
            respond(w, 400, "Bad request")
        except Exception:
            w.close()


# ----------------------------------------------- WebSocket ↔ TCP proxy (VNC) ---

async def ws_proxy_to_vnc(r, w, hdr):
    resp = ws_handshake_response(hdr)
    if not resp:
        return respond(w, 400, "Bad ws request")
    sub = pick_subprotocol(hdr)
    VDISPLAY._log("bridge", f"WS handshake OK, subprotocol={sub!r}")
    w.write(resp)
    try:
        await w.drain()
    except Exception as e:
        VDISPLAY._log("bridge", f"drain failed: {e}")
        try: w.close()
        except Exception: pass
        return

    if not VDISPLAY.is_running():
        VDISPLAY._log("bridge", "VNC chưa chạy, thử start…")
        if not VDISPLAY.start():
            err = VDISPLAY.last_error or "Không khởi động được màn hình ảo"
            VDISPLAY._log("bridge", "start fail: " + err)
            try:
                w.write(frame(1, err.encode("utf-8")))
                w.write(frame(8, b"")); await w.drain()
            except Exception:
                pass
            try: w.close()
            except Exception: pass
            return

    for _ in range(40):
        if VDISPLAY._check_port(): break
        await asyncio.sleep(0.1)

    try:
        vnc_r, vnc_w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", CFG["vnc"]["vnc_port"]), 5)
        VDISPLAY._log("bridge", f"TCP → x11vnc:{CFG['vnc']['vnc_port']} OK")
    except Exception as e:
        VDISPLAY.last_error = f"Không kết nối được x11vnc:{CFG['vnc']['vnc_port']}: {e}"
        VDISPLAY._log("bridge", VDISPLAY.last_error)
        try:
            w.write(frame(1, VDISPLAY.last_error.encode("utf-8")))
            w.write(frame(8, b"")); await w.drain(); w.close()
        except Exception:
            pass
        return

    stats = {"c2v": 0, "v2c": 0}

    async def c2v():
        try:
            while True:
                fin, op, data = await read_frame(r)
                if op == 8:
                    VDISPLAY._log("bridge", "client close frame")
                    break
                if op == 9:
                    w.write(frame(10, data)); continue
                if op == 10: continue
                if data:
                    stats["c2v"] += len(data)
                    vnc_w.write(data); await vnc_w.drain()
        except asyncio.IncompleteReadError:
            VDISPLAY._log("bridge", "client EOF")
        except Exception as e:
            VDISPLAY._log("bridge", f"c2v error: {type(e).__name__}: {e}")
        finally:
            try: vnc_w.close()
            except Exception: pass

    async def v2c():
        try:
            first = True
            while True:
                data = await vnc_r.read(65536)
                if not data:
                    VDISPLAY._log("bridge", "x11vnc closed TCP")
                    break
                if first:
                    VDISPLAY._log("bridge", f"first from VNC: {data[:20]!r}")
                    first = False
                stats["v2c"] += len(data)
                w.write(frame(2, data)); await w.drain()
        except Exception as e:
            VDISPLAY._log("bridge", f"v2c error: {type(e).__name__}: {e}")
        finally:
            try:
                w.write(frame(8, b"")); w.close()
            except Exception: pass

    try:
        await asyncio.gather(c2v(), v2c())
    except Exception as e:
        VDISPLAY._log("bridge", f"gather error: {e}")
    finally:
        VDISPLAY._log("bridge", f"closed. c2v={stats['c2v']}B v2c={stats['v2c']}B")
        try: vnc_w.close()
        except Exception: pass
        try: w.close()
        except Exception: pass


# ---------------------------------------------------------- Terminal session ---

async def ws_session(r, w):
    cl = Client(w)
    try:
        while True:
            op, data = await recv_msg(r, cl)
            if op is None: break
            if op != 1: continue
            try:
                m = json.loads(data)
            except ValueError:
                continue
            t = m.get("t"); s = cl.sess
            if t == "attach":
                cols, rows = int(m.get("c") or 80), int(m.get("r") or 24)
                sess = SESSIONS.get(m.get("id"))
                if sess is None:
                    if len(SESSIONS) >= MAX_SESSIONS:
                        cl.text({"t": "error", "msg": "quá nhiều tab"}); continue
                    sess = Session(cols, rows)
                else:
                    sess.resize(cols, rows)
                if s: s.clients.discard(cl)
                cl.sess = sess; sess.clients.add(cl)
                cl.text({"t": "attached", "id": sess.id})
                if sess.buf: cl.write(frame(2, bytes(sess.buf)))
            elif s is None:
                continue
            elif t == "i":
                s.write(str(m.get("d", "")).encode("utf-8"))
            elif t == "r":
                s.resize(int(m.get("c", 0)), int(m.get("r", 0)))
            elif t == "kill":
                s.close(); break
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
        pass
    finally:
        if cl.sess: cl.sess.clients.discard(cl)
        w.close()


# ---------------------------------------------------------------- INDEX HTML ---

INDEX_HTML = r"""<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content">
<title>Termux Web</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
<style>
  :root { --dim: .55; }
  html, body { margin: 0; height: 100%; overflow: hidden; background: #000; color: #fff; font: 14px monospace; }
  #bg { position: fixed; inset: -24px; background: #000 center / cover no-repeat; }
  #dim { position: fixed; inset: 0; background: rgba(0,0,0,var(--dim)); pointer-events: none; }
  #wrap { position: relative; z-index: 1; height: 100dvh; display: flex; flex-direction: column; }
  #top, #bar { display: flex; gap: 4px; padding: 4px; background: rgba(0,0,0,.45); align-items: center; }
  #tabs { flex: 1; display: flex; gap: 4px; overflow-x: auto; }
  button { padding: 8px 10px; border: 1px solid rgba(255,255,255,.25); border-radius: 6px;
           background: rgba(255,255,255,.08); color: #fff; font: 14px monospace; white-space: nowrap; }
  button.on { background: rgba(80,160,255,.55); }
  #bar { overflow-x: auto; }
  #bar button { flex: 1 0 auto; padding: 10px 12px; }
  #st { font-size: 12px; opacity: .8; padding: 0 4px; }
  #panes { flex: 1; min-height: 0; position: relative; touch-action: pan-y; }
  .pane { position: absolute; inset: 0; padding: 4px; display: none; }
  .pane.active { display: block; }
  .pane.scroll { overflow: auto; }
  .xterm .xterm-viewport { background: transparent !important; }
  #vnc-pane { padding: 0; }
  #vnc-pane iframe { width: 100%; height: 100%; border: none; background: #000; display: block; overflow: hidden; }
  #panel { position: fixed; z-index: 5; left: 0; right: 0; bottom: 0; display: none; padding: 12px;
           background: rgba(20,20,24,.96); border-top: 1px solid rgba(255,255,255,.25);
           max-height: 80vh; overflow-y: auto; }
  #panel.open { display: block; }
  #panel label { display: flex; align-items: center; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
  #panel label span { width: 130px; } #panel input[type=range] { flex: 1; }
  #panel select { flex: 1; padding: 6px; background: #222; color: #fff; }
  #panel input[type=number], #panel input[type=text] { padding: 6px; background: #222;
    color: #fff; border: 1px solid rgba(255,255,255,.2); border-radius: 4px; font: 13px monospace; }
  #panel .row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; } #panel .row button { flex: 1 0 40%; }
  #hint { font-size: 11px; opacity: .65; margin: 4px 0 8px; }
</style></head>
<body>
<div id="bg"></div><div id="dim"></div>
<div id="wrap">
  <div id="top">
    <div id="tabs"></div>
    <span id="st"></span>
    <button id="new">＋</button>
    <button id="vnc-btn" title="Màn hình ảo">🖥️</button>
    <button id="vnc-full" title="Mở noVNC full (Settings) trong tab mới">⚙</button>
    <button id="cfg">☰</button>
  </div>
  <div id="panes"></div>
  <div id="bar">
    <button data-k="esc">Esc</button><button data-k="tab">Tab</button><button id="ctrl">Ctrl</button>
    <button data-k="up">↑</button><button data-k="down">↓</button><button data-k="left">←</button><button data-k="right">→</button>
    <button data-k="home">Home</button><button data-k="end">End</button><button data-k="pgup">PgUp</button><button data-k="pgdn">PgDn</button>
    <button data-s="/">/</button><button data-s="-">-</button><button data-s="|">|</button><button data-s="~">~</button>
    <button id="copy">Chép</button><button id="paste">Dán</button>
  </div>
</div>
<div id="panel">
  <label><span>Cỡ chữ</span><input type="range" id="f_font" min="8" max="28" step="1"></label>
  <label><span>Độ tối</span><input type="range" id="f_dim" min="0" max="0.95" step="0.05"></label>
  <label><span>Làm mờ ảnh</span><input type="range" id="f_blur" min="0" max="20" step="1"></label>
  <label><span>Giao diện</span><select id="f_theme">
    <option value="default">Mặc định</option>
    <optgroup label="Phổ biến">
      <option value="dracula">Dracula</option>
      <option value="nord">Nord</option>
      <option value="gruvbox">Gruvbox Dark</option>
      <option value="gruvboxLight">Gruvbox Light</option>
      <option value="monokai">Monokai</option>
      <option value="oneDark">One Dark</option>
      <option value="solarizedDark">Solarized Dark</option>
      <option value="solarizedLight">Solarized Light</option>
      <option value="tokyoNight">Tokyo Night</option>
      <option value="tokyoNightStorm">Tokyo Night Storm</option>
    </optgroup>
    <optgroup label="Catppuccin">
      <option value="catppuccinMocha">Catppuccin Mocha</option>
      <option value="catppuccinMacchiato">Catppuccin Macchiato</option>
      <option value="catppuccinFrappe">Catppuccin Frappé</option>
      <option value="catppuccinLatte">Catppuccin Latte</option>
    </optgroup>
    <optgroup label="Khác">
      <option value="material">Material</option>
      <option value="ayuMirage">Ayu Mirage</option>
      <option value="nightOwl">Night Owl</option>
      <option value="cobalt2">Cobalt2</option>
      <option value="shadesOfPurple">Shades of Purple</option>
      <option value="synthwave84">Synthwave '84</option>
      <option value="everforest">Everforest</option>
      <option value="rosePine">Rosé Pine</option>
      <option value="rosePineMoon">Rosé Pine Moon</option>
      <option value="kanagawa">Kanagawa</option>
      <option value="githubDark">GitHub Dark</option>
      <option value="githubLight">GitHub Light</option>
      <option value="palenight">Palenight</option>
      <option value="horizon">Horizon</option>
      <option value="snazzy">Snazzy</option>
      <option value="spaceGray">SpaceGray</option>
    </optgroup>
    <optgroup label="Sáng">
      <option value="paperLight">Paper Light</option>
      <option value="solarizedLight2">Solarized Light 2</option>
      <option value="tomorrowLight">Tomorrow Light</option>
    </optgroup>
  </select></label>
  <label><span>Ảnh nền</span><select id="f_fit">
    <option value="cover">Lấp đầy màn hình</option><option value="contain">Vừa khít (không cắt)</option></select></label>
  <label><span>Màn hình</span><input type="checkbox" id="f_wake"> giữ sáng khi đang mở</label>
  <label><span>Màn hình ảo (tab)</span><input type="checkbox" id="f_virt"> cỡ cố định</label>
  <label><span>Cột × Hàng</span><input type="number" id="f_vc" min="20" max="500" style="width:72px"> × <input type="number" id="f_vr" min="5" max="300" style="width:72px"></label>
  <label><span>Hiển thị tab</span><select id="f_vmode">
    <option value="fit">Thu nhỏ vừa màn hình</option><option value="scroll">Giữ cỡ chữ, kéo để xem</option></select></label>
  <hr style="border-color:#444; margin:12px 0">
  <div style="font-weight:bold;margin-bottom:6px">🖥️ Màn hình ảo (Xvfb + x11vnc + desktop)</div>
  <div id="hint">Resize = restart Xvfb (mọi app GUI sẽ đóng). Sau khi resize, noVNC tự kết nối lại sau ~2 giây.</div>
  <label><span>Desktop (WM)</span><select id="f_vnc_wm">
    <option value="auto">Tự động (xfce4 → lxde → …)</option>
    <option value="startxfce4">XFCE4</option>
    <option value="startlxde">LXDE</option>
    <option value="startlxqt">LXQt</option>
    <option value="mate-session">MATE</option>
    <option value="openbox-session">Openbox</option>
    <option value="fluxbox">Fluxbox</option>
    <option value="icewm-session">IceWM</option>
    <option value="i3">i3</option>
    <option value="none">Không (chỉ Xvfb + VNC)</option>
  </select></label>
  <label><span>Độ phân giải</span>
    <input type="number" id="f_vw" min="200" max="3840" step="10" value="1280" style="width:80px">
    × <input type="number" id="f_vh" min="200" max="2160" step="10" value="720" style="width:80px">
    <span style="width:auto">màu</span>
    <input type="number" id="f_vnc_depth" min="8" max="32" step="8" value="24" style="width:60px">
  </label>
  <label><span>Co giãn noVNC</span>
    <select id="f_vnc_resize">
      <option value="scale">Thu nhỏ vừa khung (mặc định)</option>
      <option value="off">Cỡ gốc, kéo/cuộn để xem</option>
    </select>
  </label>
  <label><span>Quality (1–9)</span>
    <input type="number" id="f_vnc_quality" min="0" max="9" step="1" value="6" style="width:60px">
    <span style="width:auto">Compression (0–9)</span>
    <input type="number" id="f_vnc_compress" min="0" max="9" step="1" value="2" style="width:60px">
  </label>
  <div class="row">
    <button id="vnc-start">Mở màn hình ảo</button>
    <button id="vnc-stop">Dừng tất cả</button>
    <button id="apply-res">🔧 Áp dụng độ phân giải</button>
    <button id="match-screen">📐 Khớp khung xem</button>
    <button id="fit-doc">↕ Dọc (điện thoại)</button>
    <button id="fit-land">↔ Ngang (PC)</button>
  </div>
  <div class="row">
    <button id="wm-start">Chỉ chạy desktop</button>
    <button id="wm-stop">Chỉ dừng desktop</button>
    <button id="vnc-reload">↻ Tải lại VNC tab</button>
    <button id="vnc-open-full">⚙ Mở noVNC full (tab mới)</button>
  </div>
  <div class="row"><button id="pick">Đổi ảnh nền…</button><button id="full">Toàn màn hình</button>
    <button id="reset">Đặt lại</button><button id="close">Đóng</button></div>
  <input type="file" id="file" accept="image/*" hidden>
</div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js"></script>
<script>
const K = "__TOKEN__", DEF = __DEFAULTS__, FORCE = __FORCE__;
const $ = s => document.querySelector(s);
let cfg = Object.assign({}, DEF, JSON.parse(localStorage.getItem('wt_cfg') || '{}'), FORCE);
(function(){
  const m = /^(\d+)\s*[xX×]\s*(\d+)$/.exec(cfg.vnc_geom || '');
  cfg._vw = m ? +m[1] : 1280;
  cfg._vh = m ? +m[2] : 720;
})();
const THEMES = {
  default: {},
  dracula: {foreground:'#f8f8f2',cursor:'#f8f8f2',selectionBackground:'rgba(98,114,164,.6)',red:'#ff5555',green:'#50fa7b',yellow:'#f1fa8c',blue:'#bd93f9',magenta:'#ff79c6',cyan:'#8be9fd'},
  nord:    {foreground:'#d8dee9',cursor:'#d8dee9',selectionBackground:'rgba(94,129,172,.6)',red:'#bf616a',green:'#a3be8c',yellow:'#ebcb8b',blue:'#81a1c1',magenta:'#b48ead',cyan:'#88c0d0'},
  gruvbox: {foreground:'#ebdbb2',cursor:'#ebdbb2',selectionBackground:'rgba(146,131,116,.6)',red:'#fb4934',green:'#b8bb26',yellow:'#fabd2f',blue:'#83a598',magenta:'#d3869b',cyan:'#8ec07c'},
  gruvboxLight:{foreground:'#3c3836',cursor:'#3c3836',selectionBackground:'rgba(213,196,161,.6)',red:'#9d0006',green:'#79740e',yellow:'#b57614',blue:'#076678',magenta:'#8f3f71',cyan:'#427b58'},
  monokai: {foreground:'#f8f8f2',cursor:'#f8f8f0',selectionBackground:'rgba(73,72,62,.6)',red:'#f92672',green:'#a6e22e',yellow:'#f4bf75',blue:'#66d9ef',magenta:'#ae81ff',cyan:'#a1efe4'},
  oneDark: {foreground:'#abb2bf',cursor:'#528bff',selectionBackground:'rgba(62,68,81,.6)',red:'#e06c75',green:'#98c379',yellow:'#e5c07b',blue:'#61afef',magenta:'#c678dd',cyan:'#56b6c2'},
  solarizedDark:  {foreground:'#839496',cursor:'#839496',selectionBackground:'rgba(7,54,66,.6)',red:'#dc322f',green:'#859900',yellow:'#b58900',blue:'#268bd2',magenta:'#d33682',cyan:'#2aa198'},
  solarizedLight: {foreground:'#657b83',cursor:'#657b83',selectionBackground:'rgba(238,232,213,.6)',red:'#dc322f',green:'#859900',yellow:'#b58900',blue:'#268bd2',magenta:'#d33682',cyan:'#2aa198'},
  solarizedLight2:{foreground:'#657b83',cursor:'#657b83',selectionBackground:'rgba(238,232,213,.5)',red:'#dc322f',green:'#859900',yellow:'#b58900',blue:'#268bd2',magenta:'#d33682',cyan:'#2aa198'},
  tokyoNight:      {foreground:'#a9b1d6',cursor:'#c0caf5',selectionBackground:'rgba(51,70,124,.6)',red:'#f7768e',green:'#9ece6a',yellow:'#e0af68',blue:'#7aa2f7',magenta:'#bb9af7',cyan:'#7dcfff'},
  tokyoNightStorm: {foreground:'#a9b1d6',cursor:'#c0caf5',selectionBackground:'rgba(65,72,104,.6)',red:'#f7768e',green:'#9ece6a',yellow:'#e0af68',blue:'#7aa2f7',magenta:'#bb9af7',cyan:'#7dcfff'},
  catppuccinMocha:    {foreground:'#cdd6f4',cursor:'#f5e0dc',selectionBackground:'rgba(88,91,112,.6)',red:'#f38ba8',green:'#a6e3a1',yellow:'#f9e2af',blue:'#89b4fa',magenta:'#cba6f7',cyan:'#94e2d5'},
  catppuccinMacchiato:{foreground:'#cad3f5',cursor:'#f4dbd6',selectionBackground:'rgba(91,96,120,.6)',red:'#ed8796',green:'#a6da95',yellow:'#eed49f',blue:'#8aadf4',magenta:'#c6a0f6',cyan:'#8bd5ca'},
  catppuccinFrappe:   {foreground:'#c6d0f5',cursor:'#f2d5cf',selectionBackground:'rgba(98,104,128,.6)',red:'#e78284',green:'#a6d189',yellow:'#e5c890',blue:'#8caaee',magenta:'#ca9ee6',cyan:'#81c8be'},
  catppuccinLatte:    {foreground:'#4c4f69',cursor:'#dc8a78',selectionBackground:'rgba(172,176,190,.6)',red:'#d20f39',green:'#40a02b',yellow:'#df8e1d',blue:'#1e66f5',magenta:'#8839ef',cyan:'#179299'},
  material: {foreground:'#eeffff',cursor:'#ffcc00',selectionBackground:'rgba(80,80,80,.6)',red:'#f07178',green:'#c3e88d',yellow:'#ffcb6b',blue:'#82aaff',magenta:'#c792ea',cyan:'#89ddff'},
  ayuMirage:{foreground:'#cbccc6',cursor:'#ffcc66',selectionBackground:'rgba(64,78,94,.6)',red:'#f28779',green:'#bae67e',yellow:'#ffd580',blue:'#73d0ff',magenta:'#d4bfff',cyan:'#95e6cb'},
  nightOwl: {foreground:'#d6deeb',cursor:'#80cbc4',selectionBackground:'rgba(29,61,90,.6)',red:'#ef5350',green:'#22da6e',yellow:'#addb67',blue:'#82aaff',magenta:'#c792ea',cyan:'#21c7a8'},
  cobalt2:  {foreground:'#ffffff',cursor:'#ffc600',selectionBackground:'rgba(24,53,102,.6)',red:'#ff628c',green:'#3ad900',yellow:'#ffc600',blue:'#0088ff',magenta:'#fb94ff',cyan:'#80ffbb'},
  shadesOfPurple:{foreground:'#ffffff',cursor:'#fad000',selectionBackground:'rgba(74,50,92,.6)',red:'#ec3a37',green:'#3ad900',yellow:'#fad000',blue:'#9d85ff',magenta:'#ff2c70',cyan:'#80ffbb'},
  synthwave84:{foreground:'#f92aad',cursor:'#f92aad',selectionBackground:'rgba(70,42,80,.6)',red:'#f97e72',green:'#72f1b8',yellow:'#fede5d',blue:'#36f9f6',magenta:'#ff7edb',cyan:'#36f9f6'},
  everforest:{foreground:'#d3c6aa',cursor:'#d3c6aa',selectionBackground:'rgba(80,90,75,.6)',red:'#e67e80',green:'#a7c080',yellow:'#dbbc7f',blue:'#7fbbb3',magenta:'#d699b6',cyan:'#83c092'},
  rosePine:{foreground:'#e0def4',cursor:'#e0def4',selectionBackground:'rgba(64,61,82,.6)',red:'#eb6f92',green:'#31748f',yellow:'#f6c177',blue:'#9ccfd8',magenta:'#c4a7e7',cyan:'#ebbcba'},
  rosePineMoon:{foreground:'#e0def4',cursor:'#e0def4',selectionBackground:'rgba(68,65,90,.6)',red:'#eb6f92',green:'#3e8fb0',yellow:'#f6c177',blue:'#9ccfd8',magenta:'#c4a7e7',cyan:'#ea9a97'},
  kanagawa:{foreground:'#dcd7ba',cursor:'#c8c093',selectionBackground:'rgba(84,84,109,.6)',red:'#e82424',green:'#98bb6c',yellow:'#e6c384',blue:'#7e9cd8',magenta:'#957fb8',cyan:'#6a9589'},
  githubDark:{foreground:'#c9d1d9',cursor:'#c9d1d9',selectionBackground:'rgba(51,64,81,.6)',red:'#ff7b72',green:'#7ee787',yellow:'#ffa657',blue:'#79c0ff',magenta:'#d2a8ff',cyan:'#a5d6ff'},
  githubLight:{foreground:'#24292f',cursor:'#24292f',selectionBackground:'rgba(208,215,222,.6)',red:'#cf222e',green:'#116329',yellow:'#9a6700',blue:'#0969da',magenta:'#8250df',cyan:'#1b7c83'},
  palenight:{foreground:'#a6accd',cursor:'#ffcc00',selectionBackground:'rgba(68,71,90,.6)',red:'#f07178',green:'#c3e88d',yellow:'#ffcb6b',blue:'#82aaff',magenta:'#c792ea',cyan:'#89ddff'},
  horizon:  {foreground:'#d5d8da',cursor:'#ffcc00',selectionBackground:'rgba(74,54,74,.6)',red:'#e95678',green:'#29d398',yellow:'#fab795',blue:'#26bbd9',magenta:'#ee64ac',cyan:'#59e3e3'},
  snazzy:   {foreground:'#eff0eb',cursor:'#97979b',selectionBackground:'rgba(73,73,86,.6)',red:'#ff5c57',green:'#5af78e',yellow:'#f3f99d',blue:'#57c7ff',magenta:'#ff6ac1',cyan:'#9aedfe'},
  spaceGray:{foreground:'#b3b8c3',cursor:'#b3b8c3',selectionBackground:'rgba(74,80,96,.6)',red:'#ff5f5f',green:'#a1c181',yellow:'#ffd580',blue:'#6f9bff',magenta:'#c792ea',cyan:'#7dd3fc'},
  paperLight:{foreground:'#333333',cursor:'#333333',selectionBackground:'rgba(200,200,200,.6)',red:'#c82829',green:'#718c00',yellow:'#eab700',blue:'#4271ae',magenta:'#8959a8',cyan:'#3e999f'},
  tomorrowLight:{foreground:'#4d4d4c',cursor:'#4d4d4c',selectionBackground:'rgba(214,214,214,.6)',red:'#c82829',green:'#718c00',yellow:'#eab700',blue:'#4271ae',magenta:'#8959a8',cyan:'#3e999f'}
};
const theme = () => Object.assign({ background: 'rgba(0,0,0,0)' }, THEMES[cfg.theme] || {});
let tabs = [], active = null, ctrl = false, vncTab = null;

function setBg() { $('#bg').style.backgroundImage = 'url("/bg?k=' + K + '&v=' + Date.now() + '")'; }
function applyCfg() {
  localStorage.setItem('wt_cfg', JSON.stringify(cfg));
  document.documentElement.style.setProperty('--dim', cfg.dim);
  $('#bg').style.filter = 'blur(' + cfg.blur + 'px)';
  $('#bg').style.backgroundSize = cfg.fit;
  tabs.forEach(t => { if (t.term) { t.term.options.fontSize = cfg.font; t.term.options.theme = theme(); } });
  fitActive();
}
const tx = (t, o) => { if (t && t.ws && t.ws.readyState === 1) t.ws.send(JSON.stringify(o)); };
function saveIds() { localStorage.setItem('wt_ids', JSON.stringify(tabs.filter(t => t.id != null).map(t => t.id))); }

let RATIO = null;
function ratio() {
  if (RATIO) return RATIO;
  const s = document.createElement('span');
  s.style.cssText = 'position:absolute;visibility:hidden;font:100px monospace;line-height:normal;white-space:pre';
  s.textContent = 'W'.repeat(10); document.body.appendChild(s);
  RATIO = { w: s.offsetWidth / 1000, h: s.offsetHeight / 100 };
  s.remove(); return RATIO;
}
function fitVirtual(t) {
  const c = cfg.vcols, r = cfg.vrows;
  if (t.term.cols !== c || t.term.rows !== r) t.term.resize(c, r);
  if (cfg.vfit) {
    const k = ratio(), w = t.el.clientWidth - 8, h = t.el.clientHeight - 8;
    if (w > 0 && h > 0)
      t.term.options.fontSize = Math.max(3, Math.min(40, Math.floor(Math.min(w / (c * k.w), h / (r * k.h)) * 2) / 2));
  }
}
function fitActive() {
  if (!active) return;
  if (active.isVnc) return;
  active.el.classList.toggle('scroll', !!cfg.virt);
  if (cfg.virt) fitVirtual(active);
  else { try { active.fit.fit(); } catch (e) {} }
  tx(active, { t: 'r', c: active.term.cols, r: active.term.rows });
}
function status(s) { $('#st').textContent = s || ''; }

function renderTabs() {
  const box = $('#tabs'); box.innerHTML = '';
  tabs.forEach((t, i) => {
    const b = document.createElement('button');
    b.textContent = (i + 1) + (t === active ? ' ✕' : '') + (t.isVnc ? ' 🖥️' : '');
    b.className = t === active ? 'on' : '';
    b.onclick = () => t === active ? removeTab(t, true) : select(t);
    box.appendChild(b);
  });
}
function select(t) {
  active = t;
  tabs.forEach(x => x.el.classList.toggle('active', x === t));
  renderTabs(); fitActive(); if (t.term) t.term.focus();
}
function removeTab(t, kill) {
  if (t.gone) return; t.gone = true;
  if (t.isVnc) {
    vncTab = null; t.el.remove();
    tabs = tabs.filter(x => x !== t);
    if (!tabs.length) addTab(null);
    else if (active === t) select(tabs[tabs.length - 1]);
    else renderTabs();
    return;
  }
  if (kill) tx(t, { t: 'kill' });
  try { t.ws.close(); } catch (e) {}
  t.term.dispose(); t.el.remove();
  tabs = tabs.filter(x => x !== t); saveIds();
  if (!tabs.length) addTab(null);
  else if (active === t) select(tabs[tabs.length - 1]);
  else renderTabs();
}
function connect(t) {
  if (t.gone) return;
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?k=' + K);
  ws.binaryType = 'arraybuffer'; t.ws = ws;
  ws.onopen = () => { t.retry = 0; if (t === active) status(''); ws.send(JSON.stringify({ t: 'attach', id: t.id, c: t.term.cols, r: t.term.rows })); };
  ws.onmessage = e => {
    if (typeof e.data !== 'string') return t.term.write(new Uint8Array(e.data));
    const m = JSON.parse(e.data);
    if (m.t === 'attached') { t.term.reset(); if (t.id !== m.id) { t.id = m.id; saveIds(); } }
    else if (m.t === 'exit') removeTab(t, false);
    else if (m.t === 'error') status(m.msg);
  };
  ws.onclose = () => { if (t.gone || t.ws !== ws) return; if (t === active) status('mất kết nối…'); t.retry = (t.retry || 0) + 1; setTimeout(() => connect(t), Math.min(500 * t.retry, 4000)); };
}
function addTab(id) {
  const el = document.createElement('div'); el.className = 'pane'; $('#panes').appendChild(el);
  const term = new Terminal({ allowTransparency: true, cursorBlink: true, fontSize: cfg.font, fontFamily: 'monospace', theme: theme(), scrollback: 5000 });
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  if (window.WebLinksAddon) term.loadAddon(new WebLinksAddon.WebLinksAddon());
  term.open(el);
  const t = { id, term, fit, el, ws: null, isVnc: false };
  term.onData(d => {
    if (ctrl && d.length === 1) { const c = d.toUpperCase().charCodeAt(0); if (c >= 64 && c <= 95) d = String.fromCharCode(c - 64); setCtrl(false); }
    tx(t, { t: 'i', d });
  });
  tabs.push(t); select(t); connect(t); return t;
}

// ─── FIX: path PHẢI có '/' đầu. vnc.html (full) không tự thêm '/' như vnc_lite.html.
function buildVncUrl() {
  const resizeMode = (cfg.vnc_resize === 'off') ? 'off' : 'scale';
  const p = new URLSearchParams({
    path: '/novnc/ws/' + K,          // <-- FIX: có '/' đầu, token không encode
    autoconnect: '1',
    reconnect: '1',
    reconnect_delay: '2000',
    resize: resizeMode,
    show_dot: '1',
    shared: '1',
    view_only: '0',
    view_clip: '1',
    clipboard_up: '1',
    clipboard_down: '1',
    bell: '0',
    quality: String(cfg.vnc_quality || 6),
    compression: String(cfg.vnc_compress || 2),
  });
  return '/novnc/vnc.html?' + p.toString();
}
function addVncTab() {
  if (vncTab) { select(vncTab); return vncTab; }
  const el = document.createElement('div'); el.className = 'pane'; el.id = 'vnc-pane';
  const src = buildVncUrl();
  console.log('[vnc] iframe src =', src);
  el.innerHTML = '<iframe src="' + src + '" allow="clipboard-read; clipboard-write"></iframe>';
  $('#panes').appendChild(el);
  const t = { id: null, term: null, fit: null, el, ws: null, isVnc: true };
  vncTab = t; tabs.push(t); select(t);
  return t;
}
function reloadVncTab() {
  if (!vncTab) return addVncTab();
  const old = vncTab;
  removeTab(old, false);
  return addVncTab();
}
function openVncFull() {
  window.open(buildVncUrl() + '&_=' + Date.now(), '_blank', 'noopener');
}

async function vncStart() {
  await fetch('/vnc/config?k=' + K
              + '&wm=' + encodeURIComponent(cfg.vnc_wm || 'auto')
              + '&resize=' + encodeURIComponent(cfg.vnc_resize || 'scale'));
  status('đang tải noVNC…');
  const r = await fetch('/vnc/start?k=' + K);
  if (r.ok) { status('Màn hình ảo đã khởi động'); addVncTab(); }
  else status('Lỗi khởi động — xem /vnc/status?k=' + K);
}
async function vncStop() {
  await fetch('/vnc/stop?k=' + K);
  status('Đã dừng màn hình ảo');
  if (vncTab) removeTab(vncTab, false);
}
async function wmStart() {
  await fetch('/vnc/config?k=' + K + '&wm=' + encodeURIComponent(cfg.vnc_wm || 'auto'));
  const r = await fetch('/vnc/wm/start?k=' + K);
  status(r.ok ? 'Desktop đã chạy' : 'Không chạy được desktop');
}
async function wmStop() {
  await fetch('/vnc/wm/stop?k=' + K);
  status('Đã dừng desktop');
}
async function applyResolution(ww, hh, d) {
  ww = Math.max(200, Math.min(3840, Math.round(ww)));
  hh = Math.max(200, Math.min(2160, Math.round(hh)));
  d = d || cfg.vnc_depth || 24;
  status('đang restart Xvfb ' + ww + '×' + hh + '…');
  const url = '/vnc/resize?k=' + K + '&w=' + ww + '&h=' + hh + '&d=' + d;
  const r = await fetch(url);
  if (r.ok) {
    cfg._vw = ww; cfg._vh = hh;
    cfg.vnc_geom = ww + 'x' + hh;
    cfg.vnc_depth = d;
    localStorage.setItem('wt_cfg', JSON.stringify(cfg));
    fillPanel();
    status('✅ Đã đổi thành ' + ww + '×' + hh + ' — noVNC sẽ tự kết nối lại sau vài giây');
  } else {
    status('❌ Resize thất bại — xem /tmp/webterm-vnc.log');
  }
}
function paneSize() {
  const box = $('#panes');
  const w = box.clientWidth || window.innerWidth;
  const h = box.clientHeight || window.innerHeight;
  return [w, h];
}
async function matchScreen() {
  const [w, h] = paneSize();
  await applyResolution(w - 4, h - 4);
}
async function fitPortrait() {
  const [w, h] = paneSize();
  const short = Math.min(w, h), long = Math.max(w, h);
  await applyResolution(short - 4, long - 4);
}
async function fitLandscape() {
  const [w, h] = paneSize();
  const short = Math.min(w, h), long = Math.max(w, h);
  await applyResolution(long - 4, short - 4);
}

function setCtrl(v) { ctrl = v; $('#ctrl').classList.toggle('on', v); }

const KEYS = { esc: '\x1b', tab: '\t', up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D', home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~' };
document.querySelectorAll('#bar button[data-k], #bar button[data-s]').forEach(b => b.onclick = () => {
  if (active && active.term) { tx(active, { t: 'i', d: b.dataset.s !== undefined ? b.dataset.s : KEYS[b.dataset.k] }); active.term.focus(); }
});
$('#bar').addEventListener('mousedown', e => e.preventDefault());
$('#ctrl').onclick = () => { setCtrl(!ctrl); if (active && active.term) active.term.focus(); };
$('#copy').onclick = () => { if (active && active.term) navigator.clipboard.writeText(active.term.getSelection()).catch(() => {}); };
$('#paste').onclick = () => { if (active && active.term) navigator.clipboard.readText().then(s => active.term.paste(s)).catch(() => {}); };
$('#new').onclick = () => addTab(null);
$('#vnc-btn').onclick = () => { if (vncTab) { select(vncTab); } else { vncStart(); } };
$('#vnc-full').onclick = openVncFull;

function fillPanel() {
  $('#f_font').value = cfg.font; $('#f_dim').value = cfg.dim; $('#f_blur').value = cfg.blur; $('#f_theme').value = cfg.theme;
  $('#f_fit').value = cfg.fit; $('#f_wake').checked = !!cfg.wake;
  $('#f_virt').checked = !!cfg.virt; $('#f_vc').value = cfg.vcols; $('#f_vr').value = cfg.vrows;
  $('#f_vmode').value = cfg.vfit ? 'fit' : 'scroll';
  $('#f_vnc_wm').value = cfg.vnc_wm || 'auto';
  $('#f_vw').value = cfg._vw || 1280;
  $('#f_vh').value = cfg._vh || 720;
  $('#f_vnc_depth').value = cfg.vnc_depth || 24;
  $('#f_vnc_resize').value = cfg.vnc_resize || 'scale';
  $('#f_vnc_quality').value = cfg.vnc_quality || 6;
  $('#f_vnc_compress').value = cfg.vnc_compress || 2;
}
$('#cfg').onclick = () => { fillPanel(); $('#panel').classList.toggle('open'); };
$('#close').onclick = () => { $('#panel').classList.remove('open'); if (active && active.term) active.term.focus(); };
$('#f_font').oninput = e => { cfg.font = +e.target.value; applyCfg(); };
$('#f_dim').oninput = e => { cfg.dim = +e.target.value; applyCfg(); };
$('#f_blur').oninput = e => { cfg.blur = +e.target.value; applyCfg(); };
$('#f_theme').onchange = e => { cfg.theme = e.target.value; applyCfg(); };
$('#f_fit').onchange = e => { cfg.fit = e.target.value; applyCfg(); };
$('#f_virt').onchange = e => { cfg.virt = e.target.checked; applyCfg(); };
$('#f_vc').onchange = e => { cfg.vcols = Math.max(20, Math.min(500, +e.target.value || 80)); e.target.value = cfg.vcols; applyCfg(); };
$('#f_vr').onchange = e => { cfg.vrows = Math.max(5, Math.min(300, +e.target.value || 24)); e.target.value = cfg.vrows; applyCfg(); };
$('#f_vmode').onchange = e => { cfg.vfit = e.target.value === 'fit'; applyCfg(); };
$('#f_wake').onchange = e => { cfg.wake = e.target.checked; applyCfg(); setWake(cfg.wake); };
$('#full').onclick = () => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {}); };
$('#f_vnc_wm').onchange = e => { cfg.vnc_wm = e.target.value; localStorage.setItem('wt_cfg', JSON.stringify(cfg));
  fetch('/vnc/config?k=' + K + '&wm=' + encodeURIComponent(cfg.vnc_wm)); };
$('#f_vw').onchange = e => { cfg._vw = +e.target.value || 1280; localStorage.setItem('wt_cfg', JSON.stringify(cfg)); };
$('#f_vh').onchange = e => { cfg._vh = +e.target.value || 720; localStorage.setItem('wt_cfg', JSON.stringify(cfg)); };
$('#f_vnc_depth').onchange = e => { cfg.vnc_depth = +e.target.value || 24; localStorage.setItem('wt_cfg', JSON.stringify(cfg)); };
$('#f_vnc_resize').onchange = e => {
  cfg.vnc_resize = e.target.value;
  localStorage.setItem('wt_cfg', JSON.stringify(cfg));
  fetch('/vnc/config?k=' + K + '&resize=' + encodeURIComponent(cfg.vnc_resize));
  if (vncTab) reloadVncTab();
};
$('#f_vnc_quality').onchange = e => {
  cfg.vnc_quality = Math.max(0, Math.min(9, +e.target.value || 6));
  localStorage.setItem('wt_cfg', JSON.stringify(cfg));
  if (vncTab) reloadVncTab();
};
$('#f_vnc_compress').onchange = e => {
  cfg.vnc_compress = Math.max(0, Math.min(9, +e.target.value || 2));
  localStorage.setItem('wt_cfg', JSON.stringify(cfg));
  if (vncTab) reloadVncTab();
};
$('#vnc-start').onclick = vncStart;
$('#vnc-stop').onclick = vncStop;
$('#wm-start').onclick = wmStart;
$('#wm-stop').onclick = wmStop;
$('#apply-res').onclick = () => applyResolution(+$('#f_vw').value || 1280, +$('#f_vh').value || 720, +$('#f_vnc_depth').value || 24);
$('#match-screen').onclick = matchScreen;
$('#fit-doc').onclick = fitPortrait;
$('#fit-land').onclick = fitLandscape;
$('#vnc-reload').onclick = reloadVncTab;
$('#vnc-open-full').onclick = openVncFull;

let wl = null;
async function setWake(on) {
  try { if (on && navigator.wakeLock) { if (!wl) wl = await navigator.wakeLock.request('screen'); } else if (wl) { await wl.release(); wl = null; } } catch (e) { wl = null; }
}

let pinch = 0;
const dist = e => Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
$('#panes').addEventListener('touchstart', e => { if (e.touches.length === 2) pinch = dist(e); }, { passive: true });
$('#panes').addEventListener('touchmove', e => { if (e.touches.length !== 2 || !pinch) return; const d = dist(e), nf = Math.max(8, Math.min(28, Math.round(cfg.font * d / pinch))); if (nf !== cfg.font) { cfg.font = nf; pinch = d; applyCfg(); } }, { passive: true });
$('#panes').addEventListener('touchend', () => { pinch = 0; }, { passive: true });
$('#reset').onclick = () => { cfg = Object.assign({}, DEF); applyCfg(); fillPanel(); };
$('#pick').onclick = () => $('#file').click();
$('#file').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const r = await fetch('/bg?k=' + K, { method: 'POST', body: f });
  if (r.ok) setBg(); else alert('Không tải được ảnh (' + r.status + ')');
  e.target.value = '';
};

new ResizeObserver(fitActive).observe($('#panes'));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    tabs.forEach(t => { if (t.ws && (!t.ws || t.ws.readyState > 1)) connect(t); });
    if (cfg.wake) { wl = null; setWake(true); }
  }
});

setBg(); applyCfg(); if (cfg.wake) setWake(true);
const saved = JSON.parse(localStorage.getItem('wt_ids') || '[]');
if (saved.length) saved.forEach(addTab); else addTab(null);
</script>
</body></html>
"""


# --------------------------------------------------------------------- main ---

async def amain():
    server = await asyncio.start_server(handle, CFG["host"], CFG["port"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    display_host = CFG["host"]
    if display_host == "0.0.0.0":
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); display_host = s.getsockname()[0]; s.close()
        except Exception:
            display_host = "127.0.0.1"
    url = "http://%s:%d/?k=%s" % (display_host, CFG["port"], TOKEN)
    print("✅ Web terminal đang chạy")
    print("🌐 Mở:", url)
    print(f"   bind={CFG['host']} allow_lan={CFG['allow_lan']}")
    if CFG["vnc"]["enabled"]:
        ensure_novnc()
        print(f"🖥️  Màn hình ảo (Xvfb {CFG['vnc']['geometry']}, WM {CFG['vnc']['wm']}, viewer={pick_viewer()})")
    print("   (Ctrl+C để dừng)")
    if shutil.which("termux-open-url"):
        subprocess.Popen(["termux-open-url", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if CFG["vnc"]["enabled"]:
        VDISPLAY.start()

    await stop.wait()
    server.close()
    for s in list(SESSIONS.values()): s.close()
    VDISPLAY.stop()
    print("\n👋 Đã dừng.")


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="Web terminal + màn hình ảo cho Termux")
    ap.add_argument("image", nargs="?", help="ảnh nền")
    ap.add_argument("--dim", type=float, default=CFG["dim"])
    ap.add_argument("--blur", type=int, default=CFG["blur"])
    ap.add_argument("--font", type=int, default=CFG["font"])
    ap.add_argument("--port", type=int, default=CFG["port"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--shell", help="shell muốn chạy")
    ap.add_argument("--stable", action="store_true")
    ap.add_argument("--virtual", metavar="CỘTxHÀNG")
    ap.add_argument("--display", metavar="WxH", default="1280x720")
    ap.add_argument("--depth", type=int, default=24)
    ap.add_argument("--vnc", action="store_true")
    ap.add_argument("--wm", default="auto")
    ap.add_argument("--no-wm", action="store_true")
    a = ap.parse_args()

    CFG.update(dim=max(0.0, min(1.0, a.dim)), blur=max(0, a.blur),
               font=a.font, port=a.port, shell=a.shell, host=a.host)
    CFG["allow_lan"] = a.lan or a.host == "0.0.0.0"
    CFG["vnc"]["geometry"] = a.display
    CFG["vnc"]["depth"] = a.depth
    CFG["vnc"]["enabled"] = a.vnc
    CFG["vnc"]["wm"] = "none" if a.no_wm else a.wm

    if a.virtual:
        m = re.fullmatch(r"(\d+)[xX×](\d+)", a.virtual.strip())
        if not m or not (20 <= int(m.group(1)) <= 500 and 5 <= int(m.group(2)) <= 300):
            print("❌ --virtual CỘTxHÀNG (cột 20-500, hàng 5-300)"); sys.exit(1)
        CFG["virtual"] = (int(m.group(1)), int(m.group(2)))

    if a.image:
        img = os.path.expanduser(a.image)
        if not os.path.isfile(img):
            print("❌ Không tìm thấy ảnh:", img); sys.exit(1)
        CFG["image"] = img
    elif os.path.isfile(SAVED_BG):
        CFG["image"] = SAVED_BG

    if a.stable:
        os.makedirs(CONF_DIR, exist_ok=True)
        if os.path.isfile(TOKEN_FILE):
            TOKEN = open(TOKEN_FILE).read().strip()
        if not TOKEN:
            TOKEN = secrets.token_urlsafe(16)
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(TOKEN)
    else:
        TOKEN = secrets.token_urlsafe(16)

    VDISPLAY.wm_name = CFG["vnc"]["wm"]

    try:
        asyncio.run(amain())
    except OSError as e:
        print("❌ Không mở được %s:%d: %s" % (CFG["host"], CFG["port"], e))
        sys.exit(1)


if __name__ == "__main__":
    main()
