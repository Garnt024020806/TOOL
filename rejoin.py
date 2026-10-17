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
from logging.handlers import TimedRotatingFileHandler

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

VERSION = "1.0"
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
    "webhook": {
        "url": "",
        "enabled": False,
        "send_screenshot": True,
        "events": {"start": True, "crash": True, "rejoin": True, "stop": True},
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


def cached(key, ttl, fn):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    r = fn()
    _cache[key] = (time.time(), r)
    return r


def foreground_text():
    return cached("fg", 5, lambda: sh(
        "(dumpsys activity activities; dumpsys window) 2>/dev/null | "
        "grep -E 'ResumedActivity|mCurrentFocus|mFocusedApp'", root=True, timeout=25)[1])


def proc_alive(pkg):
    rc, out = sh("pidof " + shlex.quote(pkg), root=ENV.root, timeout=10)
    if rc == 127 or "not found" in out:
        _, out = sh("ps -A 2>/dev/null | grep -w %s | grep -v grep" % shlex.quote(pkg), root=ENV.root, timeout=10)
        return bool(out.strip())
    return bool(re.search(r"\b\d+\b", out))


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
                cookie = clean_cookie(l)
                if cookie:
                    res.append(cookie)
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
EV_COLOR = {"start": 0x2ECC71, "crash": 0xE74C3C, "rejoin": 0x3498DB, "stop": 0x95A5A6, "test": 0x9B59B6}


class Hook:
    def __init__(self, cfg):
        self.cfg = cfg

    def send(self, event, title, desc, fields=None, shot=None, sync=False):
        w = self.cfg.d["webhook"]
        if event != "test" and not (w["enabled"] and w["events"].get(event)):
            return True
        if not HOOK_RE.fullmatch(str(w.get("url", ""))):
            log.warning("Webhook URL ไม่ถูกต้อง")
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


# ----------------------------------------------------------------- engine
class Inst:
    def __init__(self, idx, pkg, cookie=None):
        self.idx, self.pkg, self.cookie = idx, pkg, cookie
        self.label = "#%d %s" % (idx, pkg)
        self.session = None
        self.uid = None
        self.user = ""
        self.cookie_bad = False
        self.state, self.detail = "queued", ""
        self.fails = self.rejoins = 0
        self.launched_at = 0.0
        self.ok_since = None
        self.next_at = 0.0
        self.started_at = time.time()


REASON = {"dead": "แอปปิด/ค้าง (ไม่พบโปรเซส)", "background": "แอปไม่ได้อยู่หน้าจอ",
          "notingame": "ไม่ได้อยู่ในเกม", "timed": "รีจอยตามเวลาที่ตั้งไว้"}
STATE_TH = {"queued": ("รอคิวเปิด", "dim"), "launching": ("กำลังเปิดแอป", "cyan"),
            "joining": ("กำลังเข้าเกม", "yellow"), "ok": ("ปกติ", "bold green"),
            "unknown": ("ไม่ทราบสถานะ", "magenta"), "backoff": ("รอรีจอย", "bold red"),
            "stopped": ("หยุดแล้ว", "dim")}


def fmt_dur(sec):
    sec = int(max(0, sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


class Engine:
    def __init__(self, cfg, hook, uri, instances):
        self.cfg, self.hook, self.uri, self.insts = cfg, hook, uri, instances
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
        for inst in self.insts:
            t = threading.Thread(target=self._worker, args=(inst,), daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
        wake_lock(False)
        up = fmt_dur(time.time() - self.t0)
        self.event("หยุดการทำงาน (รันมา %s)" % up)
        self.hook.send("stop", "⏹️ หยุดรีจอยอัตโนมัติ", "รันมาแล้ว " + up,
                       [(i.pkg, "รีจอย %d ครั้ง" % i.rejoins) for i in self.insts], sync=True)

    # -- worker
    def _worker(self, inst):
        try:
            if not self._launch(inst, "start"):
                return
            while True:
                inst.next_at = time.time() + self.interval()
                if self.stop.wait(self.interval()):
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
        return in_game(inst.session, inst.uid)

    def check(self, inst):
        alive = proc_alive(inst.pkg)
        if ENV.root and not alive:
            return "dead", REASON["dead"]
        if ENV.root and alive and self.cfg.d["auto"]["foreground_check"]:
            if (inst.pkg + "/") not in foreground_text():
                return "background", REASON["background"]
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
            base = min(3600, max(1, int(c["backoff_base"])))
            maximum = min(86400, max(base, int(c["backoff_max"])))
            delay = min(maximum, base * (1 << min(inst.fails - 1, 17)))
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
        head = Text.assemble(
            ("โหมด: ", "dim"), ("ROOT" if ENV.root else "ไม่รูท", "bold green" if ENV.root else "bold yellow"),
            ("  |  ตรวจทุก %ds" % self.interval(), ""), ("  |  หน่วงเปิด %ss" % self.cfg.d["launch_delay"], ""),
            ("  |  รันมา " + fmt_dur(now - self.t0), "bold"), ("\nกด Ctrl+C เพื่อหยุดและกลับเมนู", "dim"))
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
    insts = [Inst(i + 1, p, cmap.get(p)) for i, p in enumerate(pkgs)]
    if cfg.d["auto"]["wake_lock"]:
        wake_lock(True)
    eng = Engine(cfg, hook, parsed[0], insts)
    eng.start()
    try:
        with Live(get_renderable=eng.render, console=console, refresh_per_second=2):
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    say("\nกำลังหยุด...", "yellow")
    eng.shutdown()
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
        console.print(t)
        c = ask("เลือกหมายเลขเพื่อสลับ/แก้ไข (0=กลับ)")
        if c == "0":
            return
        if c.isdigit() and 1 <= int(c) <= n:
            k = AUTO_ITEMS[int(c) - 1][0]
            a[k] = not a[k]
            cfg.save()
        elif c in (str(n + 1), str(n + 2)):
            v = ask("ค่าใหม่ (ตัวเลข):")
            if v.isdigit():
                if c == str(n + 1):
                    a["timed_rejoin_min"] = int(v)
                else:
                    cfg.d["join_grace"] = max(30, int(v))
                cfg.save()


def menu_webhook(cfg, hook):
    w = cfg.d["webhook"]
    names = [("start", "เริ่มทำงาน"), ("crash", "หลุด/ค้าง"), ("rejoin", "รีจอยแล้ว"), ("stop", "หยุดทำงาน")]
    while True:
        banner(cfg)
        t = Table(title="Discord Webhook", box=box.ROUNDED, header_style="bold cyan")
        for col in ("#", "รายการ", "ค่า"):
            t.add_column(col)
        t.add_row("1", "URL Webhook", T(scrub(w["url"]) or "(ยังไม่ตั้ง)"))
        t.add_row("2", "เปิดใช้งาน Webhook", onoff(w["enabled"]))
        for i, (k, label) in enumerate(names, 3):
            t.add_row(str(i), "แจ้งเตือน: " + label, onoff(w["events"][k]))
        t.add_row("7", "แนบภาพหน้าจอ (รูทเท่านั้น, เก็บล่าสุด %d รูป)" % MAX_SHOTS, onoff(w["send_screenshot"]))
        t.add_row("8", "ส่งข้อความทดสอบ", "")
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
        elif c in ("3", "4", "5", "6"):
            k = names[int(c) - 3][0]
            w["events"][k] = not w["events"][k]
        elif c == "7":
            w["send_screenshot"] = not w["send_screenshot"]
        elif c == "8":
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
