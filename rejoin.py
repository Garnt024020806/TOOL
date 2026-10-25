#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GARNT TOOL REJOIN - Roblox auto-rejoin for UG Phone + Termux.
For your own accounts and devices only."""
import json
import logging
import os
import re
import select
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import parse_qs, urlparse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import termios
    import tty
except ImportError:  # not a POSIX terminal
    termios = tty = None

try:
    import requests
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError as _e:
    print("Required library not found: %s\nRun: pip install -r requirements.txt" % _e)
    sys.exit(1)

VERSION = "1.5"
TERMUX = "/data/data/com.termux/files"
LOGO = r"""
  ____    _    ____  _   _ _____
 / ___|  / \  |  _ \| \ | |_   _|
| |  _  / _ \ | |_) |  \| | | |
| |_| |/ ___ \|  _ <| |\  | | |
 \____/_/   \_\_| \_\_| \_| |_|
"""

for _p in ("/system/bin", "/system/xbin"):
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + ":" + _p


# ----------------------------------------------------------------- paths
def pick_base():
    """Prefer /sdcard/Download/RejoinTool; fall back to Termux home. Works from any cwd."""
    cands = [os.path.join(t, "RejoinTool") for t in ("/sdcard/Download", "/storage/emulated/0/Download")]
    for d in cands:
        try:
            for sub in ("logs", "screenshots"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
            probe = os.path.join(d, ".probe")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return d, True
        except OSError:
            continue
    home = TERMUX + "/home" if os.path.isdir(TERMUX + "/home") else os.path.expanduser("~")
    d = os.path.join(home, "RejoinTool")
    for sub in ("logs", "screenshots"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d, False


BASE, STORAGE_OK = pick_base()
CFG_PATH = os.path.join(BASE, "config.json")
COOKIE_PATH = os.path.join(BASE, "cookie.txt")
LOG_DIR = os.path.join(BASE, "logs")
SHOT_DIR = os.path.join(BASE, "screenshots")
TMP_DIR = TERMUX + "/usr/tmp" if os.path.isdir(TERMUX + "/usr/tmp") else tempfile.gettempdir()
MAX_SHOTS = 20

console = Console()
log = logging.getLogger("rejoin")

# ----------------------------------------------------------------- secrets scrubbing
SECRETS = set()  # raw cookies loaded in memory; used only to scrub output
_PATS = [
    (re.compile(r"_\|WARNING:[^\s\"';,]+"), "***"),
    (re.compile(r"(\.ROBLOSECURITY=)[^\s;\"']+"), r"\1***"),
    (re.compile(r"(webhooks/\d+/)[\w-]+"), r"\1***"),
    (re.compile(r"[A-Fa-f0-9]{150,}"), "***"),
]


def scrub(s):
    s = str(s)
    for c in list(SECRETS):
        if c:
            s = s.replace(c, "***")
    for p, r in _PATS:
        s = p.sub(r, s)
    return s


def mask(cookie):
    return "********  (%d chars)" % len(cookie)


class ScrubFilter(logging.Filter):
    def filter(self, record):
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def setup_logging():
    log.setLevel(logging.INFO)
    log.propagate = False
    h = TimedRotatingFileHandler(os.path.join(LOG_DIR, "rejoin.log"), when="midnight",
                                 backupCount=7, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    h.addFilter(ScrubFilter())
    log.addHandler(h)


# ----------------------------------------------------------------- config
DEFAULTS = {
    "game_link": "",
    "check_interval": 30,
    "launch_delay": 15,
    "package_prefix": "com.roblox.",
    "packages": [],          # empty = run every detected package
    "detected": [],
    "backoff_base": 10,
    "backoff_max": 300,
    "stable_reset": 300,
    "join_grace": 90,
    "auto": {
        "start_on_launch": False,
        "wake_lock": True,
        "force_stop": True,
        "foreground_check": False,
        "screenshot_on_crash": True,
        "cookie_login_on_start": False,
        "presence_check": True,
        "timed_rejoin_min": 0,
    },
    "heartbeat": {"enabled": True, "port": 8765, "interval": 15, "timeout": 90},
    "autoexec": {"auto_create": True, "enabled_once": False, "filename": "heartbeat.lua",
                 "dirs": ["/storage/emulated/0/Delta/Autoexecute"]},   # {pkg} in a path = one folder per clone
    "performance": {"low_cpu": True, "ui_refresh_sec": 2},
    "console_webhook": {"enabled": False, "url": "", "flush_sec": 10},   # blank url = reuse the main webhook
    "dialog_detect": {"enabled": True, "every_sec": 30, "confirm": 2},
    "screen_guard": {"enabled": True, "check_sec": 60, "fix_stacked": True, "kill_stray": False,
                     "keep": ["com.termux", "com.android.systemui", "com.topjohnwu.magisk"]},
    "ram": {"enabled": True, "interval_min": 30, "min_free_mb": 300, "kill_cached": False},
    "webhook": {
        "url": "",
        "enabled": False,
        "send_screenshot": True,
        "periodic_min": 10,   # status report + screenshot every N minutes (0 = off)
        "events": {"start": True, "crash": True, "rejoin": True, "stop": True, "status": True},
    },
}


def merge(base, new):
    out = {}
    for k, v in base.items():
        nv = new.get(k) if isinstance(new, dict) else None
        out[k] = merge(v, nv if isinstance(nv, dict) else {}) if isinstance(v, dict) else (nv if nv is not None else v)
    return out


class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.d = self.load()

    def load(self):
        raw = {}
        try:
            with open(CFG_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("bad config")
        except FileNotFoundError:
            raw = {}
        except Exception:
            try:
                shutil.copy(CFG_PATH, CFG_PATH + ".bak")
            except OSError:
                pass
            raw = {}
        return merge(DEFAULTS, raw)

    def save(self):
        with self.lock:
            tmp = CFG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CFG_PATH)


# ----------------------------------------------------------------- shell / root
class Env:
    root = False
    su = None


ENV = Env()


def find_su():
    p = shutil.which("su")
    if p:
        return p
    for c in ("/system/xbin/su", "/system/bin/su", "/sbin/su", "/su/bin/su", "/debug_ramdisk/su"):
        if os.path.exists(c):
            return c
    return None


def sh(cmd, root=False, timeout=20):
    """Run a shell command (as root through su when requested). Returns (rc, stdout+stderr)."""
    if root and os.geteuid() != 0:
        if not ENV.su:
            return 1, ""
        argv = [ENV.su, "-c", cmd]
    else:
        argv = ["sh", "-c", cmd]
    try:
        p = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as e:
        return 1, scrub(e)


def detect_root():
    ENV.su = find_su()
    if os.geteuid() == 0:
        ENV.root = True
        return
    if not ENV.su:
        ENV.root = False
        return
    _, out = sh("id", root=True, timeout=20)  # = su -c id
    ENV.root = "uid=0" in out


_cache = {}
_clock = threading.RLock()


def cached(key, ttl, fn):
    """TTL cache; the lock makes simultaneous callers share a single (expensive) call."""
    with _clock:
        v = _cache.get(key)
        if v and time.time() - v[0] < ttl:
            return v[1]
        r = fn()
        _cache[key] = (time.time(), r)
        return r


FG_CMD = "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'"
SNAP = {"fg": False, "ttl": 5}


def snapshot():
    """ONE shell call per check cycle: process names (+ foreground info when enabled)."""
    def run():
        cmd = "ps -A -o NAME 2>/dev/null"
        if SNAP["fg"]:
            cmd += "; echo '@@FG@@'; " + FG_CMD
        _, out = sh(cmd, root=ENV.root, timeout=40)
        names, _, fg = out.partition("@@FG@@")
        procs = {l.strip() for l in names.splitlines() if l.strip()}
        return {"procs": procs, "fg": fg, "ps_ok": len(procs) > 5}
    return cached(("snap", SNAP["fg"]), SNAP["ttl"], run)


def foreground_text():
    if SNAP["fg"]:
        return snapshot()["fg"]
    return cached("fg", 5, lambda: sh(FG_CMD, root=True, timeout=25)[1])


def proc_alive(pkg):
    snap = snapshot()
    if pkg in snap["procs"]:
        return True
    if ENV.root and snap["ps_ok"]:
        return False
    rc, out = sh("pidof " + shlex.quote(pkg), root=ENV.root, timeout=10)
    if rc == 127 or "not found" in out:
        return False
    return bool(re.search(r"\b\d+\b", out))


def mem_available_mb():
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                d[k] = int(v.split()[0])
        kb = d.get("MemAvailable") or (d.get("MemFree", 0) + d.get("Cached", 0))
        return int(kb / 1024)
    except Exception:
        return None


def AM():
    """Use the system `am` when root: Termux's wrapper `am` often fails under su."""
    return "/system/bin/am" if ENV.root and os.path.exists("/system/bin/am") else "am"


RAM_LAST = {"detail": ""}


def drop_ram(kill_cached=False):
    """Root only. Drop page cache (and optionally kill cached background apps).
    -> (ok, before_mb, after_mb); a per-step report is left in RAM_LAST["detail"]."""
    if not ENV.root:
        RAM_LAST["detail"] = "Root required"
        return False, None, None
    before = mem_available_mb()
    steps = []
    rc, out = sh("sync; echo 3 > /proc/sys/vm/drop_caches", root=True, timeout=40)
    steps.append(("drop_caches", rc == 0, out))
    if rc != 0:  # read-only /proc/sys (containers / cloud phones) or SELinux: try sysctl
        rc, out = sh("sysctl -w vm.drop_caches=3", root=True, timeout=20)
        steps.append(("sysctl", rc == 0, out))
    if kill_cached:
        rc, out = sh(AM() + " kill-all", root=True, timeout=30)
        steps.append(("kill-all", rc == 0, out))
    time.sleep(1)
    RAM_LAST["detail"] = "; ".join(
        "%s: %s" % (n, "ok" if ok else (" ".join(m.split())[:90] or "failed")) for n, ok, m in steps)
    log.info("ram steps: %s", RAM_LAST["detail"])
    return any(ok for _, ok, _ in steps), before, mem_available_mb()


def sh_bytes(cmd, root=False, timeout=25):
    if root and os.geteuid() != 0:
        if not ENV.su:
            return b""
        argv = [ENV.su, "-c", cmd]
    else:
        argv = ["sh", "-c", cmd]
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout or b""
    except Exception:
        return b""


def parse_raw_frame(data):
    """Raw `screencap` output: header (w, h, format[, colorspace]) + RGBA pixels. -> (w, h, fmt, offset) or None"""
    if len(data) < 16:
        return None
    w, h, fmt = struct.unpack_from("<III", data, 0)
    if not (0 < w < 10000 and 0 < h < 10000):
        return None
    for off in (12, 16):
        if len(data) - off == w * h * 4:
            return w, h, fmt, off
    return None


def find_disconnect_dialog(data, w, h, off, step=None):
    """Detect Roblox's error prompt ("Disconnected ... Error Code: 277"): a big uniform dark-gray panel
    (~#393B3D) that contains a wide white button. Works on a sub-sampled pixel grid (cheap, no extra libs)."""
    step = step or max(2, min(w, h) // 150)
    gw, gh = w // step, h // step
    stride = w * 4
    G, Wt = bytearray(gw * gh), bytearray(gw * gh)
    for gy in range(gh):
        row = off + gy * step * stride
        base = gy * gw
        for gx in range(gw):
            i = row + gx * step * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            if r > 231 and g > 231 and b > 231:
                Wt[base + gx] = 1
            elif 44 <= r <= 72 and 44 <= g <= 74 and 46 <= b <= 76 and max(r, g, b) - min(r, g, b) <= 10:
                G[base + gx] = 1
    seen, best = bytearray(gw * gh), None
    for start in range(gw * gh):
        if not G[start] or seen[start]:
            continue
        stack, n = [start], 0
        seen[start] = 1
        x0 = y0 = 1 << 30
        x1 = y1 = -1
        while stack:
            p = stack.pop()
            n += 1
            y, x = divmod(p, gw)
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for q, ok in ((p - 1, x > 0), (p + 1, x < gw - 1), (p - gw, y > 0), (p + gw, y < gh - 1)):
                if ok and G[q] and not seen[q]:
                    seen[q] = 1
                    stack.append(q)
        if best is None or n > best[0]:
            best = (n, x0, y0, x1, y1)
    if not best:
        return False
    n, x0, y0, x1, y1 = best
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if bw < gw * 0.08 or bh < gh * 0.05 or bw > gw * 0.95 or bh > gh * 0.8:
        return False
    if n < 0.55 * bw * bh or not 0.9 <= bw / bh <= 4.0:
        return False
    run_best = white = 0
    for y in range(y0 + int(bh * 0.45), y1 + 1):
        run = 0
        for x in range(x0, x1 + 1):
            if Wt[y * gw + x]:
                run += 1
                white += 1
                run_best = max(run_best, run)
            else:
                run = 0
    return run_best >= max(4, int(bw * 0.12)) and white >= 15


def detect_screen_dialog():
    """Root only. -> (found: True/False/None, info). None = frame could not be read."""
    data = sh_bytes("screencap", root=True)
    fr = parse_raw_frame(data)
    if not fr or fr[2] not in (1, 2, 5):
        return None, "Could not read the screen capture"
    w, h, fmt, off = fr
    return find_disconnect_dialog(data, w, h, off), "%dx%d format=%d" % (w, h, fmt)


def parse_focus(text):
    top = ""
    for line in text.splitlines():
        m = re.search(r"\bu\d+\s+([\w.]+)/", line)
        if not m:
            continue
        if "mCurrentFocus" in line:
            return m.group(1)
        top = top or m.group(1)
    return top


GUARD_CMD = ("echo '@@FOCUS@@'; dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp'; "
             "echo '@@DLG@@'; dumpsys window windows 2>/dev/null | grep -E 'Application Error|Application Not Responding|AppErrorDialog|AppNotRespondingDialog'; "
             r"echo '@@TASK@@'; dumpsys activity activities 2>/dev/null | grep -E '\* (Task|TaskRecord)\{'")


def parse_guard(out, prefix):
    parts = re.split(r"@@(FOCUS|DLG|TASK)@@", out)
    sec = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    dialogs = set()
    for l in sec.get("DLG", "").splitlines():
        if not l.strip():
            continue
        m = re.search(r"(?:Application Error|Application Not Responding|Not Responding)[: ]+([\w.]+)", l)
        dialogs.add(m.group(1) if m else "?")
    tasks = {}
    for l in sec.get("TASK", "").splitlines():
        m = re.search(r"\bA=(?:\d+:)?([\w.]+)", l)
        if m:
            tid = re.search(r"#(\d+)", l)
            tasks.setdefault(m.group(1), set()).add(tid.group(1) if tid else l.strip())
    return {"top": parse_focus(sec.get("FOCUS", "")), "dialogs": dialogs,
            "stacked": sorted(p for p, ids in tasks.items() if len(ids) > 1 and p.startswith(prefix))}


def home_pkg():
    def run():
        _, o = sh("cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME",
                  root=True, timeout=15)
        for l in reversed(o.strip().splitlines()):
            m = re.match(r"([\w.]+)/", l.strip())
            if m:
                return m.group(1)
        return ""
    return cached("home", 3600, run)


def screen_scan(cfg):
    _, out = sh(GUARD_CMD, root=True, timeout=40)
    return parse_guard(out, cfg.d["package_prefix"])


def stray_pkg(cfg, info):
    g, top = cfg.d["screen_guard"], info["top"]
    if not (g["kill_stray"] and top):
        return ""
    if top.startswith(cfg.d["package_prefix"]) or top in g["keep"] or top == home_pkg():
        return ""
    return top


def screen_clear(cfg, info, force_all=False):
    """Close system dialogs and force-stop apps causing stacked/overlapping windows. -> list of action descriptions."""
    g, prefix, q = cfg.d["screen_guard"], cfg.d["package_prefix"], shlex.quote
    sh(AM() + " broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS", root=True, timeout=20)
    acts = ["Closed system dialogs"]
    for p in sorted(info["dialogs"]):
        if p != "?" and (p.startswith(prefix) or (g["kill_stray"] and p not in g["keep"])):
            sh(AM() + " force-stop " + q(p), root=True, timeout=20)
            acts.append("force-stop %s (error dialog)" % p)
    if g["fix_stacked"] or force_all:
        for p in info["stacked"]:
            sh(AM() + " force-stop " + q(p), root=True, timeout=20)
            acts.append("force-stop %s (stacked window)" % p)
    sp = stray_pkg(cfg, info)
    if sp:
        sh(AM() + " force-stop " + q(sp), root=True, timeout=20)
        acts.append("force-stop %s (covering the screen)" % sp)
    return acts


_NICED = False


def apply_perf(cfg):
    """Lower this process priority so the game keeps the CPU (takes effect once per run)."""
    global _NICED
    if cfg.d["performance"]["low_cpu"] and not _NICED:
        try:
            os.nice(10)
            _NICED = True
        except (OSError, AttributeError):
            pass


def wake_lock(on):
    if on:
        sh("termux-wake-lock", timeout=10)
        if ENV.root:
            sh("echo garnt_rejoin > /sys/power/wake_lock", root=True)
    else:
        sh("termux-wake-unlock", timeout=10)
        if ENV.root:
            sh("echo garnt_rejoin > /sys/power/wake_unlock", root=True)


# ----------------------------------------------------------------- packages / links
def scan_packages(prefix):
    out = ""
    for root in ((True, False) if ENV.root else (False,)):
        for cmd in ("pm list packages", "cmd package list packages"):
            _, o = sh(cmd, root=root, timeout=40)
            if "package:" in o:
                out = o
                break
        if out:
            break
    pk = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            name = line.split(":", 1)[1].strip()
            if name.startswith(prefix):
                pk.add(name)
    return sorted(pk, key=lambda p: (p != "com.roblox.client", p))


def parse_link(link):
    """Return (uri, place_id) or None. Accepts place id, roblox:// link, /games/ link, private-server and share links."""
    s = (link or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{3,}", s):
        return "roblox://placeID=" + s, s
    if s.lower().startswith(("roblox://", "robloxmobile://")):
        m = re.search(r"place[iI][dD]=(\d+)", s)
        return s, (m.group(1) if m else "")
    m = re.search(r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/(\d+)", s)
    if m:
        pid = m.group(1)
        c = re.search(r"privateServerLinkCode=([\w-]+)", s)
        return "roblox://placeID=%s%s" % (pid, "&linkCode=" + c.group(1) if c else ""), pid
    if re.match(r"https?://(www\.)?(roblox\.com|ro\.blox\.com)/", s):
        return s, ""
    return None


def open_game(pkg, uri, force_stop):
    q = shlex.quote
    if ENV.root and force_stop:
        sh(AM() + " force-stop " + q(pkg), root=True, timeout=20)
        time.sleep(2)
    cmd = AM() + " start -a android.intent.action.VIEW -d %s -p %s -f 0x10008000" % (q(uri), q(pkg))
    rc, out = sh(cmd, root=ENV.root, timeout=30)
    if rc != 0 or re.search(r"Error|Exception|unable to resolve", out, re.I):
        rc, out2 = sh("monkey -p %s -c android.intent.category.LAUNCHER 1" % q(pkg), root=ENV.root, timeout=30)
        if rc != 0:
            return False, scrub((out + out2).strip()[:200])
        return True, "App opened but the game link did not work (used launcher instead)"
    return True, "ok"


def take_shot(pkg):
    if not ENV.root:
        return None
    name = "%s_%s.png" % (datetime.now().strftime("%Y%m%d_%H%M%S"), pkg.split(".")[-1])
    path = os.path.join(SHOT_DIR, name)
    sh("screencap -p " + shlex.quote(path), root=True, timeout=20)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        prune_shots()
        return path
    return None


def prune_shots():
    try:
        files = sorted((os.path.join(SHOT_DIR, f) for f in os.listdir(SHOT_DIR) if f.endswith(".png")),
                       key=os.path.getmtime, reverse=True)
        for f in files[MAX_SHOTS:]:
            os.remove(f)
    except OSError:
        pass


# ----------------------------------------------------------------- cookies / roblox api
COOKIE_TEMPLATE = (
    "# GARNT TOOL REJOIN - cookie.txt\n"
    "# Put the .ROBLOSECURITY cookie of your own accounts here, 1 line per account\n"
    "# Order follows the detected instances (first line = first package). Never share this file\n"
    "# Blank lines and lines starting with # are skipped\n"
)


def clean_cookie(line):
    c = line.strip().strip("\"'").rstrip(";").strip()
    if c.upper().startswith(".ROBLOSECURITY="):
        c = c.split("=", 1)[1].strip().strip("\"'")
    return c


def ensure_cookie_file():
    if not os.path.exists(COOKIE_PATH):
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            f.write(COOKIE_TEMPLATE)
        return True
    return False


def read_cookies():
    res = []
    try:
        with open(COOKIE_PATH, encoding="utf-8-sig") as f:
            for line in f:
                l = line.strip()
                if not l or l.startswith("#"):
                    continue
                res.append(clean_cookie(l))
    except OSError:
        return []
    SECRETS.update(res)
    return res


def cookie_pairs(cfg):
    """Map cookie lines to ALL detected instances in order."""
    ck = read_cookies()
    return [(pkg, ck[i]) for i, pkg in enumerate(cfg.d["detected"]) if i < len(ck)], len(ck)


UA = "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"


def make_session(cookie):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
    return s


def who_am_i(s):
    """-> (user_id, name, error)"""
    try:
        r = s.get("https://users.roblox.com/v1/users/authenticated", timeout=12)
        if r.status_code == 401:
            return None, None, "Cookie expired/invalid"
        if r.status_code != 200:
            return None, None, "HTTP %d" % r.status_code
        j = r.json()
        return j["id"], j.get("name", "?"), None
    except Exception as e:
        return None, None, scrub(e)[:80]


def in_game(s, uid):
    """True / False / None(unknown)"""
    url = "https://presence.roblox.com/v1/presence/users"
    try:
        r = s.post(url, json={"userIds": [uid]}, timeout=12)
        if r.status_code == 403 and "x-csrf-token" in r.headers:
            s.headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
            r = s.post(url, json={"userIds": [uid]}, timeout=12)
        if r.status_code != 200:
            return None
        return r.json()["userPresences"][0].get("userPresenceType") == 2
    except Exception:
        return None


def chrome_now():
    return int((time.time() + 11644473600) * 1000000)


def _edit_cookie_db(dbfile, cookie):
    con = sqlite3.connect(dbfile, timeout=10)
    try:
        cols = con.execute("PRAGMA table_info(cookies)").fetchall()
        if not cols:
            raise RuntimeError("cookies table not found")
        now = chrome_now()
        vals = {
            "creation_utc": now, "host_key": ".roblox.com", "name": ".ROBLOSECURITY", "value": cookie,
            "encrypted_value": b"", "path": "/", "expires_utc": now + 400 * 86400 * 1000000,
            "is_secure": 1, "is_httponly": 1, "last_access_utc": now, "has_expires": 1,
            "is_persistent": 1, "persistent": 1, "priority": 1, "samesite": -1, "source_scheme": 2,
            "source_port": 443, "last_update_utc": now, "top_frame_site_key": "", "is_same_party": 0,
            "source_type": 0,
        }
        con.execute("DELETE FROM cookies WHERE name='.ROBLOSECURITY'")
        names, params = [], []
        for _cid, name, typ, notnull, dflt, _pk in cols:
            if name in vals:
                names.append(name)
                params.append(vals[name])
            elif notnull and dflt is None:
                t = (typ or "").upper()
                names.append(name)
                params.append(b"" if "BLOB" in t else ("" if ("TEXT" in t or "CHAR" in t) else 0))
        con.execute("INSERT INTO cookies (%s) VALUES (%s)" % (",".join(names), ",".join("?" * len(names))), params)
        con.commit()
    finally:
        con.close()


def inject_cookie(pkg, cookie):
    """Root only: write .ROBLOSECURITY into the cloned app's WebView cookie DB."""
    if not ENV.root:
        return False, "Root access required"
    q = shlex.quote
    _, out = sh("find /data/data/%s -maxdepth 4 -type f -name Cookies 2>/dev/null | head -n 1" % q(pkg), root=True)
    path = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not path:
        return False, "Cookie database not found (open the app once and leave it running)"
    _, own = sh("stat -c '%%u:%%g' /data/data/%s" % q(pkg), root=True)
    own = own.strip()
    if not re.fullmatch(r"\d+:\d+", own):
        return False, "Could not read the app folder owner"
    sh(AM() + " force-stop " + q(pkg), root=True)
    tmp = os.path.join(TMP_DIR, "garnt_%s.db" % pkg.replace(".", "_"))
    try:
        rc, o = sh("cp %s %s && chmod 666 %s" % (q(path), q(tmp), q(tmp)), root=True)
        if rc != 0:
            return False, "Failed to copy the database"
        _edit_cookie_db(tmp, cookie)
        rc, o = sh("cp %s %s && chown %s %s && chmod 660 %s" % (q(tmp), q(path), own, q(path), q(path)), root=True)
        if rc != 0:
            return False, "Failed to write the database back"
        sh("rm -f %s-journal %s-wal %s-shm; restorecon %s" % (q(path), q(path), q(path), q(path)), root=True)
        return True, "Cookie injected"
    except Exception as e:
        return False, scrub(e)[:100]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def cookie_login(pairs):
    res = []
    for pkg, ck in pairs:
        uid, name, err = who_am_i(make_session(ck))
        if not uid:
            res.append((pkg, False, "Cookie unusable: %s" % err))
            continue
        ok, msg = inject_cookie(pkg, ck)
        res.append((pkg, ok, ("%s: %s" % (name, msg)) if ok else msg))
    return res


# ----------------------------------------------------------------- discord webhook
HOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
EV_COLOR = {"status": 0xF1C40F, "start": 0x2ECC71, "crash": 0xE74C3C, "rejoin": 0x3498DB, "stop": 0x95A5A6, "test": 0x9B59B6}


class Hook:
    def __init__(self, cfg):
        self.cfg = cfg

    def send(self, event, title, desc, fields=None, shot=None, sync=False):
        w = self.cfg.d["webhook"]
        if event != "test" and not (w["enabled"] and w["events"].get(event)):
            return True
        if not w["url"]:
            return False
        embed = {"title": scrub(title), "description": scrub(desc), "color": EV_COLOR.get(event, 0),
                 "timestamp": datetime.now(timezone.utc).isoformat(), "footer": {"text": "GARNT TOOL REJOIN"},
                 "fields": [{"name": scrub(k), "value": scrub(v) or "-", "inline": True} for k, v in (fields or [])]}
        if shot and not (w["send_screenshot"] and os.path.exists(shot)):
            shot = None
        args = (w["url"], {"username": "GARNT TOOL REJOIN", "embeds": [embed]}, shot)
        if sync:
            return self._post(*args)
        threading.Thread(target=self._post, args=args, daemon=True).start()
        return True

    def _post(self, url, payload, shot):
        try:
            for _ in range(2):
                if shot:
                    name = os.path.basename(shot)
                    payload["embeds"][0]["image"] = {"url": "attachment://" + name}
                    with open(shot, "rb") as fh:
                        r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                          files={"file": (name, fh, "image/png")}, timeout=25)
                else:
                    r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 429:
                    time.sleep(min(float(r.json().get("retry_after", 2)), 10))
                    continue
                if r.status_code >= 300:
                    log.warning("webhook HTTP %s", r.status_code)
                    return False
                return True
        except Exception as e:
            log.warning("webhook failed: %s", scrub(e))
        return False


# ----------------------------------------------------------------- heartbeat (in-game script -> local receiver)
LUA_TEMPLATE = r"""-- GARNT TOOL REJOIN heartbeat (auto-generated)
-- For your own accounts and devices only. Must run through an executor that supports HTTP
local HOST     = "http://127.0.0.1:__PORT__"
local ID       = "__ID__"       -- package name or instance number; must match the tool
local INTERVAL = __INTERVAL__   -- send every N seconds

if getgenv then
    if getgenv().__GARNT_HB then return end
    getgenv().__GARNT_HB = true
end

if not game:IsLoaded() then game.Loaded:Wait() end
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local GuiService = game:GetService("GuiService")

local lp = Players.LocalPlayer
while not lp do task.wait(1); lp = Players.LocalPlayer end

local lastFrame = os.clock()
RunService.Heartbeat:Connect(function() lastFrame = os.clock() end)

local function httpGet(url)
    local req = (syn and syn.request) or http_request or request or (http and http.request)
    if req then
        local ok = pcall(req, { Url = url, Method = "GET" })
        if ok then return true end
    end
    return (pcall(function() return game:HttpGet(url) end))
end

local function errorText()
    local ok, msg = pcall(function() return GuiService:GetErrorMessage() end)
    if ok and type(msg) == "string" and msg ~= "" then return msg end
    return nil
end

local function beat()
    if os.clock() - lastFrame > 5 then return end   -- game frozen: do not send
    local url = string.format("%s/hb?id=%s&user=%s&uid=%d&place=%d&job=%s", HOST,
        HttpService:UrlEncode(ID), HttpService:UrlEncode(lp.Name), lp.UserId, game.PlaceId,
        HttpService:UrlEncode(game.JobId))
    local err = errorText()                          -- Disconnected prompt showing: send the error text immediately
    if err then url = url .. "&err=" .. HttpService:UrlEncode(string.sub(err, 1, 120)) end
    httpGet(url)
end

pcall(function() GuiService.ErrorMessageChanged:Connect(function() pcall(beat) end) end)

task.spawn(function()
    while true do
        pcall(beat)
        task.wait(INTERVAL)
    end
end)
"""


def render_lua(inst_id, port, interval):
    return (LUA_TEMPLATE.replace("__PORT__", str(int(port))).replace("__INTERVAL__", str(int(interval)))
            .replace("__ID__", re.sub(r"[^\w.\-]", "", str(inst_id))))


def write_heartbeat_files(cfg):
    h = cfg.d["heartbeat"]
    d = os.path.join(BASE, "heartbeat")
    os.makedirs(d, exist_ok=True)
    det = cfg.d["detected"] or ["1"]
    paths = []
    for i, pkg in enumerate(det, 1):
        p = os.path.join(d, "heartbeat_%d_%s.lua" % (i, pkg.split(".")[-1]))
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_lua(pkg, h["port"], h["interval"]))
        paths.append(p)
    return paths


HB_MARK = "-- GARNT TOOL REJOIN heartbeat"


def peer_uid(client_port, server_port):
    """Root can read /proc/net/tcp: find which Android UID owns the loopback connection that is calling us."""
    for f in ("/proc/net/tcp6", "/proc/net/tcp"):
        try:
            with open(f) as fh:
                next(fh, None)
                for line in fh:
                    p = line.split()
                    if len(p) > 7 and int(p[1].rsplit(":", 1)[1], 16) == client_port \
                            and int(p[2].rsplit(":", 1)[1], 16) == server_port:
                        return int(p[7])
        except (OSError, ValueError):
            continue
    return None


def uid_packages():
    def run():
        _, out = sh("pm list packages -U", root=ENV.root, timeout=40)
        d = {}
        for l in out.splitlines():
            m = re.match(r"package:([\w.]+)\s+uid:(\d+)", l.strip())
            if m:
                d[int(m.group(2))] = m.group(1)
        return d
    return cached("uidmap", 600, run)


def autoexec_targets(cfg):
    out = []
    for d in cfg.d["autoexec"]["dirs"]:
        if "{pkg}" in d:
            out += [(d.replace("{pkg}", p), p) for p in cfg.d["detected"]]
        else:
            out.append((d, "auto"))   # shared folder: one script for every clone (id found from the caller's UID)
    return out


def install_autoexec(cfg):
    """Create/refresh heartbeat.lua in the executor's autoexec folder(s). -> [(path, created|updated|ok|skipped|error)]"""
    a, h = cfg.d["autoexec"], cfg.d["heartbeat"]
    res = []
    for d, ident in autoexec_targets(cfg):
        path = os.path.join(d, a["filename"])
        src = render_lua(ident, h["port"], h["interval"])
        try:
            old = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old = f.read()
                if not old.startswith(HB_MARK):
                    res.append((path, "skipped"))      # someone else's file: never overwrite
                    continue
                if old == src:
                    res.append((path, "ok"))
                    continue
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            res.append((path, "created" if old is None else "updated"))
        except OSError as e:
            res.append((path, "error: %s" % e))
    return res


def report_autoexec(cfg, results, quiet_ok=True):
    names = {"created": "Created", "updated": "Updated", "ok": "Ready",
             "skipped": "Skipped (a same-name file not made by this tool exists)"}
    for path, st in results:
        if st == "ok" and quiet_ok:
            continue
        say("heartbeat.lua: %s → %s" % (names.get(st, st), path), "red" if st.startswith("error") else "green" if st != "skipped" else "yellow")
    if any(st == "created" for _, st in results) and not cfg.d["autoexec"]["enabled_once"]:
        cfg.d["autoexec"]["enabled_once"] = True
        cfg.d["heartbeat"]["enabled"] = True       # first install only: turn heartbeat on (later changes are yours)
        cfg.save()
        say("Heartbeat enabled for you (turn it off at Menu 6 > Heartbeat > 1)", "cyan")


class Heartbeats:
    def __init__(self):
        self.lock = threading.Lock()
        self.d, self.by_user, self.single = {}, {}, False

    def put(self, q, peer_pkg=None):
        key = peer_pkg or re.sub(r"[^\w.\-]", "", q.get("id", ""))[:64]
        if not key:
            return False
        rec = {"ts": time.time(), "user": re.sub(r"[^\w .\-]", "", q.get("user", ""))[:30],
               "uid": q.get("uid", "")[:20], "place": q.get("place", "")[:20], "job": q.get("job", "")[:64],
               "err": re.sub(r"[^\w .:()\-]", "", q.get("err", ""))[:120]}
        with self.lock:
            self.d[key] = rec
            if rec["user"]:
                self.by_user[rec["user"]] = rec
        return True

    def latest(self, inst):
        with self.lock:
            recs = [self.d[k] for k in (inst.pkg, str(inst.idx)) if k in self.d]
            if inst.user and inst.user in self.by_user:
                recs.append(self.by_user[inst.user])
            if self.single and "auto" in self.d:
                recs.append(self.d["auto"])
        return max(recs, key=lambda r: r["ts"]) if recs else None


class HBServer:
    """Tiny local-only HTTP receiver: GET /hb?id=..&user=..&uid=..&place=..&job=.. and GET /ping."""

    def __init__(self, store, port, prefix="com.roblox."):
        self.store, self.port, self.httpd, self.prefix = store, int(port), None, prefix

    def start(self):
        store, prefix = self.store, self.prefix

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                code, body = 404, b"not found"
                if u.path == "/ping":
                    code, body = 200, b"pong"
                elif u.path == "/hb":
                    q = {k: v[0][:80] for k, v in parse_qs(u.query).items() if v}
                    pkg = None
                    uid = peer_uid(self.client_address[1], self.server.server_address[1])
                    if uid is not None:
                        pkg = uid_packages().get(uid)
                        if pkg and not pkg.startswith(prefix):
                            pkg = None
                    code, body = (200, b"ok") if store.put(q, pkg) else (400, b"missing id")
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


# ----------------------------------------------------------------- console webhook (mirrors the console log)
class ConsoleHook:
    """Buffers console/event lines and posts them to Discord in batches (code block, <= 1900 chars)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.q = deque(maxlen=300)
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def url(self):
        c = self.cfg.d["console_webhook"]
        return c["url"] or self.cfg.d["webhook"]["url"]

    def active(self):
        return bool(self.cfg.d["console_webhook"]["enabled"] and self.url())

    def push(self, line):
        if self.active():
            with self.lock:
                self.q.append(scrub(line)[:300])

    def start(self):
        if self.active():
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.wait(max(3, int(self.cfg.d["console_webhook"]["flush_sec"]))):
            self.flush()

    def flush(self):
        with self.lock:
            lines = list(self.q)
            self.q.clear()
        if not lines or not self.url():
            return True
        ok, chunk, size = True, [], 0
        for ln in lines + [None]:
            if ln is None or size + len(ln) + 1 > 1850:
                if chunk:
                    ok = self._post("\n".join(chunk)) and ok
                chunk, size = [], 0
            if ln is not None:
                chunk.append(ln)
                size += len(ln) + 1
        return ok

    def _post(self, text):
        try:
            for _ in range(2):
                r = requests.post(self.url(), json={"username": "GARNT CONSOLE", "content": "```\n%s\n```" % text}, timeout=15)
                if r.status_code == 429:
                    time.sleep(min(float(r.json().get("retry_after", 2)), 10))
                    continue
                return r.status_code < 300
        except Exception as e:
            log.warning("console webhook failed: %s", scrub(e))
        return False

    def close(self):
        self.stop.set()
        self.flush()


# ----------------------------------------------------------------- raw keyboard input + live loop
class RawInput:
    """cbreak keyboard reader: no echo, no Enter needed for polling; Ctrl+C still raises KeyboardInterrupt."""

    def __init__(self):
        self.fd, self.old, self.buf, self.dirty, self._cr = None, None, "", False, False

    def __enter__(self):
        if termios and sys.stdin.isatty():
            try:
                self.fd = sys.stdin.fileno()
                self.old = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            except Exception:
                self.fd = None
        return self

    def __exit__(self, *a):
        if self.fd is not None and self.old is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            except Exception:
                pass

    def feed(self, txt):
        """Process typed text -> list of completed lines (may include "" for a bare Enter)."""
        lines, i = [], 0
        self._cr = False   # CR+LF is merged only inside one chunk
        while i < len(txt):
            ch = txt[i]
            if ch == "\x1b":  # skip arrow keys / other escape sequences
                m = re.match(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.)", txt[i:])
                i += len(m.group(0)) if m else 1
                continue
            if ch in "\r\n":
                if not (ch == "\n" and self._cr):
                    lines.append(self.buf)
                    self.buf = ""
                self._cr = ch == "\r"
            else:
                self._cr = False
                if ch in "\x7f\b":
                    self.buf = self.buf[:-1]
                elif ch == "\x15":
                    self.buf = ""
                elif ch.isprintable() and len(self.buf) < 120:
                    self.buf += ch
            self.dirty = True
            i += 1
        return lines

    def poll(self, timeout):
        if self.fd is None:
            time.sleep(timeout)
            return []
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        return self.feed(os.read(self.fd, 256).decode("utf-8", "ignore"))


def run_live(render, handler, refresh_sec, inp=None, once=False):
    """Live panel + typed commands. render(buf) -> renderable; handler(line) may return "quit"."""
    refresh_sec = max(0.5, float(refresh_sec))
    with (inp or RawInput()) as ri, Live(console=console, auto_refresh=False) as live:
        live.update(render(ri.buf), refresh=True)
        if once and getattr(ri, "fd", 1) is None:   # no keyboard (not a terminal): show once and return
            return
        last = time.time()
        while True:
            lines = ri.poll(refresh_sec)
            for ln in lines:
                if handler(ln) == "quit":
                    live.update(render(ri.buf), refresh=True)
                    return
            if lines or ri.dirty or time.time() - last >= refresh_sec:
                ri.dirty = False
                live.update(render(ri.buf), refresh=True)
                last = time.time()


def sys_load():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


CONSOLE_HELP = ("Commands: rejoin <n|all> | pause <n|all> | resume <n|all> | status | ram | shot | clear | help | quit")


# ----------------------------------------------------------------- engine
class Inst:
    def __init__(self, idx, pkg, cookie=None):
        self.idx, self.pkg, self.cookie = idx, pkg, cookie
        self.label = "#%d %s" % (idx, pkg)
        self.session = None
        self.uid = None
        self.user = ""
        self.cookie_bad = False
        self.pres_at, self.pres_val = 0.0, None
        self.dlg_hits, self.dlg_serial = 0, 0
        self.wake, self.cmd, self.paused = threading.Event(), "", False
        self.state, self.detail = "queued", ""
        self.fails = self.rejoins = 0
        self.launched_at = 0.0
        self.ok_since = None
        self.next_at = 0.0
        self.started_at = time.time()


REASON = {"dead": "App closed/frozen (process not found)", "background": "App is not in the foreground",
          "noheartbeat": "No heartbeat from the in-game script",
          "disconnected": "Disconnected prompt detected (e.g. Error Code 277)",
          "notingame": "Not in a game", "timed": "Timed rejoin (as configured)"}
STATE_LABEL = {"queued": ("Queued", "dim"), "launching": ("Launching app", "cyan"),
            "joining": ("Joining game", "yellow"), "ok": ("OK", "bold green"),
            "unknown": ("Unknown", "magenta"), "backoff": ("Waiting to rejoin", "bold red"),
            "stopped": ("Stopped", "dim"), "paused": ("Paused", "yellow")}


def fmt_dur(sec):
    sec = int(max(0, sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


class Engine:
    MIN = 60        # seconds per "minute" (overridable for tests)
    HK_TICK = 30    # housekeeping resolution in seconds

    def __init__(self, cfg, hook, uri, instances, hb=None, chook=None):
        self.cfg, self.hook, self.uri, self.insts, self.hb = cfg, hook, uri, instances, hb
        self.chook = chook
        self.stop = threading.Event()
        self.gate = threading.Lock()
        self.last_launch = 0.0
        self.events = deque(maxlen=10)
        self._dlg_n = 0
        self._stack_seen, self._stack_last = {}, {}
        self.threads = []
        self.t0 = time.time()

    # -- helpers
    def event(self, msg, style=""):
        msg = scrub(msg)
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append((ts, msg, style))
        log.info(msg)
        if self.chook:
            self.chook.push("%s %s" % (ts, msg))

    def interval(self):
        return max(10, int(self.cfg.d["check_interval"]))

    def start(self):
        self.event("Started %d instance(s) (mode %s)" % (len(self.insts), "ROOT" if ENV.root else "non-root"), "green")
        self.hook.send("start", "▶️ Auto-rejoin started", "Managing %d instance(s)" % len(self.insts),
                       [("Mode", "ROOT" if ENV.root else "non-root"), ("Check every", "%ds" % self.interval()),
                        ("Package", ", ".join(i.pkg for i in self.insts))])
        SNAP["fg"] = bool(ENV.root and (self.cfg.d["auto"]["foreground_check"] or self.cfg.d["dialog_detect"]["enabled"]))
        for target, args in [(self._worker, (i,)) for i in self.insts] + [(self._housekeeper, ())]:
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self):
        self.stop.set()
        for i in self.insts:
            i.wake.set()
        for t in self.threads:
            t.join(timeout=5)
        wake_lock(False)
        SNAP["fg"] = False
        up = fmt_dur(time.time() - self.t0)
        self.event("Stopped (ran for %s)" % up)
        self.hook.send("stop", "⏹️ Auto-rejoin stopped", "Ran for " + up,
                       [(i.pkg, "Rejoined %d time(s)" % i.rejoins) for i in self.insts], sync=True)

    def _next_tick(self):
        """All instances are checked on the same time grid so one snapshot serves every instance."""
        iv = self.interval()
        return self.t0 + (int((time.time() - self.t0) // iv) + 1) * iv

    # -- housekeeping: RAM clearing + periodic status report with screenshot
    def _housekeeper(self):
        c = self.cfg.d
        last_ram = last_rep = last_guard = time.time()
        while not self.stop.wait(self.HK_TICK):
            now = time.time()
            r = c["ram"]
            if r["enabled"] and ENV.root:
                free = mem_available_mb()
                due = r["interval_min"] > 0 and now - last_ram >= r["interval_min"] * self.MIN
                low = bool(r["min_free_mb"] > 0 and free is not None and free < r["min_free_mb"] and now - last_ram >= self.MIN)
                if due or low:
                    self.clear_ram("low free RAM" if low else "scheduled")
                    last_ram = time.time()
            g = c["screen_guard"]
            if g["enabled"] and ENV.root and now - last_guard >= int(g["check_sec"]):
                last_guard = now
                try:
                    self.guard_tick()
                except Exception as e:
                    self.event("Stacked-window clear failed: %s" % e, "red")
            pm = int(c["webhook"]["periodic_min"])
            if pm > 0 and now - last_rep >= pm * self.MIN:
                last_rep = now
                try:
                    self.report(pm)
                except Exception as e:
                    self.event("Failed to send status report: %s" % e, "red")

    def guard_tick(self):
        """Clear stacked/overlapping windows: error dialogs, duplicate tasks (debounced), stray apps on top."""
        info = screen_scan(self.cfg)
        now = time.time()
        own = {i.pkg for i in self.insts}
        stacked_now = [p for p in info["stacked"] if p in own]
        for p in list(self._stack_seen):
            if p not in stacked_now:
                del self._stack_seen[p]
        confirmed = []
        for p in stacked_now:
            self._stack_seen[p] = self._stack_seen.get(p, 0) + 1
            if self._stack_seen[p] >= 2 and now - self._stack_last.get(p, 0) >= 10 * self.MIN:
                confirmed.append(p)
                self._stack_last[p] = now
        info["stacked"] = confirmed
        if info["dialogs"] or confirmed or stray_pkg(self.cfg, info):
            self.event("Cleared stacked windows: " + ", ".join(screen_clear(self.cfg, info)), "yellow")

    def clear_ram(self, why):
        ok, before, after = drop_ram(self.cfg.d["ram"]["kill_cached"])
        if ok:
            self.event("RAM cleared (%s): free %s → %s MB" % (why, before, after), "cyan")
        else:
            self.event("RAM clear failed: %s" % RAM_LAST["detail"], "red")

    def report(self, minutes, shot=None):
        w = self.cfg.d["webhook"]
        if not (w["enabled"] and w["url"] and (w["events"].get("status") or minutes is None)):
            return
        if shot is None and minutes is not None and ENV.root and w["send_screenshot"]:
            shot = take_shot("status")
        mem = mem_available_mb()
        fields = [(i.label, "%s | rejoins %d | fails %d%s" % (STATE_LABEL.get(i.state, (i.state,))[0], i.rejoins, i.fails,
                                                              (" | " + i.user) if i.user else "")) for i in self.insts][:22]
        fields.append(("Free RAM", ("%d MB" % mem) if mem is not None else "-"))
        fields.append(("Uptime", fmt_dur(time.time() - self.t0)))
        self.hook.send("status", "📸 Status report", "On-demand report" if minutes is None else "Periodic report every %d min" % minutes, fields, shot)

    # -- console commands (typed in the live panel)
    def _take_cmd(self, inst):
        cmd, inst.cmd = inst.cmd, ""
        inst.wake.clear()
        return cmd

    def _manual(self, inst):
        cmd = self._take_cmd(inst)
        if cmd == "pause":
            inst.paused, inst.state, inst.detail = True, "paused", "Paused by command (type resume)"
            self.event("%s: paused" % inst.label, "yellow")
        elif cmd in ("rejoin", "resume"):
            inst.paused, inst.fails, inst.ok_since = False, 0, None
            self.event("%s: rejoin by command" % inst.label, "cyan")
            self._launch(inst, "rejoin", force=True)

    def _targets(self, arg):
        if arg in ("all", "a", "*"):
            return list(self.insts)
        return [i for i in self.insts if arg.isdigit() and i.idx == int(arg)]

    def console(self, line):
        line = line.strip()
        if not line:
            return None
        self.event("› " + line, "dim")
        parts = line.split()
        cmd, args = parts[0].lower(), [a.lower() for a in parts[1:]]
        if cmd in ("quit", "exit", "q"):
            return "quit"
        if cmd in ("help", "?", "h"):
            self.event(CONSOLE_HELP, "cyan")
        elif cmd in ("rejoin", "r", "pause", "p", "resume"):
            act = {"r": "rejoin", "p": "pause"}.get(cmd, cmd)
            ts = self._targets(args[0]) if args else []
            if not ts:
                self.event("Usage: %s <instance number|all>" % cmd, "red")
            else:
                for i in ts:
                    i.cmd = act
                    i.wake.set()
                self.event("Ran %s on instance %s" % (act, ", ".join(str(i.idx) for i in ts)), "cyan")
        elif cmd == "status":
            for i in self.insts:
                self.event("#%d %s | %s" % (i.idx, STATE_LABEL.get(i.state, (i.state,))[0], i.detail or "-"))
        elif cmd in ("ram", "shot", "clear"):
            if not ENV.root:
                self.event("Command %s requires root" % cmd, "red")
            else:
                threading.Thread(target=self._cmd_root, args=(cmd,), daemon=True).start()
        else:
            self.event("Unknown command: %s (type help)" % cmd, "red")
        return None

    def _cmd_root(self, cmd):
        try:
            if cmd == "ram":
                self.clear_ram("Command")
            elif cmd == "clear":
                self.event("Screen cleared: " + ", ".join(screen_clear(self.cfg, screen_scan(self.cfg), force_all=True)), "yellow")
            else:
                path = take_shot("console")
                self.event("Screenshot saved: %s" % os.path.basename(path) if path else "Screenshot failed", "cyan" if path else "red")
                if path:
                    self.report(None, shot=path)
        except Exception as e:
            self.event("Command %s failed: %s" % (cmd, e), "red")

    # -- worker
    def _worker(self, inst):
        try:
            if not self._launch(inst, "start"):
                return
            while True:
                inst.next_at = self._next_tick()
                woke = inst.wake.wait(max(0.1, inst.next_at - time.time()))
                if self.stop.is_set():
                    break
                try:
                    if woke:
                        self._manual(inst)
                    elif not inst.paused:
                        self._tick(inst)
                except Exception as e:
                    self.event("%s: error %s" % (inst.label, e), "red")
        except Exception as e:
            self.event("%s: worker stopped because %s" % (inst.label, e), "red")
        inst.state = "stopped"

    def _launch(self, inst, kind, force=False):
        c = self.cfg.d
        inst.state, inst.detail = "queued", "Waiting in launch queue"
        with self.gate:
            wait = self.last_launch + max(0, int(c["launch_delay"])) - time.time()
            if wait > 0 and self.stop.wait(wait):
                return False
            if self.stop.is_set():
                return False
            inst.state, inst.detail = "launching", ""
            ok, msg = open_game(inst.pkg, self.uri, c["auto"]["force_stop"] or force)
            self.last_launch = time.time()
        inst.launched_at = time.time()
        inst.pres_at = 0.0
        inst.dlg_hits = 0
        inst.state, inst.detail = "joining", "Waiting to join"
        if not ok:
            self.event("%s: failed to open the game: %s" % (inst.label, msg), "red")
            return True
        if kind == "start":
            self.event("%s: game opened" % inst.label, "green")
        else:
            inst.rejoins += 1
            self.event("%s: rejoined (#%d)" % (inst.label, inst.rejoins), "cyan")
            self.hook.send("rejoin", "🔁 Rejoined", inst.label,
                           [("Package", inst.pkg), ("Account", inst.user or "-"), ("Rejoin #", str(inst.rejoins))])
        return True

    def presence(self, inst):
        if not (inst.cookie and self.cfg.d["auto"]["presence_check"]) or inst.cookie_bad:
            return None
        if inst.session is None:
            inst.session = make_session(inst.cookie)
        if inst.uid is None:
            uid, name, err = who_am_i(inst.session)
            if not uid:
                if err and "Cookie" in err:
                    inst.cookie_bad = True
                    self.event("%s: %s" % (inst.label, err), "red")
                return None
            inst.uid, inst.user = uid, name
        ttl = 60 if self.cfg.d["performance"]["low_cpu"] else 0
        if ttl and time.time() - inst.pres_at < ttl - 1:
            return inst.pres_val
        inst.pres_val, inst.pres_at = in_game(inst.session, inst.uid), time.time()
        return inst.pres_val

    def dialog_state(self):
        """One screenshot analysis shared by all instances (cached). -> dict or None"""
        d = self.cfg.d["dialog_detect"]
        if not (ENV.root and d["enabled"]):
            return None

        def run():
            found, _ = detect_screen_dialog()
            self._dlg_n += 1
            return {"found": bool(found), "top": parse_focus(foreground_text()), "n": self._dlg_n}
        return cached("dlg", max(5, int(d["every_sec"]) - 1), run)

    def hb_state(self, inst):
        """None = no usable heartbeat info (never seen / disabled); else ("ok"|"stale", detail)."""
        hc = self.cfg.d["heartbeat"]
        if not (hc["enabled"] and self.hb):
            return None
        rec = self.hb.latest(inst)
        if rec is None:
            return None  # script never reported: fall back to other signals
        if rec["ts"] < inst.launched_at:
            return "stale", "No heartbeat since the last relaunch"
        age = time.time() - rec["ts"]
        if rec.get("err") and age <= int(hc["timeout"]):
            return "err", "Disconnected: " + rec["err"]
        if rec["user"] and not inst.user:
            inst.user = rec["user"]
        who = (" [%s]" % rec["user"]) if rec["user"] else ""
        if age <= int(hc["timeout"]):
            return "ok", "heartbeat %ds ago%s" % (age, who)
        return "stale", "no heartbeat for %ds%s" % (age, who)

    def check(self, inst):
        alive = proc_alive(inst.pkg)
        if ENV.root and not alive:
            return "dead", REASON["dead"]
        if ENV.root and alive and self.cfg.d["auto"]["foreground_check"]:
            if (inst.pkg + "/") not in foreground_text():
                return "background", REASON["background"]
        hs = self.hb_state(inst)
        if hs:
            if hs[0] == "err":
                return "disconnected", hs[1]
            return ("ok", hs[1]) if hs[0] == "ok" else ("noheartbeat", hs[1])
        ds = self.dialog_state()
        if ds is not None:
            hit = ds["found"] and (len(self.insts) == 1 or ds["top"] == inst.pkg)
            if hit and ds["n"] != inst.dlg_serial:
                inst.dlg_hits += 1
            elif not hit:
                inst.dlg_hits = 0
            inst.dlg_serial = ds["n"]
            if hit and inst.dlg_hits >= int(self.cfg.d["dialog_detect"]["confirm"]):
                return "disconnected", REASON["disconnected"]
            if hit:
                return "ok", "Abnormal window detected, confirming %d more round(s)" % (int(self.cfg.d["dialog_detect"]["confirm"]) - inst.dlg_hits)
        pres = self.presence(inst)
        who = (" [%s]" % inst.user) if inst.user else ""
        if pres is True:
            return "ok", "In game" + who
        if pres is False:
            return "notingame", REASON["notingame"] + who
        if alive:
            return "ok", "App is running (no game-state info)"
        return "unknown", "Unknown state (non-root / no cookie)"

    def _tick(self, inst):
        c = self.cfg.d
        now = time.time()
        status, detail = self.check(inst)
        inst.detail = detail
        if status in ("ok", "unknown"):
            inst.state = status
            if inst.ok_since is None:
                inst.ok_since = now
            if status == "ok" and inst.fails and now - inst.ok_since >= c["stable_reset"]:
                inst.fails = 0
                self.event("%s: stable for %ds, backoff reset" % (inst.label, c["stable_reset"]))
            tm = int(c["auto"]["timed_rejoin_min"])
            if tm > 0 and now - inst.launched_at >= tm * 60:
                return self._recover(inst, "timed")
            return
        elapsed = now - inst.launched_at
        jg = c["join_grace"]
        early = {"dead": min(30, jg), "disconnected": min(15, jg)}.get(status, jg)
        if elapsed < early:
            inst.state = "joining"
            return
        self._recover(inst, status)

    def _recover(self, inst, reason):
        c = self.cfg.d
        inst.ok_since = None
        delay = 0
        if reason != "timed":
            inst.fails += 1
            delay = min(int(c["backoff_max"]), int(c["backoff_base"]) * 2 ** (inst.fails - 1))
            self.event("%s: %s (#%d) rejoining in %ds" % (inst.label, REASON[reason], inst.fails, delay), "red")
            shot = take_shot(inst.pkg) if c["auto"]["screenshot_on_crash"] else None
            self.hook.send("crash", "⚠️ Dropped / frozen", inst.label,
                           [("Reason", REASON[reason]), ("Package", inst.pkg), ("Account", inst.user or "-"),
                            ("Consecutive fails", str(inst.fails)), ("Rejoin in", "%ds" % delay)], shot)
        else:
            self.event("%s: %s" % (inst.label, REASON[reason]), "cyan")
        if delay:
            inst.state, inst.next_at = "backoff", time.time() + delay
            if inst.wake.wait(delay):
                if self.stop.is_set():
                    return
                if self._take_cmd(inst) == "pause":
                    inst.paused, inst.state, inst.detail = True, "paused", "Paused by command (type resume)"
                    return
        self._launch(inst, "rejoin")

    # -- live panel
    def render(self, buf=None):
        now = time.time()
        mem = mem_available_mb()
        load = sys_load()
        head = Text.assemble(
            ("Mode: ", "dim"), ("ROOT" if ENV.root else "non-root", "bold green" if ENV.root else "bold yellow"),
            ("  |  check every %ds" % self.interval(), ""), ("  |  launch delay %ss" % self.cfg.d["launch_delay"], ""),
            ("  |  uptime " + fmt_dur(now - self.t0), "bold"),
            (("  |  free RAM %d MB" % mem) if mem is not None else "", ""),
            (("  |  load %.1f" % load) if load is not None else "", ""),
            ("\nType a command and press Enter (help = command list) | quit or Ctrl+C = stop and return to menu", "dim"))
        t = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
        for col in ("#", "Package", "Account", "State", "Played", "Fails", "Rejoins", "Next", "Details"):
            t.add_column(col, overflow="fold")
        for i in self.insts:
            label, style = STATE_LABEL.get(i.state, (i.state, ""))
            nxt = "%ds" % max(0, int(i.next_at - now)) if i.next_at and i.state in ("ok", "unknown", "joining", "backoff") else "-"
            up = fmt_dur(now - i.ok_since) if i.ok_since and i.state == "ok" else "-"
            t.add_row(str(i.idx), i.pkg, i.user or "-", Text(label, style=style), up, str(i.fails),
                      str(i.rejoins), nxt, T(i.detail))
        ev = Text()
        for ts, msg, st in list(reversed(self.events))[:8]:   # newest first so cropping never hides the latest
            ev.append(ts + "  ", style="dim")
            ev.append(msg + "\n", style=st)
        parts = [Panel(head, title="GARNT TOOL REJOIN", border_style="cyan"), t]
        if buf is not None:
            parts.append(Panel(Text.assemble(("› ", "bold yellow"), (scrub(buf), "bold"), ("█", "blink")),
                               title="Console", border_style="yellow"))
        parts.append(Panel(ev, title="Latest events (newest first)", border_style="blue"))
        return Group(*parts)


# ----------------------------------------------------------------- ui helpers
def T(s, style=""):
    return Text(scrub(s), style=style)


def say(s="", style=""):
    console.print(T(s, style), highlight=False)


def ask(prompt, default=""):
    try:
        v = console.input(Text(prompt + " ", style="bold yellow")).strip()
    except EOFError:
        return default or "0"
    return v or default


def confirm(prompt):
    return ask(prompt + " (y/N)").lower() in ("y", "yes")


def pause():
    ask("Press Enter to go back to the menu")


def onoff(v):
    return Text("ON" if v else "OFF", style="bold green" if v else "bold red")


def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[3J\033[2J\033[H")
        sys.stdout.flush()


def banner(cfg):
    clear_screen()
    console.print(Text(LOGO.strip("\n"), style="bold cyan"), highlight=False)
    console.print(Text("      T O O L   R E J O I N   •   ควายเผือก rejoin   •   v" + VERSION, style="bold magenta"))
    link = bool(cfg.d["game_link"])
    console.print(Text.assemble(
        ("Mode: ", "dim"), ("ROOT" if ENV.root else "non-root", "bold green" if ENV.root else "bold yellow"),
        ("   Instances: ", "dim"), (str(len(cfg.d["detected"])), "bold"),
        ("   Game link: ", "dim"), ("set" if link else "not set", "green" if link else "red"),
        ("   Webhook: ", "dim"), ("ON" if cfg.d["webhook"]["enabled"] else "OFF",
                                  "green" if cfg.d["webhook"]["enabled"] else "red")))
    console.print(Text("Folder: " + BASE, style="dim"))
    console.print()


def selected_packages(cfg):
    sel = cfg.d["packages"]
    return [p for p in cfg.d["detected"] if not sel or p in sel]


def refresh_packages(cfg):
    found = scan_packages(cfg.d["package_prefix"])
    if found:
        cfg.d["detected"] = found
        cfg.d["packages"] = [p for p in cfg.d["packages"] if p in found]
        cfg.save()
    return found


# ----------------------------------------------------------------- menus
def menu_start(cfg, hook):
    parsed = parse_link(cfg.d["game_link"])
    pkgs = selected_packages(cfg)
    if not parsed:
        say("Game link not set (or invalid). Go to Menu 3 first", "red")
        return pause()
    if not pkgs:
        say("No Roblox packages found. Go to Menu 4 to scan first", "red")
        return pause()
    if cfg.d["auto"]["cookie_login_on_start"]:
        if ENV.root:
            pairs = [(p, c) for p, c in cookie_pairs(cfg)[0] if p in pkgs]
            for pkg, ok, msg in cookie_login(pairs):
                say("%s: %s" % (pkg, msg), "green" if ok else "red")
        else:
            say("Skipping cookie login: root required", "yellow")
    if cfg.d["autoexec"]["auto_create"]:
        report_autoexec(cfg, install_autoexec(cfg))
    cmap = dict(cookie_pairs(cfg)[0])
    det = cfg.d["detected"]
    insts = [Inst(det.index(p) + 1, p, cmap.get(p)) for p in pkgs]
    hb = hbs = None
    hc = cfg.d["heartbeat"]
    if hc["enabled"]:
        hb = Heartbeats()
        hbs = HBServer(hb, hc["port"], cfg.d["package_prefix"])
        hb.single = len(insts) == 1
        try:
            hbs.start()
            say("Receiving heartbeat at 127.0.0.1:%d (test: curl http://127.0.0.1:%d/ping)" % (hbs.port, hbs.port), "green")
        except OSError as e:
            say("Cannot open the heartbeat port (%s); heartbeat disabled for this run" % e, "red")
            hb = hbs = None
    if cfg.d["auto"]["wake_lock"]:
        wake_lock(True)
    apply_perf(cfg)
    pf = cfg.d["performance"]
    rate = 1.0 / max(1, int(pf["ui_refresh_sec"])) if pf["low_cpu"] else 2
    chook = ConsoleHook(cfg)
    chook.start()
    eng = Engine(cfg, hook, parsed[0], insts, hb, chook)
    eng.start()
    try:
        run_live(eng.render, eng.console, 1.0 / rate)
    except KeyboardInterrupt:
        pass
    say("\nStopping...", "yellow")
    eng.shutdown()
    chook.close()
    if hbs:
        hbs.stop()
    say("Stopped", "green")
    pause()


def status_view(cfg, info, buf=""):
    det = cfg.d["detected"]
    pairs, n_ck = info
    cmap = dict(pairs)
    fg = foreground_text() if ENV.root else ""
    t = Table(title="Detected instances", box=box.ROUNDED, header_style="bold cyan", expand=True)
    for col in ("#", "Package", "Selected", "Cookie", "Running", "Foreground"):
        t.add_column(col)
    sel = selected_packages(cfg)
    for i, pkg in enumerate(det, 1):
        alive = proc_alive(pkg)
        run = Text("yes", style="green") if alive else (Text("No", style="red") if ENV.root else Text("Unknown", style="magenta"))
        front = Text("-") if not ENV.root else (Text("yes", style="green") if (pkg + "/") in fg else Text("No", style="dim"))
        t.add_row(str(i), pkg, onoff(pkg in sel), Text("Yes" if pkg in cmap else "-"), run, front)
    mem, load = mem_available_mb(), sys_load()
    foot = "Mode: %s | check every %ss | launch delay %ss | cookie lines in file: %d | free RAM %s MB | load %s%s" % (
        "ROOT" if ENV.root else "non-root", cfg.d["check_interval"], cfg.d["launch_delay"], n_ck,
        mem if mem is not None else "?", ("%.1f" % load) if load is not None else "?",
        "" if ENV.root else " | non-root mode cannot check processes/foreground")
    return Group(t, T(foot, "dim"), T("Live panel refreshes automatically — press Enter or Ctrl+C to go back", "yellow"))


def menu_status(cfg):
    if not cfg.d["detected"]:
        say("No packages found yet. Try Menu 4 (scan packages)", "red")
        return pause()
    info = cookie_pairs(cfg)
    pf = cfg.d["performance"]
    try:
        run_live(lambda buf: status_view(cfg, info, buf), lambda ln: "quit",
                 max(1, int(pf["ui_refresh_sec"])) if pf["low_cpu"] else 1, once=True)
    except KeyboardInterrupt:
        pass


def menu_settings(cfg):
    while True:
        banner(cfg)
        p = parse_link(cfg.d["game_link"])
        t = Table(title="Settings", box=box.ROUNDED, header_style="bold cyan")
        t.add_column("#")
        t.add_column("Item")
        t.add_column("Current value")
        link = cfg.d["game_link"]
        t.add_row("1", "Game link / Place ID", T((link[:60] + "…" if len(link) > 60 else link) or "(not set)"))
        t.add_row("2", "Check interval (seconds)", str(cfg.d["check_interval"]))
        t.add_row("3", "Delay between launching instances (seconds)", str(cfg.d["launch_delay"]))
        console.print(t)
        if p:
            say("Link actually used to launch: " + p[0], "dim")
        c = ask("Choose a number (0=back)")
        if c == "0":
            return
        if c == "1":
            v = ask("Paste the game link (roblox.com/games/..., private-server link, share link, or Place ID):")
            if v and not parse_link(v):
                say("Invalid link format", "red")
                pause()
            elif v:
                cfg.d["game_link"] = v
                cfg.save()
        elif c in ("2", "3"):
            key, lo = ("check_interval", 10) if c == "2" else ("launch_delay", 0)
            v = ask("New value (seconds, minimum %d):" % lo)
            if v.isdigit() and int(v) >= lo:
                cfg.d[key] = int(v)
                cfg.save()
            elif v:
                say("Must be a number >= %d" % lo, "red")
                pause()


def menu_scan(cfg):
    say("Scanning packages %s* ..." % cfg.d["package_prefix"], "cyan")
    found = refresh_packages(cfg)
    if not found:
        say("No packages found (if your instances use other names, type the package names, comma-separated)", "red")
        v = ask("Packages (Enter=skip):")
        names = [x.strip() for x in v.split(",") if re.fullmatch(r"[\w.]+", x.strip())]
        if names:
            cfg.d["detected"] = names
            cfg.save()
            found = names
        else:
            return pause()
    t = Table(title="Packages found", box=box.ROUNDED, header_style="bold cyan")
    for col in ("#", "Package", "Selected"):
        t.add_column(col)
    sel = selected_packages(cfg)
    for i, p in enumerate(found, 1):
        t.add_row(str(i), p, onoff(p in sel))
    console.print(t)
    v = ask("Choose which to run, e.g. 1,3 | all=everything | Enter=keep as is:").lower()
    if v == "all":
        cfg.d["packages"] = []
    elif v:
        idx = [int(x) for x in re.findall(r"\d+", v) if 0 < int(x) <= len(found)]
        if idx:
            cfg.d["packages"] = [found[i - 1] for i in sorted(set(idx))]
    cfg.save()
    say("Saved", "green")
    pause()


def menu_cookie(cfg):
    if ensure_cookie_file():
        say("Sample file created: " + COOKIE_PATH, "green")
    pairs, n_ck = cookie_pairs(cfg)
    if not n_ck:
        say("No cookies in cookie.txt yet  (1 line per account, in instance order)\nFile: " + COOKIE_PATH, "yellow")
        return pause()
    det = cfg.d["detected"]
    if n_ck > len(det):
        say("More cookies than instances (%d > %d); extras will be ignored" % (n_ck, len(det)), "yellow")
    t = Table(title="Cookies matched to instances (in order)", box=box.ROUNDED, header_style="bold cyan")
    for col in ("Line", "Package", "Cookie", "Check result"):
        t.add_column(col)
    valid = []
    for i, (pkg, ck) in enumerate(pairs, 1):
        uid, name, err = who_am_i(make_session(ck))
        if uid:
            valid.append((pkg, ck))
        t.add_row(str(i), pkg, mask(ck), Text(("Valid: " + name) if uid else ("Invalid: " + str(err)),
                                              style="green" if uid else "red"))
    console.print(t)
    if not ENV.root:
        say("Non-root mode: cookies can only be validated; injecting them into the app requires root", "yellow")
        return pause()
    if valid and confirm("Inject %d valid cookie(s) into the apps (apps will be force-stopped)?" % len(valid)):
        for pkg, ok, msg in cookie_login(valid):
            say("%s: %s" % (pkg, msg), "green" if ok else "red")
    pause()


AUTO_ITEMS = [
    ("start_on_launch", "Start rejoining as soon as the tool opens", False),
    ("wake_lock", "Wake-lock while running", False),
    ("force_stop", "Force-stop before relaunching the game", True),
    ("foreground_check", "Check that the app is in the foreground", True),
    ("screenshot_on_crash", "Screenshot when dropped/frozen", True),
    ("cookie_login_on_start", "Automatic cookie login before start", True),
    ("presence_check", "Check in-game state via the Roblox API (needs cookies)", False),
]


def menu_auto(cfg):
    a = cfg.d["auto"]
    while True:
        banner(cfg)
        t = Table(title="Auto settings", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "Item", "Value", "Note"):
            t.add_column(col)
        for i, (k, label, root_only) in enumerate(AUTO_ITEMS, 1):
            t.add_row(str(i), label, onoff(a[k]), "Root only" if root_only else "")
        n = len(AUTO_ITEMS)
        t.add_row(str(n + 1), "Timed rejoin (minutes, 0=off)", str(a["timed_rejoin_min"]), "")
        t.add_row(str(n + 2), "Time to wait for joining after launch (seconds)", str(cfg.d["join_grace"]), "")
        t.add_row(str(n + 3), "Heartbeat (in-game script) ...", onoff(cfg.d["heartbeat"]["enabled"]), "Submenu")
        t.add_row(str(n + 4), "Performance: clear RAM / reduce CPU use ...", "", "Submenu")
        t.add_row(str(n + 5), "Screen: Disconnected check / clear stacked windows ...", onoff(cfg.d["dialog_detect"]["enabled"]), "Submenu")
        console.print(t)
        c = ask("Choose a number to toggle/edit (0=back)")
        if c == "0":
            return
        if c.isdigit() and 1 <= int(c) <= n:
            k = AUTO_ITEMS[int(c) - 1][0]
            a[k] = not a[k]
            cfg.save()
        elif c == str(n + 3):
            menu_heartbeat(cfg)
        elif c == str(n + 4):
            menu_perf(cfg)
        elif c == str(n + 5):
            menu_screen(cfg)
        elif c in (str(n + 1), str(n + 2)):
            v = ask("New value (number):")
            if v.isdigit():
                if c == str(n + 1):
                    a["timed_rejoin_min"] = int(v)
                else:
                    cfg.d["join_grace"] = max(30, int(v))
                cfg.save()


def menu_heartbeat(cfg):
    h = cfg.d["heartbeat"]
    while True:
        banner(cfg)
        t = Table(title="Heartbeat (in-game script pings this tool)", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "Item", "Value"):
            t.add_column(col)
        t.add_row("1", "Enable heartbeat (turn on once the script is installed in every clone)", onoff(h["enabled"]))
        t.add_row("2", "Listening port (127.0.0.1)", str(h["port"]))
        t.add_row("3", "Script sends every (seconds)", str(h["interval"]))
        t.add_row("4", "Treat as silent after (seconds)", str(h["timeout"]))
        t.add_row("5", "Generate .lua scripts for each instance (heartbeat/ folder)", "")
        t.add_row("6", "Delta autoexec folder (use {pkg} for one folder per clone)", T(", ".join(cfg.d["autoexec"]["dirs"])))
        t.add_row("7", "Auto create/update heartbeat.lua in that folder", onoff(cfg.d["autoexec"]["auto_create"]))
        t.add_row("8", "Create/update heartbeat.lua now", "")
        console.print(t)
        c = ask("Choose a number (0=back)")
        if c == "0":
            return
        if c == "1":
            h["enabled"] = not h["enabled"]
        elif c in ("2", "3", "4"):
            key, lo, hi = {"2": ("port", 1024, 65535), "3": ("interval", 5, 120), "4": ("timeout", 30, 600)}[c]
            v = ask("New value (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                h[key] = int(v)
                if cfg.d["autoexec"]["auto_create"]:
                    report_autoexec(cfg, install_autoexec(cfg))   # keep the installed script in sync (port/interval)
            elif v:
                say("Value must be between %d-%d" % (lo, hi), "red")
                pause()
        elif c == "5":
            for p in write_heartbeat_files(cfg):
                say("Created: " + p, "green")
            say("Copy each clone's file into that clone's executor autoexec folder (or run it after joining a game)", "dim")
            pause()
        elif c == "6":
            v = ask("Autoexec folder path (Enter=keep, separate several folders with commas):")
            if v:
                cfg.d["autoexec"]["dirs"] = [x.strip() for x in v.split(",") if x.strip().startswith("/")] or cfg.d["autoexec"]["dirs"]
        elif c == "7":
            cfg.d["autoexec"]["auto_create"] = not cfg.d["autoexec"]["auto_create"]
        elif c == "8":
            report_autoexec(cfg, install_autoexec(cfg), quiet_ok=False)
            pause()
        cfg.save()


def menu_screen(cfg):
    d, g = cfg.d["dialog_detect"], cfg.d["screen_guard"]
    while True:
        banner(cfg)
        t = Table(title="Screen: Disconnected check / clear stacked windows", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "Item", "Value"):
            t.add_column(col)
        rows = [("Detect the Disconnected/Error window (e.g. Error Code 277) from screenshots, then rejoin", onoff(d["enabled"])),
                ("Check every (seconds, 15-300)", str(d["every_sec"])),
                ("Confirmations before rejoining (1-5)", str(d["confirm"])),
                ("Auto-clear stacked windows (system error dialogs / duplicate tasks)", onoff(g["enabled"])),
                ("Check every (seconds, 30-600)", str(g["check_sec"])),
                ("force-stop apps with stacked windows (duplicate tasks)", onoff(g["fix_stacked"])),
                ("Close stray apps covering the screen ⚠ (except Termux/system/launcher)", onoff(g["kill_stray"])),
                ("Test Disconnected detection now (does nothing)", ""),
                ("Check for stacked windows now (does nothing)", ""),
                ("Clear the screen now (force)", "")]
        for i, (label, val) in enumerate(rows, 1):
            t.add_row(str(i), label, val)
        console.print(t)
        if not ENV.root:
            say("All screen features need root (in non-root mode, cookies/heartbeat detect drops instead)", "yellow")
        c = ask("Choose a number (0=back)")
        if c == "0":
            return
        if c == "1":
            d["enabled"] = not d["enabled"]
        elif c == "4":
            g["enabled"] = not g["enabled"]
        elif c == "6":
            g["fix_stacked"] = not g["fix_stacked"]
        elif c == "7":
            g["kill_stray"] = not g["kill_stray"]
        elif c in ("2", "3", "5"):
            sec, key, lo, hi = {"2": (d, "every_sec", 15, 300), "3": (d, "confirm", 1, 5), "5": (g, "check_sec", 30, 600)}[c]
            v = ask("New value (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                sec[key] = int(v)
            elif v:
                say("Value must be between %d-%d" % (lo, hi), "red")
                pause()
        elif c in ("8", "9", "10"):
            if not ENV.root:
                say("Root required", "red")
            elif c == "8":
                found, info = detect_screen_dialog()
                say("Result: %s (%s)  focused window: %s" % (
                    "Disconnected window found" if found else ("Not found" if found is False else "Cannot check"), info,
                    parse_focus(foreground_text()) or "-"), "green" if found is False else "yellow")
            else:
                info = screen_scan(cfg)
                say("Focus: %s | error window: %s | apps with duplicate tasks: %s" % (
                    info["top"] or "-", ", ".join(sorted(info["dialogs"])) or "-", ", ".join(info["stacked"]) or "-"))
                if c == "10":
                    say("Done: " + ", ".join(screen_clear(cfg, info, force_all=True)), "green")
            pause()
        cfg.save()


def menu_perf(cfg):
    r, p = cfg.d["ram"], cfg.d["performance"]
    while True:
        banner(cfg)
        t = Table(title="Performance: clear RAM / reduce CPU use", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "Item", "Value"):
            t.add_column(col)
        t.add_row("1", "Auto-clear RAM (root only)", onoff(r["enabled"]))
        t.add_row("2", "Clear RAM every (minutes, 0=no scheduled clear)", str(r["interval_min"]))
        t.add_row("3", "Clear when free RAM drops below (MB, 0=off)", str(r["min_free_mb"]))
        t.add_row("4", "Also kill cached background apps (am kill-all) ⚠ may kill background clones", onoff(r["kill_cached"]))
        t.add_row("5", "Low-CPU mode (lower process priority, cache game state for 60s, slower screen refresh)", onoff(p["low_cpu"]))
        t.add_row("6", "Refresh the status panel every (seconds, used with low-CPU mode)", str(p["ui_refresh_sec"]))
        t.add_row("7", "Clear RAM now", "")
        console.print(t)
        free = mem_available_mb()
        say("Free RAM now: %s MB%s" % (free if free is not None else "?", "" if ENV.root else "  (non-root mode cannot clear RAM)"), "dim")
        c = ask("Choose a number (0=back)")
        if c == "0":
            return
        if c == "1":
            r["enabled"] = not r["enabled"]
        elif c == "4":
            r["kill_cached"] = not r["kill_cached"]
        elif c == "5":
            p["low_cpu"] = not p["low_cpu"]
        elif c in ("2", "3", "6"):
            sec, key, lo, hi = {"2": (r, "interval_min", 0, 1440), "3": (r, "min_free_mb", 0, 8192),
                                "6": (p, "ui_refresh_sec", 1, 10)}[c]
            v = ask("New value (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                sec[key] = int(v)
            elif v:
                say("Value must be between %d-%d" % (lo, hi), "red")
                pause()
        elif c == "7":
            ok, b4, af = drop_ram(r["kill_cached"])
            say(("RAM cleared: free %s → %s MB" % (b4, af)) if ok else ("RAM clear failed: " + RAM_LAST["detail"]),
                "green" if ok else "red")
            if ok:
                say("Step results: " + RAM_LAST["detail"], "dim")
                if b4 is not None and af is not None and af - b4 < 100:
                    say("Free RAM barely increased: drop_caches only frees cache, not RAM the apps are using", "yellow")
            elif ENV.root and not r["kill_cached"]:
                say("Try enabling item 4 (am kill-all) when the device does not allow writing drop_caches (common on cloud phones/containers)", "yellow")
            pause()
        cfg.save()


def menu_webhook(cfg, hook):
    w = cfg.d["webhook"]
    names = [("start", "Started"), ("crash", "Dropped/frozen"), ("rejoin", "Rejoined"), ("stop", "Stopped"),
             ("status", "Status report + screenshot on a schedule")]
    n_shot = 3 + len(names)
    n_per = n_shot + 1
    n_cu, n_ce, n_cf, n_ct, n_test = n_per + 1, n_per + 2, n_per + 3, n_per + 4, n_per + 5
    cw = cfg.d["console_webhook"]
    while True:
        banner(cfg)
        t = Table(title="Discord Webhook", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "Item", "Value"):
            t.add_column(col)
        t.add_row("1", "URL Webhook", T(scrub(w["url"]) or "(not set)"))
        t.add_row("2", "Enable Webhook", onoff(w["enabled"]))
        for i, (k, label) in enumerate(names, 3):
            t.add_row(str(i), "Notify: " + label, onoff(w["events"][k]))
        t.add_row(str(n_shot), "Attach screenshot (root only, keeps the latest %d)" % MAX_SHOTS, onoff(w["send_screenshot"]))
        t.add_row(str(n_per), "Send a status report + screenshot every N minutes (0=off)", str(w["periodic_min"]))
        t.add_row(str(n_cu), "Console webhook: URL (empty = use main URL)", T(scrub(cw["url"]) or "(uses main URL)"))
        t.add_row(str(n_ce), "Console webhook: send console output to Discord", onoff(cw["enabled"]))
        t.add_row(str(n_cf), "Console webhook: batch and send every (seconds, 3-300)", str(cw["flush_sec"]))
        t.add_row(str(n_ct), "Test console webhook", "")
        t.add_row(str(n_test), "Send test message", "")
        console.print(t)
        c = ask("Choose a number (0=back)")
        if c == "0":
            return
        if c == "1":
            v = ask("Paste the Webhook URL (Enter=remove):")
            if not v:
                w["url"], w["enabled"] = "", False
            elif HOOK_RE.match(v):
                w["url"] = v
            else:
                say("Invalid URL format", "red")
                pause()
        elif c == "2":
            w["enabled"] = not w["enabled"]
        elif c.isdigit() and 3 <= int(c) < n_shot:
            k = names[int(c) - 3][0]
            w["events"][k] = not w["events"][k]
        elif c == str(n_shot):
            w["send_screenshot"] = not w["send_screenshot"]
        elif c == str(n_per):
            v = ask("Every how many minutes (0-1440):")
            if v.isdigit() and 0 <= int(v) <= 1440:
                w["periodic_min"] = int(v)
            elif v:
                say("Must be a number 0-1440", "red")
                pause()
        elif c == str(n_cu):
            v = ask("Paste the Console webhook URL (Enter=use main URL):")
            if not v:
                cw["url"] = ""
            elif HOOK_RE.match(v):
                cw["url"] = v
            else:
                say("Invalid URL format", "red")
                pause()
        elif c == str(n_ce):
            cw["enabled"] = not cw["enabled"]
        elif c == str(n_cf):
            v = ask("New value (3-300):")
            if v.isdigit() and 3 <= int(v) <= 300:
                cw["flush_sec"] = int(v)
            elif v:
                say("Must be a number 3-300", "red")
                pause()
        elif c == str(n_ct):
            ch = ConsoleHook(cfg)
            if not ch.url():
                say("No URL yet (set the main URL or the Console URL first)", "red")
            else:
                ok = ch._post("GARNT TOOL REJOIN: console webhook test ✅")
                say("Sent successfully" if ok else "Send failed, check the URL", "green" if ok else "red")
            pause()
        elif c == str(n_test):
            ok = hook.send("test", "✅ Webhook test", "Connected successfully", [("Mode", "ROOT" if ENV.root else "non-root")], sync=True)
            say("Sent successfully" if ok else "Send failed, check the URL", "green" if ok else "red")
            pause()
        cfg.save()


MENU = [("1", "Start auto-rejoin"), ("2", "Show instances / status"),
        ("3", "Settings (game link, check interval, launch delay)"), ("4", "Scan packages"),
        ("5", "Cookie login (optional)"), ("6", "Auto settings"), ("7", "Discord Webhook"), ("0", "Exit")]


def main():
    detect_root()
    cfg = Config()
    setup_logging()
    hook = Hook(cfg)
    apply_perf(cfg)
    log.info("Program started, mode=%s folder=%s", "root" if ENV.root else "non-root", BASE)
    if not STORAGE_OK:
        say("Cannot write to /sdcard/Download (storage permission not granted?). Using fallback folder: " + BASE, "yellow")
    say("Scanning packages...", "dim")
    refresh_packages(cfg)
    if cfg.d["autoexec"]["auto_create"]:
        report_autoexec(cfg, install_autoexec(cfg))
    if cfg.d["auto"]["start_on_launch"] and parse_link(cfg.d["game_link"]) and cfg.d["detected"]:
        say("Auto-starting in 5 seconds (Ctrl+C to cancel)", "yellow")
        try:
            time.sleep(5)
            menu_start(cfg, hook)
        except KeyboardInterrupt:
            pass
    while True:
        banner(cfg)
        for k, label in MENU:
            console.print(Text.assemble(("  [%s] " % k, "bold cyan"), (label, "bold" if k != "0" else "red")))
        console.print()
        c = ask("Choose a menu:")
        if c == "1":
            menu_start(cfg, hook)
        elif c == "2":
            menu_status(cfg)
        elif c == "3":
            menu_settings(cfg)
        elif c == "4":
            menu_scan(cfg)
        elif c == "5":
            menu_cookie(cfg)
        elif c == "6":
            menu_auto(cfg)
        elif c == "7":
            menu_webhook(cfg, hook)
        elif c == "0":
            say("Goodbye!", "cyan")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nExiting the program", "cyan")
