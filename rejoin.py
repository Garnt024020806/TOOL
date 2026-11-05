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
    print("ไม่พบไลบรารีที่จำเป็น: %s\nรันคำสั่ง: pip install -r requirements.txt" % _e)
    sys.exit(1)

VERSION = "1.7"
FPS_CAP_MIN, FPS_CAP_MAX = 5, 240
TERMUX = "/data/data/com.termux/files"
LOGO = r"""
 ██████╗  █████╗ ██████╗ ███╗   ██╗████████╗
██╔════╝ ██╔══██╗██╔══██╗████╗  ██║╚══██╔══╝
██║  ███╗███████║██████╔╝██╔██╗ ██║   ██║   
██║   ██║██╔══██║██╔══██╗██║╚██╗██║   ██║   
╚██████╔╝██║  ██║██║  ██║██║ ╚████║   ██║   
 ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝ 
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
    return "********  (%d ตัวอักษร)" % len(cookie)


# ----------------------------------------------------------------- streamer mode (display only)
STREAMER = {"on": True}
USERS = set()   # account names seen so far (used to mask them inside free text)


def hide_name(name):
    """First 4 characters stay visible, the rest becomes # (screen only; data and logs are unchanged)."""
    name = str(name or "")
    if not STREAMER["on"] or len(name) <= 4:
        return name
    return name[:4] + "#" * (len(name) - 4)


def hide_id(uid):
    """Roblox user ID on screen: first 3 digits stay visible in streamer mode."""
    uid = str(uid or "")
    if not STREAMER["on"] or len(uid) <= 3:
        return uid
    return uid[:3] + "#" * (len(uid) - 3)


def hide_users(text):
    if not STREAMER["on"]:
        return text
    for u in sorted(list(USERS), key=len, reverse=True):
        if len(u) > 4:
            text = re.sub(r"(?<![A-Za-z0-9_.])%s(?![A-Za-z0-9_])" % re.escape(u),
                          lambda m, r=hide_name(u): r, text)
    return text


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
    "block_gap_sec": 20,   # social menu item 5: min seconds between two blocks from the SAME account (Roblox ~3/min per cookie)
    "open_all_delay": 10,   # main menu 9: seconds between opening each Roblox instance
    "package_prefix": "com.roblox.",
    "extra_prefixes": ["aya."],   # also scanned / protected / checked exactly like package_prefix
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
        "cookie_pull_on_start": False,
        "presence_check": True,
        "timed_rejoin_min": 0,
    },
    "heartbeat": {"enabled": True, "port": 8765, "interval": 15, "timeout": 90, "show_fps": True,
                  "fps_cap": {"default": 0, "accounts": {}}},   # accounts: {"<Roblox UserId>": {"name": ..., "cap": N}}; default 0 = leave FPS alone
    "autoexec": {"auto_create": True, "enabled_once": False, "filename": "heartbeat.lua",
                 "dirs": ["/storage/emulated/0/Delta/Autoexecute"]},   # {pkg} in a path = one folder per clone
    "performance": {"low_cpu": True, "ui_refresh_sec": 2},
    "ui": {"streamer_mode": True},   # hide account names on screen (first 4 chars + #)
    "console_webhook": {"enabled": False, "url": "", "flush_sec": 10},   # blank url = reuse the main webhook
    "ram": {"enabled": True, "interval_min": 30, "min_free_mb": 300, "kill_cached": False,
            "kill_others": False, "protect": ["termux", "magisk", "delta"]},
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


class CpuMeter:
    """CPU usage from /proc/stat deltas (root fallback; last resort: load average / cores)."""

    def __init__(self):
        self.prev, self.prev_t = None, 0.0     # sample at the previous read -> average since then

    @staticmethod
    def _sample():
        line = ""
        try:
            with open("/proc/stat") as f:
                line = f.readline()
        except OSError:
            pass
        if not line.startswith("cpu") and ENV.root:
            _, out = sh("head -n 1 /proc/stat", root=True, timeout=10)
            line = out.strip().splitlines()[0] if out.strip() else ""
        try:
            n = [int(x) for x in line.split()[1:9]]
        except ValueError:
            return None
        if not line.startswith("cpu") or len(n) < 4:
            return None
        return n[3] + (n[4] if len(n) > 4 else 0), sum(n)    # (idle+iowait, total)

    @staticmethod
    def _pct(a, b):
        di, dt = b[0] - a[0], b[1] - a[1]
        return None if dt <= 0 else max(0.0, min(100.0, 100.0 * (1 - di / dt)))

    def read(self, wait=1.0):
        """-> {"now": % over `wait` seconds, "avg": % since the previous read or None, "cores": n, "approx": bool}"""
        cores = os.cpu_count() or 1
        a = self._sample()
        if a is None:
            load = sys_load()
            return {"now": None if load is None else min(100.0, 100.0 * load / cores),
                    "avg": None, "cores": cores, "approx": True}
        time.sleep(wait)
        b = self._sample() or a
        avg = self._pct(self.prev, b) if self.prev and time.time() - self.prev_t >= 20 else None
        self.prev, self.prev_t = b, time.time()
        return {"now": self._pct(a, b), "avg": avg, "cores": cores, "approx": False}


CPU = CpuMeter()


def cpu_text():
    """Webhook text: CPU % now (1 s sample) + average since the previous report."""
    try:
        r = CPU.read()
    except Exception:
        return "-"
    if r["now"] is None:
        return "-"
    s = ("~%.0f%%" if r["approx"] else "%.0f%%") % r["now"]
    if r["avg"] is not None:
        s += " | เฉลี่ย %.0f%%" % r["avg"]
    s += " | %d คอร์" % r["cores"]
    if r["approx"]:
        s += " (ประมาณจากโหลดเฉลี่ย)"
    return s


def AM():
    """Use the system `am` when root: Termux's wrapper `am` often fails under su."""
    return "/system/bin/am" if ENV.root and os.path.exists("/system/bin/am") else "am"


RAM_LAST = {"detail": ""}


KEEP_PKGS = ["com.termux", "com.android.systemui", "com.topjohnwu.magisk"]


def kill_others(cfg):
    """Root only: force-stop RUNNING user-installed apps that are not protected. -> list of stopped packages.
    Protected: Roblox clones, anything matching ram.protect (substring), the launcher."""
    r = cfg.d["ram"]
    _, o3 = sh("pm list packages -3", root=True, timeout=30)
    third = {l.split(":", 1)[1].strip() for l in o3.splitlines() if l.startswith("package:")}
    snap = snapshot()
    if not snap["ps_ok"]:
        raise RuntimeError("cannot list processes")
    running = {p.split(":")[0] for p in snap["procs"]}
    # termux/root managers are ALWAYS protected, whatever the user types in the protect list
    prot = ([x.strip().lower() for x in r["protect"] if x.strip()] + [x.lower() for x in prefixes(cfg)] + [
                                                                      "termux", "magisk", "kernelsu", "supersu"])
    keep = set(cfg.d["detected"]) | set(KEEP_PKGS) | {home_pkg()}
    victims = [p for p in sorted(third & running) if p not in keep and not any(x in p.lower() for x in prot)]
    for p in victims:
        sh(AM() + " force-stop " + shlex.quote(p), root=True, timeout=20)
    return victims


def drop_ram(kill_cached=False, cfg=None):
    """Root only. Drop page cache (and optionally kill cached background apps).
    -> (ok, before_mb, after_mb); a per-step report is left in RAM_LAST["detail"]."""
    if not ENV.root:
        RAM_LAST["detail"] = "ต้องใช้รูท"
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
    if cfg is not None and cfg.d["ram"]["kill_others"]:
        try:
            v = kill_others(cfg)
            steps.append(("kill-others", True, ("closed %d: %s" % (len(v), ", ".join(v))) if v else "nothing to close"))
        except Exception as e:
            steps.append(("kill-others", False, str(e)))
    time.sleep(1)
    RAM_LAST["detail"] = "; ".join(
        "%s: %s" % (n, "ok" if (ok and not m.strip()) else (" ".join(m.split())[:160] or "failed")) for n, ok, m in steps)
    log.info("ram steps: %s", RAM_LAST["detail"])
    return any(ok for _, ok, _ in steps), before, mem_available_mb()


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
def prefixes(cfg):
    """package_prefix + extra_prefixes (e.g. 'aya.') as a tuple that str.startswith accepts."""
    out = [cfg.d["package_prefix"]] + list(cfg.d.get("extra_prefixes") or [])
    return tuple(dict.fromkeys(x.strip() for x in out if isinstance(x, str) and x.strip()))


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
    main = prefix[0] if isinstance(prefix, tuple) else prefix   # main prefix first, so existing cookie order is kept
    return sorted(pk, key=lambda p: (p != "com.roblox.client", not p.startswith(main), p))


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
        return True, "เปิดแอปแล้วแต่ลิงก์เกมไม่ทำงาน (ใช้ launcher แทน)"
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
    "# ใส่คุกกี้ .ROBLOSECURITY ของบัญชีตัวเอง 1 บรรทัด / 1 บัญชี\n"
    "# เรียงตามอินสแตนซ์ที่ตรวจพบ (บรรทัดแรก = แพ็กเกจแรก) ห้ามแชร์ไฟล์นี้ให้ใคร\n"
    "# บรรทัดว่างและบรรทัดที่ขึ้นต้นด้วย # จะถูกข้าม\n"
    "# บรรทัดที่มีแค่ - = อินสแตนซ์นั้นยังไม่มีคุกกี้ (ใช้คงลำดับ)\n"
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
    """One entry per cookie line; a line that is just '-' is a placeholder (no cookie for that instance) -> ''."""
    res = []
    try:
        with open(COOKIE_PATH, encoding="utf-8-sig") as f:
            for line in f:
                l = line.strip()
                if not l or l.startswith("#"):
                    continue
                res.append("" if l == "-" else clean_cookie(l))
    except OSError:
        return []
    SECRETS.update(c for c in res if c)
    return res


def cookie_pairs(cfg):
    """Map cookie lines to ALL detected instances in order (placeholder lines are skipped)."""
    ck = read_cookies()
    pairs = [(pkg, ck[i]) for i, pkg in enumerate(cfg.d["detected"]) if i < len(ck) and ck[i]]
    return pairs, len([c for c in ck if c])


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
            return None, None, "คุกกี้หมดอายุ/ไม่ถูกต้อง"
        if r.status_code != 200:
            return None, None, "HTTP %d" % r.status_code
        j = r.json()
        name = j.get("name", "?")
        USERS.add(name)
        return j["id"], name, None
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
            raise RuntimeError("ไม่พบตาราง cookies")
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
        return False, "ต้องใช้สิทธิ์รูท"
    q = shlex.quote
    _, out = sh("find /data/data/%s -maxdepth 4 -type f -name Cookies 2>/dev/null | head -n 1" % q(pkg), root=True)
    path = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not path:
        return False, "ไม่พบฐานข้อมูลคุกกี้ (เปิดแอปค้างไว้ 1 ครั้งก่อน)"
    _, own = sh("stat -c '%%u:%%g' /data/data/%s" % q(pkg), root=True)
    own = own.strip()
    if not re.fullmatch(r"\d+:\d+", own):
        return False, "อ่านเจ้าของโฟลเดอร์แอปไม่ได้"
    sh(AM() + " force-stop " + q(pkg), root=True)
    tmp = os.path.join(TMP_DIR, "garnt_%s.db" % pkg.replace(".", "_"))
    try:
        rc, o = sh("cp %s %s && chmod 666 %s" % (q(path), q(tmp), q(tmp)), root=True)
        if rc != 0:
            return False, "คัดลอกฐานข้อมูลไม่สำเร็จ"
        _edit_cookie_db(tmp, cookie)
        rc, o = sh("cp %s %s && chown %s %s && chmod 660 %s" % (q(tmp), q(path), own, q(path), q(path)), root=True)
        if rc != 0:
            return False, "เขียนฐานข้อมูลกลับไม่สำเร็จ"
        sh("rm -f %s-journal %s-wal %s-shm; restorecon %s" % (q(path), q(path), q(path), q(path)), root=True)
        return True, "ใส่คุกกี้แล้ว"
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
            res.append((pkg, False, "คุกกี้ใช้ไม่ได้: %s" % err))
            continue
        ok, msg = inject_cookie(pkg, ck)
        res.append((pkg, ok, ("%s: %s" % (hide_name(name), msg)) if ok else msg))
    return res


def find_cookie_db(pkg):
    _, out = sh("find /data/data/%s -maxdepth 4 -type f -name Cookies 2>/dev/null | head -n 1" % shlex.quote(pkg), root=True)
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def read_app_cookie(pkg):
    """Root only: read .ROBLOSECURITY from the clone's WebView cookie DB (works on a copy, never edits the app).
    -> (cookie | None, message)"""
    if not ENV.root:
        return None, "ต้องใช้สิทธิ์รูท"
    path = find_cookie_db(pkg)
    if not path:
        return None, "ไม่พบฐานข้อมูลคุกกี้ (เปิดแอปค้างไว้ 1 ครั้งก่อน)"
    tmpd = tempfile.mkdtemp(prefix="garnt_", dir=TMP_DIR)
    try:
        s, t = shlex.quote(path), shlex.quote(tmpd)
        rc, _ = sh("cp %s %s/ && chmod -R a+rw %s" % (s, t, t), root=True)
        if rc != 0:
            return None, "คัดลอกฐานข้อมูลไม่สำเร็จ"
        sh("cp %s-wal %s-shm %s-journal %s/ 2>/dev/null; chmod -R a+rw %s" % (s, s, s, t, t), root=True)
        con = sqlite3.connect(os.path.join(tmpd, os.path.basename(path)), timeout=10)
        try:
            con.text_factory = lambda b: b.decode("utf-8", "replace")
            sel = "SELECT value, encrypted_value FROM cookies WHERE name='.ROBLOSECURITY' AND host_key LIKE '%roblox.com'"
            try:
                rows = con.execute(sel + " ORDER BY last_access_utc DESC").fetchall()
            except sqlite3.OperationalError:
                rows = con.execute(sel).fetchall()
        finally:
            con.close()
        for val, enc in rows:
            val = (val or "").strip()
            if val:
                ck = clean_cookie(val)
                SECRETS.add(ck)
                return ck, "ok"
        if any(enc for _, enc in rows):
            return None, "คุกกี้ในแอปถูกเข้ารหัส อ่านไม่ได้"
        return None, "ไม่พบคุกกี้ (ยังไม่ได้ล็อกอินในแอปนี้)"
    except Exception as e:
        return None, scrub(e)[:100]
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def pull_validated(pkgs):
    """Read each clone's cookie and check it with the Roblox API. -> [(pkg, cookie|None, name|None, message)]"""
    out = []
    for pkg in pkgs:
        ck, msg = read_app_cookie(pkg)
        if not ck:
            out.append((pkg, None, None, msg))
            continue
        uid, name, err = who_am_i(make_session(ck))
        if uid:
            out.append((pkg, ck, name, ""))
        else:
            out.append((pkg, None, None, "คุกกี้ในแอปใช้ไม่ได้: " + str(err)))
    return out


def store_cookies(cfg, found):
    """found = {pkg: cookie}. Updates cookie.txt in place: comments are kept, the line order follows the detected
    instances, and gaps are filled with '-'. Returns how many lines were written."""
    det = cfg.d["detected"]
    upd = {det.index(p): c for p, c in found.items() if p in det}
    if not upd:
        return 0
    try:
        with open(COOKIE_PATH, encoding="utf-8-sig") as f:
            raw = f.read().splitlines()
    except OSError:
        raw = COOKIE_TEMPLATE.splitlines()
    pos = [n for n, l in enumerate(raw) if l.strip() and not l.strip().startswith("#")]
    for i in sorted(upd):
        if i < len(pos):
            raw[pos[i]] = upd[i]
        else:
            while len(pos) < i:
                raw.append("-")
                pos.append(len(raw) - 1)
            raw.append(upd[i])
            pos.append(len(raw) - 1)
    tmp = COOKIE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(raw) + "\n")
    os.replace(tmp, COOKIE_PATH)
    try:
        os.chmod(COOKIE_PATH, 0o600)
    except OSError:
        pass
    SECRETS.update(upd.values())
    return len(upd)


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
            log.warning("webhook ล้มเหลว: %s", scrub(e))
        return False


# ----------------------------------------------------------------- heartbeat (in-game script -> local receiver)
LUA_TEMPLATE = r"""-- GARNT TOOL REJOIN heartbeat (auto-generated)
-- ใช้กับบัญชีและเครื่องของตัวเองเท่านั้น ต้องรันผ่านเอ็กซิคิวเตอร์ที่รองรับ HTTP
local HOST     = "http://127.0.0.1:__PORT__"
local ID       = "__ID__"       -- ชื่อแพ็กเกจหรือเลขอินสแตนซ์ ต้องตรงกับในเครื่องมือ
local INTERVAL = __INTERVAL__   -- ส่งทุกกี่วินาที
local SHOW_FPS = __SHOWFPS__    -- true = แสดงตัวเลข FPS บนจอ (ลากย้ายได้)
local DEFAULT_CAP = __DEFCAP__  -- FPS cap ของบัญชีที่ไม่อยู่ในรายการ (0 = ไม่แตะ)
local CAPS = {                  -- FPS cap ต่อบัญชี: [Roblox UserId] = FPS (ตั้งจากเมนูในเครื่องมือ)
__CAPS__
}

if getgenv then
    if getgenv().__GARNT_HB then return end
    getgenv().__GARNT_HB = true
end

if not game:IsLoaded() then game.Loaded:Wait() end
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local GuiService = game:GetService("GuiService")
local UserInputService = game:GetService("UserInputService")

local lp = Players.LocalPlayer
while not lp do task.wait(1); lp = Players.LocalPlayer end

-- FPS cap: ดู UserId ของบัญชีที่กำลังเล่นอยู่ แล้วตั้งเพดานตามที่ตั้งไว้ (ต้องมี setfpscap ในเอ็กซิคิวเตอร์)
-- capInfo ถูกส่งไปกับ heartbeat (cap=) และแสดงในคอนโซล: off=ไม่ได้ตั้งให้ ID นี้ nofn=ไม่มีฟังก์ชัน err=เรียกแล้ว error
--   ignNN=เรียกแล้วแต่อ่านกลับได้ NN flagNN=ใช้ FFlag สำรอง setNN=ตั้งแล้ว
local capInfo = "off"

local function findCapFn()
    if setfpscap then return setfpscap end
    if set_fps_cap then return set_fps_cap end
    if setfpslimit then return setfpslimit end
    local envs = { _G }
    if getgenv then table.insert(envs, 1, getgenv()) end
    for _, env in ipairs(envs) do
        for _, n in ipairs({ "setfpscap", "set_fps_cap", "setfpslimit" }) do
            local ok, f = pcall(function() return env[n] end)
            if ok and type(f) == "function" then return f end
        end
    end
    local ok, f = pcall(function() return syn and syn.set_fps_cap end)
    if ok and type(f) == "function" then return f end
    return nil
end

local function applyCap()
    local cap = CAPS[lp.UserId] or DEFAULT_CAP
    if type(cap) ~= "number" or cap <= 0 then capInfo = "off"; return end
    local fn = findCapFn()
    if fn then
        if not pcall(fn, cap) then capInfo = "err"; return end
        local back
        if getfpscap then
            local ok, v = pcall(getfpscap)
            if ok and type(v) == "number" then back = math.floor(v + 0.5) end
        end
        capInfo = (back and back ~= cap) and ("ign" .. tostring(back)) or ("set" .. tostring(cap))
        return
    end
    -- ฟังก์ชันสำรอง: setfflag (ไม่ยืนยันว่าได้ผลในทุกเอ็กซิคิวเตอร์)
    if setfflag then
        local ok = pcall(setfflag, "DFIntTaskSchedulerTargetFps", tostring(cap))
        capInfo = ok and ("flag" .. tostring(cap)) or "nofn"
        return
    end
    capInfo = "nofn"
end
pcall(applyCap)

local lastFrame = os.clock()
local frames, fps, winStart = 0, 0, os.clock()
RunService.Heartbeat:Connect(function()
    local now = os.clock()
    lastFrame = now
    frames = frames + 1
    if now - winStart >= 1 then
        fps = math.floor(frames / (now - winStart) + 0.5)
        frames, winStart = 0, now
    end
end)

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
    if os.clock() - lastFrame > 5 then return end   -- เกมค้าง: ไม่ส่ง
    local url = string.format("%s/hb?id=%s&user=%s&uid=%d&place=%d&job=%s&fps=%d", HOST,
        HttpService:UrlEncode(ID), HttpService:UrlEncode(lp.Name), lp.UserId, game.PlaceId,
        HttpService:UrlEncode(game.JobId), fps)
    local err = errorText()                          -- ขึ้นหน้าต่าง Disconnected: ส่งข้อความ error ทันที
    if err then url = url .. "&err=" .. HttpService:UrlEncode(string.sub(err, 1, 120)) end
    url = url .. "&cap=" .. capInfo
    httpGet(url)
end

pcall(function() GuiService.ErrorMessageChanged:Connect(function() pcall(beat) end) end)

task.spawn(function()
    while true do
        pcall(applyCap)                              -- ตั้งซ้ำทุกรอบ เผื่อเกม/เอ็กซิคิวเตอร์รีเซ็ตค่า
        pcall(beat)
        task.wait(INTERVAL)
    end
end)

-- ตัวเลข FPS: ไม่มีกรอบ ฟอนต์กลม ขนาดปรับตามความกว้างจออัตโนมัติ ลากไปวางที่ไหนก็ได้
-- (จำตำแหน่งไว้ถ้าเอ็กซิคิวเตอร์รองรับ writefile)
local POS_FILE = "garnt_fps_pos.txt"

local function parentGui(gui)
    local ok = false
    if gethui then ok = pcall(function() gui.Parent = gethui() end) end
    if not ok or not gui.Parent then
        ok = pcall(function() gui.Parent = game:GetService("CoreGui") end)
    end
    if not ok or not gui.Parent then
        local pg = lp:FindFirstChildOfClass("PlayerGui") or lp:WaitForChild("PlayerGui", 10)
        if pg then gui.Parent = pg end
    end
    return gui.Parent ~= nil
end

local function loadPos()
    if not (isfile and readfile) then return nil end
    local ok, data = pcall(function() if isfile(POS_FILE) then return readfile(POS_FILE) end end)
    if ok and type(data) == "string" then
        local x, y = string.match(data, "^(%-?%d+),(%-?%d+)$")
        if x then return tonumber(x), tonumber(y) end
    end
    return nil
end

local function savePos(x, y)
    if writefile then pcall(writefile, POS_FILE, string.format("%d,%d", x, y)) end
end

local function roundFont()
    for _, name in ipairs({ "FredokaOne", "Nunito", "GothamBold" }) do   -- ตัวกลมก่อน ถ้าไม่มีค่อยถอยไปตัวถัดไป
        local ok, f = pcall(function() return Enum.Font[name] end)
        if ok and f then return f end
    end
    return Enum.Font.SourceSansBold
end

local function buildFpsBox()
    local gui = Instance.new("ScreenGui")
    gui.Name = "GARNT_FPS"
    gui.ResetOnSpawn = false
    gui.IgnoreGuiInset = true
    gui.DisplayOrder = 999999
    if not parentGui(gui) then return end

    local box = Instance.new("Frame")
    box.Name = "Box"
    box.BackgroundColor3 = Color3.fromRGB(0, 0, 0)       -- พื้นดำ ไม่มีกรอบ
    box.BackgroundTransparency = 0.25
    box.BorderSizePixel = 0
    box.Active = true
    box.Parent = gui

    local corner = Instance.new("UICorner")
    corner.CornerRadius = UDim.new(1, 0)                 -- ขอบมนเต็มที่
    corner.Parent = box

    local label = Instance.new("TextLabel")
    label.BackgroundTransparency = 1
    label.Size = UDim2.new(1, 0, 1, 0)
    label.Font = roundFont()
    label.TextScaled = true
    label.TextColor3 = Color3.fromRGB(255, 255, 255)
    label.Text = "FPS --"
    label.Parent = box

    local pad = Instance.new("UIPadding")
    pad.PaddingLeft = UDim.new(0.12, 0)
    pad.PaddingRight = UDim.new(0.12, 0)
    pad.PaddingTop = UDim.new(0.14, 0)
    pad.PaddingBottom = UDim.new(0.14, 0)
    pad.Parent = label

    local W, H = 72, 28
    local function viewport()
        local cam = workspace.CurrentCamera
        return (cam and cam.ViewportSize) or Vector2.new(800, 600)
    end

    local function clamp(x, y)
        local vp = viewport()
        return math.clamp(x, 0, math.max(0, vp.X - W)), math.clamp(y, 0, math.max(0, vp.Y - H))
    end

    local function layout()                              -- ขนาดตามความกว้างจอ: เล็กกระทัดรัดแต่ยังอ่านออก
        local vp = viewport()
        W = math.clamp(math.floor(vp.X * 0.055), 56, 110)
        H = math.floor(W * 0.4)
        box.Size = UDim2.fromOffset(W, H)
        local x, y = clamp(box.Position.X.Offset, box.Position.Y.Offset)
        box.Position = UDim2.fromOffset(x, y)
    end

    local sx, sy = loadPos()
    if not sx then sx, sy = 12, 64 end
    box.Position = UDim2.fromOffset(sx, sy)
    layout()

    local cam = workspace.CurrentCamera
    if cam then
        cam:GetPropertyChangedSignal("ViewportSize"):Connect(function() pcall(layout) end)   -- หมุนจอ/ย่อขยายหน้าต่าง
    end

    local dragging, dragStart, startX, startY = false, nil, 0, 0
    box.InputBegan:Connect(function(input)
        local t = input.UserInputType
        if t == Enum.UserInputType.MouseButton1 or t == Enum.UserInputType.Touch then
            dragging = true
            dragStart = input.Position
            startX, startY = box.Position.X.Offset, box.Position.Y.Offset
            input.Changed:Connect(function()
                if input.UserInputState == Enum.UserInputState.End then
                    dragging = false
                    savePos(box.Position.X.Offset, box.Position.Y.Offset)
                end
            end)
        end
    end)
    UserInputService.InputChanged:Connect(function(input)
        if not dragging then return end
        local t = input.UserInputType
        if t == Enum.UserInputType.MouseMovement or t == Enum.UserInputType.Touch then
            local x, y = clamp(startX + (input.Position.X - dragStart.X), startY + (input.Position.Y - dragStart.Y))
            box.Position = UDim2.fromOffset(x, y)
        end
    end)

    while gui.Parent do
        label.Text = (fps > 0) and ("FPS " .. tostring(fps)) or "FPS --"
        task.wait(0.5)
    end
end

if SHOW_FPS then
    task.spawn(function() pcall(buildFpsBox) end)
end
"""


def cap_txt(rec):
    """Short status of the in-game FPS cap, from the heartbeat record."""
    c = rec.get("cap", "")
    if not c:
        return ""
    num = "".join(ch for ch in c if ch.isdigit())
    if c == "off":
        return " | cap: ไม่ได้ตั้งให้ ID นี้ (%s)" % hide_id(rec.get("uid", ""))
    if c == "nofn":
        return " | cap: เอ็กซิคิวเตอร์ไม่มี setfpscap"
    if c == "err":
        return " | cap: เรียกแล้ว error"
    if c.startswith("ign"):
        return " | cap: เรียกแล้วแต่ไม่ติด (อ่านกลับได้ %s)" % num
    if c.startswith("flag"):
        return " | cap: ใช้ FFlag สำรอง %s (ไม่ยืนยัน)" % num
    if c.startswith("set"):
        return " | cap %s ✓" % num
    return ""


def fps_caps(cfg):
    """-> (default_cap, {uid: cap}) from config; invalid entries are dropped."""
    fc = cfg.d["heartbeat"].get("fps_cap", {})
    try:
        default = max(0, min(int(fc.get("default", 0)), FPS_CAP_MAX))
    except (TypeError, ValueError):
        default = 0
    caps = {}
    for uid, rec in (fc.get("accounts") or {}).items():
        try:
            cap = int(rec.get("cap", 0) if isinstance(rec, dict) else rec)
            if str(uid).isdigit() and FPS_CAP_MIN <= cap <= FPS_CAP_MAX:
                caps[int(uid)] = cap
        except (TypeError, ValueError):
            continue
    return default, caps


def render_lua(inst_id, port, interval, show_fps=True, default_cap=0, caps=None):
    rows = ",\n".join("    [%d] = %d" % (u, c) for u, c in sorted((caps or {}).items()))
    return (LUA_TEMPLATE.replace("__PORT__", str(int(port))).replace("__INTERVAL__", str(int(interval)))
            .replace("__SHOWFPS__", "true" if show_fps else "false")
            .replace("__DEFCAP__", str(int(default_cap)))
            .replace("__CAPS__", rows + ("," if rows else ""))
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
            f.write(render_lua(pkg, h["port"], h["interval"], h.get("show_fps", True), *fps_caps(cfg)))
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
        src = render_lua(ident, h["port"], h["interval"], h.get("show_fps", True), *fps_caps(cfg))
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
    names = {"created": "สร้างแล้ว", "updated": "อัปเดตแล้ว", "ok": "พร้อมใช้งาน",
             "skipped": "ข้าม (มีไฟล์ชื่อเดียวกันที่ไม่ใช่ของเครื่องมือ)"}
    for path, st in results:
        if st == "ok" and quiet_ok:
            continue
        say("heartbeat.lua: %s → %s" % (names.get(st, st), path), "red" if st.startswith("error") else "green" if st != "skipped" else "yellow")
    if any(st == "created" for _, st in results) and not cfg.d["autoexec"]["enabled_once"]:
        cfg.d["autoexec"]["enabled_once"] = True
        cfg.d["heartbeat"]["enabled"] = True       # first install only: turn heartbeat on (later changes are yours)
        cfg.save()
        say("เปิดใช้ heartbeat ให้แล้ว (ปิดได้ที่ เมนู 6 > Heartbeat > 1)", "cyan")


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
               "err": re.sub(r"[^\w .:()\-]", "", q.get("err", ""))[:120],
               "fps": re.sub(r"\D", "", q.get("fps", ""))[:4],
               "cap": re.sub(r"[^A-Za-z0-9]", "", q.get("cap", ""))[:12]}
        with self.lock:
            self.d[key] = rec
            if rec["user"]:
                self.by_user[rec["user"]] = rec
                USERS.add(rec["user"])
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
            log.warning("console webhook ล้มเหลว: %s", scrub(e))
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
    with (inp or RawInput()) as ri, Live(console=console, auto_refresh=False, screen=True, vertical_overflow="crop") as live:
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


CONSOLE_HELP = ("คำสั่ง: rejoin <เลข|all> | pause <เลข|all> | resume <เลข|all> | status | ram | shot | streamer | help | quit")


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
        self.wake, self.cmd, self.paused = threading.Event(), "", False
        self.state, self.detail = "queued", ""
        self.fails = self.rejoins = 0
        self.launched_at = 0.0
        self.ok_since = None
        self.next_at = 0.0
        self.started_at = time.time()


REASON = {"dead": "แอปปิด/ค้าง (ไม่พบโปรเซส)", "background": "แอปไม่ได้อยู่หน้าจอ",
          "noheartbeat": "ไม่มี heartbeat จากสคริปต์ในเกม",
          "disconnected": "พบหน้าต่าง Disconnected (เช่น Error Code 277)",
          "notingame": "ไม่ได้อยู่ในเกม", "timed": "รีจอยตามเวลาที่ตั้งไว้"}
STATE_LABEL = {"queued": ("รอคิวเปิด", "dim"), "launching": ("กำลังเปิดแอป", "cyan"),
            "joining": ("กำลังเข้าเกม", "yellow"), "ok": ("ปกติ", "bold green"),
            "unknown": ("ไม่ทราบสถานะ", "magenta"), "backoff": ("รอรีจอย", "bold red"),
            "stopped": ("หยุดแล้ว", "dim"), "paused": ("หยุดชั่วคราว", "yellow")}


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
        self.event("เริ่มทำงาน %d อินสแตนซ์ (โหมด %s)" % (len(self.insts), "ROOT" if ENV.root else "ไม่รูท"), "green")
        self.hook.send("start", "▶️ เริ่มรีจอยอัตโนมัติ", "เริ่มจัดการ %d อินสแตนซ์" % len(self.insts),
                       [("โหมด", "ROOT" if ENV.root else "ไม่รูท"), ("ตรวจทุก", "%ds" % self.interval()),
                        ("แพ็กเกจ", ", ".join(i.pkg for i in self.insts))])
        SNAP["fg"] = bool(ENV.root and (self.cfg.d["auto"]["foreground_check"]))
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
        self.event("หยุดการทำงาน (รันมา %s)" % up)
        self.hook.send("stop", "⏹️ หยุดรีจอยอัตโนมัติ", "รันมาแล้ว " + up,
                       [(i.pkg, "รีจอย %d ครั้ง" % i.rejoins) for i in self.insts], sync=True)

    def _next_tick(self):
        """All instances are checked on the same time grid so one snapshot serves every instance."""
        iv = self.interval()
        return self.t0 + (int((time.time() - self.t0) // iv) + 1) * iv

    # -- housekeeping: RAM clearing + periodic status report with screenshot
    def _housekeeper(self):
        c = self.cfg.d
        last_ram = last_rep = time.time()
        while not self.stop.wait(self.HK_TICK):
            now = time.time()
            r = c["ram"]
            if r["enabled"] and ENV.root:
                free = mem_available_mb()
                due = r["interval_min"] > 0 and now - last_ram >= r["interval_min"] * self.MIN
                low = bool(r["min_free_mb"] > 0 and free is not None and free < r["min_free_mb"] and now - last_ram >= self.MIN)
                if due or low:
                    self.clear_ram("แรมว่างต่ำ" if low else "ตามเวลา")
                    last_ram = time.time()
            pm = int(c["webhook"]["periodic_min"])
            if pm > 0 and now - last_rep >= pm * self.MIN:
                last_rep = now
                try:
                    self.report(pm)
                except Exception as e:
                    self.event("ส่งรายงานสถานะไม่สำเร็จ: %s" % e, "red")

    def clear_ram(self, why):
        ok, before, after = drop_ram(self.cfg.d["ram"]["kill_cached"], self.cfg)
        if ok:
            self.event("ล้างแรม (%s): ว่าง %s → %s MB | %s" % (why, before, after, RAM_LAST["detail"]), "cyan")
        else:
            self.event("ล้างแรมไม่สำเร็จ: %s" % RAM_LAST["detail"], "red")

    def _fps_txt(self, inst):
        rec = self.hb.latest(inst) if self.hb else None
        if rec and rec.get("fps") and time.time() - rec["ts"] <= int(self.cfg.d["heartbeat"]["timeout"]):
            return " | FPS " + rec["fps"] + cap_txt(rec)
        return ""

    def report(self, minutes, shot=None):
        w = self.cfg.d["webhook"]
        if not (w["enabled"] and w["url"] and (w["events"].get("status") or minutes is None)):
            return
        if shot is None and minutes is not None and ENV.root and w["send_screenshot"]:
            shot = take_shot("status")
        mem = mem_available_mb()
        fields = [(i.label, "%s | รีจอย %d | ล้มเหลว %d%s" % (STATE_LABEL.get(i.state, (i.state,))[0], i.rejoins, i.fails,
                                                              (" | " + i.user) if i.user else "") + self._fps_txt(i)) for i in self.insts][:22]
        fields.append(("RAM ว่าง", ("%d MB" % mem) if mem is not None else "-"))
        fields.append(("รันมาแล้ว", fmt_dur(time.time() - self.t0)))
        fields.append(("CPU", cpu_text()))
        self.hook.send("status", "📸 รายงานสถานะ", "รายงานตามคำสั่ง" if minutes is None else "รายงานประจำทุก %d นาที" % minutes, fields, shot)

    # -- console commands (typed in the live panel)
    def _take_cmd(self, inst):
        cmd, inst.cmd = inst.cmd, ""
        inst.wake.clear()
        return cmd

    def _manual(self, inst):
        cmd = self._take_cmd(inst)
        if cmd == "pause":
            inst.paused, inst.state, inst.detail = True, "paused", "หยุดชั่วคราวด้วยคำสั่ง (พิมพ์ resume)"
            self.event("%s: หยุดชั่วคราว" % inst.label, "yellow")
        elif cmd in ("rejoin", "resume"):
            inst.paused, inst.fails, inst.ok_since = False, 0, None
            self.event("%s: รีจอยด้วยคำสั่ง" % inst.label, "cyan")
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
                self.event("ใช้: %s <เลขอินสแตนซ์|all>" % cmd, "red")
            else:
                for i in ts:
                    i.cmd = act
                    i.wake.set()
                self.event("สั่ง %s อินสแตนซ์ %s" % (act, ", ".join(str(i.idx) for i in ts)), "cyan")
        elif cmd == "status":
            for i in self.insts:
                self.event("#%d %s | %s" % (i.idx, STATE_LABEL.get(i.state, (i.state,))[0], i.detail or "-"))
        elif cmd in ("streamer", "hide"):
            on = (not STREAMER["on"]) if not args else args[0] in ("on", "1", "yes", "y")
            STREAMER["on"] = self.cfg.d["ui"]["streamer_mode"] = on
            self.cfg.save()
            self.event("โหมดซ่อนชื่อบัญชี (streamer): " + ("เปิด" if on else "ปิด"), "cyan")
        elif cmd in ("ram", "shot"):
            if not ENV.root:
                self.event("คำสั่ง %s ต้องใช้รูท" % cmd, "red")
            else:
                threading.Thread(target=self._cmd_root, args=(cmd,), daemon=True).start()
        else:
            self.event("ไม่รู้จักคำสั่ง: %s (พิมพ์ help)" % cmd, "red")
        return None

    def _cmd_root(self, cmd):
        try:
            if cmd == "ram":
                self.clear_ram("คำสั่ง")
            else:
                path = take_shot("console")
                self.event("บันทึกภาพหน้าจอ: %s" % os.path.basename(path) if path else "แคปหน้าจอไม่สำเร็จ", "cyan" if path else "red")
                if path:
                    self.report(None, shot=path)
        except Exception as e:
            self.event("คำสั่ง %s ผิดพลาด: %s" % (cmd, e), "red")

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
                    self.event("%s: ข้อผิดพลาด %s" % (inst.label, e), "red")
        except Exception as e:
            self.event("%s: worker หยุดเพราะ %s" % (inst.label, e), "red")
        inst.state = "stopped"

    def _launch(self, inst, kind, force=False):
        c = self.cfg.d
        inst.state, inst.detail = "queued", "รอคิวเปิดแอป"
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
        inst.state, inst.detail = "joining", "รอเข้าเกม"
        if not ok:
            self.event("%s: เปิดเกมไม่สำเร็จ: %s" % (inst.label, msg), "red")
            return True
        if kind == "start":
            self.event("%s: เปิดเกมแล้ว" % inst.label, "green")
        else:
            inst.rejoins += 1
            self.event("%s: รีจอยแล้ว (ครั้งที่ %d)" % (inst.label, inst.rejoins), "cyan")
            self.hook.send("rejoin", "🔁 รีจอยแล้ว", inst.label,
                           [("แพ็กเกจ", inst.pkg), ("บัญชี", inst.user or "-"), ("ครั้งที่", str(inst.rejoins))])
        return True

    def presence(self, inst):
        if not (inst.cookie and self.cfg.d["auto"]["presence_check"]) or inst.cookie_bad:
            return None
        if inst.session is None:
            inst.session = make_session(inst.cookie)
        if inst.uid is None:
            uid, name, err = who_am_i(inst.session)
            if not uid:
                if err and "คุกกี้" in err:
                    inst.cookie_bad = True
                    self.event("%s: %s" % (inst.label, err), "red")
                return None
            inst.uid, inst.user = uid, name
        ttl = 60 if self.cfg.d["performance"]["low_cpu"] else 0
        if ttl and time.time() - inst.pres_at < ttl - 1:
            return inst.pres_val
        inst.pres_val, inst.pres_at = in_game(inst.session, inst.uid), time.time()
        return inst.pres_val

    def hb_state(self, inst):
        """None = no usable heartbeat info (never seen / disabled); else ("ok"|"stale", detail)."""
        hc = self.cfg.d["heartbeat"]
        if not (hc["enabled"] and self.hb):
            return None
        rec = self.hb.latest(inst)
        if rec is None:
            return None  # script never reported: fall back to other signals
        if rec["ts"] < inst.launched_at:
            return "stale", "ยังไม่มี heartbeat หลังเปิดเกมใหม่"
        age = time.time() - rec["ts"]
        if rec.get("err") and age <= int(hc["timeout"]):
            return "err", "Disconnected: " + rec["err"]
        if rec["user"] and not inst.user:
            inst.user = rec["user"]
        who = (" [%s]" % rec["user"]) if rec["user"] else ""
        if age <= int(hc["timeout"]):
            return "ok", "heartbeat %ds ก่อน%s%s" % (age, who, (" | FPS " + rec["fps"] + cap_txt(rec)) if rec.get("fps") else "")
        return "stale", "ไม่มี heartbeat มา %ds%s" % (age, who)

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
        pres = self.presence(inst)
        who = (" [%s]" % inst.user) if inst.user else ""
        if pres is True:
            return "ok", "อยู่ในเกม" + who
        if pres is False:
            return "notingame", REASON["notingame"] + who
        if alive:
            return "ok", "แอปทำงานอยู่ (ไม่มีข้อมูลสถานะเกม)"
        return "unknown", "ไม่ทราบสถานะ (ไม่รูท/ไม่มีคุกกี้)"

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
                self.event("%s: เสถียรครบ %ds รีเซ็ต backoff" % (inst.label, c["stable_reset"]))
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
            self.event("%s: %s (ครั้งที่ %d) รีจอยใน %ds" % (inst.label, REASON[reason], inst.fails, delay), "red")
            shot = take_shot(inst.pkg) if c["auto"]["screenshot_on_crash"] else None
            self.hook.send("crash", "⚠️ หลุด/ค้าง", inst.label,
                           [("สาเหตุ", REASON[reason]), ("แพ็กเกจ", inst.pkg), ("บัญชี", inst.user or "-"),
                            ("ล้มเหลวติดกัน", str(inst.fails)), ("รีจอยใน", "%ds" % delay)], shot)
        else:
            self.event("%s: %s" % (inst.label, REASON[reason]), "cyan")
        if delay:
            inst.state, inst.next_at = "backoff", time.time() + delay
            if inst.wake.wait(delay):
                if self.stop.is_set():
                    return
                if self._take_cmd(inst) == "pause":
                    inst.paused, inst.state, inst.detail = True, "paused", "หยุดชั่วคราวด้วยคำสั่ง (พิมพ์ resume)"
                    return
        self._launch(inst, "rejoin")

    # -- live panel
    def render(self, buf=None):
        now = time.time()
        mem = mem_available_mb()
        load = sys_load()
        head = Text.assemble(
            ("โหมด: ", "dim"), ("ROOT" if ENV.root else "ไม่รูท", "bold green" if ENV.root else "bold yellow"),
            ("  |  ตรวจทุก %ds" % self.interval(), ""), ("  |  หน่วงเปิด %ss" % self.cfg.d["launch_delay"], ""),
            ("  |  รันมา " + fmt_dur(now - self.t0), "bold"),
            (("  |  RAM ว่าง %d MB" % mem) if mem is not None else "", ""),
            (("  |  โหลด %.1f" % load) if load is not None else "", ""),
            ("\nพิมพ์คำสั่งแล้วกด Enter (help = รายการคำสั่ง) | quit หรือ Ctrl+C = หยุดและกลับเมนู", "dim"))
        t = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
        for col in ("#", "แพ็กเกจ", "บัญชี", "สถานะ", "เล่นมา", "ล้มเหลว", "รีจอย", "ถัดไป", "รายละเอียด"):
            t.add_column(col, overflow="fold")
        for i in self.insts:
            label, style = STATE_LABEL.get(i.state, (i.state, ""))
            nxt = "%ds" % max(0, int(i.next_at - now)) if i.next_at and i.state in ("ok", "unknown", "joining", "backoff") else "-"
            up = fmt_dur(now - i.ok_since) if i.ok_since and i.state == "ok" else "-"
            t.add_row(str(i.idx), i.pkg, hide_name(i.user) or "-", Text(label, style=style), up, str(i.fails),
                      str(i.rejoins), nxt, T(hide_users(i.detail)))
        # fit the terminal: fixed rows = head 4 + table (n+5) + console 3 + events border 2
        n_ev = max(1, min(8, console.size.height - (len(self.insts) + 14)))
        ev = Text(no_wrap=True, overflow="ellipsis")
        for ts, msg, st in list(reversed(self.events))[:n_ev]:   # newest first so cropping never hides the latest
            ev.append(ts + "  ", style="dim")
            ev.append(hide_users(msg) + "\n", style=st)
        parts = [Panel(head, title="GARNT TOOL REJOIN", border_style="cyan"), t]
        if buf is not None:
            parts.append(Panel(Text.assemble(("› ", "bold yellow"), (scrub(buf), "bold"), ("█", "blink")),
                               title="คอนโซล", border_style="yellow"))
        parts.append(Panel(ev, title="เหตุการณ์ล่าสุด (ใหม่สุดอยู่บน)", border_style="blue"))
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
    return ask(prompt + " (y/N)").lower() in ("y", "yes", "ใช่")


def pause():
    ask("กด Enter เพื่อกลับเมนู")


def onoff(v):
    return Text("เปิด" if v else "ปิด", style="bold green" if v else "bold red")


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
        ("โหมด: ", "dim"), ("ROOT" if ENV.root else "ไม่รูท", "bold green" if ENV.root else "bold yellow"),
        ("   อินสแตนซ์: ", "dim"), (str(len(cfg.d["detected"])), "bold"),
        ("   ลิงก์เกม: ", "dim"), ("ตั้งแล้ว" if link else "ยังไม่ตั้ง", "green" if link else "red"),
        ("   Webhook: ", "dim"), ("เปิด" if cfg.d["webhook"]["enabled"] else "ปิด",
                                  "green" if cfg.d["webhook"]["enabled"] else "red")))
    console.print(Text("โฟลเดอร์: " + BASE, style="dim"))
    console.print()


def selected_packages(cfg):
    sel = cfg.d["packages"]
    return [p for p in cfg.d["detected"] if not sel or p in sel]


def refresh_packages(cfg):
    found = scan_packages(prefixes(cfg))
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
        say("ยังไม่ได้ตั้งลิงก์เกม (หรือลิงก์ไม่ถูกต้อง) ไปที่เมนู 3 ก่อน", "red")
        return pause()
    if not pkgs:
        say("ไม่พบแพ็กเกจ Roblox ไปที่เมนู 4 เพื่อสแกนก่อน", "red")
        return pause()
    if cfg.d["auto"]["cookie_pull_on_start"] and ENV.root:
        have = {p for p, _ in cookie_pairs(cfg)[0]}
        need = [p for p in pkgs if p not in have]
        if need:
            say("กำลังดึงคุกกี้จากแอป %d อินสแตนซ์ที่ยังไม่มีคุกกี้..." % len(need), "dim")
            got = {}
            for pkg, ck, name, msg in pull_validated(need):
                if ck:
                    got[pkg] = ck
                say("%s: %s" % (pkg, ("ได้คุกกี้ของ " + hide_name(name)) if ck else msg), "green" if ck else "yellow")
            if got:
                store_cookies(cfg, got)
    if cfg.d["auto"]["cookie_login_on_start"]:
        if ENV.root:
            pairs = [(p, c) for p, c in cookie_pairs(cfg)[0] if p in pkgs]
            for pkg, ok, msg in cookie_login(pairs):
                say("%s: %s" % (pkg, msg), "green" if ok else "red")
        else:
            say("ข้ามการล็อกอินด้วยคุกกี้: ต้องใช้รูท", "yellow")
    if cfg.d["autoexec"]["auto_create"]:
        report_autoexec(cfg, install_autoexec(cfg))
    cmap = dict(cookie_pairs(cfg)[0])
    det = cfg.d["detected"]
    insts = [Inst(det.index(p) + 1, p, cmap.get(p)) for p in pkgs]
    hb = hbs = None
    hc = cfg.d["heartbeat"]
    if hc["enabled"]:
        hb = Heartbeats()
        hbs = HBServer(hb, hc["port"], prefixes(cfg))
        hb.single = len(insts) == 1
        try:
            hbs.start()
            say("รับ heartbeat ที่ 127.0.0.1:%d (ทดสอบ: curl http://127.0.0.1:%d/ping)" % (hbs.port, hbs.port), "green")
        except OSError as e:
            say("เปิดพอร์ต heartbeat ไม่ได้ (%s) ข้ามการใช้ heartbeat" % e, "red")
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
    say("\nกำลังหยุด...", "yellow")
    eng.shutdown()
    chook.close()
    if hbs:
        hbs.stop()
    say("หยุดการทำงานแล้ว", "green")
    pause()


def status_view(cfg, info, buf=""):
    det = cfg.d["detected"]
    pairs, n_ck = info
    cmap = dict(pairs)
    fg = foreground_text() if ENV.root else ""
    t = Table(title="อินสแตนซ์ที่ตรวจพบ", box=box.ROUNDED, header_style="bold cyan", expand=True)
    for col in ("#", "แพ็กเกจ", "เลือกรัน", "คุกกี้", "ทำงานอยู่", "อยู่หน้าจอ"):
        t.add_column(col)
    sel = selected_packages(cfg)
    for i, pkg in enumerate(det, 1):
        alive = proc_alive(pkg)
        run = Text("ใช่", style="green") if alive else (Text("ไม่", style="red") if ENV.root else Text("ไม่ทราบ", style="magenta"))
        front = Text("-") if not ENV.root else (Text("ใช่", style="green") if (pkg + "/") in fg else Text("ไม่", style="dim"))
        t.add_row(str(i), pkg, onoff(pkg in sel), Text("มี" if pkg in cmap else "-"), run, front)
    mem, load = mem_available_mb(), sys_load()
    foot = "โหมด: %s | ตรวจทุก %ss | หน่วงเปิด %ss | คุกกี้ในไฟล์: %d บรรทัด | RAM ว่าง %s MB | โหลด %s%s" % (
        "ROOT" if ENV.root else "ไม่รูท", cfg.d["check_interval"], cfg.d["launch_delay"], n_ck,
        mem if mem is not None else "?", ("%.1f" % load) if load is not None else "?",
        "" if ENV.root else " | โหมดไม่รูทตรวจโปรเซส/หน้าจอไม่ได้")
    return Group(t, T(foot, "dim"), T("แผงสดรีเฟรชอัตโนมัติ — กด Enter หรือ Ctrl+C เพื่อกลับเมนู", "yellow"))


def menu_status(cfg):
    if not cfg.d["detected"]:
        say("ยังไม่พบแพ็กเกจ ลองเมนู 4 (สแกนแพ็กเกจ)", "red")
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
        t = Table(title="ตั้งค่า", box=box.ROUNDED, header_style="bold cyan")
        t.add_column("#")
        t.add_column("รายการ")
        t.add_column("ค่าปัจจุบัน")
        link = cfg.d["game_link"]
        t.add_row("1", "ลิงก์เกม / Place ID", T((link[:60] + "…" if len(link) > 60 else link) or "(ยังไม่ตั้ง)"))
        t.add_row("2", "ช่วงเวลาตรวจสอบ (วินาที)", str(cfg.d["check_interval"]))
        t.add_row("3", "หน่วงเวลาเปิดแต่ละอินสแตนซ์ (วินาที)", str(cfg.d["launch_delay"]))
        t.add_row("4", "ซ่อนชื่อบัญชี (streamer mode: โชว์ 4 ตัวแรก ที่เหลือเป็น #)", onoff(cfg.d["ui"]["streamer_mode"]))
        console.print(t)
        if p:
            say("ลิงก์ที่ใช้เปิดจริง: " + p[0], "dim")
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            v = ask("วางลิงก์เกม (roblox.com/games/..., ลิงก์ private server, ลิงก์แชร์ หรือ Place ID):")
            if v and not parse_link(v):
                say("รูปแบบลิงก์ไม่ถูกต้อง", "red")
                pause()
            elif v:
                cfg.d["game_link"] = v
                cfg.save()
        elif c == "4":
            cfg.d["ui"]["streamer_mode"] = STREAMER["on"] = not cfg.d["ui"]["streamer_mode"]
            cfg.save()
        elif c in ("2", "3"):
            key, lo = ("check_interval", 10) if c == "2" else ("launch_delay", 0)
            v = ask("ค่าใหม่ (วินาที, ต่ำสุด %d):" % lo)
            if v.isdigit() and int(v) >= lo:
                cfg.d[key] = int(v)
                cfg.save()
            elif v:
                say("ต้องเป็นตัวเลข >= %d" % lo, "red")
                pause()


def menu_scan(cfg):
    say("กำลังสแกนแพ็กเกจ %s ..." % ", ".join(p + "*" for p in prefixes(cfg)), "cyan")
    found = refresh_packages(cfg)
    if not found:
        say("ไม่พบแพ็กเกจ (ถ้าอินสแตนซ์ใช้ชื่ออื่น พิมพ์ชื่อแพ็กเกจเองได้ คั่นด้วยคอมมา)", "red")
        v = ask("แพ็กเกจ (Enter=ข้าม):")
        names = [x.strip() for x in v.split(",") if re.fullmatch(r"[\w.]+", x.strip())]
        if names:
            cfg.d["detected"] = names
            cfg.save()
            found = names
        else:
            return pause()
    t = Table(title="แพ็กเกจที่พบ", box=box.ROUNDED, header_style="bold cyan")
    for col in ("#", "แพ็กเกจ", "เลือกรัน"):
        t.add_column(col)
    sel = selected_packages(cfg)
    for i, p in enumerate(found, 1):
        t.add_row(str(i), p, onoff(p in sel))
    console.print(t)
    v = ask("เลือกที่จะรัน เช่น 1,3 | all=ทั้งหมด | Enter=คงเดิม:").lower()
    if v == "all":
        cfg.d["packages"] = []
    elif v:
        idx = [int(x) for x in re.findall(r"\d+", v) if 0 < int(x) <= len(found)]
        if idx:
            cfg.d["packages"] = [found[i - 1] for i in sorted(set(idx))]
    cfg.save()
    say("บันทึกแล้ว", "green")
    pause()


def menu_cookie(cfg):
    if ensure_cookie_file():
        say("สร้างไฟล์ตัวอย่างแล้ว: " + COOKIE_PATH, "green")
    pairs, n_ck = cookie_pairs(cfg)
    if not n_ck:
        say("ยังไม่มีคุกกี้ใน cookie.txt  (1 บรรทัด / 1 บัญชี เรียงตามอินสแตนซ์)\nไฟล์: " + COOKIE_PATH, "yellow")
        return pause()
    det = cfg.d["detected"]
    if n_ck > len(det):
        say("คุกกี้มากกว่าอินสแตนซ์ (%d > %d) ส่วนเกินจะถูกข้าม" % (n_ck, len(det)), "yellow")
    t = Table(title="คุกกี้ที่จับคู่กับอินสแตนซ์ (ตามลำดับ)", box=box.ROUNDED, header_style="bold cyan")
    for col in ("บรรทัด", "แพ็กเกจ", "คุกกี้", "ผลตรวจ"):
        t.add_column(col)
    valid = []
    for i, (pkg, ck) in enumerate(pairs, 1):
        uid, name, err = who_am_i(make_session(ck))
        if uid:
            valid.append((pkg, ck))
        t.add_row(str(i), pkg, mask(ck), Text(("ใช้ได้: " + hide_name(name)) if uid else ("ใช้ไม่ได้: " + str(err)),
                                              style="green" if uid else "red"))
    console.print(t)
    if not ENV.root:
        say("โหมดไม่รูท: ตรวจคุกกี้ได้อย่างเดียว การใส่คุกกี้ลงแอปต้องใช้รูท", "yellow")
        return pause()
    if valid and confirm("ใส่คุกกี้ที่ใช้ได้ %d ตัวลงแอป (แอปจะถูก force-stop)?" % len(valid)):
        for pkg, ok, msg in cookie_login(valid):
            say("%s: %s" % (pkg, msg), "green" if ok else "red")
    pause()


def api_post(s, url, body=None):
    """POST with CSRF retry -> (ok, message)"""
    retried = False
    try:
        if "apis.roblox.com" in url:
            s.headers.setdefault("Origin", "https://www.roblox.com")
            s.headers.setdefault("Referer", "https://www.roblox.com/")
        r = s.post(url, json=body, timeout=15)
        if r.status_code == 403 and "x-csrf-token" in r.headers:
            s.headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
            r = s.post(url, json=body, timeout=15)
            retried = True
    except Exception as e:
        return False, scrub(e)[:80]
    msg = ""
    try:
        j = r.json()
        msg = (j.get("errors") or [{}])[0].get("message", "") or j.get("message", "")
        if r.status_code == 200 and j.get("success") is False:
            return False, msg or "success=false"
    except Exception:
        pass
    if r.status_code == 200:
        return True, "ok"
    if r.status_code == 401:
        msg = "คุกกี้หมดอายุ"
    elif r.status_code == 429:
        msg = "ส่งถี่เกินไป รอสักพัก"
    if not msg and r.status_code not in (401, 429):
        msg = scrub(" ".join((r.text or "").split()))[:70] or "(เนื้อหาตอบกลับว่าง)"
    if r.status_code == 403:
        msg += " [csrf: " + ("ส่งโทเคนซ้ำแล้วยังไม่ผ่าน" if retried else
                             "เซิร์ฟเวอร์ไม่ส่งโทเคนมา") + "]"
    return False, ("HTTP %d %s" % (r.status_code, msg)).strip()


def resolve_user(s, text):
    """username or numeric ID -> (id, name) or (None, None)"""
    text = text.strip().lstrip("@")
    try:
        if text.isdigit():
            r = s.get("https://users.roblox.com/v1/users/%s" % text, timeout=12)
            if r.status_code == 200:
                j = r.json()
                USERS.add(j.get("name", ""))
                return j["id"], j.get("name", "?")
            return None, None
        r = s.post("https://users.roblox.com/v1/usernames/users",
                   json={"usernames": [text], "excludeBannedUsers": False}, timeout=12)
        d = r.json().get("data") or []
        if d:
            USERS.add(d[0]["name"])
            return d[0]["id"], d[0]["name"]
    except Exception:
        pass
    return None, None


SOCIAL_URL = {"block": "https://apis.roblox.com/user-blocking-api/v1/users/%d/block-user",
              "unblock": "https://apis.roblox.com/user-blocking-api/v1/users/%d/unblock-user",
              "friend": "https://friends.roblox.com/v1/users/%d/request-friendship",
              "accept": "https://friends.roblox.com/v1/users/%d/accept-friend-request"}
SOCIAL_NAME = {"block": "บล็อก", "unblock": "เลิกบล็อก", "friend": "ส่งคำขอเป็นเพื่อน"}

SOCIAL_LEGACY = {"block": "https://accountsettings.roblox.com/v1/users/%d/block",
                 "unblock": "https://accountsettings.roblox.com/v1/users/%d/unblock"}


def ensure_tracker(s, uid=0, force_fake=False):
    """ให้เซสชันมีคุกกี้ RBXEventTrackerV2 (browserid) ที่ apis.roblox.com ใช้เป็น BrowserTrackerID: ขอจากเซิร์ฟเวอร์ก่อน ไม่ได้ก็สร้างเอง"""
    if not force_fake:
        if any(c.name == "RBXEventTrackerV2" for c in s.cookies):
            return
        if not getattr(s, "_garnt_home", False):
            s._garnt_home = True
            try:
                s.get("https://www.roblox.com/home", timeout=15)
            except Exception:
                pass
            if any(c.name == "RBXEventTrackerV2" for c in s.cookies):
                return
    import random
    now = datetime.now()
    val = "CreateDate=%d/%d/%d %d:%02d:%02d %s&rbxid=%d&browserid=%d" % (
        now.month, now.day, now.year, now.hour % 12 or 12, now.minute, now.second,
        "AM" if now.hour < 12 else "PM", int(uid or 0), random.randint(10 ** 10, 10 ** 11 - 1))
    s.cookies.set("RBXEventTrackerV2", val, domain=".roblox.com", path="/")


def is_blocking(s, tid):
    """True/False/None(ไม่ทราบ): บัญชีนี้มี tid อยู่ในรายการที่บล็อกไว้หรือไม่"""
    cursor = ""
    try:
        for _ in range(10):
            r = s.get("https://apis.roblox.com/user-blocking-api/v1/users/get-blocked-users",
                      params={"cursor": cursor, "count": 50}, timeout=15)
            if r.status_code != 200:
                return None
            d = (r.json() or {}).get("data") or {}
            if int(tid) in [int(x) for x in (d.get("blockedUserIds") or [])]:
                return True
            cursor = d.get("cursor") or ""
            if not cursor:
                return False
    except Exception:
        return None
    return None


def social_call(s, action, tid, uid=0):
    """บล็อก/เลิกบล็อก/เพื่อน -> (ok, ข้อความ) ใช้ user-blocking-api ก่อน ถ้าได้ 404 ค่อยลอง URL เก่า"""
    body = {} if action != "friend" else None
    web = "apis.roblox.com" in SOCIAL_URL[action]
    if web:
        ensure_tracker(s, uid)
    ok, msg = api_post(s, SOCIAL_URL[action] % tid, body)
    if web and not ok and "BrowserTrackerID" in msg:
        ensure_tracker(s, uid, force_fake=True)
        ok, msg = api_post(s, SOCIAL_URL[action] % tid, body)
    if web and not ok and msg.startswith("HTTP 400") and action in ("block", "unblock"):
        st = is_blocking(s, tid)
        if st is True and action == "block":
            return True, "บล็อกอยู่แล้ว (ตรวจจากรายการบล็อก)"
        if st is False and action == "unblock":
            return True, "ไม่ได้บล็อกอยู่ ไม่ต้องทำอะไร"
        if st is not None:
            msg += " [ตรวจรายการบล็อกแล้ว: " + ("บล็อกอยู่" if st else "ไม่ได้บล็อก") + "]"
    if not ok and msg.startswith("HTTP 404") and action in SOCIAL_LEGACY:
        ok2, msg2 = api_post(s, SOCIAL_LEGACY[action] % tid, body)
        if ok2:
            return True, msg2
        msg += " / " + msg2
    return ok, msg


def social_accounts(cfg):
    """Accounts that have a valid cookie in cookie.txt -> [(pkg, session, uid, name)]"""
    out = []
    for pkg, ck in cookie_pairs(cfg)[0]:
        s = make_session(ck)
        uid, name, err = who_am_i(s)
        if uid:
            out.append((pkg, s, uid, name))
        else:
            say("%s: คุกกี้ใช้ไม่ได้ (%s)" % (pkg, err), "red")
    return out


def pick_accounts(accts):
    v = ask("เลือกบัญชี (เช่น 1,3 หรือ all, Enter=all):").strip().lower()
    if v in ("", "all", "a"):
        return list(accts)
    idx = {int(x) for x in v.replace(" ", "").split(",") if x.isdigit()}
    return [a for n, a in enumerate(accts, 1) if n in idx]


def run_social(action, accts, tid):
    """Every selected account does `action` on user tid. -> [(pkg, name, ok, message)]"""
    rows = []
    for pkg, s, uid, name in accts:
        if uid == tid:
            rows.append((pkg, name, False, "เป็นบัญชีเดียวกับเป้าหมาย ข้าม"))
            continue
        ok, msg = social_call(s, action, tid, uid)
        rows.append((pkg, name, ok, msg))
        time.sleep(1.5)     # stay under the rate limit
    return rows


def show_social(rows):
    t = Table(box=box.ROUNDED, header_style="bold cyan")
    for col in ("แพ็กเกจ", "บัญชี", "ผล"):
        t.add_column(col)
    for pkg, name, ok, msg in rows:
        t.add_row(pkg, hide_name(name), Text(("✓ " if ok else "✗ ") + msg, style="green" if ok else "red"))
    console.print(t)


def pick_target(accts):
    """Target of block/unblock/friend: one of the cookie accounts, or a typed username / ID (both kept). -> (id, name)"""
    src = ask("เลือกเป้าหมายจาก: 1=บัญชีในคุกกี้, 2=พิมพ์ชื่อ/ID Roblox (Enter=1):").strip()
    if src in ("", "1"):
        v = ask("เลือกเลขบัญชีเป้าหมาย (ตามตารางด้านบน):").strip()
        if v.isdigit() and 1 <= int(v) <= len(accts):
            a = accts[int(v) - 1]
            return a[2], a[3]
        return None, None
    who = ask("ชื่อผู้ใช้หรือ User ID เป้าหมาย:")
    return resolve_user(accts[0][1], who) if who else (None, None)


def blocked_ids(s):
    """ชุด User ID ที่บัญชีนี้บล็อกไว้ หรือ None ถ้าอ่านไม่ได้"""
    out, cursor = set(), ""
    try:
        for _ in range(40):
            r = s.get("https://apis.roblox.com/user-blocking-api/v1/users/get-blocked-users",
                      params={"cursor": cursor, "count": 50}, timeout=15)
            if r.status_code != 200:
                return None
            d = (r.json() or {}).get("data") or {}
            out.update(int(x) for x in (d.get("blockedUserIds") or []))
            cursor = d.get("cursor") or ""
            if not cursor:
                return out
    except Exception:
        return None
    return out


def block_each_other(cfg, accts):
    """ทุกบัญชีในคุกกี้บล็อกกันเองทุกคู่ (ทั้งสองทิศ) ไม่ต้องเลือกเป้าหมาย"""
    seen = set()
    accts = [a for a in accts if not (a[2] in seen or seen.add(a[2]))]   # คุกกี้สองตัวของบัญชีเดียวกัน นับครั้งเดียว
    n = len(accts)
    if n < 2:
        say("ต้องมีบัญชีที่ใช้ได้อย่างน้อย 2 บัญชี", "red")
        return pause()
    try:
        gap = max(0, int(cfg.d.get("block_gap_sec", 20)))
    except (TypeError, ValueError):
        gap = 20
    est = max(1, int(round((n - 1) * max(gap, n * 1.0) / 60.0)))
    if not confirm("บล็อกกันเอง %d บัญชี (%d การบล็อก) ใช้เวลาประมาณ %d นาที?" % (n, n * (n - 1), est)):
        return
    say("กำลังอ่านรายการบล็อกของแต่ละบัญชี...", "dim")
    have = []
    for pkg, s, uid, name in accts:
        ensure_tracker(s, uid)
        have.append(blocked_ids(s))
    stats = [[0, 0, 0] for _ in accts]      # newly blocked, already blocked, failed
    fails, last = [], [0.0] * n
    try:
        for k in range(1, n):
            say("รอบ %d/%d" % (k, n - 1), "bold")
            for i in range(n):
                j = (i + k) % n
                pkg, s, uid, name = accts[i]
                tpkg, _, tuid, tname = accts[j]
                if uid == tuid:
                    continue
                if have[i] is not None and tuid in have[i]:
                    stats[i][1] += 1
                    continue
                for attempt in range(3):
                    wait = last[i] + gap - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    ok, msg = social_call(s, "block", tuid, uid)
                    last[i] = time.time()
                    if ok or not msg.startswith("HTTP 429") or attempt == 2:
                        break
                    say("%s: ส่งถี่เกิน (429) รอ 60 วินาทีแล้วลองใหม่" % pkg, "yellow")
                    time.sleep(60)
                say("  %s → %s: %s" % (pkg, hide_name(tname), ("✓ " if ok else "✗ ") + msg), "green" if ok else "red")
                if ok:
                    stats[i][0] += 1
                else:
                    stats[i][2] += 1
                    fails.append((pkg, tname, msg))
                time.sleep(1)
    except KeyboardInterrupt:
        say("หยุดแล้ว (Ctrl+C) สรุปเท่าที่ทำไปแล้ว", "yellow")
    t = Table(box=box.ROUNDED, header_style="bold cyan")
    for col in ("แพ็กเกจ", "บัญชี", "ผล"):
        t.add_column(col)
    for (pkg, _, _, name), (a, b, c) in zip(accts, stats):
        t.add_row(pkg, hide_name(name), Text("บล็อกใหม่ %d | บล็อกอยู่แล้ว %d | ล้ม %d" % (a, b, c),
                                             style="green" if c == 0 else "red"))
    console.print(t)
    for pkg, tname, msg in fails[:8]:
        say("%s → %s: %s" % (pkg, hide_name(tname), msg), "red")
    if len(fails) > 8:
        say("...และอีก %d รายการ" % (len(fails) - 8), "red")
    pause()


def unblock_each_other(cfg, accts):
    """ทุกบัญชีในคุกกี้ปลดบล็อกกันเอง (เฉพาะคู่ที่บล็อกกันอยู่จริง) ไม่แตะรายการบล็อกอื่น"""
    seen = set()
    accts = [a for a in accts if not (a[2] in seen or seen.add(a[2]))]   # คุกกี้สองตัวของบัญชีเดียวกัน นับครั้งเดียว
    n = len(accts)
    if n < 2:
        say("ต้องมีบัญชีที่ใช้ได้อย่างน้อย 2 บัญชี", "red")
        return pause()
    try:
        gap = max(0, int(cfg.d.get("block_gap_sec", 20)))
    except (TypeError, ValueError):
        gap = 20
    say("กำลังอ่านรายการบล็อกของแต่ละบัญชี...", "dim")
    have = []
    for pkg, s, uid, name in accts:
        ensure_tracker(s, uid)
        have.append(blocked_ids(s))

    def todo(i, j):
        return accts[i][2] != accts[j][2] and (have[i] is None or accts[j][2] in have[i])

    per = [sum(1 for j in range(n) if todo(i, j)) for i in range(n)]
    total = sum(per)
    if not total:
        say("ไม่มีคู่ไหนบล็อกกันอยู่ ไม่ต้องทำอะไร", "green")
        return pause()
    est = max(1, int(round(max(per) * gap / 60.0)))
    if not confirm("ปลดบล็อกกันเอง %d คู่ (ทิศทาง) ใช้เวลาประมาณ %d นาที?" % (total, est)):
        return
    stats = [[0, n - 1 - per[i], 0] for i in range(n)]      # unblocked, not blocked (skipped), failed
    fails, last = [], [0.0] * n
    try:
        for k in range(1, n):
            if not any(todo(i, (i + k) % n) for i in range(n)):
                continue
            say("รอบ %d/%d" % (k, n - 1), "bold")
            for i in range(n):
                j = (i + k) % n
                if not todo(i, j):
                    continue
                pkg, s, uid, name = accts[i]
                tpkg, _, tuid, tname = accts[j]
                for attempt in range(3):
                    wait = last[i] + gap - time.time()
                    if wait > 0:
                        time.sleep(wait)
                    ok, msg = social_call(s, "unblock", tuid, uid)
                    last[i] = time.time()
                    if ok or not msg.startswith("HTTP 429") or attempt == 2:
                        break
                    say("%s: ส่งถี่เกิน (429) รอ 60 วินาทีแล้วลองใหม่" % pkg, "yellow")
                    time.sleep(60)
                say("  %s → %s: %s" % (pkg, hide_name(tname), ("✓ " if ok else "✗ ") + msg), "green" if ok else "red")
                if ok:
                    stats[i][0] += 1
                else:
                    stats[i][2] += 1
                    fails.append((pkg, tname, msg))
                time.sleep(1)
    except KeyboardInterrupt:
        say("หยุดแล้ว (Ctrl+C) สรุปเท่าที่ทำไปแล้ว", "yellow")
    t = Table(box=box.ROUNDED, header_style="bold cyan")
    for col in ("แพ็กเกจ", "บัญชี", "ผล"):
        t.add_column(col)
    for (pkg, _, _, name), (a, b, c) in zip(accts, stats):
        t.add_row(pkg, hide_name(name), Text("ปลดบล็อก %d | ไม่ได้บล็อกอยู่ %d | ล้ม %d" % (a, b, c),
                                             style="green" if c == 0 else "red"))
    console.print(t)
    for pkg, tname, msg in fails[:8]:
        say("%s → %s: %s" % (pkg, hide_name(tname), msg), "red")
    if len(fails) > 8:
        say("...และอีก %d รายการ" % (len(fails) - 8), "red")
    pause()


def launch_all_roblox(cfg):
    """เปิด Roblox ทุกอินสแตนซ์ที่เลือกทีละตัว หน่วง open_all_delay วินาที (ค่าเริ่มต้น 10) ระหว่างตัว"""
    pkgs = selected_packages(cfg)
    if not pkgs:
        say("ไม่พบแพ็กเกจ Roblox ไปที่เมนู 4 เพื่อสแกนก่อน", "red")
        return pause()
    try:
        delay = max(0, int(cfg.d.get("open_all_delay", 10)))
    except (TypeError, ValueError):
        delay = 10
    parsed = parse_link(cfg.d["game_link"])
    uri = parsed[0] if parsed else None
    say("จะเปิดเกมตามลิงก์ที่ตั้งไว้" if uri else
        "ยังไม่ได้ตั้งลิงก์เกม จะเปิดแค่แอป Roblox", "dim")
    if not confirm("เปิด Roblox %d อินสแตนซ์ ห่างกันตัวละ %d วินาที?" % (len(pkgs), delay)):
        return
    done = 0
    try:
        for i, pkg in enumerate(pkgs, 1):
            if uri:
                ok, msg = open_game(pkg, uri, False)
            else:
                rc, out = sh("monkey -p %s -c android.intent.category.LAUNCHER 1" % shlex.quote(pkg),
                             root=ENV.root, timeout=30)
                ok, msg = rc == 0, (scrub(out.strip()[:120]) or "ok")
            done += 1 if ok else 0
            say("[%d/%d] %s: %s" % (i, len(pkgs), pkg, msg), "green" if ok else "red")
            if i < len(pkgs) and delay:
                say("รอ %d วินาทีก่อนเปิดตัวถัดไป..." % delay, "dim")
                time.sleep(delay)
    except KeyboardInterrupt:
        say("หยุดแล้ว (Ctrl+C)", "yellow")
    say("เปิดสำเร็จ %d/%d อินสแตนซ์" % (done, len(pkgs)), "green" if done == len(pkgs) else "yellow")
    pause()


def menu_open_all(cfg):
    while True:
        banner(cfg)
        t = Table(title="เปิด Roblox ทั้งหมด", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            t.add_column(col)
        t.add_row("1", "เปิด Roblox ทุกอินสแตนซ์ที่เลือกตอนนี้ (ทีละตัว ไม่ force-stop)", "")
        t.add_row("2", "หน่วงระหว่างตัว (วินาที, 0-600)", str(cfg.d.get("open_all_delay", 10)))
        console.print(t)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            launch_all_roblox(cfg)
        elif c == "2":
            v = ask("ค่าหน่วงใหม่ (0-600):")
            if v.isdigit() and 0 <= int(v) <= 600:
                cfg.d["open_all_delay"] = int(v)
                cfg.save()
            elif v:
                say("ค่าต้องอยู่ระหว่าง 0-600", "red")
                pause()


def menu_social(cfg):
    if not cookie_pairs(cfg)[0]:
        say("ยังไม่มีคุกกี้ใน cookie.txt (เมนู 5)", "red")
        return pause()
    say("กำลังตรวจบัญชีจากคุกกี้...", "dim")
    accts = social_accounts(cfg)
    if not accts:
        return pause()
    while True:
        banner(cfg)
        t = Table(title="บล็อก / เพื่อน (ด้วยบัญชีจากคุกกี้)", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ"):
            t.add_column(col)
        t.add_row("1", "บล็อกผู้ใช้")
        t.add_row("2", "เลิกบล็อกผู้ใช้")
        t.add_row("3", "เพิ่มเพื่อน (ส่งคำขอไปยังผู้ใช้)")
        t.add_row("4", "เพิ่มเพื่อนระหว่างบัญชีของเราเอง (ส่งคำขอ + กดรับให้อัตโนมัติ)")
        t.add_row("5", "บล็อกกันเองทั้งหมด (ทุกบัญชีในคุกกี้บล็อกกันเอง ไม่ต้องเลือกเป้าหมาย)")
        t.add_row("6", "ปลดบล็อกกันเองทั้งหมด (ทุกบัญชีในคุกกี้ปลดบล็อกกันเอง เฉพาะคู่ที่บล็อกกันอยู่)")
        console.print(t)
        a = Table(box=box.SIMPLE, header_style="bold cyan")
        for col in ("#", "แพ็กเกจ", "บัญชี"):
            a.add_column(col)
        for n, (pkg, _, _, name) in enumerate(accts, 1):
            a.add_row(str(n), pkg, hide_name(name))
        console.print(a)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c in ("1", "2", "3"):
            action = {"1": "block", "2": "unblock", "3": "friend"}[c]
            tid, tname = pick_target(accts)
            if not tid:
                say("ไม่พบเป้าหมายนี้", "red")
                pause()
                continue
            sel = pick_accounts(accts)
            if sel and confirm("%s %s (%d) ด้วย %d บัญชี?" % (SOCIAL_NAME[action], hide_name(tname), tid, len(sel))):
                show_social(run_social(action, sel, tid))
                pause()
        elif c == "6":
            unblock_each_other(cfg, accts)
        elif c == "5":
            block_each_other(cfg, accts)
        elif c == "4":
            sel = pick_accounts(accts)
            if len(sel) < 2:
                say("ต้องเลือกอย่างน้อย 2 บัญชี", "red")
                pause()
                continue
            if not confirm("เพิ่มเพื่อนระหว่าง %d บัญชี?" % len(sel)):
                continue
            rows = []
            for i in range(len(sel)):
                for j in range(i + 1, len(sel)):
                    (p1, s1, u1, n1), (p2, s2, u2, n2) = sel[i], sel[j]
                    ok, msg = api_post(s1, SOCIAL_URL["friend"] % u2)
                    time.sleep(1.5)
                    if ok:
                        ok, msg = api_post(s2, SOCIAL_URL["accept"] % u1, {})
                        time.sleep(1.5)
                    rows.append((p1 + " ↔ " + p2, hide_name(n1) + " ↔ " + hide_name(n2), ok, msg))
            show_social(rows)
            pause()


def menu_cookie_pull(cfg):
    if not ENV.root:
        say("การดึงคุกกี้จากแอปต้องใช้สิทธิ์รูท", "yellow")
        return pause()
    det = cfg.d["detected"]
    if not det:
        say("ยังไม่พบแพ็กเกจ ลองเมนู 4 (สแกนแพ็กเกจ)", "red")
        return pause()
    say("กำลังอ่านคุกกี้จากแอป %d อินสแตนซ์ แล้วตรวจกับ Roblox API..." % len(det), "dim")
    results = pull_validated(det)
    t = Table(title="คุกกี้ที่ดึงจากแอป Roblox", box=box.ROUNDED, header_style="bold cyan")
    for col in ("#", "แพ็กเกจ", "บัญชี", "ผล"):
        t.add_column(col)
    found = {}
    for i, (pkg, ck, name, msg) in enumerate(results, 1):
        if ck:
            found[pkg] = ck
        t.add_row(str(i), pkg, hide_name(name) if name else "-",
                  Text(("ดึงได้ ใช้ได้: " + mask(ck)) if ck else msg, style="green" if ck else "red"))
    console.print(t)
    if found and confirm("บันทึก %d ตัวลง cookie.txt (แทนที่บรรทัดของอินสแตนซ์นั้น)?" % len(found)):
        n = store_cookies(cfg, found)
        say("บันทึกแล้ว %d บรรทัด → " % n + COOKIE_PATH, "green")
        say("ตอนรีจอยจะใช้คุกกี้เหล่านี้เช็คสถานะในเกมผ่าน API (เปิด \"ตรวจสถานะในเกมผ่าน Roblox API\" ที่เมนู 6)", "dim")
    pause()


def menu_cookie_hub(cfg):
    while True:
        banner(cfg)
        t = Table(title="คุกกี้", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ"):
            t.add_column(col)
        t.add_row("1", "ตรวจ / ใส่คุกกี้จาก cookie.txt ลงแอป (ล็อกอินด้วยคุกกี้)")
        t.add_row("2", "ดึงคุกกี้จากแอป Roblox มาเก็บใน cookie.txt (ไว้เช็คสถานะผ่าน API) — รูท")
        console.print(t)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            menu_cookie(cfg)
        elif c == "2":
            menu_cookie_pull(cfg)


AUTO_ITEMS = [
    ("start_on_launch", "เริ่มรีจอยทันทีเมื่อเปิดเครื่องมือ", False),
    ("wake_lock", "เปิด Wake-lock ขณะรัน", False),
    ("force_stop", "Force-stop ก่อนเปิดเกมใหม่", True),
    ("foreground_check", "ตรวจว่าแอปอยู่หน้าจอ", True),
    ("screenshot_on_crash", "แคปหน้าจอเมื่อหลุด/ค้าง", True),
    ("cookie_login_on_start", "ล็อกอินด้วยคุกกี้อัตโนมัติก่อนเริ่ม", True),
    ("presence_check", "ตรวจสถานะในเกมผ่าน Roblox API (ต้องมีคุกกี้)", False),
    ("cookie_pull_on_start", "ดึงคุกกี้จากแอปมาเก็บอัตโนมัติก่อนเริ่ม (เฉพาะอินสแตนซ์ที่ยังไม่มีคุกกี้)", True),
]


def menu_auto(cfg):
    a = cfg.d["auto"]
    while True:
        banner(cfg)
        t = Table(title="ตั้งค่าอัตโนมัติ", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า", "หมายเหตุ"):
            t.add_column(col)
        for i, (k, label, root_only) in enumerate(AUTO_ITEMS, 1):
            t.add_row(str(i), label, onoff(a[k]), "เฉพาะรูท" if root_only else "")
        n = len(AUTO_ITEMS)
        t.add_row(str(n + 1), "รีจอยตามเวลา (นาที, 0=ปิด)", str(a["timed_rejoin_min"]), "")
        t.add_row(str(n + 2), "เวลารอเข้าเกมหลังเปิดแอป (วินาที)", str(cfg.d["join_grace"]), "")
        t.add_row(str(n + 3), "Heartbeat (สคริปต์ในเกม) ...", onoff(cfg.d["heartbeat"]["enabled"]), "เมนูย่อย")
        t.add_row(str(n + 4), "ประสิทธิภาพ: ล้างแรม / ลดการใช้ CPU ...", "", "เมนูย่อย")
        console.print(t)
        c = ask("เลือกหมายเลขเพื่อสลับ/แก้ไข (0=กลับ)")
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
        elif c in (str(n + 1), str(n + 2)):
            v = ask("ค่าใหม่ (ตัวเลข):")
            if v.isdigit():
                if c == str(n + 1):
                    a["timed_rejoin_min"] = int(v)
                else:
                    cfg.d["join_grace"] = max(30, int(v))
                cfg.save()


def fps_cap_summary(cfg):
    default, caps = fps_caps(cfg)
    if not default and not caps:
        return "ไม่ตั้ง"
    return "%d บัญชี, ค่าเริ่มต้น %s" % (len(caps), default or "ไม่แตะ")


def read_cap(prompt, allow_zero=False):
    """-> (ok, value). Enter = clear (None)."""
    v = ask(prompt).strip()
    if not v:
        return True, None
    if v.isdigit() and (FPS_CAP_MIN <= int(v) <= FPS_CAP_MAX or (allow_zero and int(v) == 0)):
        return True, int(v)
    say("ค่าต้องอยู่ระหว่าง %d-%d" % (FPS_CAP_MIN, FPS_CAP_MAX), "red")
    pause()
    return False, None


def menu_fps_cap(cfg):
    """Per-account FPS cap. The account is identified by its Roblox user ID (fetched from the cookie),
    and the generated heartbeat.lua looks the ID up in-game, so it follows the account, not the clone."""
    fc = cfg.d["heartbeat"].setdefault("fps_cap", {})
    fc.setdefault("default", 0)
    if not isinstance(fc.get("accounts"), dict):
        fc["accounts"] = {}
    if not cookie_pairs(cfg)[0]:
        say("ยังไม่มีคุกกี้ใน cookie.txt (เมนู 5)", "red")
        return pause()
    say("กำลังดึง Roblox ID จากคุกกี้...", "dim")
    accts = social_accounts(cfg)
    if not accts:
        return pause()

    def apply():
        cfg.save()
        if cfg.d["autoexec"]["auto_create"]:
            report_autoexec(cfg, install_autoexec(cfg), quiet_ok=False)
        say("สคริปต์ใหม่จะมีผลตอนเข้าเกมครั้งถัดไป (รีจอยหรือเปิดเกมใหม่) ต้องมี setfpscap ในเอ็กซิคิวเตอร์", "dim")
        pause()

    while True:
        banner(cfg)
        t = Table(title="FPS cap ต่อบัญชี (จับคู่ด้วย Roblox ID)", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "แพ็กเกจ", "บัญชี", "Roblox ID", "FPS cap"):
            t.add_column(col)
        for n, (pkg, _, uid, name) in enumerate(accts, 1):
            rec = fc["accounts"].get(str(uid))
            cap = rec.get("cap") if isinstance(rec, dict) else rec
            t.add_row(str(n), pkg, hide_name(name), hide_id(uid), str(cap) if cap else "ไม่ตั้ง")
        console.print(t)
        n = len(accts)
        a = Table(box=box.SIMPLE, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            a.add_column(col)
        a.add_row(str(n + 1), "ตั้งเท่ากันทุกบัญชีในตาราง", "")
        a.add_row(str(n + 2), "ค่าเริ่มต้นของบัญชีที่ไม่อยู่ในตาราง (0=ไม่แตะ)", str(fc["default"] or "ไม่แตะ"))
        a.add_row(str(n + 3), "ล้างค่าทั้งหมด", "")
        console.print(a)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c.isdigit() and 1 <= int(c) <= n:
            pkg, _, uid, name = accts[int(c) - 1]
            ok, cap = read_cap("FPS cap ของ %s (%d-%d, Enter=ล้าง):" % (hide_name(name), FPS_CAP_MIN, FPS_CAP_MAX))
            if not ok:
                continue
            if cap:
                fc["accounts"][str(uid)] = {"name": name, "cap": cap}
            else:
                fc["accounts"].pop(str(uid), None)
            apply()
        elif c == str(n + 1):
            ok, cap = read_cap("FPS cap สำหรับทุกบัญชี (%d-%d, Enter=ล้าง):" % (FPS_CAP_MIN, FPS_CAP_MAX))
            if not ok:
                continue
            for _, _, uid, name in accts:
                if cap:
                    fc["accounts"][str(uid)] = {"name": name, "cap": cap}
                else:
                    fc["accounts"].pop(str(uid), None)
            apply()
        elif c == str(n + 2):
            ok, cap = read_cap("ค่าเริ่มต้น (%d-%d, 0 หรือ Enter=ไม่แตะ):" % (FPS_CAP_MIN, FPS_CAP_MAX), allow_zero=True)
            if not ok:
                continue
            fc["default"] = cap or 0
            apply()
        elif c == str(n + 3):
            if confirm("ล้าง FPS cap ทั้งหมด?"):
                fc["accounts"].clear()
                fc["default"] = 0
                apply()


def menu_heartbeat(cfg):
    h = cfg.d["heartbeat"]
    while True:
        banner(cfg)
        t = Table(title="Heartbeat (สคริปต์ในเกม ส่งสัญญาณมาที่เครื่องมือ)", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            t.add_column(col)
        t.add_row("1", "เปิดใช้ heartbeat (เปิดเมื่อติดตั้งสคริปต์ในทุกโคลนแล้ว)", onoff(h["enabled"]))
        t.add_row("2", "พอร์ตรับสัญญาณ (127.0.0.1)", str(h["port"]))
        t.add_row("3", "สคริปต์ส่งทุก (วินาที)", str(h["interval"]))
        t.add_row("4", "ถือว่าหยุดส่งเมื่อเงียบเกิน (วินาที)", str(h["timeout"]))
        t.add_row("5", "สร้างไฟล์สคริปต์ .lua ให้แต่ละอินสแตนซ์ (โฟลเดอร์ heartbeat/)", "")
        t.add_row("6", "โฟลเดอร์ autoexec ของ Delta (ใส่ {pkg} = แยกโฟลเดอร์ต่อโคลน)", T(", ".join(cfg.d["autoexec"]["dirs"])))
        t.add_row("7", "สร้าง/อัปเดต heartbeat.lua ในโฟลเดอร์นั้นอัตโนมัติ", onoff(cfg.d["autoexec"]["auto_create"]))
        t.add_row("8", "สร้าง/อัปเดต heartbeat.lua ตอนนี้", "")
        t.add_row("9", "แสดงตัวเลข FPS บนจอเกม (ลากได้ ไม่มีกรอบ ปรับขนาดตามจอ)", onoff(h["show_fps"]))
        t.add_row("10", "FPS cap ต่อบัญชี (ดึง Roblox ID จากคุกกี้) ...", T(fps_cap_summary(cfg)))
        console.print(t)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            h["enabled"] = not h["enabled"]
        elif c in ("2", "3", "4"):
            key, lo, hi = {"2": ("port", 1024, 65535), "3": ("interval", 5, 120), "4": ("timeout", 30, 600)}[c]
            v = ask("ค่าใหม่ (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                h[key] = int(v)
                if cfg.d["autoexec"]["auto_create"]:
                    report_autoexec(cfg, install_autoexec(cfg))   # keep the installed script in sync (port/interval)
            elif v:
                say("ค่าต้องอยู่ระหว่าง %d-%d" % (lo, hi), "red")
                pause()
        elif c == "5":
            for p in write_heartbeat_files(cfg):
                say("สร้างแล้ว: " + p, "green")
            say("คัดลอกไฟล์ของแต่ละโคลนไปไว้ในโฟลเดอร์ autoexec ของเอ็กซิคิวเตอร์ในโคลนนั้น (หรือรันตอนเข้าเกม)", "dim")
            pause()
        elif c == "6":
            v = ask("พาธโฟลเดอร์ autoexec (Enter=ไม่เปลี่ยน, คั่นหลายโฟลเดอร์ด้วยคอมมา):")
            if v:
                cfg.d["autoexec"]["dirs"] = [x.strip() for x in v.split(",") if x.strip().startswith("/")] or cfg.d["autoexec"]["dirs"]
        elif c == "7":
            cfg.d["autoexec"]["auto_create"] = not cfg.d["autoexec"]["auto_create"]
        elif c == "8":
            report_autoexec(cfg, install_autoexec(cfg), quiet_ok=False)
            pause()
        elif c == "9":
            h["show_fps"] = not h["show_fps"]
            if cfg.d["autoexec"]["auto_create"]:
                report_autoexec(cfg, install_autoexec(cfg), quiet_ok=False)
                say("สคริปต์ใหม่จะโหลดตอนเข้าเกมครั้งถัดไป (รีจอยหรือเปิดเกมใหม่)", "dim")
                pause()
        elif c == "10":
            menu_fps_cap(cfg)
        cfg.save()


def menu_perf(cfg):
    r, p = cfg.d["ram"], cfg.d["performance"]
    while True:
        banner(cfg)
        t = Table(title="ประสิทธิภาพ: ล้างแรม / ลดการใช้ CPU", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            t.add_column(col)
        t.add_row("1", "ล้างแรมอัตโนมัติ (เฉพาะรูท)", onoff(r["enabled"]))
        t.add_row("2", "ล้างแรมทุก (นาที, 0=ไม่ล้างตามเวลา)", str(r["interval_min"]))
        t.add_row("3", "ล้างเมื่อแรมว่างต่ำกว่า (MB, 0=ปิด)", str(r["min_free_mb"]))
        t.add_row("4", "ปิดแอปพื้นหลังที่ cached ด้วย (am kill-all) ⚠ อาจปิดโคลนที่อยู่เบื้องหลัง", onoff(r["kill_cached"]))
        t.add_row("5", "ปิดแอปอื่นที่ไม่เกี่ยวข้องแบบปลอดภัย (force-stop แอปที่ติดตั้งเอง เว้น Roblox/Termux/Delta และรายชื่อข้อ 6)", onoff(r["kill_others"]))
        t.add_row("6", "รายชื่อแอปที่ห้ามปิด (ส่วนของชื่อแพ็กเกจ)", ", ".join(r["protect"]))
        t.add_row("7", "โหมดประหยัด CPU (ลดความสำคัญโปรเซส, เก็บสถานะเกมไว้ 60 วินาที, รีเฟรชจอช้าลง)", onoff(p["low_cpu"]))
        t.add_row("8", "รีเฟรชแผงสถานะทุก (วินาที, ใช้เมื่อเปิดโหมดประหยัด CPU)", str(p["ui_refresh_sec"]))
        t.add_row("9", "ล้างแรมเดี๋ยวนี้", "")
        console.print(t)
        free = mem_available_mb()
        say("RAM ว่างตอนนี้: %s MB%s" % (free if free is not None else "?", "" if ENV.root else "  (โหมดไม่รูทล้างแรมไม่ได้)"), "dim")
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            r["enabled"] = not r["enabled"]
        elif c == "4":
            r["kill_cached"] = not r["kill_cached"]
        elif c == "5":
            r["kill_others"] = not r["kill_others"]
        elif c == "6":
            v = ask("รายชื่อที่ห้ามปิด คั่นด้วยคอมมา (Enter=คงเดิม):")
            if v:
                r["protect"] = [x.strip() for x in v.split(",") if x.strip()]
        elif c == "7":
            p["low_cpu"] = not p["low_cpu"]
        elif c in ("2", "3", "8"):
            sec, key, lo, hi = {"2": (r, "interval_min", 0, 1440), "3": (r, "min_free_mb", 0, 8192),
                                "8": (p, "ui_refresh_sec", 1, 10)}[c]
            v = ask("ค่าใหม่ (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                sec[key] = int(v)
            elif v:
                say("ค่าต้องอยู่ระหว่าง %d-%d" % (lo, hi), "red")
                pause()
        elif c == "9":
            ok, b4, af = drop_ram(r["kill_cached"], cfg)
            say(("ล้างแรมแล้ว: ว่าง %s → %s MB" % (b4, af)) if ok else ("ล้างแรมไม่สำเร็จ: " + RAM_LAST["detail"]),
                "green" if ok else "red")
            if ok:
                say("ผลแต่ละขั้น: " + RAM_LAST["detail"], "dim")
                if b4 is not None and af is not None and af - b4 < 100:
                    say("แรมว่างแทบไม่เพิ่ม: drop_caches ล้างได้เฉพาะแคช ไม่ใช่แรมที่แอปใช้อยู่", "yellow")
            elif ENV.root and not (r["kill_cached"] or r["kill_others"]):
                say("ลองเปิดข้อ 5 (ปิดแอปอื่นแบบปลอดภัย) เมื่อ drop_caches ถูกล็อก (มักเป็นตู้คลาวด์/คอนเทนเนอร์)", "yellow")
            pause()
        cfg.save()


def menu_webhook(cfg, hook):
    w = cfg.d["webhook"]
    names = [("start", "เริ่มทำงาน"), ("crash", "หลุด/ค้าง"), ("rejoin", "รีจอยแล้ว"), ("stop", "หยุดทำงาน"),
             ("status", "รายงานสถานะ+ภาพหน้าจอเป็นรอบ")]
    n_shot = 3 + len(names)
    n_per = n_shot + 1
    n_cu, n_ce, n_cf, n_ct, n_test = n_per + 1, n_per + 2, n_per + 3, n_per + 4, n_per + 5
    cw = cfg.d["console_webhook"]
    while True:
        banner(cfg)
        t = Table(title="Discord Webhook", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            t.add_column(col)
        t.add_row("1", "URL Webhook", T(scrub(w["url"]) or "(ยังไม่ตั้ง)"))
        t.add_row("2", "เปิดใช้งาน Webhook", onoff(w["enabled"]))
        for i, (k, label) in enumerate(names, 3):
            t.add_row(str(i), "แจ้งเตือน: " + label, onoff(w["events"][k]))
        t.add_row(str(n_shot), "แนบภาพหน้าจอ (รูทเท่านั้น, เก็บล่าสุด %d รูป)" % MAX_SHOTS, onoff(w["send_screenshot"]))
        t.add_row(str(n_per), "ส่งรายงานสถานะ+ภาพหน้าจอทุกกี่นาที (0=ปิด)", str(w["periodic_min"]))
        t.add_row(str(n_cu), "Console webhook: URL (ว่าง = ใช้ URL หลัก)", T(scrub(cw["url"]) or "(ใช้ URL หลัก)"))
        t.add_row(str(n_ce), "Console webhook: ส่งบันทึกคอนโซลเข้า Discord", onoff(cw["enabled"]))
        t.add_row(str(n_cf), "Console webhook: รวมส่งทุก (วินาที, 3-300)", str(cw["flush_sec"]))
        t.add_row(str(n_ct), "ทดสอบ Console webhook", "")
        t.add_row(str(n_test), "ส่งข้อความทดสอบ", "")
        console.print(t)
        c = ask("เลือกหมายเลข (0=กลับ)")
        if c == "0":
            return
        if c == "1":
            v = ask("วาง URL Webhook (Enter=ลบ):")
            if not v:
                w["url"], w["enabled"] = "", False
            elif HOOK_RE.match(v):
                w["url"] = v
            else:
                say("รูปแบบ URL ไม่ถูกต้อง", "red")
                pause()
        elif c == "2":
            w["enabled"] = not w["enabled"]
        elif c.isdigit() and 3 <= int(c) < n_shot:
            k = names[int(c) - 3][0]
            w["events"][k] = not w["events"][k]
        elif c == str(n_shot):
            w["send_screenshot"] = not w["send_screenshot"]
        elif c == str(n_per):
            v = ask("ทุกกี่นาที (0-1440):")
            if v.isdigit() and 0 <= int(v) <= 1440:
                w["periodic_min"] = int(v)
            elif v:
                say("ต้องเป็นตัวเลข 0-1440", "red")
                pause()
        elif c == str(n_cu):
            v = ask("วาง URL Console webhook (Enter=ใช้ URL หลัก):")
            if not v:
                cw["url"] = ""
            elif HOOK_RE.match(v):
                cw["url"] = v
            else:
                say("รูปแบบ URL ไม่ถูกต้อง", "red")
                pause()
        elif c == str(n_ce):
            cw["enabled"] = not cw["enabled"]
        elif c == str(n_cf):
            v = ask("ค่าใหม่ (3-300):")
            if v.isdigit() and 3 <= int(v) <= 300:
                cw["flush_sec"] = int(v)
            elif v:
                say("ต้องเป็นตัวเลข 3-300", "red")
                pause()
        elif c == str(n_ct):
            ch = ConsoleHook(cfg)
            if not ch.url():
                say("ยังไม่มี URL (ตั้ง URL หลักหรือ Console URL ก่อน)", "red")
            else:
                ok = ch._post("GARNT TOOL REJOIN: ทดสอบ console webhook ✅")
                say("ส่งสำเร็จ" if ok else "ส่งไม่สำเร็จ ตรวจสอบ URL", "green" if ok else "red")
            pause()
        elif c == str(n_test):
            ok = hook.send("test", "✅ ทดสอบ Webhook", "เชื่อมต่อสำเร็จ", [("โหมด", "ROOT" if ENV.root else "ไม่รูท")], sync=True)
            say("ส่งสำเร็จ" if ok else "ส่งไม่สำเร็จ ตรวจสอบ URL", "green" if ok else "red")
            pause()
        cfg.save()


def menu_list(cfg):
    """Menu 10: static list of detected instances (no live refresh, no network)."""
    det = cfg.d["detected"]
    if not det:
        say("ยังไม่พบแพ็กเกจ ลองเมนู 4 (สแกนแพ็กเกจ)", "red")
        return pause()
    pairs, n_ck = cookie_pairs(cfg)
    cmap = dict(pairs)
    sel = selected_packages(cfg)
    banner(cfg)
    t = Table(title="รายการอินสแตนซ์ (%d)" % len(det), box=box.ROUNDED, header_style="bold cyan")
    for col in ("#", "แพ็กเกจ", "เลือกรัน", "คุกกี้"):
        t.add_column(col)
    for i, pkg in enumerate(det, 1):
        t.add_row(str(i), pkg, onoff(pkg in sel), Text("มี", style="green") if pkg in cmap else Text("-", style="dim"))
    console.print(t)
    link = cfg.d["game_link"]
    say("เลือกรัน %d/%d | คุกกี้ในไฟล์ %d บรรทัด | ลิงก์เกม: %s" % (
        len(sel), len(det), n_ck, (link[:60] + "…" if len(link) > 60 else link) or "(ยังไม่ตั้ง)"), "dim")
    pause()


MENU = [("1", "เริ่มรีจอยอัตโนมัติ"), ("2", "แสดงอินสแตนซ์ / สถานะ"),
        ("3", "ตั้งค่า (ลิงก์เกม, ช่วงเวลาตรวจสอบ, หน่วงการเปิด)"), ("4", "สแกนแพ็กเกจ"),
        ("5", "คุกกี้ (ล็อกอิน / ดึงจากแอป Roblox)"), ("6", "ตั้งค่าอัตโนมัติ"), ("7", "Discord Webhook"), ("8", "บล็อก / เลิกบล็อก / เพิ่มเพื่อน (ด้วยคุกกี้)"),
        ("9", "เปิด Roblox ทั้งหมด (ตั้งหน่วงเวลาได้)"), ("10", "แสดงรายการอินสแตนซ์"), ("0", "ออก")]


def main():
    detect_root()
    cfg = Config()
    STREAMER["on"] = bool(cfg.d["ui"]["streamer_mode"])
    setup_logging()
    hook = Hook(cfg)
    apply_perf(cfg)
    log.info("เริ่มโปรแกรม โหมด=%s โฟลเดอร์=%s", "root" if ENV.root else "non-root", BASE)
    if not STORAGE_OK:
        say("เขียน /sdcard/Download ไม่ได้ (ยังไม่อนุญาตที่เก็บข้อมูล?) ใช้โฟลเดอร์สำรอง: " + BASE, "yellow")
    say("กำลังสแกนแพ็กเกจ...", "dim")
    refresh_packages(cfg)
    if cfg.d["autoexec"]["auto_create"]:
        report_autoexec(cfg, install_autoexec(cfg))
    if cfg.d["auto"]["start_on_launch"] and parse_link(cfg.d["game_link"]) and cfg.d["detected"]:
        say("เริ่มอัตโนมัติใน 5 วินาที (Ctrl+C เพื่อยกเลิก)", "yellow")
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
        c = ask("เลือกเมนู:")
        if c == "1":
            menu_start(cfg, hook)
        elif c == "2":
            menu_status(cfg)
        elif c == "3":
            menu_settings(cfg)
        elif c == "4":
            menu_scan(cfg)
        elif c == "5":
            menu_cookie_hub(cfg)
        elif c == "6":
            menu_auto(cfg)
        elif c == "7":
            menu_webhook(cfg, hook)
        elif c == "8":
            menu_social(cfg)
        elif c == "9":
            menu_open_all(cfg)
        elif c == "10":
            menu_list(cfg)
        elif c == "0":
            say("ลาก่อน!", "cyan")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nออกจากโปรแกรม", "cyan")
