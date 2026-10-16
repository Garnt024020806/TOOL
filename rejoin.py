#!/usr/bin/env python3
"""GARNT TOOL REJOIN - keeps cloned Roblox apps inside a game (Termux, UG Phone)."""
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

if os.environ.get("TERM", "dumb") in ("", "dumb", "unknown"):
    os.environ["TERM"] = "xterm-256color"
try:
    import requests
    from rich.console import Console
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    print("ไม่พบโมดูล rich/requests -> รัน: python -m pip install -r /storage/emulated/0/Download/RejoinTool/requirements.txt")
    sys.exit(1)

BASE = "/storage/emulated/0/Download/RejoinTool"
LOG_DIR = BASE + "/logs"
SHOT_DIR = BASE + "/screenshots"
CONFIG_FILE = BASE + "/config.json"
COOKIE_FILE = BASE + "/cookie.txt"
PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
ENV = dict(os.environ, PATH=os.environ.get("PATH", "") + ":" + PREFIX + "/bin:/system/bin:/system/xbin")
ENV.pop("LD_LIBRARY_PATH", None)

COOKIE_PREFIX = "_|WARNING:-DO-NOT-SHARE-THIS"
COOKIE_HEADER = (
    "# ใส่คุกกี้ 1 บรรทัดต่อ 1 บัญชี เรียงตามลำดับแพ็กเกจที่สแกนเจอ (เมนู 4)\n"
    "# บรรทัดว่างและบรรทัดที่ขึ้นต้นด้วย # จะถูกข้าม\n"
    "# ใช้กับบัญชีของตัวเองเท่านั้น และห้ามแชร์ไฟล์นี้ให้ใคร\n"
)
# Safety net: anything that looks like a secret is masked before it reaches a log or webhook.
SECRET_RE = re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS\S*|(?i:privateserverlinkcode|linkcode)=[A-Za-z0-9_-]+|/api/webhooks/\S+")
WEBHOOK_RE = re.compile(r"https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+")
TOKEN = r"[A-Za-z0-9_-]+"
BLIND_RELAUNCH = 600  # non-root fallback: relaunch period when processes are invisible

DEFAULTS = {
    "game_link": "",
    "check_interval": 30,
    "launch_delay": 15,
    "backoff_start": 10,
    "backoff_max": 300,
    "stable_reset": 300,
    "check_foreground": True,
    "auto_start": False,
    "webhook_url": "",
    "events": {"start": True, "crash": True, "rejoin": True, "stop": True},
}
EVENTS = {"start": "เริ่มทำงาน", "crash": "เกมหลุด/แอปค้าง", "rejoin": "เข้าเกมใหม่", "stop": "หยุดทำงาน"}
STATE_STYLE = {"RUNNING": "green", "STARTING": "cyan", "RESTARTING": "yellow",
               "CRASHED": "red", "DISCONNECTED": "red", "WAITING": "white", "ERROR": "red"}

console = Console()
HAS_ROOT = False
_log_lock = threading.Lock()


# ---------- basic helpers ----------
def say(msg="", style=""):
    console.print(msg, style=style, markup=False, highlight=False)


def scrub(s):
    return SECRET_RE.sub(lambda m: m.group(0)[:6] + "***", str(s))


def log(msg):
    now = datetime.now()
    try:
        with _log_lock, open(f"{LOG_DIR}/{now:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
            f.write(f"{now:%H:%M:%S} {scrub(msg)}\n")
    except OSError:
        pass


def prune_logs(days=7):
    cutoff = time.time() - days * 86400
    try:
        for n in os.listdir(LOG_DIR):
            p = f"{LOG_DIR}/{n}"
            if n.endswith(".log") and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def ask(prompt=""):
    try:
        return input(prompt).strip()
    except KeyboardInterrupt:
        print()
        return ""
    except EOFError:
        raise SystemExit(0)


def ask_int(label, cur, lo, hi):
    s = ask(f"{label} [{cur}] ({lo}-{hi}, Enter = ค่าเดิม): ")
    if not s:
        return cur
    if s.isdecimal() and lo <= int(s) <= hi:
        return int(s)
    say(f"✖ ค่าไม่ถูกต้อง ใช้ค่าเดิม ({cur})", "red")
    return cur


def run(cmd, timeout=15, as_root=False):
    """Run a shell command; wraps in `su -c` only when root is available and we are not uid 0."""
    argv = ["su", "-c", cmd] if (as_root and HAS_ROOT and os.getuid() != 0) else ["sh", "-c", cmd]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, env=ENV)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, str(e)


def sysbin(name):
    return "/system/bin/" + name if HAS_ROOT else name


def detect_root():
    if os.getuid() == 0:
        return True
    try:
        p = subprocess.run(["su", "-c", "id"], capture_output=True, text=True, timeout=5, env=ENV)
        if p.returncode == 0 and "uid=0" in p.stdout:
            return True
        say("⚠ su ปฏิเสธสิทธิ์ (permission denied) — กด 'อนุญาต' ในแอป Root แล้วรันใหม่", "yellow")
    except FileNotFoundError:
        say("⚠ ไม่พบคำสั่ง su — เครื่องอาจไม่มี Root", "yellow")
    except subprocess.TimeoutExpired:
        say("⚠ su ไม่ตอบสนองใน 5 วินาที (อาจรออนุญาต Root อยู่)", "yellow")
    except OSError as e:
        say(f"⚠ เรียก su ไม่ได้: {e.strerror}", "yellow")
    return False


def wake(lock=True):
    rc, _ = run(f"{PREFIX}/bin/termux-wake-{'lock' if lock else 'unlock'}", timeout=10)
    if rc != 0 and lock:
        say("⚠ เปิด wake-lock ไม่สำเร็จ (พบบ่อยเมื่อรันด้วย root) — ทำงานต่อได้ แต่ควรตั้ง Termux ไม่จำกัดแบตเตอรี่", "yellow")


# ---------- startup ----------
def startup():
    global HAS_ROOT
    try:
        for d in (LOG_DIR, SHOT_DIR):
            os.makedirs(d, exist_ok=True)
        with open(BASE + "/.w", "w") as f:
            f.write("ok")
        os.remove(BASE + "/.w")
    except OSError as e:
        say(f"✖ เข้าถึงที่เก็บข้อมูลไม่ได้ ({e.strerror})", "red")
        say("  รัน termux-setup-storage แล้วกด 'อนุญาต' จากนั้นรัน setup.sh ใหม่", "yellow")
        sys.exit(1)
    prune_logs()
    HAS_ROOT = detect_root()
    if HAS_ROOT:
        say("✔ พบสิทธิ์ Root — ใช้งานได้ครบทุกฟีเจอร์", "green")
    else:
        say("⚠ โหมด non-root — ปิดใช้งาน: ตรวจ foreground, force-stop ก่อนเปิดใหม่, screenshot", "yellow")
        say("  (webhook ส่งเป็นข้อความอย่างเดียว / ตรวจโปรเซสแบบ best effort)", "yellow")
    wake()
    say("เตือน: ตั้ง Termux เป็น 'ไม่จำกัด/ไม่เพิ่มประสิทธิภาพแบตเตอรี่' เพื่อไม่ให้ระบบปิดโปรเซส", "cyan")
    log(f"startup root={HAS_ROOT}")


# ---------- config ----------
def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        say(f"✖ บันทึก config.json ไม่ได้ ({e.strerror})", "red")


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except FileNotFoundError:
        save_config(cfg)
        return cfg
    except (ValueError, OSError):
        say("⚠ config.json เสียหาย — สำรองเป็น config.json.bad และรีเซ็ตเป็นค่าเริ่มต้น", "yellow")
        try:
            os.replace(CONFIG_FILE, CONFIG_FILE + ".bad")
        except OSError:
            pass
        save_config(cfg)
        return cfg
    for k, v in data.items():
        if k == "events" and isinstance(v, dict):
            cfg["events"].update({e: x for e, x in v.items() if e in cfg["events"] and isinstance(x, bool)})
        elif k in cfg and k != "events" and type(v) is type(cfg[k]):
            cfg[k] = v
    for k, lo in (("check_interval", 5), ("launch_delay", 0), ("backoff_start", 1), ("backoff_max", 1), ("stable_reset", 30)):
        cfg[k] = max(lo, cfg[k])
    cfg["backoff_max"] = max(cfg["backoff_max"], cfg["backoff_start"])
    return cfg


# ---------- game link -> URI ----------
def build_uri(link):
    """Place ID / roblox.com links -> roblox:// deep link. Returns None when unrecognised."""
    link = link.strip()
    if re.fullmatch(r"\d{4,20}", link):
        return f"roblox://experiences/start?placeId={link}"
    if re.fullmatch(r"roblox://[A-Za-z0-9_./?=&%:-]+", link):
        return link
    u = urlparse(link)
    if u.scheme in ("http", "https") and (u.hostname or "").endswith("roblox.com"):
        q = parse_qs(u.query)
        m = re.search(r"/games/(\d+)", u.path)
        if m:
            uri = f"roblox://experiences/start?placeId={m.group(1)}"
            code = (q.get("privateServerLinkCode") or [""])[0]
            return uri + (f"&linkCode={code}" if re.fullmatch(TOKEN, code) else "")
        code = (q.get("code") or [""])[0]
        if u.path.rstrip("/") == "/share" and q.get("type") == ["Server"] and re.fullmatch(TOKEN, code):
            return f"roblox://navigation/share_links?code={code}&type=Server"
    return None


# ---------- cookies (status + mapping preview only; values never leave this section unmasked) ----------
def mask(c):
    return c[:6] + "***"


def cookie_problem(c):
    if re.search(r"\s", c):
        return "มีช่องว่าง"
    if not c.startswith(COOKIE_PREFIX):
        return "รูปแบบไม่ถูกต้อง"
    if not 100 <= len(c) <= 4000:
        return "ความยาวผิดปกติ"
    return None


def ensure_cookie_file():
    if os.path.exists(COOKIE_FILE):
        return
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(COOKIE_HEADER)
    except OSError as e:
        say(f"⚠ สร้าง cookie.txt ไม่ได้ ({e.strerror})", "yellow")


def load_cookies():
    cookies, warns = [], []
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return [], [f"อ่าน cookie.txt ไม่ได้ ({e.strerror})"]
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        bad = cookie_problem(s)
        if bad:
            warns.append(f"บรรทัด {n}: ข้าม ({bad})")
        else:
            cookies.append(s)
    return cookies, warns


# ---------- packages / app control ----------
def scan():
    rc, out = run(sysbin("pm") + " list packages com.roblox", timeout=30, as_root=True)
    found = {l[8:].strip() for l in out.splitlines() if l.startswith("package:")}
    pkgs = sorted((p for p in found if re.fullmatch(r"com\.roblox\.[A-Za-z0-9_.]+", p)),
                  key=lambda p: (p != "com.roblox.client", p))
    if not pkgs:
        if rc != 0 and out:
            say("✖ สแกนไม่สำเร็จ: " + scrub(out.splitlines()[0])[:120], "red")
            say("  ตรวจสอบสิทธิ์ Root ของ Termux หรือคำสั่ง pm", "yellow")
        else:
            say("✖ ไม่พบแพ็กเกจ com.roblox.* — ตรวจสอบว่าติดตั้ง Roblox/แอปโคลนแล้ว", "red")
            if not HAS_ROOT:
                say("  (โหมด non-root อาจมองไม่เห็นแอปอื่นบน Android 11+)", "yellow")
    return pkgs


def is_alive(pkg):
    rc, out = run(sysbin("pidof") + " " + pkg + " 2>/dev/null", as_root=True)
    return rc == 0 and bool(re.fullmatch(r"[0-9 ]+", out))


def in_foreground(pkg):
    """Root only. Any resumed activity of the package counts (multi-window safe); unknown -> True."""
    rc, out = run(sysbin("dumpsys") + " activity activities 2>/dev/null | grep -i ResumedActivity", timeout=20, as_root=True)
    lines = out.splitlines()
    return not lines or any(" " + pkg + "/" in l for l in lines)


def launch_reason(text):
    t = text.lower()
    if "unable to resolve" in t or "does not exist" in t or "unknown package" in t:
        return "ไม่พบแพ็กเกจ/แอปนี้ไม่รับลิงก์เกม"
    if "permission" in t or "security" in t:
        return "สิทธิ์ไม่พอ (permission denied)"
    if "not found" in t or "no such file" in t:
        return "ไม่พบคำสั่ง am"
    if t == "timeout":
        return "คำสั่งเปิดแอปหมดเวลา"
    return "เปิดแอปไม่สำเร็จ"


def take_shot(pkg):
    path = f"{SHOT_DIR}/{datetime.now():%Y%m%d_%H%M%S}_{pkg.rsplit('.', 1)[-1]}.png"
    rc, _ = run(f"{sysbin('screencap')} -p {shlex.quote(path)}", timeout=20, as_root=True)
    if rc != 0 or not os.path.exists(path):
        log(f"screenshot ล้มเหลว {pkg}")
        return None
    try:
        files = sorted((f"{SHOT_DIR}/{n}" for n in os.listdir(SHOT_DIR) if n.endswith(".png")), key=os.path.getmtime)
        for old in files[:-20]:
            os.remove(old)
    except OSError:
        pass
    return path


# ---------- discord ----------
def mask_url(u):
    return u[:39] + "***"


def wants(cfg, ev):
    return bool(cfg["webhook_url"]) and cfg["events"].get(ev, False)


def send_webhook(cfg, text, shot=None):
    """Return None on success, otherwise a short Thai error. Never raises for network problems."""
    url = cfg["webhook_url"]
    if not url:
        return "ยังไม่ได้ตั้ง Webhook URL"
    payload = {"content": scrub(text)[:1900]}
    for _ in range(3):
        try:
            if shot and os.path.isfile(shot):
                with open(shot, "rb") as f:
                    r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                      files={"file": (os.path.basename(shot), f, "image/png")}, timeout=10)
            else:
                r = requests.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            return f"เชื่อมต่อไม่ได้ ({type(e).__name__}) — ตรวจสอบอินเทอร์เน็ต"
        except OSError:
            return "อ่านไฟล์ภาพไม่ได้"
        if r.status_code == 429:
            wait = 2.0
            try:
                wait = float(r.json()["retry_after"])
            except Exception:
                try:
                    wait = float(r.headers.get("Retry-After", 2))
                except ValueError:
                    pass
            time.sleep(min(wait, 15))
            continue
        return None if r.status_code < 400 else f"HTTP {r.status_code}"
    return "ถูกจำกัดอัตรา (429) — ลองใหม่ภายหลัง"


def notify(cfg, event, text, shot=None, block=False):
    if not wants(cfg, event):
        return

    def job():
        try:
            err = send_webhook(cfg, text, shot)
            if err:
                log(f"webhook {event}: {err}")
        except Exception as e:
            log(f"webhook {event}: {type(e).__name__}")

    t = threading.Thread(target=job, daemon=True)
    t.start()
    if block:
        t.join(40)


# ---------- monitor ----------
class Inst:
    def __init__(self, pkg, backoff):
        self.pkg, self.backoff = pkg, backoff
        self.state, self.event, self.ts = "WAITING", "รอคิวเปิด", ""
        self.restarts = 0
        self.since = None
        self.last_launch = 0.0
        self.seen_alive = False
        self.blind = False

    def set(self, state, event):
        self.state, self.event, self.ts = state, event, f"{datetime.now():%H:%M:%S}"
        log(f"{self.pkg} {state} - {event}")


def start_app(ins, uri):
    cmd = f"{sysbin('am')} start -a android.intent.action.VIEW -d {shlex.quote(uri)} -p {ins.pkg}"
    rc, out = run(cmd, timeout=30, as_root=True)
    ins.last_launch = time.time()
    errs = [l for l in out.splitlines() if "Error" in l or "Exception" in l or "not found" in l]
    if rc != 0 or errs:
        log(f"{ins.pkg} launch error: {scrub(errs[0] if errs else out)[:200]}")
        return False, launch_reason(errs[0] if errs else out)
    return True, ""


def relaunch(ins, cfg, uri, stop):
    delay = 0 if ins.blind else ins.backoff
    ins.set("RESTARTING", "เปิดซ้ำตามรอบ" if ins.blind else f"รอ {delay} วินาทีก่อนเปิดใหม่")
    if HAS_ROOT:
        run(f"{sysbin('am')} force-stop {ins.pkg}", as_root=True)
    if stop.wait(delay):
        return
    if not ins.blind:
        ins.backoff = min(ins.backoff * 2, cfg["backoff_max"])
    ins.set("STARTING", "กำลังเปิดเกมอีกครั้ง")
    ok, why = start_app(ins, uri)
    ins.restarts += 1
    if not ok:
        ins.set("CRASHED", why)
    elif ins.blind:
        ins.set("RUNNING", "เปิดซ้ำตามรอบ (non-root)")
        ins.since = time.time()
    shot = take_shot(ins.pkg) if (ok and HAS_ROOT and wants(cfg, "rejoin") and not stop.wait(5)) else None
    notify(cfg, "rejoin", f"{ins.pkg}: " + ("เข้าเกมใหม่" if ok else "เปิดไม่สำเร็จ - " + why) + f" (ครั้งที่ {ins.restarts})", shot)


def beat(ins, cfg, uri, stop):
    if ins.blind:
        if time.time() - ins.last_launch >= BLIND_RELAUNCH:
            relaunch(ins, cfg, uri, stop)
        return
    alive = is_alive(ins.pkg)
    fg = in_foreground(ins.pkg) if (alive and HAS_ROOT and cfg["check_foreground"]) else True
    ins.seen_alive = ins.seen_alive or alive
    if alive and fg:
        if ins.state != "RUNNING":
            ins.set("RUNNING", "ทำงานปกติ")
            ins.since = time.time()
        elif time.time() - ins.since >= cfg["stable_reset"] and ins.backoff != cfg["backoff_start"]:
            ins.backoff = cfg["backoff_start"]
            log(f"{ins.pkg} backoff reset")
        return
    if not alive and not HAS_ROOT and ins.state == "STARTING" and not ins.seen_alive:
        ins.blind = True
        ins.since = time.time()
        ins.set("RUNNING", "non-root มองไม่เห็นโปรเซส — เปิดซ้ำทุก 10 นาที")
        return
    if alive:
        ins.set("DISCONNECTED", "แอปไม่ได้อยู่หน้าจอ (foreground)")
    else:
        ins.set("CRASHED", "ไม่พบโปรเซสของแอป")
    shot = take_shot(ins.pkg) if HAS_ROOT else None
    notify(cfg, "crash", f"{ins.pkg}: {ins.event}", shot)
    relaunch(ins, cfg, uri, stop)


def worker(ins, cfg, uri, stop, delay):
    try:
        if stop.wait(delay):
            return
        ins.set("STARTING", "กำลังเปิดเกม")
        ok, why = start_app(ins, uri)
        if not ok:
            ins.set("CRASHED", why)
        while not stop.wait(cfg["check_interval"]):
            try:
                beat(ins, cfg, uri, stop)
            except Exception as e:
                log(f"{ins.pkg} internal error {type(e).__name__}: {e}")
                ins.event = f"ข้อผิดพลาดภายใน ({type(e).__name__})"
    except Exception as e:
        ins.state, ins.event = "ERROR", f"เธรดหยุด ({type(e).__name__})"
        log(f"{ins.pkg} thread died {type(e).__name__}: {e}")


def render(insts):
    t = Table(title="GARNT TOOL REJOIN  (Ctrl+C = หยุด)")
    for c in ("แพ็กเกจ", "สถานะ", "เวลาทำงาน", "รีสตาร์ท", "เหตุการณ์ล่าสุด"):
        t.add_column(c)
    for i in insts:
        up = str(timedelta(seconds=int(time.time() - i.since))) if (i.since and i.state == "RUNNING") else "-"
        ev = f"{i.event} ({i.ts})" if i.ts else i.event
        t.add_row(i.pkg, f"[{STATE_STYLE.get(i.state, 'white')}]{i.state}[/]", up, str(i.restarts), escape(ev))
    return t


def net_ok():
    try:
        requests.head("https://www.roblox.com", timeout=5)
        return True
    except requests.RequestException:
        return False


def start_monitor(cfg):
    uri = build_uri(cfg["game_link"])
    if not uri:
        say("✖ ยังไม่ได้ตั้งลิงก์เกม หรือรูปแบบไม่ถูกต้อง — ไปที่เมนู 3", "red")
        return
    pkgs = scan()
    if not pkgs:
        return
    if not net_ok():
        say("⚠ ไม่พบการเชื่อมต่ออินเทอร์เน็ต — จะพยายามต่อไป แต่เกมอาจเข้าไม่ได้", "yellow")
    if len(pkgs) > 1 and HAS_ROOT and cfg["check_foreground"]:
        say("เตือน: หลายอินสแตนซ์ — ถ้าไม่ได้ใช้หน้าต่างลอย/แยกหน้าจอ แอปจะถูกเปิดวน ให้ปิดตรวจ foreground ที่เมนู 6", "yellow")
    wake()
    stop = threading.Event()
    insts = [Inst(p, cfg["backoff_start"]) for p in pkgs]
    threads = [threading.Thread(target=worker, args=(ins, cfg, uri, stop, n * cfg["launch_delay"]), daemon=True)
               for n, ins in enumerate(insts)]
    log(f"monitor start {len(insts)} instances")
    notify(cfg, "start", f"เริ่ม auto-rejoin {len(insts)} อินสแตนซ์")
    for t in threads:
        t.start()
    try:
        with Live(render(insts), console=console, refresh_per_second=2) as live:
            while True:
                time.sleep(0.5)
                live.update(render(insts))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
    say("หยุดการทำงานแล้ว กลับสู่เมนู", "yellow")
    log("monitor stop")
    notify(cfg, "stop", "หยุด auto-rejoin", block=True)


# ---------- menus ----------
def menu_status(cfg):
    link = scrub(cfg["game_link"])[:60] or "(ยังไม่ตั้ง)"
    say(f"โหมด: {'Root' if HAS_ROOT else 'Non-root'} | เกม: {link} | ตรวจทุก {cfg['check_interval']} วินาที | หน่วงเปิด {cfg['launch_delay']} วินาที")
    pkgs = scan()
    if not pkgs:
        return
    t = Table(title="สถานะอินสแตนซ์")
    for c in ("#", "แพ็กเกจ", "โปรเซส", "Foreground"):
        t.add_column(c)
    for i, p in enumerate(pkgs, 1):
        alive = is_alive(p)
        fg = "-" if not (HAS_ROOT and alive) else ("ใช่" if in_foreground(p) else "ไม่")
        t.add_row(str(i), p, "ทำงาน" if alive else "ไม่พบ", fg)
    console.print(t)
    if not HAS_ROOT:
        say("หมายเหตุ: non-root ตรวจได้เฉพาะโปรเซส (best effort)", "yellow")


def menu_scan(cfg):
    pkgs = scan()
    if pkgs:
        t = Table(title=f"พบ {len(pkgs)} แพ็กเกจ (ลำดับนี้ใช้จับคู่คุกกี้)")
        t.add_column("#")
        t.add_column("แพ็กเกจ")
        for i, p in enumerate(pkgs, 1):
            t.add_row(str(i), p)
        console.print(t)


def menu_settings(cfg):
    say("ลิงก์เกมปัจจุบัน: " + (scrub(cfg["game_link"])[:70] or "(ยังไม่ตั้ง)"))
    say("รูปแบบที่รองรับ -> URI ที่ใช้เปิดเกม:")
    say("  Place ID หรือ roblox.com/games/<id>   -> roblox://experiences/start?placeId=<ID>")
    say("  เซิร์ฟเวอร์ส่วนตัว (privateServerLinkCode) -> roblox://experiences/start?placeId=<ID>&linkCode=<CODE>")
    say("  roblox.com/share?code=<CODE>&type=Server -> roblox://navigation/share_links?code=<CODE>&type=Server")
    link = ask("วางลิงก์/Place ID (Enter = ไม่เปลี่ยน): ")
    if link:
        uri = build_uri(link)
        if uri:
            cfg["game_link"] = link
            say("✔ ใช้ URI: " + scrub(uri), "green")
        else:
            say("✖ รูปแบบลิงก์ไม่ถูกต้อง ไม่เปลี่ยนค่า", "red")
    cfg["check_interval"] = ask_int("ช่วงเวลาตรวจ (วินาที)", cfg["check_interval"], 5, 3600)
    cfg["launch_delay"] = ask_int("หน่วงเวลาระหว่างเปิดแต่ละอินสแตนซ์ (วินาที)", cfg["launch_delay"], 0, 600)
    save_config(cfg)


def menu_cookie(cfg):
    ensure_cookie_file()
    cookies, warns = load_cookies()
    for w in warns:
        say("⚠ " + w, "yellow")
    pkgs = scan()
    t = Table(title="ตัวอย่างการจับคู่คุกกี้ ↔ อินสแตนซ์ (ตามลำดับสแกน)")
    for c in ("#", "แพ็กเกจ", "คุกกี้ (ปิดบัง)", "สถานะ"):
        t.add_column(c)
    for i, p in enumerate(pkgs):
        has = i < len(cookies)
        t.add_row(str(i + 1), p, mask(cookies[i]) if has else "-", "พร้อมจับคู่" if has else "ไม่มีคุกกี้")
    console.print(t)
    say(f"คุกกี้ที่ถูกต้อง {len(cookies)} รายการ / อินสแตนซ์ {len(pkgs)} รายการ  (ไฟล์: {COOKIE_FILE})")
    if len(cookies) > len(pkgs):
        say(f"⚠ คุกกี้เกินจำนวนอินสแตนซ์ {len(cookies) - len(pkgs)} รายการ จะไม่ถูกใช้", "yellow")
    say("หมายเหตุ: เมนูนี้แสดงสถานะ + ตัวอย่างการจับคู่เท่านั้น ยังไม่ฝังคุกกี้ลงแอปโคลนให้อัตโนมัติ", "cyan")
    say("เพราะไม่มั่นใจว่าวิธีฝังใช้ได้เสถียรทุกเวอร์ชันแอป — กรุณาล็อกอินในแต่ละแอปด้วยตัวเอง", "cyan")


def menu_auto(cfg):
    while True:
        say(f"1 เริ่มอัตโนมัติเมื่อเปิดโปรแกรม: {'เปิด' if cfg['auto_start'] else 'ปิด'}")
        say(f"2 backoff เริ่มต้น: {cfg['backoff_start']} วินาที | 3 backoff สูงสุด: {cfg['backoff_max']} วินาที")
        say(f"4 ตรวจ foreground (root): {'เปิด' if cfg['check_foreground'] else 'ปิด'}")
        say("5 รีเซ็ตค่าเริ่มต้น (คงลิงก์เกมและ webhook) | 0 กลับ")
        c = ask("เลือก: ")
        if c == "0":
            return
        if c == "1":
            cfg["auto_start"] = not cfg["auto_start"]
        elif c == "2":
            cfg["backoff_start"] = ask_int("backoff เริ่มต้น", cfg["backoff_start"], 1, 300)
            cfg["backoff_max"] = max(cfg["backoff_max"], cfg["backoff_start"])
        elif c == "3":
            cfg["backoff_max"] = ask_int("backoff สูงสุด", cfg["backoff_max"], cfg["backoff_start"], 3600)
        elif c == "4":
            cfg["check_foreground"] = not cfg["check_foreground"]
        elif c == "5":
            keep = {k: cfg[k] for k in ("game_link", "webhook_url", "events")}
            cfg.clear()
            cfg.update(copy.deepcopy(DEFAULTS))
            cfg.update(keep)
            say("✔ รีเซ็ตแล้ว", "green")
        else:
            say("กรุณาเลือก 0-5", "yellow")
            continue
        save_config(cfg)


def menu_webhook(cfg):
    while True:
        say("Webhook: " + (mask_url(cfg["webhook_url"]) if cfg["webhook_url"] else "(ยังไม่ตั้ง)"))
        say("เหตุการณ์: " + ", ".join(f"{k}={'เปิด' if v else 'ปิด'}" for k, v in cfg["events"].items()))
        say("1 ตั้ง/เปลี่ยน URL | 2 ลบ URL | 3 เปิด/ปิดเหตุการณ์ | 4 ทดสอบส่ง | 0 กลับ")
        c = ask("เลือก: ")
        if c == "0":
            return
        if c == "1":
            u = ask("วาง Webhook URL: ")
            if WEBHOOK_RE.fullmatch(u):
                cfg["webhook_url"] = u
            else:
                say("✖ URL ไม่ถูกต้อง (ต้องเป็น https://discord.com/api/webhooks/<id>/<token>)", "red")
        elif c == "2":
            cfg["webhook_url"] = ""
        elif c == "3":
            names = list(EVENTS)
            for i, k in enumerate(names, 1):
                say(f"  {i} {k} ({EVENTS[k]}): {'เปิด' if cfg['events'][k] else 'ปิด'}")
            s = ask("เลือกเหตุการณ์ที่จะสลับ: ")
            if s.isdecimal() and 1 <= int(s) <= len(names):
                k = names[int(s) - 1]
                cfg["events"][k] = not cfg["events"][k]
            else:
                say("✖ ตัวเลือกไม่ถูกต้อง", "red")
        elif c == "4":
            err = send_webhook(cfg, "ทดสอบจาก GARNT TOOL REJOIN")
            say("✔ ส่งสำเร็จ" if err is None else "✖ ส่งไม่สำเร็จ: " + err, "green" if err is None else "red")
        else:
            say("กรุณาเลือก 0-4", "yellow")
            continue
        save_config(cfg)


def show_menu(cfg):
    body = "\n".join([
        "1  เริ่ม auto-rejoin",
        "2  แสดงอินสแตนซ์/สถานะ",
        "3  ตั้งค่า (ลิงก์เกม, ช่วงตรวจ, หน่วงเวลาเปิด)",
        "4  สแกนแพ็กเกจ",
        "5  ล็อกอินด้วยคุกกี้ (สถานะ + ตัวอย่างจับคู่)",
        "6  ตั้งค่าอัตโนมัติ (เริ่มอัตโนมัติ/ค่าเริ่มต้น)",
        "7  Discord webhook",
        "0  ออก",
    ])
    mode = "ROOT" if HAS_ROOT else "NON-ROOT"
    console.print(Panel(body, title="GARNT TOOL REJOIN", subtitle=f"โหมด: {mode}", border_style="cyan"))


def auto_start(cfg):
    if not build_uri(cfg["game_link"]):
        say("ข้ามการเริ่มอัตโนมัติ: ยังไม่ได้ตั้งลิงก์เกม (เมนู 3)", "yellow")
        return
    say("เริ่มอัตโนมัติใน 5 วินาที — กด Ctrl+C เพื่อยกเลิก", "cyan")
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        say("ยกเลิกการเริ่มอัตโนมัติ", "yellow")
        return
    start_monitor(cfg)


def main():
    startup()
    cfg = load_config()
    ensure_cookie_file()
    actions = {"1": start_monitor, "2": menu_status, "3": menu_settings, "4": menu_scan,
               "5": menu_cookie, "6": menu_auto, "7": menu_webhook}
    try:
        if cfg["auto_start"]:
            auto_start(cfg)
        while True:
            show_menu(cfg)
            choice = ask("เลือกเมนู (0-7): ")
            if choice == "0":
                break
            fn = actions.get(choice)
            if not fn:
                say("กรุณาพิมพ์ตัวเลข 0-7 เท่านั้น", "yellow")
                continue
            try:
                fn(cfg)
            except KeyboardInterrupt:
                say("ยกเลิก กลับสู่เมนู", "yellow")
            except Exception as e:
                say(f"✖ เกิดข้อผิดพลาด: {type(e).__name__}", "red")
                log(f"menu {choice} error {type(e).__name__}: {e}")
    finally:
        wake(False)
    say("ออกจากโปรแกรมแล้ว")


if __name__ == "__main__":
    main()
