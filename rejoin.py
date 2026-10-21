#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GARNT TOOL REJOIN - Roblox auto-rejoin for UG Phone + Termux.
For your own accounts and devices only."""
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
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

VERSION = "1.2"
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
    return "********  (%d ตัวอักษร)" % len(cookie)


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
    "heartbeat": {"enabled": False, "port": 8765, "interval": 15, "timeout": 90},
    "performance": {"low_cpu": True, "ui_refresh_sec": 2},
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


FG_CMD = "(dumpsys activity activities; dumpsys window) 2>/dev/null | grep -E 'ResumedActivity|mCurrentFocus|mFocusedApp'"
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


def drop_ram(kill_cached=False):
    """Root only. Drop page cache (and optionally kill cached background apps). -> (ok, before_mb, after_mb)"""
    if not ENV.root:
        return False, None, None
    before = mem_available_mb()
    cmd = "sync; echo 3 > /proc/sys/vm/drop_caches"
    if kill_cached:
        cmd += "; am kill-all"
    rc, _ = sh(cmd, root=True, timeout=40)
    time.sleep(1)
    return rc == 0, before, mem_available_mb()


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
        sh("am force-stop " + q(pkg), root=True, timeout=20)
        time.sleep(2)
    cmd = "am start -a android.intent.action.VIEW -d %s -p %s -f 0x10008000" % (q(uri), q(pkg))
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
            return None, None, "คุกกี้หมดอายุ/ไม่ถูกต้อง"
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
    sh("am force-stop " + q(pkg), root=True)
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
            log.warning("webhook ล้มเหลว: %s", scrub(e))
        return False


# ----------------------------------------------------------------- heartbeat (in-game script -> local receiver)
LUA_TEMPLATE = r"""-- GARNT TOOL REJOIN heartbeat (auto-generated)
-- ใช้กับบัญชีและเครื่องของตัวเองเท่านั้น ต้องรันผ่านเอ็กซิคิวเตอร์ที่รองรับ HTTP
local HOST     = "http://127.0.0.1:__PORT__"
local ID       = "__ID__"       -- ชื่อแพ็กเกจหรือเลขอินสแตนซ์ ต้องตรงกับในเครื่องมือ
local INTERVAL = __INTERVAL__   -- ส่งทุกกี่วินาที

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

local function hasErrorPrompt()
    local ok, msg = pcall(function() return GuiService:GetErrorMessage() end)
    return ok and type(msg) == "string" and msg ~= ""
end

local function beat()
    if os.clock() - lastFrame > 5 then return end   -- เกมค้าง: ไม่ส่ง
    if hasErrorPrompt() then return end              -- ขึ้นหน้าต่าง Disconnected: ไม่ส่ง
    local url = string.format("%s/hb?id=%s&user=%s&uid=%d&place=%d&job=%s", HOST,
        HttpService:UrlEncode(ID), HttpService:UrlEncode(lp.Name), lp.UserId, game.PlaceId,
        HttpService:UrlEncode(game.JobId))
    httpGet(url)
end

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


class Heartbeats:
    def __init__(self):
        self.lock = threading.Lock()
        self.d = {}

    def put(self, q):
        key = re.sub(r"[^\w.\-]", "", q.get("id", ""))[:64]
        if not key:
            return False
        rec = {"ts": time.time(), "user": re.sub(r"[^\w .\-]", "", q.get("user", ""))[:30],
               "uid": q.get("uid", "")[:20], "place": q.get("place", "")[:20], "job": q.get("job", "")[:64]}
        with self.lock:
            self.d[key] = rec
        return True

    def latest(self, inst):
        with self.lock:
            recs = [self.d[k] for k in (inst.pkg, str(inst.idx)) if k in self.d]
        return max(recs, key=lambda r: r["ts"]) if recs else None


class HBServer:
    """Tiny local-only HTTP receiver: GET /hb?id=..&user=..&uid=..&place=..&job=.. and GET /ping."""

    def __init__(self, store, port):
        self.store, self.port, self.httpd = store, int(port), None

    def start(self):
        store = self.store

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                code, body = 404, b"not found"
                if u.path == "/ping":
                    code, body = 200, b"pong"
                elif u.path == "/hb":
                    q = {k: v[0][:80] for k, v in parse_qs(u.query).items() if v}
                    code, body = (200, b"ok") if store.put(q) else (400, b"missing id")
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
        self.state, self.detail = "queued", ""
        self.fails = self.rejoins = 0
        self.launched_at = 0.0
        self.ok_since = None
        self.next_at = 0.0
        self.started_at = time.time()


REASON = {"dead": "แอปปิด/ค้าง (ไม่พบโปรเซส)", "background": "แอปไม่ได้อยู่หน้าจอ",
          "noheartbeat": "ไม่มี heartbeat จากสคริปต์ในเกม",
          "notingame": "ไม่ได้อยู่ในเกม", "timed": "รีจอยตามเวลาที่ตั้งไว้"}
STATE_TH = {"queued": ("รอคิวเปิด", "dim"), "launching": ("กำลังเปิดแอป", "cyan"),
            "joining": ("กำลังเข้าเกม", "yellow"), "ok": ("ปกติ", "bold green"),
            "unknown": ("ไม่ทราบสถานะ", "magenta"), "backoff": ("รอรีจอย", "bold red"),
            "stopped": ("หยุดแล้ว", "dim")}


def fmt_dur(sec):
    sec = int(max(0, sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


class Engine:
    MIN = 60        # seconds per "minute" (overridable for tests)
    HK_TICK = 30    # housekeeping resolution in seconds

    def __init__(self, cfg, hook, uri, instances, hb=None):
        self.cfg, self.hook, self.uri, self.insts, self.hb = cfg, hook, uri, instances, hb
        self.stop = threading.Event()
        self.gate = threading.Lock()
        self.last_launch = 0.0
        self.events = deque(maxlen=10)
        self.threads = []
        self.t0 = time.time()

    # -- helpers
    def event(self, msg, style=""):
        msg = scrub(msg)
        self.events.append((datetime.now().strftime("%H:%M:%S"), msg, style))
        log.info(msg)

    def interval(self):
        return max(10, int(self.cfg.d["check_interval"]))

    def start(self):
        self.event("เริ่มทำงาน %d อินสแตนซ์ (โหมด %s)" % (len(self.insts), "ROOT" if ENV.root else "ไม่รูท"), "green")
        self.hook.send("start", "▶️ เริ่มรีจอยอัตโนมัติ", "เริ่มจัดการ %d อินสแตนซ์" % len(self.insts),
                       [("โหมด", "ROOT" if ENV.root else "ไม่รูท"), ("ตรวจทุก", "%ds" % self.interval()),
                        ("แพ็กเกจ", ", ".join(i.pkg for i in self.insts))])
        SNAP["fg"] = bool(ENV.root and self.cfg.d["auto"]["foreground_check"])
        for target, args in [(self._worker, (i,)) for i in self.insts] + [(self._housekeeper, ())]:
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self):
        self.stop.set()
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
        ok, before, after = drop_ram(self.cfg.d["ram"]["kill_cached"])
        if ok:
            self.event("ล้างแรม (%s): ว่าง %s → %s MB" % (why, before, after), "cyan")
        else:
            self.event("ล้างแรมไม่สำเร็จ", "red")

    def report(self, minutes):
        w = self.cfg.d["webhook"]
        if not (w["enabled"] and w["url"] and w["events"].get("status")):
            return
        shot = take_shot("status") if (ENV.root and w["send_screenshot"]) else None
        mem = mem_available_mb()
        fields = [(i.label, "%s | รีจอย %d | ล้มเหลว %d%s" % (STATE_TH.get(i.state, (i.state,))[0], i.rejoins, i.fails,
                                                              (" | " + i.user) if i.user else "")) for i in self.insts][:22]
        fields.append(("RAM ว่าง", ("%d MB" % mem) if mem is not None else "-"))
        fields.append(("รันมาแล้ว", fmt_dur(time.time() - self.t0)))
        self.hook.send("status", "📸 รายงานสถานะ", "รายงานประจำทุก %d นาที" % minutes, fields, shot)

    # -- worker
    def _worker(self, inst):
        try:
            if not self._launch(inst, "start"):
                return
            while True:
                inst.next_at = self._next_tick()
                if self.stop.wait(max(0.1, inst.next_at - time.time())):
                    break
                try:
                    self._tick(inst)
                except Exception as e:
                    self.event("%s: ข้อผิดพลาด %s" % (inst.label, e), "red")
        except Exception as e:
            self.event("%s: worker หยุดเพราะ %s" % (inst.label, e), "red")
        inst.state = "stopped"

    def _launch(self, inst, kind):
        c = self.cfg.d
        inst.state, inst.detail = "queued", "รอคิวเปิดแอป"
        with self.gate:
            wait = self.last_launch + max(0, int(c["launch_delay"])) - time.time()
            if wait > 0 and self.stop.wait(wait):
                return False
            if self.stop.is_set():
                return False
            inst.state, inst.detail = "launching", ""
            ok, msg = open_game(inst.pkg, self.uri, c["auto"]["force_stop"])
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
        if rec["user"] and not inst.user:
            inst.user = rec["user"]
        who = (" [%s]" % rec["user"]) if rec["user"] else ""
        if age <= int(hc["timeout"]):
            return "ok", "heartbeat %ds ก่อน%s" % (age, who)
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
        if elapsed < c["join_grace"] and (status != "dead" or elapsed < 30):
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
            if self.stop.wait(delay):
                return
        self._launch(inst, "rejoin")

    # -- live panel
    def render(self):
        now = time.time()
        mem = mem_available_mb()
        head = Text.assemble(
            ("โหมด: ", "dim"), ("ROOT" if ENV.root else "ไม่รูท", "bold green" if ENV.root else "bold yellow"),
            ("  |  ตรวจทุก %ds" % self.interval(), ""), ("  |  หน่วงเปิด %ss" % self.cfg.d["launch_delay"], ""),
            ("  |  รันมา " + fmt_dur(now - self.t0), "bold"),
            (("  |  RAM ว่าง %d MB" % mem) if mem is not None else "", ""),
            ("\nกด Ctrl+C เพื่อหยุดและกลับเมนู", "dim"))
        t = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
        for col in ("#", "แพ็กเกจ", "บัญชี", "สถานะ", "ล้มเหลว", "รีจอย", "ถัดไป", "รายละเอียด"):
            t.add_column(col, overflow="fold")
        for i in self.insts:
            label, style = STATE_TH.get(i.state, (i.state, ""))
            nxt = "%ds" % max(0, int(i.next_at - now)) if i.next_at and i.state in ("ok", "unknown", "joining", "backoff") else "-"
            t.add_row(str(i.idx), i.pkg, i.user or "-", Text(label, style=style), str(i.fails),
                      str(i.rejoins), nxt, T(i.detail))
        ev = Text()
        for ts, msg, st in list(self.events):
            ev.append(ts + "  ", style="dim")
            ev.append(msg + "\n", style=st)
        return Group(Panel(head, title="GARNT TOOL REJOIN", border_style="cyan"), t,
                     Panel(ev, title="เหตุการณ์ล่าสุด", border_style="blue"))


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


def banner(cfg):
    console.clear()
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
        say("ยังไม่ได้ตั้งลิงก์เกม (หรือลิงก์ไม่ถูกต้อง) ไปที่เมนู 3 ก่อน", "red")
        return pause()
    if not pkgs:
        say("ไม่พบแพ็กเกจ Roblox ไปที่เมนู 4 เพื่อสแกนก่อน", "red")
        return pause()
    if cfg.d["auto"]["cookie_login_on_start"]:
        if ENV.root:
            pairs = [(p, c) for p, c in cookie_pairs(cfg)[0] if p in pkgs]
            for pkg, ok, msg in cookie_login(pairs):
                say("%s: %s" % (pkg, msg), "green" if ok else "red")
        else:
            say("ข้ามการล็อกอินด้วยคุกกี้: ต้องใช้รูท", "yellow")
    cmap = dict(cookie_pairs(cfg)[0])
    det = cfg.d["detected"]
    insts = [Inst(det.index(p) + 1, p, cmap.get(p)) for p in pkgs]
    hb = hbs = None
    hc = cfg.d["heartbeat"]
    if hc["enabled"]:
        hb = Heartbeats()
        hbs = HBServer(hb, hc["port"])
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
    eng = Engine(cfg, hook, parsed[0], insts, hb)
    eng.start()
    try:
        with Live(get_renderable=eng.render, console=console, refresh_per_second=rate):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    say("\nกำลังหยุด...", "yellow")
    eng.shutdown()
    if hbs:
        hbs.stop()
    say("หยุดการทำงานแล้ว", "green")
    pause()


def menu_status(cfg):
    det = cfg.d["detected"]
    if not det:
        say("ยังไม่พบแพ็กเกจ ลองเมนู 4 (สแกนแพ็กเกจ)", "red")
        return pause()
    pairs, n_ck = cookie_pairs(cfg)
    cmap = dict(pairs)
    fg = foreground_text() if ENV.root else ""
    t = Table(title="อินสแตนซ์ที่ตรวจพบ", box=box.ROUNDED, header_style="bold cyan")
    for col in ("#", "แพ็กเกจ", "เลือกรัน", "คุกกี้", "ทำงานอยู่", "อยู่หน้าจอ"):
        t.add_column(col)
    sel = selected_packages(cfg)
    for i, pkg in enumerate(det, 1):
        alive = proc_alive(pkg)
        run = Text("ใช่", style="green") if alive else (Text("ไม่", style="red") if ENV.root else Text("ไม่ทราบ", style="magenta"))
        front = Text("-") if not ENV.root else (Text("ใช่", style="green") if (pkg + "/") in fg else Text("ไม่", style="dim"))
        t.add_row(str(i), pkg, onoff(pkg in sel), Text("มี" if pkg in cmap else "-"), run, front)
    console.print(t)
    say("โหมด: %s | ตรวจทุก %ss | หน่วงเปิด %ss | คุกกี้ในไฟล์: %d บรรทัด" % (
        "ROOT" if ENV.root else "ไม่รูท", cfg.d["check_interval"], cfg.d["launch_delay"], n_ck), "dim")
    if not ENV.root:
        say("โหมดไม่รูทตรวจโปรเซส/หน้าจอไม่ได้ ใช้ข้อมูลสถานะเกมจากคุกกี้แทน", "yellow")
    pause()


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
    say("กำลังสแกนแพ็กเกจ %s* ..." % cfg.d["package_prefix"], "cyan")
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
        t.add_row(str(i), pkg, mask(ck), Text(("ใช้ได้: " + name) if uid else ("ใช้ไม่ได้: " + str(err)),
                                              style="green" if uid else "red"))
    console.print(t)
    if not ENV.root:
        say("โหมดไม่รูท: ตรวจคุกกี้ได้อย่างเดียว การใส่คุกกี้ลงแอปต้องใช้รูท", "yellow")
        return pause()
    if valid and confirm("ใส่คุกกี้ที่ใช้ได้ %d ตัวลงแอป (แอปจะถูก force-stop)?" % len(valid)):
        for pkg, ok, msg in cookie_login(valid):
            say("%s: %s" % (pkg, msg), "green" if ok else "red")
    pause()


AUTO_ITEMS = [
    ("start_on_launch", "เริ่มรีจอยทันทีเมื่อเปิดเครื่องมือ", False),
    ("wake_lock", "เปิด Wake-lock ขณะรัน", False),
    ("force_stop", "Force-stop ก่อนเปิดเกมใหม่", True),
    ("foreground_check", "ตรวจว่าแอปอยู่หน้าจอ", True),
    ("screenshot_on_crash", "แคปหน้าจอเมื่อหลุด/ค้าง", True),
    ("cookie_login_on_start", "ล็อกอินด้วยคุกกี้อัตโนมัติก่อนเริ่ม", True),
    ("presence_check", "ตรวจสถานะในเกมผ่าน Roblox API (ต้องมีคุกกี้)", False),
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
        t.add_row("5", "สร้างไฟล์สคริปต์ .lua ให้แต่ละอินสแตนซ์", "")
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
            elif v:
                say("ค่าต้องอยู่ระหว่าง %d-%d" % (lo, hi), "red")
                pause()
        elif c == "5":
            for p in write_heartbeat_files(cfg):
                say("สร้างแล้ว: " + p, "green")
            say("คัดลอกไฟล์ของแต่ละโคลนไปไว้ในโฟลเดอร์ autoexec ของเอ็กซิคิวเตอร์ในโคลนนั้น (หรือรันตอนเข้าเกม)", "dim")
            pause()
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
        t.add_row("5", "โหมดประหยัด CPU (ลดความสำคัญโปรเซส, เก็บสถานะเกมไว้ 60 วินาที, รีเฟรชจอช้าลง)", onoff(p["low_cpu"]))
        t.add_row("6", "รีเฟรชแผงสถานะทุก (วินาที, ใช้เมื่อเปิดโหมดประหยัด CPU)", str(p["ui_refresh_sec"]))
        t.add_row("7", "ล้างแรมเดี๋ยวนี้", "")
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
            p["low_cpu"] = not p["low_cpu"]
        elif c in ("2", "3", "6"):
            sec, key, lo, hi = {"2": (r, "interval_min", 0, 1440), "3": (r, "min_free_mb", 0, 8192),
                                "6": (p, "ui_refresh_sec", 1, 10)}[c]
            v = ask("ค่าใหม่ (%d-%d):" % (lo, hi))
            if v.isdigit() and lo <= int(v) <= hi:
                sec[key] = int(v)
            elif v:
                say("ค่าต้องอยู่ระหว่าง %d-%d" % (lo, hi), "red")
                pause()
        elif c == "7":
            ok, b4, af = drop_ram(r["kill_cached"])
            say(("ล้างแรมแล้ว: ว่าง %s → %s MB" % (b4, af)) if ok else "ล้างแรมไม่สำเร็จ (ต้องใช้รูท)", "green" if ok else "red")
            pause()
        cfg.save()


def menu_webhook(cfg, hook):
    w = cfg.d["webhook"]
    names = [("start", "เริ่มทำงาน"), ("crash", "หลุด/ค้าง"), ("rejoin", "รีจอยแล้ว"), ("stop", "หยุดทำงาน"),
             ("status", "รายงานสถานะ+ภาพหน้าจอเป็นรอบ")]
    n_shot = 3 + len(names)
    n_per, n_test = n_shot + 1, n_shot + 2
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
        elif c == str(n_test):
            ok = hook.send("test", "✅ ทดสอบ Webhook", "เชื่อมต่อสำเร็จ", [("โหมด", "ROOT" if ENV.root else "ไม่รูท")], sync=True)
            say("ส่งสำเร็จ" if ok else "ส่งไม่สำเร็จ ตรวจสอบ URL", "green" if ok else "red")
            pause()
        cfg.save()


MENU = [("1", "เริ่มรีจอยอัตโนมัติ"), ("2", "แสดงอินสแตนซ์ / สถานะ"),
        ("3", "ตั้งค่า (ลิงก์เกม, ช่วงเวลาตรวจสอบ, หน่วงการเปิด)"), ("4", "สแกนแพ็กเกจ"),
        ("5", "ล็อกอินด้วยคุกกี้ (ไม่บังคับ)"), ("6", "ตั้งค่าอัตโนมัติ"), ("7", "Discord Webhook"), ("0", "ออก")]


def main():
    detect_root()
    cfg = Config()
    setup_logging()
    hook = Hook(cfg)
    apply_perf(cfg)
    log.info("เริ่มโปรแกรม โหมด=%s โฟลเดอร์=%s", "root" if ENV.root else "non-root", BASE)
    if not STORAGE_OK:
        say("เขียน /sdcard/Download ไม่ได้ (ยังไม่อนุญาตที่เก็บข้อมูล?) ใช้โฟลเดอร์สำรอง: " + BASE, "yellow")
    say("กำลังสแกนแพ็กเกจ...", "dim")
    refresh_packages(cfg)
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
            menu_cookie(cfg)
        elif c == "6":
            menu_auto(cfg)
        elif c == "7":
            menu_webhook(cfg, hook)
        elif c == "0":
            say("ลาก่อน!", "cyan")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nออกจากโปรแกรม", "cyan")
