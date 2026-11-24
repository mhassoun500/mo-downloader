#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MO Downloader — محمّل الفيديو والصوت
أداة محلية بواجهة عربية تعمل في المتصفح، مبنية على yt-dlp.

المزايا: كل الجودات المتاحة، قص جزء من الفيديو، عدة روابط دفعة واحدة مع طابور تنزيل،
سجل دائم مع تخطّي المكرر، متابعة القنوات والقوائم وتنزيل الجديد تلقائياً.

التشغيل:  python mo_downloader.py          (أو --background لتشغيله بدون فتح المتصفح)
"""
import base64
import importlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "MO Downloader"
APP_VERSION = "2.6"  # written by src/make.py from the VERSION file — change it there
FROZEN = bool(getattr(sys, "frozen", False))
PROTOCOL = "mo-downloader"
# started in the background (with the computer) or by the browser extension through mo-downloader://
BACKGROUND = "--background" in sys.argv or any(a.lower().startswith(PROTOCOL + ":") for a in sys.argv[1:])
DEFAULT_PORT = int(os.environ.get("MD_PORT", 8765))
PORT_SPAN = 10  # the app uses one of DEFAULT_PORT … DEFAULT_PORT+9; the browser extension looks at the same ten
SYSTEM = platform.system()

APP_DATA = Path.home() / ".media_downloader"
APP_DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = APP_DATA / "settings.json"
HISTORY_FILE = APP_DATA / "history.jsonl"  # before 2.6; moved into HISTORY_DB on first start
HISTORY_DB = APP_DATA / "history.db"
QUEUE_FILE = APP_DATA / "queue.json"
PORT_FILE = APP_DATA / "port.txt"
NATIVE_HOST = "com.mo.downloader"
EXT_ID_CHROME = "kfiopiphhapdnaeibmhmdloghbnheeoo"          # fixed by the "key" in the extension's manifest
EXT_ID_FIREFOX = "mo-downloader@mo.local"
ARCHIVE_FILE = APP_DATA / "archive.txt"
SUBS_FILE = APP_DATA / "subscriptions.json"
SUBS_DIR = APP_DATA / "subscriptions"
ENGINE_DIR = APP_DATA / "engine"
COOKIE_FILE = APP_DATA / "cookies.txt"
LOG_FILE = APP_DATA / "log.txt"
TMP_DIR = APP_DATA / "tmp"
TOOLS_DIR = APP_DATA / "tools"

def private_file(path):
    """Cookies and the saved queue: readable by this user only (on Windows the user's home folder is already private)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ping_port(timeout=0.8):
    """The port of the running MO Downloader, or None."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ports = list(range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN))
    try:
        ports.insert(0, int(PORT_FILE.read_text().strip()))
    except (OSError, ValueError):
        pass
    for p in dict.fromkeys(ports):
        try:
            with opener.open(f"http://127.0.0.1:{p}/api/ping", timeout=timeout) as r:
                if json.loads(r.read()).get("app") == APP_NAME:
                    return p
        except Exception:  # noqa: BLE001
            continue
    return None


def self_command(*args):
    return [sys.executable, *args] if FROZEN else [sys.executable, os.path.abspath(__file__), *args]


def start_detached(*args):
    """Start another copy of the app that outlives the caller (the browser ends a native host's children)."""
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if SYSTEM == "Windows":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        try:
            return subprocess.Popen(self_command(*args), creationflags=flags | 0x01000000, **kw)  # + BREAKAWAY_FROM_JOB
        except OSError:
            return subprocess.Popen(self_command(*args), creationflags=flags, **kw)
    return subprocess.Popen(self_command(*args), start_new_session=True, **kw)


def native_host_main():
    """Talk to the browser extension over native messaging: report the app's port, starting the app first if asked.
    This replaces the browser's "Open MO Downloader?" prompt of the mo-downloader:// link."""
    import struct
    if SYSTEM == "Windows":
        import msvcrt
        if sys.stdin is not None and sys.stdout is not None:
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
            inp, out = sys.stdin.buffer, sys.stdout.buffer
        else:  # the windowed build has no sys.stdin/stdout, but the browser still hands it the pipes
            import ctypes
            k = ctypes.windll.kernel32
            k.GetStdHandle.restype = ctypes.c_void_p
            inp = os.fdopen(msvcrt.open_osfhandle(k.GetStdHandle(-10), os.O_RDONLY | os.O_BINARY), "rb")
            out = os.fdopen(msvcrt.open_osfhandle(k.GetStdHandle(-11), os.O_BINARY), "wb")
    else:
        inp, out = sys.stdin.buffer, sys.stdout.buffer
    raw = inp.read(4)
    msg = json.loads(inp.read(struct.unpack("<I", raw)[0]) or b"{}") if len(raw) == 4 else {}
    port = _ping_port()
    if not port and msg.get("type") == "launch":
        start_detached("--background")
        for _ in range(60):
            time.sleep(0.5)
            port = _ping_port()
            if port:
                break
    reply = json.dumps({"ok": bool(port), "port": port, "app": APP_NAME, "version": APP_VERSION}).encode()
    out.write(struct.pack("<I", len(reply)) + reply)
    out.flush()


if any(a.startswith("chrome-extension://") or a == EXT_ID_FIREFOX for a in sys.argv[1:]):
    try:
        native_host_main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)

# Without a console (frozen app / pythonw) there is no stdout: write to a log file instead.
if sys.stdout is None or sys.stderr is None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.txt"))
        _log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _log
        sys.stderr = sys.stderr or _log
    except OSError:
        pass


def log(*a):
    try:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)
    except Exception:  # noqa: BLE001
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


DEFAULT_SETTINGS = {
    "outdir": str(Path.home() / "Downloads" / "MO Downloader"),
    "max_parallel": 2,
    "check_hours": 6,
    "autostart": False,
    "cookies": "",
    "proxy": "",
    "proxy_mode": "fallback",   # fallback = only when the direct connection fails | always
    "accel": True,              # multi-connection downloads through aria2
    "accel_conns": 8,
    "ext_cookies": "auto",      # cookies sent by the browser extension: auto (only when a site needs login) | always | never
    "ui": "window",             # window = the app's own window | browser = the default web browser
    "engine_channel": "stable", # stable | nightly (site fixes reach nightly builds first)
    "engine_checked": 0,
    # 2.7
    "lang": "ar",               # ar | en (the interface language)
    "rate_limit_kb": 0,         # download speed limit in KB/s, 0 = none
    "name_tpl": "",             # file name template, e.g. "{date} - {title}"; empty = "{title} [{id}] {quality}"
    "folder_tpl": "",           # sub-folder template, e.g. "{site}/{uploader}"; empty = straight into the folder
    "notify": True,             # a notification on the computer when a download finishes or fails
    "clipboard": False,         # suggest downloading a video link copied to the clipboard
    "phone": False,             # accept links sent from a phone on the same network
    "phone_pin": "",
    "phone_key": "",
    "sub3d_cmd": "",            # optional command for the 3D subtitle tool: {file} = the .srt file
    "update_url": "https://api.github.com/repos/mhassoun500/mo-downloader/releases/latest",
    "update_checked": 0,
}
SETTINGS = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
SETTINGS_LOCK = threading.Lock()


def save_settings():
    with SETTINGS_LOCK:
        save_json(SETTINGS_FILE, SETTINGS)


# ---------------------------------------------------------------- download engine
def _ver_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))) or (0,)


def _wheel_version(p):
    m = re.match(r"^[A-Za-z0-9_]+-([^-]+)-", Path(p).name)
    return m.group(1) if m else "0"


def bundled_versions():
    """Versions baked into a standalone build (written by the build script)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return load_json(base / "engine_version.json", {})


def use_downloaded_engine():
    """Standalone builds can't pip-upgrade, so newer engine wheels are downloaded to ENGINE_DIR
    and put first on sys.path (pure-python wheels import fine straight from the zip)."""
    if not FROZEN or not ENGINE_DIR.exists():
        return
    bundled = bundled_versions()
    stable_only = SETTINGS.get("engine_channel", "stable") != "nightly"
    for prefix, key in (("yt_dlp_ejs", "yt-dlp-ejs"), ("yt_dlp", "yt-dlp"), ("gallery_dl", "gallery-dl")):
        wheels = sorted(ENGINE_DIR.glob(f"{prefix}-[0-9]*.whl"), key=lambda p: _ver_key(_wheel_version(p)))
        if stable_only:
            for w in [w for w in wheels if "dev" in _wheel_version(w)]:
                w.unlink(missing_ok=True)
                wheels.remove(w)
        for old in wheels[:-1]:
            old.unlink(missing_ok=True)
        if wheels:
            w = wheels[-1]
            if _ver_key(_wheel_version(w)) <= _ver_key(bundled.get(key, "0")):
                w.unlink(missing_ok=True)  # the app itself was updated to something newer
            else:
                sys.path.insert(0, str(w))


def ensure(pip_name, module=None, required=True):
    module = module or pip_name
    try:
        return importlib.import_module(module)
    except ImportError:
        if FROZEN:
            if required:
                raise
            return None
        log(f"[setup] installing {pip_name} ...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "--quiet", pip_name])
            importlib.invalidate_caches()
            return importlib.import_module(module)
        except Exception as e:  # noqa: BLE001
            if required:
                log(f"[setup] failed to install {pip_name}: {e}")
                sys.exit(1)
            log(f"[setup] optional package {pip_name} unavailable: {e}")
            return None


use_downloaded_engine()

# gallery-dl runs as a child process: the app starts itself again with --gallery-dl (works the same in the
# standalone build, which has no separate Python). Its output goes to the file named in MO_GDL_OUT.
if len(sys.argv) > 1 and sys.argv[1] == "--gallery-dl":
    _gdl_out = os.environ.get("MO_GDL_OUT")
    if _gdl_out:
        sys.stdout = sys.stderr = open(_gdl_out, "a", encoding="utf-8", buffering=1)
    import gallery_dl as _gdl
    sys.argv = ["gallery-dl"] + sys.argv[2:]
    sys.exit(_gdl.main())

try:
    yt_dlp = ensure("yt-dlp[default]", "yt_dlp")
except Exception:  # noqa: BLE001  — a broken downloaded wheel must never brick the app
    if not FROZEN:
        raise
    for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if not p.endswith(".whl")]
    shutil.rmtree(ENGINE_DIR, ignore_errors=True)
    import yt_dlp  # noqa: E402
ensure("yt-dlp-ejs", "yt_dlp_ejs", required=False)
from yt_dlp.postprocessor.common import PostProcessor  # noqa: E402
from yt_dlp.utils import download_range_func, match_filter_func  # noqa: E402

ensure("curl-cffi", "curl_cffi", required=False)   # lets yt-dlp present itself like a real browser
gallery_dl = ensure("gallery-dl", "gallery_dl", required=False)
try:
    from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: E402
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _y:
        IMPERSONATE = bool(_y._get_available_impersonate_targets())
except Exception:  # noqa: BLE001
    ImpersonateTarget, IMPERSONATE = None, False

ENGINE = {"version": yt_dlp.version.__version__, "pending": None, "updating": False, "msg": "",
          "gallery": getattr(getattr(gallery_dl, "version", None), "__version__", None)}


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    mod = ensure("imageio-ffmpeg", "imageio_ffmpeg", required=False)
    if mod:
        try:
            exe = mod.get_ffmpeg_exe()
            if exe and SYSTEM != "Windows" and not os.access(exe, os.X_OK):
                os.chmod(exe, 0o755)
            return exe
        except Exception:  # noqa: BLE001
            pass
    return None


def find_js_runtimes():
    """YouTube needs an external JS runtime (Deno recommended) since yt-dlp 2025.11."""
    runtimes = {}
    deno = None
    if FROZEN:  # the standalone build ships its own deno next to the app
        cand = Path(getattr(sys, "_MEIPASS", ".")) / "deno_bin" / ("deno.exe" if SYSTEM == "Windows" else "deno")
        if cand.exists():
            deno = str(cand)
            if SYSTEM != "Windows" and not os.access(deno, os.X_OK):
                os.chmod(deno, 0o755)
    deno = deno or shutil.which("deno")
    if not deno:
        mod = ensure("deno", "deno", required=False)
        if mod and hasattr(mod, "find_deno_bin"):
            try:
                deno = mod.find_deno_bin()
                if deno and SYSTEM != "Windows" and not os.access(deno, os.X_OK):
                    os.chmod(deno, 0o755)
            except Exception:  # noqa: BLE001
                deno = None
    if deno:
        runtimes["deno"] = {"path": str(deno)}
    node = shutil.which("node")
    if node:
        runtimes["node"] = {"path": node}
    return runtimes


FFMPEG = find_ffmpeg()
JS_RUNTIMES = find_js_runtimes()

if len(sys.argv) > 2 and sys.argv[1] == "--engine-check":
    Path(sys.argv[2]).write_text(json.dumps({
        "yt_dlp": ENGINE["version"], "gallery_dl": ENGINE["gallery"], "impersonate": IMPERSONATE,
        "ffmpeg": bool(FFMPEG), "js": sorted(JS_RUNTIMES)}), encoding="utf-8")
    sys.exit(0)
TOKEN = secrets.token_urlsafe(16)
NO_WINDOW = 0x08000000 if SYSTEM == "Windows" else 0  # child processes without a console window

# English for the interface and messages (Arabic text -> English), filled in by src/make.py from src/i18n_en.json
I18N_EN = json.loads(r"""__I18N__""")
_I18N_KEYS = sorted(I18N_EN, key=len, reverse=True)
_AR = "\u0600-\u06FF"


def T(text):
    """Translate a message for the interface language (English when chosen; Arabic is the source language).
    Whole messages first; otherwise each known Arabic phrase inside a composed message."""
    if SETTINGS.get("lang") != "en" or not text or not re.search(f"[{_AR}]", str(text)):
        return text
    norm = re.sub(r"\s+", " ", str(text)).strip()
    if norm in I18N_EN:
        return I18N_EN[norm]

    def one_pass(o, short):
        for k in _I18N_KEYS:
            if (short or len(k) >= 6 or " " in k) and k in o:
                o = re.sub(f"(?<![{_AR}]){re.escape(k)}(?![{_AR}])", lambda m: I18N_EN[k], o)
        return o
    long = one_pass(norm, False)
    if not re.search(f"[{_AR}]", long):
        return long
    full = one_pass(long, True)
    return long if re.search(f"[{_AR}]", full) else full.replace(stripped, I18N_EN[stripped])
    for k in _I18N_KEYS:
        if k in text:
            text = re.sub(f"(?<![{_AR}]){re.escape(k)}(?![{_AR}])", lambda m: I18N_EN[k], text)
    return text
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def https_get(url, timeout=30):
    custom = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")  # e.g. company proxies
    try:
        if custom and os.path.exists(custom):
            ctx = ssl.create_default_context(cafile=custom)
        else:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


ENGINE_PKGS = ("yt-dlp", "yt-dlp-ejs", "gallery-dl")


def _latest_release(meta, nightly):
    """(version, files) of the newest release; nightly = also pre-releases (yt-dlp's nightly builds)."""
    if not nightly:
        return meta["info"]["version"], meta["urls"]
    best = None
    for v, files in (meta.get("releases") or {}).items():
        if files and not all(f.get("yanked") for f in files) and (best is None or _ver_key(v) > _ver_key(best)):
            best = v
    return best, meta["releases"][best]


def _installed_version(name):
    if name == "yt-dlp":
        return ENGINE["version"]
    if name == "gallery-dl" and ENGINE.get("gallery"):
        return ENGINE["gallery"]
    if not FROZEN:
        try:
            import importlib.metadata as md
            return md.version(name)
        except Exception:  # noqa: BLE001
            pass
    return bundled_versions().get(name, "0")


def _update_frozen(nightly):
    """Standalone build: download newer pure-Python wheels (checked against PyPI's SHA-256); used from next start."""
    import hashlib
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    got = []
    for name in ENGINE_PKGS:
        meta = json.loads(https_get(f"https://pypi.org/pypi/{name}/json"))
        latest, files = _latest_release(meta, nightly and name == "yt-dlp")
        prefix = name.replace("-", "_")
        have = [_wheel_version(p) for p in ENGINE_DIR.glob(f"{prefix}-[0-9]*.whl")]
        if not latest or _ver_key(latest) <= _ver_key(_installed_version(name)) or latest in have:
            continue
        wheel = next((f for f in files if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("-none-any.whl")), None)
        if not wheel:
            continue
        data = https_get(wheel["url"], timeout=180)
        if hashlib.sha256(data).hexdigest() != (wheel.get("digests") or {}).get("sha256"):
            raise RuntimeError(f"{name}: checksum mismatch, update ignored")
        tmp = ENGINE_DIR / (wheel["filename"] + ".part")
        tmp.write_bytes(data)
        tmp.replace(ENGINE_DIR / wheel["filename"])
        got.append(f"{name} {latest}")
    return got


def _update_pip(nightly):
    """Python version: upgrade the engines with pip (nightly = yt-dlp pre-releases)."""
    import importlib.metadata as md
    before = {n: _installed_version(n) for n in ENGINE_PKGS}
    pkgs = ["yt-dlp[default,curl-cffi]", "yt-dlp-ejs", "gallery-dl"]
    if not nightly and "dev" in before["yt-dlp"]:  # back to stable: pip won't downgrade on its own
        stable = json.loads(https_get("https://pypi.org/pypi/yt-dlp/json"))["info"]["version"]
        pkgs[0] = f"yt-dlp[default,curl-cffi]=={stable}"
    base = [sys.executable, "-m", "pip", "install", "-U", "--quiet", "--disable-pip-version-check"] + (["--pre"] if nightly else [])
    flags = 0x08000000 if SYSTEM == "Windows" else 0
    for extra in ([], ["--user"], ["--break-system-packages"]):
        r = subprocess.run(base + extra + pkgs, capture_output=True, timeout=900, creationflags=flags)
        if r.returncode == 0:
            break
    else:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-300:])
    got = []
    for n in ENGINE_PKGS:
        try:
            v = md.version(n)
        except Exception:  # noqa: BLE001
            continue
        if v != before[n]:
            got.append(f"{n} {v}")
    return got


def engine_update_check(force=False):
    """Keep the download engines current: once a day on its own, or right away from the "update now" button.
    New versions are used after the app restarts."""
    if ENGINE["updating"]:
        return
    if not force and time.time() - float(SETTINGS.get("engine_checked") or 0) < 20 * 3600:
        return
    ENGINE.update(updating=True, msg="جارٍ التحقق من تحديثات المحرك…")
    try:
        nightly = SETTINGS.get("engine_channel") == "nightly"
        got = _update_frozen(nightly) if FROZEN else _update_pip(nightly)
        if got:
            ENGINE["pending"] = "، ".join(got)
            ENGINE["msg"] = "تم تنزيل: " + ENGINE["pending"] + " — يُستخدم بعد إعادة تشغيل البرنامج."
            log(f"[engine] updated: {got}")
        else:
            ENGINE["msg"] = "المحرك محدّث إلى آخر إصدار" + (" يومي." if nightly else " مستقر.")
        SETTINGS["engine_checked"] = time.time()
        save_settings()
    except Exception as e:  # noqa: BLE001
        ENGINE["msg"] = "تعذّر تحديث المحرك: " + str(e)[:200]
        log(f"[engine] update check failed: {e}")
    finally:
        ENGINE["updating"] = False


# ---------------------------------------------------------------- state
JOBS = {}
JOBS_LOCK = threading.RLock()
QUEUE = threading.Condition(JOBS_LOCK)
RUNNING = ("starting", "downloading", "processing")
PAUSED = "paused"
FINISHED = ("done", "error", "cancelled")

HISTORY_LOCK = threading.Lock()
HISTORY_COLS = ("key", "id", "title", "url", "file", "quality", "size", "date", "uploader", "duration", "site",
                "source", "clip", "method")


def _history_insert(db, entries):
    for e in entries:
        extra = {k: v for k, v in e.items() if k not in HISTORY_COLS}
        db.execute(f"INSERT INTO history ({', '.join(HISTORY_COLS)}, extra) VALUES ({', '.join('?' * (len(HISTORY_COLS) + 1))})",
                   [e.get(k) if not isinstance(e.get(k), (dict, list)) else json.dumps(e.get(k)) for k in HISTORY_COLS]
                   + [json.dumps(extra, ensure_ascii=False) if extra else None])


def _history_db():
    """The download history in a local SQLite database (before 2.6 it was one JSON file read whole into memory
    and rewritten on every delete)."""
    import sqlite3
    db = sqlite3.connect(str(HISTORY_DB), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS history (n INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, id TEXT, title TEXT,"
               " url TEXT, file TEXT, quality TEXT, size INTEGER, date REAL, uploader TEXT, duration REAL, site TEXT,"
               " source TEXT, clip TEXT, method TEXT, extra TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS history_key ON history(key)")
    if HISTORY_FILE.exists():
        rows = []
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        _history_insert(db, rows)
        db.commit()
        HISTORY_FILE.replace(HISTORY_FILE.with_name("history.jsonl.migrated"))
    return db


def _history_row(r):
    d = {k: r[k] for k in HISTORY_COLS if r[k] is not None}
    if r["extra"]:
        try:
            d.update(json.loads(r["extra"]))
        except ValueError:
            pass
    return d


HISTORY = _history_db()


def history_add(entry):
    with HISTORY_LOCK:
        _history_insert(HISTORY, [entry])
        HISTORY.commit()


ARCHIVE = set(ARCHIVE_FILE.read_text(encoding="utf-8").split("\n")) - {""} if ARCHIVE_FILE.exists() else set()

SUBS_LOCK = threading.RLock()
SUBS = load_json(SUBS_FILE, [])
SUBS_DIR.mkdir(parents=True, exist_ok=True)


def save_subs():
    with SUBS_LOCK:
        save_json(SUBS_FILE, SUBS)


def find_sub(sid):
    return next((s for s in SUBS if s["id"] == sid), None)


def sub_archive_path(sid):
    return SUBS_DIR / f"{sid}.txt"


def read_set(path):
    return set(Path(path).read_text(encoding="utf-8").split("\n")) - {""} if Path(path).exists() else set()


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class Cancelled(yt_dlp.utils.DownloadCancelled):
    """Pause / cancel. A DownloadCancelled is never swallowed by yt-dlp, so a list stops too instead of
    treating it as one failed item and moving on to the next."""
    msg = "Cancelled by the user"


# Web pages without a dedicated extractor (the "generic" one) number their videos by position or name them after
# the file ("master", "index"): keys that change when a page adds a video, or that collide across sites. Key them by
# the media address instead, so "skip what I already have" and following such a page work.
_yt_archive_id = yt_dlp.YoutubeDL._make_archive_id


def _mo_archive_id(self, info):
    key = _yt_archive_id(self, info)
    if key and key.split(" ", 1)[0] in ("generic", "html5mediaembed"):
        import hashlib
        media = next((f.get("url") for f in info.get("formats") or [] if f.get("url")), None) or info.get("url") \
            or info.get("webpage_url")
        if media:
            key = key.split(" ", 1)[0] + " u" + hashlib.sha1(str(media).encode()).hexdigest()[:16]
    return key


yt_dlp.YoutubeDL._make_archive_id = _mo_archive_id


# ---------------------------------------------------------------- aria2 (multi-connection downloads)
ARIA2_WIN_URL = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
ARIA2_WIN_SHA256 = "67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288"


def find_aria2():
    exe = "aria2c.exe" if SYSTEM == "Windows" else "aria2c"
    cands = []
    if FROZEN:
        cands.append(Path(getattr(sys, "_MEIPASS", ".")) / "aria2" / exe)
    cands += [TOOLS_DIR / exe]
    for c in cands:
        if c.exists():
            if SYSTEM != "Windows" and not os.access(c, os.X_OK):
                os.chmod(c, 0o755)
            return str(c)
    return shutil.which("aria2c")


def fetch_aria2_windows():
    """Python version on Windows: get the official aria2 build once (checked against a pinned SHA-256)."""
    global ARIA2C
    if SYSTEM != "Windows" or ARIA2C:
        return
    try:
        import hashlib
        import io
        import zipfile
        data = https_get(ARIA2_WIN_URL, timeout=120)
        if hashlib.sha256(data).hexdigest() != ARIA2_WIN_SHA256:
            log("[aria2] download checksum mismatch; not used")
            return
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.endswith("aria2c.exe"))
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            (TOOLS_DIR / "aria2c.exe").write_bytes(z.read(member))
        ARIA2C = find_aria2()
        log(f"[aria2] installed {ARIA2C}")
    except Exception as e:  # noqa: BLE001
        log(f"[aria2] could not fetch: {e}")


ARIA2C = None  # set in main() once https_get exists
ARIA2_BROKEN = {"why": None}  # aria2 crashed on this computer: stop using it until the app restarts


def aria2_env():
    """aria2 built against OpenSSL 3 loads a 'legacy' module at start; point it at the copy shipped with the app."""
    env = os.environ.copy()
    if ARIA2C:
        mods = Path(ARIA2C).parent / "ossl-modules"
        if mods.is_dir():
            env["OPENSSL_MODULES"] = str(mods)
    return env


def aria2_failed(text):
    """True when the failure is aria2 itself (not the site): such jobs are retried without acceleration."""
    low = str(text).lower()
    return "aria2" in low and not any(k in low for k in ("status=4", "status=5", "404", "not found"))


def accel_on(proxy=None, flags=None):
    if not (ARIA2C and SETTINGS.get("accel", True)) or ARIA2_BROKEN["why"] or (flags or {}).get("_no_accel"):
        return False
    return not (proxy and str(proxy).lower().startswith("socks"))  # aria2 only speaks http proxies


def _size(txt):
    return int(txt) if str(txt).isdigit() else 0


def aria2_run(url, dirpath, out=None, headers=None, cookiefile=None, proxy=None, on_progress=None, should_stop=None):
    """Download one URL with aria2 (several connections), reporting progress through its local RPC.
    Returns the final file path. Raises Cancelled if should_stop() becomes true."""
    conns = max(1, min(16, int(SETTINGS.get("accel_conns") or 8)))
    port, secret = free_port(16800), secrets.token_hex(8)
    cmd = [ARIA2C, "--no-conf", "--enable-rpc", f"--rpc-listen-port={port}", f"--rpc-secret={secret}",
           "--rpc-listen-all=false", "--console-log-level=error", "--summary-interval=0", "--download-result=hide",
           "--show-console-readout=false", f"--max-connection-per-server={conns}", f"--split={conns}",
           "--min-split-size=1M", "--file-allocation=none", "--continue=true", "--allow-overwrite=false",
           "--auto-file-renaming=" + ("false" if out else "true"), "--content-disposition-default-utf8=true",
           "--max-tries=5", "--retry-wait=2", "--http-accept-gzip=true", f"--dir={dirpath}"]
    if SYSTEM == "Windows":
        cmd.append("--enable-color=false")
    if out:
        cmd.append(f"--out={out}")
    for k, v in (headers or {}).items():
        if k.lower() not in ("cookie", "accept-encoding"):
            cmd += ["--header", f"{k}: {v}"]
    if cookiefile:
        cmd.append(f"--load-cookies={cookiefile}")
    if proxy:
        cmd.append(f"--all-proxy={proxy}")
    cmd += ["--", url]
    flags = 0x08000000 if SYSTEM == "Windows" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags, env=aria2_env())
    rpc_url = f"http://127.0.0.1:{port}/jsonrpc"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(method, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": "mo", "method": method, "params": [f"token:{secret}", *params]})
        with opener.open(urllib.request.Request(rpc_url, body.encode(), {"Content-Type": "application/json"}), timeout=5) as r:
            return json.loads(r.read())["result"]

    keys = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorMessage", "files", "followedBy"]
    try:
        while True:
            if should_stop and should_stop():
                raise Cancelled()
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", "replace").strip()
                if "exception" in err.lower() or "ossl" in err.lower():  # aria2 itself can't start here
                    ARIA2_BROKEN["why"] = err[-200:]
                    log(f"[aria2] disabled for this session: {err[-200:]}")
                raise RuntimeError("aria2: " + (err[-300:] or f"exit {proc.returncode}"))
            time.sleep(0.5)
            try:
                items = rpc("aria2.tellActive", keys) + rpc("aria2.tellWaiting", 0, 10, keys) + rpc("aria2.tellStopped", 0, 10, keys)
            except Exception:  # noqa: BLE001 — RPC not up yet
                continue
            items = [i for i in items if not i.get("followedBy")]
            if not items:
                continue
            it = items[0]
            done, total, speed = _size(it["completedLength"]), _size(it["totalLength"]), _size(it["downloadSpeed"])
            path = (it.get("files") or [{}])[0].get("path") or ""
            if on_progress and it["status"] in ("active", "waiting", "complete"):
                on_progress(done, total or None, speed or None, path)
            if it["status"] == "complete":
                return path
            if it["status"] in ("error", "removed"):
                raise RuntimeError("aria2: " + (it.get("errorMessage") or "download failed"))
    finally:
        # pause first, then a graceful shutdown: the partial file and its .aria2 control file stay,
        # so a paused download continues where it stopped
        try:
            rpc("aria2.forcePauseAll")
        except Exception:  # noqa: BLE001
            pass
        try:
            rpc("aria2.shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()


def _install_aria2_downloader():
    """Make yt-dlp's 'aria2c' external downloader report live progress (it normally shows none)."""
    from yt_dlp.downloader import external as ext_mod

    class MOAria2FD(ext_mod.Aria2cFD):
        def _call_downloader(self, tmpfilename, info_dict):
            dn, fn = os.path.split(os.path.abspath(tmpfilename))

            def prog(done, total, speed, _path):
                eta = int((total - done) / speed) if (total and speed) else None
                self._hook_progress({"status": "downloading", "downloaded_bytes": done, "total_bytes": total,
                                     "speed": speed, "eta": eta, "filename": tmpfilename,
                                     "tmpfilename": tmpfilename}, info_dict)
            aria2_run(info_dict["url"], dn, fn, headers=info_dict.get("http_headers"),
                      cookiefile=self._write_cookies(), proxy=self.params.get("proxy"), on_progress=prog)
            return 0

    ext_mod._BY_NAME["aria2c"] = MOAria2FD


# ---------------------------------------------------------------- cookies sent by the browser extension
EXT_COOKIES = {}          # site -> {"text": netscape cookies, "ts": time}
EXT_COOKIES_LOCK = threading.Lock()


def site_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def store_ext_cookies(url, text):
    if not text or "\t" not in str(text):
        return
    with EXT_COOKIES_LOCK:
        EXT_COOKIES[site_of(url)] = {"text": str(text), "ts": time.time()}


def ext_cookies_for(url):
    host = site_of(url)
    if not host or SETTINGS.get("ext_cookies") == "never":
        return None
    with EXT_COOKIES_LOCK:
        for k, v in list(EXT_COOKIES.items()):
            if time.time() - v["ts"] > 6 * 3600:
                del EXT_COOKIES[k]
            elif host == k or host.endswith("." + k) or k.endswith("." + host):
                return v["text"]
    return None


def cookie_file_for(url):
    text = ext_cookies_for(url)
    if not text:
        return None
    import hashlib
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    f = TMP_DIR / f"ck-{hashlib.sha1(site_of(url).encode()).hexdigest()[:12]}.txt"
    if not text.lstrip().startswith("#"):
        text = "# Netscape HTTP Cookie File\n" + text
    if SYSTEM != "Windows":
        TMP_DIR.chmod(0o700)
    f.write_text(text, encoding="utf-8")
    private_file(f)
    job = TMP_COOKIE_JOBS.get(threading.get_ident())
    if job is not None:
        job.setdefault("_tmp_files", set()).add(str(f))
    return str(f)


TMP_COOKIE_JOBS = {}  # job thread -> job, so its temporary cookie files are deleted when it ends


def drop_tmp_files(job):
    for f in job.pop("_tmp_files", None) or ():
        if not any(f in (j.get("_tmp_files") or ()) for j in list(JOBS.values()) if j is not job and j["status"] in RUNNING):
            Path(f).unlink(missing_ok=True)


# Only phrases that really mean "log in / prove who you are": a word like "cookies" or "account" alone also appears in
# messages where logging in does not help (e.g. "account terminated"), and wrongly retried with the browser's login.
AUTH_ERRORS = ("sign in to confirm", "sign in to view", "sign in to watch", "please sign in", "sign in required",
               "login required", "log in to", "log in or sign up", "logged-in", "logged in users", "requires login",
               "requires authentication", "authentication required", "use --cookies", "--cookies-from-browser",
               "not a bot", "age-restricted", "age restricted", "confirm your age", "inappropriate for some users",
               "private video", "video is private", "members only", "members-only", "join this channel",
               "premium members", "requires a premium", "youtube premium", "registered users", "subscribers only",
               "subscriber-only", "http error 401", "status=401", "401 unauthorized", "account required",
               "requires an account")


def is_auth_error(msg):
    low = str(msg).lower()
    return any(k in low for k in AUTH_ERRORS)


# ---------------------------------------------------------------- direct files (browser downloads, images)
def filename_from(url, headers):
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd) or re.search(r'filename\s*=\s*"?([^";]+)"?', cd)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip()
    from urllib.parse import unquote
    return unquote(Path(urlparse(url).path).name) or "download"


def clean_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .") or "download"
    stem, ext = os.path.splitext(name)
    return stem[:150] + ext[:16]


def unique_path(dirpath, name):
    p = Path(dirpath) / name
    i = 1
    while p.exists():
        stem, ext = os.path.splitext(name)
        p = Path(dirpath) / f"{stem} ({i}){ext}"
        i += 1
    return p


def py_download(url, dirpath, out, headers, cookiefile, proxy, on_progress, should_stop):
    """Plain single-connection download with resume — used when aria2 isn't available."""
    import http.cookiejar
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy and not proxy.startswith("socks") else {})]
    if cookiefile:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(cookiefile, ignore_discard=True, ignore_expires=True)
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    try:
        import certifi
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    except Exception:  # noqa: BLE001
        pass
    opener = urllib.request.build_opener(*handlers)
    hdrs = {k: v for k, v in (headers or {}).items() if k.lower() not in ("cookie", "accept-encoding")}
    hdrs.setdefault("User-Agent", "Mozilla/5.0")
    with opener.open(urllib.request.Request(url, headers=hdrs), timeout=30) as r:
        name = out or clean_filename(filename_from(r.geturl(), r.headers))
        final = unique_path(dirpath, name) if not out else Path(dirpath) / name
        part = Path(str(final) + ".part")
        total = int(r.headers.get("Content-Length") or 0) or None
        start = 0
    if part.exists():
        start = part.stat().st_size
        hdrs["Range"] = f"bytes={start}-"
    with opener.open(urllib.request.Request(url, headers=hdrs), timeout=30) as r:
        if start and r.status != 206:
            start = 0
        mode = "ab" if start else "wb"
        done, t0, last = start, time.time(), 0
        with open(part, mode) as f:
            while True:
                if should_stop and should_stop():
                    raise Cancelled()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if on_progress and now - last > 0.4:
                    last = now
                    on_progress(done, total, (done - start) / max(0.001, now - t0), str(final))
    part.replace(final)
    return str(final)


def run_file_job(job, req, flags):
    outdir = outdir_of(req)
    job["outdir"] = str(outdir)
    headers = dict(req.get("headers") or {})
    proxy = proxy_value() if (flags.get("_use_proxy") or SETTINGS.get("proxy_mode") == "always") and proxy_value() else None
    cookiefile = cookie_file_for(req["url"]) if (flags.get("_use_cookies") or SETTINGS.get("ext_cookies") == "always"
                                                  or req.get("kind") == "file") else None
    out = clean_filename(req["filename"]) if req.get("filename") else None
    if out and (Path(outdir) / out).exists() and not Path(str(Path(outdir) / out) + ".aria2").exists():
        out = unique_path(outdir, out).name

    fast = accel_on(proxy, flags)

    def prog(done, total, speed, path):
        with JOBS_LOCK:
            if path and not job.get("many"):
                job["title"] = Path(path).name or job["title"]
            job.update(status="downloading", stage="جارٍ التنزيل" + (" (مسرّع)" if fast else ""),
                       downloaded=done, total=total, speed=speed,
                       eta=int((total - done) / speed) if (total and speed) else None,
                       percent=(done / total * 100) if total else None)

    stop = lambda: bool(job.get("cancel"))  # noqa: E731
    try:
        if fast:
            path = aria2_run(req["url"], str(outdir), out, headers, cookiefile, proxy, prog, stop)
        else:
            path = py_download(req["url"], str(outdir), out, headers, cookiefile, proxy, prog, stop)
    except Cancelled:
        if not job.get("pause") and out:  # cancelled for good: don't leave half a file behind
            for leftover in (Path(outdir) / out, Path(outdir) / (out + ".aria2"), Path(outdir) / (out + ".part")):
                leftover.unlink(missing_ok=True)
        raise
    entry = {"key": None, "id": None, "title": Path(path).name, "url": req.get("page") or req["url"], "file": path,
             "quality": "ملف · " + (Path(path).suffix.lstrip(".") or "—"),
             "size": os.path.getsize(path) if os.path.exists(path) else None, "date": time.time(),
             "uploader": None, "duration": None, "site": site_of(req.get("page") or req["url"]),
             "source": job.get("source_label") or "", "clip": ""}
    history_add(entry)
    with JOBS_LOCK:
        job.update(file=path, done_count=1, title=Path(path).name)
    return 0


# ---------------------------------------------------------------- helpers
def clean_err(e):
    msg = ANSI.sub("", str(e)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r";?\s*please report this issue on.*$", "", msg, flags=re.S | re.I).strip()  # yt-dlp boilerplate
    if re.search(r"[؀-ۿ]", msg):  # our own Arabic message, already explains itself
        return msg
    hints = []
    low = msg.lower()
    if "drm" in low:
        hints.append("هذا المحتوى محمي بتقنية منع النسخ ولا يمكن تنزيله.")
    if "could not copy" in low and "cookie" in low:
        hints.append("المتصفح مفتوح ويقفل ملف الكوكيز. أغلقه تماماً (من أيقونته بجانب الساعة أيضاً) ثم أعد المحاولة، "
                     "أو الأفضل: اختر «ملف cookies.txt» أو Firefox من الإعدادات.")
    elif "dpapi" in low or "decrypt" in low or "app-bound" in low or "app bound" in low:
        hints.append("إصدارات Chrome وEdge الحديثة على ويندوز تشفّر الكوكيز بطريقة تمنع البرامج الأخرى من قراءتها. "
                     "استخدم «ملف cookies.txt» أو Firefox من الإعدادات بدلاً منها.")
    elif any(k in low for k in ("sign in", "log in", "login", "--cookies", "cookies-from-browser",
                                "not a bot", "age-restricted", "members only", "private video")):
        hints.append("الموقع يطلب تسجيل الدخول: اختر «ملف cookies.txt» أو Firefox من الإعدادات ثم أعد المحاولة.")
    if is_network_error(msg):
        hints.append("تعذّر الوصول إلى الموقع من شبكتك (قد يكون محجوباً أو الاتصال مقطوعاً). "
                     + ("جرّبت الأداة البروكسي أيضاً ولم ينجح؛ تأكد أن البروكسي يعمل من «اختبار البروكسي» في الإعدادات."
                        if SETTINGS.get("proxy") else "يمكنك ضبط بروكسي من تبويب الإعدادات لتعيد الأداة المحاولة عبره تلقائياً."))
    if is_block_error(msg) and not is_network_error(msg):
        hints.append("الموقع رفض الاتصال لأنه يحمي نفسه من البرامج الآلية. جرّبت الأداة انتحال بصمة متصفح حقيقي. "
                     "سجّل دخولك في الموقع من المتصفح، ثم نزّل الفيديو من زر إضافة المتصفح لتستخدم الأداة تسجيل دخولك.")
    if is_no_media(msg):
        hints.append("لم يُعثر على فيديو يفهمه المحرك في هذه الصفحة. شغّل الفيديو في المتصفح، ثم افتح نافذة إضافة "
                     "المتصفح واختر من «فيديوهات التُقطت من هذه الصفحة».")
    if any(k in low for k in ("video unavailable", "has been removed", "http error 404", "status=404", "http error 410",
                              "no longer available", "does not exist")):
        hints.append("الفيديو محذوف أو غير متاح، أو الرابط غير صحيح.")
    if any(k in low for k in ("not available in your country", "geo restrict", "geo-restrict", "geo-blocked")):
        hints.append("هذا المحتوى غير متاح في بلدك من مصدره.")
    if any(k in low for k in ("premieres in", "live event will begin", "is_upcoming", "will begin in")):
        hints.append("البث لم يبدأ بعد. أعد المحاولة عند بدايته، أو استخدم «تسجيل البث» لتنتظره الأداة.")
    if "js runtime" in low or "javascript runtime" in low:
        hints.append("ثبّت Deno ثم أعد تشغيل الأداة.")
    if "requested format is not available" in low:
        hints.append("هذه الجودة لم تعد متاحة. اضغط «عرض الجودات» مجدداً واختر غيرها.")
    return msg + ("\n\n" + "\n".join(hints) if hints else "")


# Connection problems the user's proxy can help with. Not "blocked", "proxy", "timeout" or 403 on their own:
# those words also appear in copyright blocks, settings names and anti-bot refusals.
NET_ERRORS = (
    "failed to resolve", "getaddrinfo", "name or service not known", "nodename nor servname", "no address associated",
    "temporary failure in name resolution", "connection refused", "connection reset", "connection aborted",
    "remote end closed connection", "timed out", "connecttimeout", "readtimeout", "network is unreachable",
    "unable to connect", "eof occurred in violation of protocol", "[ssl:", "sslerror", "certificate verify failed",
    "not available in your country", "not made this video available in your country", "geo restrict",
    "geo-restrict", "geoblocked", "http error 451", "tunnel connection failed", "proxyerror",
    "unable to connect to proxy",
)


def is_network_error(msg):
    low = str(msg).lower()
    return any(k in low for k in NET_ERRORS)


# the site refuses automated clients -> try again presenting ourselves like a real browser
BLOCK_ERRORS = ("http error 403", "http error 429", "http error 503", "status=403", "status=429", "cloudflare",
                "captcha", "impersonat", "too many requests", "access denied", "unable to download webpage",
                "unable to download api page", "anti-bot")
# the page has no video yt-dlp understands -> try the gallery engine, then streams caught on the page
NO_MEDIA_ERRORS = ("unsupported url", "no video formats", "not a video", "no media", "no video found",
                   "there is no video", "no video could be found", "does not contain any video", "no formats found",
                   "unable to extract", "no video in", "no videos found", "لم يُعثر على")


def is_block_error(msg):
    low = str(msg).lower()
    return any(k in low for k in BLOCK_ERRORS)


def is_no_media(msg):
    low = str(msg).lower()
    return any(k in low for k in NO_MEDIA_ERRORS)


def gallery_site(url):
    """gallery-dl's name for the site if it supports this link (images, albums, multi-file posts)."""
    if not gallery_dl or not str(url).startswith(("http://", "https://")):
        return None
    try:
        ex = gallery_dl.extractor.find(url)
        return ex.category if ex else None
    except Exception:  # noqa: BLE001
        return None


METHOD_NAMES = (("_alt_stream", "البث الملتقط من الصفحة"), ("_gallery", "المحرك الثاني (gallery-dl)"),
                ("_impersonate", "انتحال بصمة المتصفح"), ("_use_cookies", "تسجيل دخولك"), ("_use_proxy", "البروكسي"))


def method_label(flags):
    parts = [name for key, name in METHOD_NAMES if flags.get(key)]
    return " + ".join(parts) or "مباشر"


def redact_url(url):
    """For error reports: keep the site and path, drop query values that can hold tokens (except YouTube's v/list)."""
    u = urlparse(str(url))
    q = parse_qs(u.query)
    keep = {k: v for k, v in q.items() if k in ("v", "list")} if "youtu" in (u.hostname or "") else {}
    tail = ("?" + "&".join(f"{k}={v[0]}" for k, v in keep.items())) if keep else ("?…" if u.query else "")
    host = (u.netloc or "").rsplit("@", 1)[-1]  # never user:password@
    return f"{u.scheme}://{host}{u.path}{tail}"


def redact_text(text):
    return re.sub(r"https?://[^\s'\"<>)]+", lambda m: redact_url(m.group(0)), str(text or ""))


def proxy_value():
    return str(SETTINGS.get("proxy") or "").strip()


def proxy_fallback_on():
    return bool(proxy_value()) and SETTINGS.get("proxy_mode", "fallback") == "fallback"


def parse_time(s):
    s = str(s or "").strip().replace("٫", ".").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"صيغة الوقت «{s}» غير صحيحة. اكتبه مثل 1:30 أو 1:02:45.")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def fmt_hms(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def safe_name(s, limit=80):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(s or "")).strip(" .")
    return (s[:limit] or "MO Downloader").replace("%", "%%")


def outdir_of(req):
    p = Path(os.path.expanduser(str(req.get("outdir") or SETTINGS["outdir"]).strip()))
    if not p.is_absolute():
        p = Path.home() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def base_opts(req, logger=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": not req.get("playlist", False),
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
        "trim_file_name": 180,
    }
    if logger:
        opts["logger"] = logger
    if proxy_value() and (req.get("_use_proxy") or SETTINGS.get("proxy_mode") == "always"):
        opts["proxy"] = proxy_value()
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    if req.get("_impersonate") and IMPERSONATE:
        opts["impersonate"] = ImpersonateTarget("chrome")
    if req.get("headers"):
        opts["http_headers"] = {k: v for k, v in req["headers"].items() if k.lower() not in ("cookie",)}
    if req.get("_use_cookies") or (SETTINGS.get("ext_cookies") == "always" and ext_cookies_for(req.get("url") or "")):
        ck = cookie_file_for(req.get("url") or "")
        if ck:
            opts["cookiefile"] = ck
            return opts
    browser = (req.get("cookies") if req.get("cookies") is not None else SETTINGS.get("cookies") or "").strip()
    if browser == "file":
        if not COOKIE_FILE.exists():
            raise ValueError("اخترت «ملف cookies.txt» لكن لم تحمّل الملف بعد. حمّله من تبويب الإعدادات.")
        opts["cookiefile"] = str(COOKIE_FILE)
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def archive_key(ydl, info):
    try:
        return ydl._make_archive_id(info)
    except Exception:  # noqa: BLE001
        ie = info.get("extractor_key") or info.get("ie_key") or ""
        return f"{ie.lower()} {info.get('id')}" if info.get("id") else None


def last_download(key):
    if not key:
        return None
    with HISTORY_LOCK:
        r = HISTORY.execute("SELECT * FROM history WHERE key=? ORDER BY n DESC LIMIT 1", (key,)).fetchone()
    return _history_row(r) if r else None


# ---------------------------------------------------------------- info
def fmt_row(f, duration):
    vc, ac = f.get("vcodec"), f.get("acodec")
    if f.get("ext") == "mhtml" or (vc == "none" and ac == "none"):
        return None
    if vc == "none" or f.get("resolution") == "audio only":
        kind = "audio"
    elif ac == "none":
        kind = "video"
    else:
        kind = "av"
    size = f.get("filesize")
    approx = False
    if not size:
        size = f.get("filesize_approx")
        approx = bool(size)
    if not size and f.get("tbr") and duration:
        size = int(f["tbr"] * 125 * duration)
        approx = True
    return {
        "id": str(f.get("format_id")), "kind": kind, "ext": f.get("ext"),
        "height": f.get("height"), "width": f.get("width"), "fps": f.get("fps"),
        "vcodec": vc, "acodec": ac, "tbr": f.get("tbr"), "abr": f.get("abr"), "vbr": f.get("vbr"),
        "asr": f.get("asr"), "size": size, "approx": approx, "note": f.get("format_note") or "",
        "proto": f.get("protocol") or "", "lang": f.get("language"),
        "hdr": (f.get("dynamic_range") or "SDR") != "SDR",
    }


def gallery_info(url, site):
    return {"type": "gallery", "title": site, "site": site, "url": url, "method": "المحرك الثاني (gallery-dl)"}


def get_info(req):
    flags = {}
    url = (req.get("url") or "").strip()
    for _ in range(5):
        try:
            info = _get_info({**req, **flags})
            if info.get("type") == "playlist" and not info.get("count") and gallery_site(url):
                return gallery_info(url, gallery_site(url))
            info["method"] = method_label(flags)
            if flags.get("_use_proxy"):
                info["via_proxy"] = True
            if flags.get("_use_cookies"):
                info["via_cookies"] = True
            return info
        except Exception as e:  # noqa: BLE001
            if "_impersonate" not in flags and IMPERSONATE and is_block_error(e):
                log("[impersonate] info blocked, retrying as a real browser")
                flags["_impersonate"] = True
            elif "_use_cookies" not in flags and is_auth_error(e) and ext_cookies_for(url):
                log("[cookies] info needs login, retrying with the browser's cookies")
                flags["_use_cookies"] = True
            elif is_no_media(e) and gallery_site(url):
                return gallery_info(url, gallery_site(url))
            elif "_use_proxy" not in flags and proxy_fallback_on() and is_network_error(e):
                log(f"[proxy] info failed directly ({str(e)[:120]}), retrying through proxy")
                flags["_use_proxy"] = True
            else:
                raise
    raise RuntimeError("تعذّر قراءة الرابط.")


def _get_info(req):
    url = (req.get("url") or "").strip()
    if not url:
        raise ValueError("ضع رابطاً أولاً.")
    opts = base_opts(req)
    opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)

        if info.get("_type") == "playlist" or info.get("entries") is not None:
            entries, done = [], 0
            for e in info.get("entries") or []:
                if not e:
                    continue
                seen = archive_key(ydl, e) in ARCHIVE
                done += seen
                entries.append({"title": e.get("title") or e.get("id") or "—", "duration": e.get("duration"),
                                "url": e.get("webpage_url") or e.get("url"), "seen": seen})
            return {"type": "playlist", "title": info.get("title") or "قائمة تشغيل",
                    "uploader": info.get("uploader") or info.get("channel"), "site": info.get("extractor_key"),
                    "count": len(entries), "already": done, "entries": entries[:500]}
        key = archive_key(ydl, info)

    duration = info.get("duration")
    rows = [r for r in (fmt_row(f, duration) for f in info.get("formats") or [info]) if r]

    def vkey(r):
        return (r["height"] or 0, r["fps"] or 0, r["tbr"] or 0)

    def akey(r):
        return (r["abr"] or r["tbr"] or 0, r["asr"] or 0)

    return {
        "type": "video",
        "title": info.get("title") or "بدون عنوان",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "site": info.get("extractor_key"),
        "webpage_url": info.get("webpage_url") or url,
        "is_live": bool(info.get("is_live")),
        "live_status": info.get("live_status"),
        "subtitles": sorted(set((info.get("subtitles") or {}).keys()))[:40],
        "previous": last_download(key),
        "formats": {
            "av": sorted([r for r in rows if r["kind"] == "av"], key=vkey, reverse=True),
            "video": sorted([r for r in rows if r["kind"] == "video"], key=vkey, reverse=True),
            "audio": sorted([r for r in rows if r["kind"] == "audio"], key=akey, reverse=True),
        },
    }


# ---------------------------------------------------------------- downloading
class JobLogger:
    def __init__(self, job):
        self.job = job

    def debug(self, msg):
        msg = ANSI.sub("", str(msg))
        with JOBS_LOCK:
            if "has already been recorded in the archive" in msg:
                self.job["skipped"] = self.job.get("skipped", 0) + 1
            m = re.search(r"Downloading item (\d+) of (\d+)", msg)
            if m:
                self.job["item"] = f"{m.group(1)} / {m.group(2)}"

    info = debug

    def warning(self, msg):
        pass

    def error(self, msg):
        with JOBS_LOCK:
            self.job["failed"] = self.job.get("failed", 0) + 1
            errs = self.job.setdefault("errors", [])
            if len(errs) < 5:
                errs.append(clean_err(msg).split("\n")[0][:300])


class HistoryPP(PostProcessor):
    """Runs after each finished file: writes the history entry and the 'already downloaded' archive."""

    def __init__(self, ydl, job):
        super().__init__(ydl)
        self.job = job

    def run(self, info):
        try:
            record_download(self._downloader, info, self.job)
        except Exception as e:  # noqa: BLE001
            log(f"[history] {e}")
        return [], info


def record_download(ydl, info, job):
    fp = info.get("filepath") or info.get("_filename")
    key = archive_key(ydl, info)
    ext = (Path(fp).suffix.lstrip(".") if fp else info.get("ext") or "").lower()
    height = info.get("height") or max((f.get("height") or 0 for f in info.get("requested_formats") or []), default=0)
    if ext in ("mp3", "m4a", "opus", "flac", "wav", "ogg", "aac", "weba") or info.get("vcodec") == "none":
        quality = f"صوت · {ext}"
    else:
        quality = f"{height}p · {ext}" if height else ext
    entry = {
        "key": key, "id": info.get("id"), "title": (job.get("_req") or {}).get("name") or info.get("title") or info.get("id"),
        "url": info.get("webpage_url") or info.get("original_url"), "file": fp, "quality": quality,
        "size": os.path.getsize(fp) if fp and os.path.exists(fp) else None, "date": time.time(),
        "uploader": info.get("uploader") or info.get("channel"), "duration": info.get("duration"),
        "site": info.get("extractor_key"), "source": job.get("source_label") or "", "clip": job.get("clip") or "",
        "method": job.get("method_now") or "مباشر",
    }
    history_add(entry)
    if key:
        job.setdefault("done_keys", []).append(key)  # a paused list resumes after these
    with HISTORY_LOCK:
        if key and key not in ARCHIVE:
            ARCHIVE.add(key)
            append_line(ARCHIVE_FILE, key)
    if key and job.get("sub_id"):
        append_line(sub_archive_path(job["sub_id"]), key)
    with JOBS_LOCK:
        job["file"] = fp
        job["done_count"] = job.get("done_count", 0) + 1
        if not job.get("many") and info.get("title"):
            job["title"] = info["title"]


def build_download_opts(req, job, logger):
    opts = base_opts(req, logger)
    outdir = outdir_of(req)
    job["outdir"] = str(outdir)
    opts["paths"] = {"home": str(outdir)}
    opts["continuedl"] = True
    opts["overwrites"] = False
    limit = int(SETTINGS.get("rate_limit_kb") or 0)
    if limit > 0:
        opts["ratelimit"] = limit * 1024  # (aria2's many connections would go around the limit: not used then)
    elif accel_on(opts.get("proxy"), req) and not req.get("_impersonate"):  # aria2 can't present itself as a browser
        opts["external_downloader"] = {"http": ARIA2C, "https": ARIA2C}
        opts["concurrent_fragment_downloads"] = max(4, min(16, int(SETTINGS.get("accel_conns") or 8)))

    container = req.get("container") or "mp4"          # mp4 | mkv | original
    audio_conv = req.get("audio_convert") or "original"  # original|mp3|m4a|opus|flac|wav
    audio_q = str(req.get("audio_quality") or "0")       # 0 = best VBR, or kbps
    want_subs = bool(req.get("subs"))
    want_meta = req.get("meta", True)
    pps = []

    if req.get("mode") == "preset":
        preset = str(req.get("preset") or "best")
        if preset == "audio":
            fmt, kind = "ba/b", "audio"
            if audio_conv == "original":
                audio_conv = "mp3"
        else:
            h = "" if preset == "best" else f"[height<={int(preset)}]"
            fmt, kind = f"bv*{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/bv*+ba/b", "av"
    else:
        fid = str(req.get("format_id"))
        kind = req.get("kind") or "av"
        if kind == "video":
            fmt = f"{fid}+ba[ext=m4a]/{fid}+ba" if container == "mp4" else f"{fid}+ba"
        else:
            fmt = fid
    opts["format"] = fmt

    # --- clip (feature: download only part of the video)
    start, end = parse_time(req.get("clip_start")), parse_time(req.get("clip_end"))
    clip_suffix = ""
    if start is not None or end is not None:
        if req.get("playlist"):
            raise ValueError("قص جزء متاح للفيديو الواحد فقط، وليس للقوائم.")
        start = start or 0.0
        if end is not None and end <= start:
            raise ValueError("وقت النهاية يجب أن يكون بعد وقت البداية.")
        if not FFMPEG:
            raise ValueError("قص الفيديو يحتاج ffmpeg.")
        opts["download_ranges"] = download_range_func(None, [(start, end if end is not None else float("inf"))])
        opts["force_keyframes_at_cuts"] = bool(req.get("clip_precise"))
        job["clip"] = f"{fmt_hms(start)} → {fmt_hms(end) if end is not None else 'النهاية'}"
        clip_suffix = f" (مقطع {fmt_hms(start)}-{fmt_hms(end) if end is not None else 'end'})"

    # --- file names
    quality_part = "" if kind == "audio" else "%(height& {}p|)s"
    name = f"%(title).150B [%(id)s]{quality_part}{clip_suffix}.%(ext)s"
    if SETTINGS.get("name_tpl"):
        name = tpl_to_ytdlp(SETTINGS["name_tpl"], quality_part) + f"{clip_suffix}.%(ext)s"
    if req.get("sub_id"):
        folder = safe_name(req.get("folder") or "Channel")
        opts["outtmpl"] = {"default": f"{folder}/%(upload_date>%Y-%m-%d&{{}} - |)s{name}"}
    elif req.get("playlist"):
        # a whole list -> "<list name>/007 - title"
        opts["outtmpl"] = {"default": f"%(playlist_title,playlist_id|Playlist)s/%(playlist_index&{{:03d}} - |)s{name}"}
    elif req.get("name"):
        # a stream caught on a web page: name it after the page, not after "master.m3u8"
        import hashlib
        tag = hashlib.sha1(req["url"].encode()).hexdigest()[:6]
        opts["outtmpl"] = {"default": f"{safe_name(req['name'], 120)} [{tag}]{quality_part}{clip_suffix}.%(ext)s"}
    else:
        # never start the template with a field that can be empty followed by "/" — that makes an absolute path
        folder = tpl_to_ytdlp(SETTINGS["folder_tpl"], quality_part, folder=True) if SETTINGS.get("folder_tpl") else ""
        opts["outtmpl"] = {"default": f"{folder}/{name}" if folder else name}
    if req.get("split_chapters") and FFMPEG:
        # one file per chapter (the whole video is kept too), in a folder next to it
        pps.append({"key": "FFmpegSplitChapters", "force_keyframes": True})  # cut exactly at the chapter times
        opts["outtmpl"]["chapter"] = "%(title).120B - chapters/%(section_number)03d - %(section_title).100B.%(ext)s"

    if req.get("live") in ("now", "start"):
        opts["live_from_start"] = req["live"] == "start"
        opts["wait_for_video"] = (15, 60)  # an upcoming stream: wait until it starts
        job["live"] = True
    if req.get("playlist") or req.get("sub_id") or req.get("batch"):
        opts["ignoreerrors"] = "only_download"  # a private/removed item must not stop the rest
    if req.get("playlistend"):
        opts["playlistend"] = int(req["playlistend"])
    if req.get("sub_id"):
        opts["match_filter"] = match_filter_func("!is_live & live_status!=?is_upcoming & live_status!=?is_live")

    # --- skip what was downloaded before
    if req.get("skip") or req.get("_done_keys"):
        archive = set(ARCHIVE) if req.get("skip") else set()
        if req.get("sub_id"):
            archive |= read_set(sub_archive_path(req["sub_id"]))
        archive |= set(req.get("_done_keys") or ())  # a paused list continues after what it already finished
        opts["download_archive"] = archive  # a set: yt-dlp checks it but we write the files ourselves
    if req.get("_break_existing"):
        # a channel, newest first: read until the first video already downloaded, however many are new
        opts["break_on_existing"] = True
        opts["break_per_url"] = True

    final_ext = None
    if kind == "audio":
        if audio_conv != "original":
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": audio_conv,
                        "preferredquality": audio_q, "nopostoverwrites": False})
            final_ext = audio_conv
    else:
        if container in ("mp4", "mkv"):
            opts["merge_output_format"] = container
            pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})
            final_ext = container
        if want_subs:
            mode = req.get("sub_mode") or "both"  # embed | file | both
            opts.update({
                "writesubtitles": True, "writeautomaticsub": bool(req.get("sub_auto")),
                "subtitleslangs": sub_langs(req.get("sub_langs")),
                "subtitlesformat": "srt/vtt/best",
            })
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
            if final_ext in ("mp4", "mkv") and mode in ("embed", "both"):
                # already_have_subtitle=True keeps the .srt files next to the video ("both")
                pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": mode == "both"})

    if want_meta:
        pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
        if final_ext in ("mp4", "mkv", "mp3", "m4a", "flac", "opus"):
            opts["writethumbnail"] = True
            pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    if not FFMPEG:
        opts["format"] = fmt if kind != "video" else "b"
        pps = []
    opts["postprocessors"] = pps
    return opts


def gallery_cmd():
    return [sys.executable, "--gallery-dl"] if FROZEN else [sys.executable, str(Path(__file__).resolve()), "--gallery-dl"]


def run_gallery_job(job, req, flags):
    """Download a gallery / multi-file post with gallery-dl in a child process; files are reported as they land."""
    if not gallery_dl:
        raise RuntimeError("المحرك الثاني (gallery-dl) غير متوفر في هذه النسخة.")
    url = req["url"]
    outdir = outdir_of(req)
    job["outdir"] = str(outdir)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    out, logf = TMP_DIR / f"gdl-{job['id']}.out", TMP_DIR / f"gdl-{job['id']}.log"
    for f in (out, logf):
        f.unlink(missing_ok=True)
    args = gallery_cmd() + ["-o", "output.mode=pipe", "-o", "output.progress=false", "--write-log", str(logf),
                            "-d", str(outdir)]
    ck = cookie_file_for(url) if (flags.get("_use_cookies") or SETTINGS.get("ext_cookies") == "always") else None
    browser = str(SETTINGS.get("cookies") or "")
    if ck:
        args += ["--cookies", ck]
    elif browser == "file" and COOKIE_FILE.exists():
        args += ["--cookies", str(COOKIE_FILE)]
    elif browser and browser != "file":
        args += ["--cookies-from-browser", browser]
    if proxy_value() and (flags.get("_use_proxy") or SETTINGS.get("proxy_mode") == "always"):
        args += ["--proxy", proxy_value()]
    ua = (req.get("headers") or {}).get("User-Agent")
    if ua:
        args += ["--user-agent", ua]
    args += ["--", url]
    env = os.environ.copy()
    env["MO_GDL_OUT"] = str(out)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, creationflags=0x08000000 if SYSTEM == "Windows" else 0)
    files, skipped, pos = [], 0, 0
    with JOBS_LOCK:
        job.update(status="downloading", stage="جارٍ التنزيل بالمحرك الثاني (gallery-dl)…", percent=None, gallery=True)

    def read_new():
        nonlocal pos, skipped
        if not out.exists():
            return
        with open(out, encoding="utf-8", errors="replace") as fh:
            fh.seek(pos)
            chunk = fh.read()
            pos = fh.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if line.startswith("# "):
                skipped += 1
            elif line and os.path.sep in line and os.path.isfile(line):
                files.append(line)
        with JOBS_LOCK:
            job.update(done_count=len(files), skipped=skipped,
                       stage=f"جارٍ التنزيل بالمحرك الثاني · {len(files)} ملف" + (f" (تُخطّي {skipped})" if skipped else ""))
            if files:
                job["file"] = files[-1]
                job["title"] = Path(files[0]).parent.name or job["title"]
    try:
        while proc.poll() is None:
            if job.get("cancel"):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
                raise Cancelled()
            read_new()
            time.sleep(0.4)
        read_new()
    finally:
        if proc.poll() is None:
            proc.kill()
    errors = []
    if logf.exists():
        for line in logf.read_text(encoding="utf-8", errors="replace").splitlines():
            if "][error]" in line or "][warning] Unable" in line:
                errors.append(re.sub(r"^\[[^\]]*\]\[(error|warning)\]\s*", "", line.strip())[:300])
    for f in (out, logf):
        f.unlink(missing_ok=True)
    with JOBS_LOCK:
        job["errors"] = errors[-5:]
        if errors:
            job["failed"] = len(errors)
    if not files and not skipped:
        with JOBS_LOCK:
            job["errors"] = (errors or ["لم يُعثر على ملفات في هذا الرابط."])[-5:]
        return 1
    if files:
        folder = str(Path(files[0]).parent)
        entry = {"key": None, "id": None, "title": Path(folder).name, "url": url, "file": files[0],
                 "quality": f"معرض · {len(files)} ملف", "size": sum(os.path.getsize(f) for f in files if os.path.exists(f)),
                 "date": time.time(), "uploader": None, "duration": None, "site": gallery_site(url) or site_of(url),
                 "source": job.get("source_label") or "", "clip": "", "method": job.get("method_now") or "gallery-dl"}
        history_add(entry)
        with JOBS_LOCK:
            job["outdir"] = folder
    return 0 if proc.returncode == 0 or files else proc.returncode


# ---------------------------------------------------------------- live recording: stop and keep what was recorded
LIVE_PROCS = {}  # job thread -> the ffmpeg process recording a live stream


def _install_live_stop():
    """yt-dlp records most live streams with ffmpeg; remember that process so 'stop' can ask ffmpeg to finish
    the file properly (it then stays playable) instead of killing it."""
    from yt_dlp.downloader import external as ext_mod
    base = ext_mod.Popen

    class TrackedPopen(base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            LIVE_PROCS[threading.get_ident()] = self

        def __exit__(self, *exc):
            if LIVE_PROCS.get(threading.get_ident()) is self:
                LIVE_PROCS.pop(threading.get_ident(), None)
            return super().__exit__(*exc)
    ext_mod.Popen = TrackedPopen


def stop_live(job):
    job["stop_live"] = True
    proc = LIVE_PROCS.get(job.get("_thread"))
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            job["_stop_sent"] = True  # ffmpeg finishes the file itself; yt-dlp then saves it normally
            return True
        except Exception as e:  # noqa: BLE001
            log(f"[live] could not ask ffmpeg to stop: {e}")
    return False


def next_step(job, req, flags, failed_text, empty, alts):
    """Decide the next way to try after a failure. Order: without the accelerator, as a real browser, with the
    user's login, the gallery engine, a stream caught on the page, through the proxy."""
    url = req["url"]
    kind = req.get("kind")
    if job.get("done_count") and not job.get("many"):
        return None, None, None  # something already came out: report the rest as notes, don't start over
    if "_no_accel" not in flags and aria2_failed(failed_text):
        return "_no_accel", True, "تعذّر التسريع على هذا الجهاز — إكمال التنزيل بالطريقة العادية…"
    if kind != "gallery" and "_impersonate" not in flags and IMPERSONATE and is_block_error(failed_text):
        return "_impersonate", True, "الموقع رفض الاتصال — إعادة المحاولة بانتحال بصمة متصفح حقيقي…"
    still_refused = is_block_error(failed_text) and ("_impersonate" in flags or not IMPERSONATE)
    if "_use_cookies" not in flags and (is_auth_error(failed_text) or still_refused) and ext_cookies_for(url):
        return "_use_cookies", True, "الموقع يطلب تسجيل الدخول — إعادة المحاولة بكوكيز المتصفح…"
    if (kind not in ("file", "gallery") and "_gallery" not in flags and not flags.get("_alt_stream")
            and (empty or is_no_media(failed_text) or is_block_error(failed_text)) and gallery_site(url)):
        return "_gallery", True, "لا يوجد فيديو يفهمه المحرك الأول — المحاولة بالمحرك الثاني (gallery-dl)…"
    if kind not in ("file",) and alts:
        return "_alt_stream", alts.pop(0), "إعادة المحاولة بالبث الذي التُقط من الصفحة…"
    if "_use_proxy" not in flags and proxy_fallback_on() and is_network_error(failed_text):
        return "_use_proxy", True, "فشل الاتصال المباشر — إعادة المحاولة عبر البروكسي…"
    return None, None, None


def run_job(job_id):
    job = JOBS[job_id]
    req = job["_req"]

    def check_cancel():
        if job.get("cancel"):
            if job.get("pause") and job.get("_pause_at") and time.time() - job["_pause_at"] < 20:
                return  # pausing: let the pieces already in flight finish, so they are kept for the resume
            if job.get("pause"):
                job["_pause_at"] = time.time()
            raise Cancelled()

    def progress_hook(d):
        check_cancel()
        if job.get("stop_live") and not job.get("_stop_sent") and threading.get_ident() == job.get("_thread"):
            job["_stop_sent"] = True
            raise KeyboardInterrupt  # yt-dlp keeps the fragments it has for live streams and finishes the file
        info = d.get("info_dict") or {}
        with JOBS_LOCK:
            if info.get("title"):
                job["current"] = info["title"]
                if not job.get("many"):
                    job["title"] = info["title"]
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                job.update(status="downloading", stage="جارٍ تسجيل البث المباشر" if job.get("live") else "جارٍ التنزيل",
                           downloaded=done, total=total,
                           speed=d.get("speed"), eta=d.get("eta"),
                           percent=(done / total * 100) if total else None)
            elif d["status"] == "finished":
                job.update(stage="جارٍ المعالجة", percent=100, speed=None, eta=None)

    def pp_hook(d):
        check_cancel()
        names = {
            "Merger": "دمج الصورة والصوت", "FFmpegMerger": "دمج الصورة والصوت",
            "FFmpegExtractAudio": "تحويل الصوت", "FFmpegVideoRemuxer": "تغيير الحاوية",
            "EmbedThumbnail": "تضمين الصورة المصغّرة", "FFmpegMetadata": "إضافة البيانات",
            "FFmpegEmbedSubtitle": "تضمين الترجمة", "FFmpegSubtitlesConvertor": "تحويل الترجمة",
        }
        with JOBS_LOCK:
            if d["status"] == "started" and d.get("postprocessor") in names:
                job.update(status="processing", stage=names[d["postprocessor"]])

    try:
        flags, crash, code = {}, None, 0
        job["_thread"] = threading.get_ident()
        TMP_COOKIE_JOBS[job["_thread"]] = job
        if req.get("tool"):
            run_tool_job(job, req)
            return
        attempts = job.setdefault("attempts", [])
        alts = [a for a in (req.get("alt_streams") or []) if a.get("url") and a["url"] != req["url"]][:3]
        if req.get("live"):
            with JOBS_LOCK:
                job.update(status="downloading", percent=None,
                           stage="جارٍ تسجيل البث المباشر…" if req["live"] == "now" else "جارٍ تسجيل البث من بدايته…")
        for attempt in range(8):
            attempt_req = {**req, **{k: v for k, v in flags.items() if k != "_alt_stream"}}
            if flags.get("_alt_stream"):
                a = flags["_alt_stream"]
                attempt_req.update(url=a["url"], headers={**(req.get("headers") or {}), **(a.get("headers") or {})},
                                   name=req.get("title") or a.get("title") or "video", playlist=False, kind="media")
            if flags and job.get("many"):
                attempt_req["skip"] = True  # only what failed; finished items are in the archive
            logger = JobLogger(job)
            crash = None
            job["method_now"] = method_label(flags)
            try:
                if req.get("kind") == "file":
                    code = run_file_job(job, attempt_req, flags)
                elif req.get("kind") == "gallery" or flags.get("_gallery"):
                    code = run_gallery_job(job, attempt_req, flags)
                else:
                    opts = build_download_opts(attempt_req, job, logger)
                    opts["progress_hooks"] = [progress_hook]
                    opts["postprocessor_hooks"] = [pp_hook]
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.add_post_processor(HistoryPP(ydl, job), when="after_move")
                        code = ydl.download([attempt_req["url"], *(attempt_req.get("extra_urls") or [])])
            except Cancelled:
                raise
            except KeyboardInterrupt:
                if not job.get("stop_live"):
                    raise
                code = 0  # live recording stopped by the user
            except Exception as e:  # noqa: BLE001
                if job.get("cancel") or "Cancelled" in type(e).__name__:
                    raise Cancelled() from e
                code, crash = 1, e
            # finished without error but nothing came out (e.g. a page with no media): treat as a failure
            empty = (not code and not crash and not job.get("failed") and not job.get("done_count")
                     and not job.get("skipped") and not req.get("sub_id") and not job.get("stop_live")
                     and req.get("kind") != "file")
            if not code and not job.get("failed") and not empty:
                break
            failed_text = " ".join(job.get("errors") or []) + " " + str(crash or "") + (" no media found" if empty else "")
            attempts.append({"method": job["method_now"], "error": ANSI.sub("", failed_text).strip()[:300]})
            if job.get("stop_live"):
                break
            key, val, stage = next_step(job, req, flags, failed_text, empty, alts)
            if not key:
                if empty:
                    code = 1
                    with JOBS_LOCK:
                        job["errors"] = ["لم يُعثر على فيديو أو ملفات في هذا الرابط."]
                break
            if key == "_alt_stream":
                flags.pop("_gallery", None)
            flags[key] = val
            log(f"[retry] job {job_id}: {stage}")
            with JOBS_LOCK:
                job.update(status="starting", stage=stage, failed=0, errors=[], percent=0, speed=None, eta=None,
                           done_count=0, skipped=0)
        job["method"] = method_label(flags)
        if flags.get("_use_proxy") or (proxy_value() and SETTINGS.get("proxy_mode") == "always"):
            job["via_proxy"] = True
        if flags.get("_use_cookies"):
            job["via_cookies"] = True
        with JOBS_LOCK:
            n, sk, fl = job.get("done_count", 0), job.get("skipped", 0), job.get("failed", 0)
            many = req.get("playlist") or req.get("sub_id")
            if crash is not None and not n:
                job.update(status="error", stage="فشل", error=clean_err(crash), speed=None, eta=None)
            elif code and not n and not sk:
                job.update(status="error", stage="فشل",
                           error=clean_err("\n".join(job.get("errors") or ["تعذّر التنزيل."])))
            elif code or fl:
                job.update(status="done", stage="اكتمل مع ملاحظات", percent=100, speed=None, eta=None,
                           error="تعذّر تنزيل بعض العناصر (قد تكون خاصة أو محذوفة أو تتطلب تسجيل دخول):\n"
                                 + "\n".join(job.get("errors") or []))
            elif not n and sk:
                job.update(status="done", stage="لا جديد — كل العناصر نُزّلت سابقاً" if many else "نُزّل سابقاً — تم التخطّي",
                           percent=100, speed=None, eta=None)
            elif job.get("stop_live"):
                job.update(status="done", stage="أُوقف التسجيل وحُفظ ✓", percent=100, speed=None, eta=None)
            else:
                job.update(status="done", stage="اكتمل ✓", percent=100, speed=None, eta=None)
        if job["status"] == "done" and job.get("file") and not job.get("many"):
            after_download(job, req)
    except Cancelled:
        with JOBS_LOCK:
            if job.get("pause"):
                job.update(status=PAUSED, stage="متوقف مؤقتاً", speed=None, eta=None)
            else:
                job.update(status="cancelled", stage="أُلغي", speed=None, eta=None)
    except Exception as e:  # noqa: BLE001
        with JOBS_LOCK:
            if job.get("pause"):
                job.update(status=PAUSED, stage="متوقف مؤقتاً", speed=None, eta=None)
            elif job.get("cancel") or "Cancelled" in type(e).__name__:
                job.update(status="cancelled", stage="أُلغي", speed=None, eta=None)
            else:
                job.update(status="error", stage="فشل", error=clean_err(e), speed=None, eta=None)
    finally:
        job["finished"] = time.time()
        TMP_COOKIE_JOBS.pop(job.get("_thread"), None)
        drop_tmp_files(job)
        if req.get("sub_id") and job["status"] != PAUSED:
            sub_finished(req["sub_id"], job)
        job_notify(job)
        with QUEUE:
            QUEUE.notify_all()


# ================================================================ 2.7: tools, subtitles, notifications, …
def sub_langs(v):
    """'ar, en' or a list -> yt-dlp languages (each with its regional variants: en -> en, en-.*)."""
    items = v if isinstance(v, list) else re.split(r"[\s,،]+", str(v or ""))
    out = []
    for x in (i.strip() for i in items):
        if not x:
            continue
        if x in ("all", ".*"):
            return ["all"]
        out += [x] if ("*" in x or "-" in x) else [x, f"{x}-.*"]
    return out or ["ar", "ar-.*", "en", "en-.*"]


TPL_FIELDS = {
    "title": "%(title).150B", "id": "%(id)s", "uploader": "%(uploader,channel,uploader_id|unknown)s",
    "channel": "%(channel,uploader,uploader_id|unknown)s", "site": "%(extractor_key|site)s",
    "date": "%(upload_date>%Y-%m-%d|no-date)s", "year": "%(upload_date>%Y|no-date)s",
    "month": "%(upload_date>%Y-%m|no-date)s", "playlist": "%(playlist_title,playlist_id|no-playlist)s",
    "index": "%(playlist_index|0)03d", "resolution": "%(height|0)sp", "ext": "%(ext)s",
}


def tpl_to_ytdlp(tpl, quality_part="", folder=False):
    """User template ({title}, {uploader}, {site}, {date}, …) -> yt-dlp output template."""
    tpl = str(tpl or "").strip().replace("\\", "/").strip("/")
    tpl = tpl.replace("%", "%%")

    def field(m):
        k = m.group(1).lower()
        if k == "quality":
            return quality_part.strip() if not folder else "%(height|0)sp"
        return TPL_FIELDS.get(k, m.group(0))
    out = re.sub(r"\{(\w+)\}", field, tpl)
    out = "/".join(x.strip() for x in out.split("/") if x.strip() and x.strip() not in (".", ".."))
    return out


def tpl_check(tpl, folder=False):
    t = str(tpl or "").strip()
    if not t:
        return ""
    bad = [k for k in re.findall(r"\{(\w+)\}", t) if k.lower() not in TPL_FIELDS and k.lower() != "quality"]
    if bad:
        raise ValueError("رموز غير معروفة في القالب: " + "، ".join("{" + b + "}" for b in bad))
    if not folder and not re.search(r"\{(title|id)\}", t, re.I):
        raise ValueError("قالب اسم الملف يجب أن يحتوي {title} أو {id} حتى لا تتشابه أسماء الملفات.")
    if re.search(r'[<>:"|?*]', re.sub(r"\{\w+\}", "", t)):
        raise ValueError('القالب يحتوي رموزاً غير مسموحة في أسماء الملفات: < > : " | ? *')
    return t


def tpl_example(name_tpl, folder_tpl):
    ex = {"title": "عنوان الفيديو", "id": "dQw4w9WgXcQ", "uploader": "اسم القناة", "channel": "اسم القناة",
          "site": "Youtube", "date": "2026-11-24", "year": "2026", "month": "2026-11", "playlist": "قائمة التشغيل",
          "index": "007", "resolution": "1080p", "quality": "1080p", "ext": "mp4"}

    ex = {k: T(v) for k, v in ex.items()}

    def fill(t):
        return re.sub(r"\{(\w+)\}", lambda m: ex.get(m.group(1).lower(), m.group(0)), t)
    name = fill(name_tpl) if name_tpl else T("عنوان الفيديو") + " [dQw4w9WgXcQ] 1080p"
    return (fill(folder_tpl).strip("/") + "/" if folder_tpl else "") + name + ".mp4"


# ---------------------------------------------------------------- tools: change files that are already on disk
TOOL_PRESETS = {  # ready-made targets
    "mobile": {"label": "للجوال (720p)", "height": 720, "crf": 26, "abr": "128k"},
    "tv": {"label": "للتلفاز (1080p)", "height": 1080, "crf": 21, "abr": "192k"},
}
AUDIO_CODECS = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "m4a": ["-c:a", "aac", "-b:a", "192k"],
                "opus": ["-c:a", "libopus", "-b:a", "128k"], "flac": ["-c:a", "flac"], "wav": ["-c:a", "pcm_s16le"]}


def probe_media(path):
    """Duration, video height and chapters of a file (from ffmpeg's own report: no ffprobe in the bundle)."""
    import subprocess
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    t = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", t)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    hm = re.search(r"Stream #\S+.*Video:.*?(\d{2,5})x(\d{2,5})", t)
    chapters = []
    for cm in re.finditer(r"Chapter #\S+ start ([\d.]+), end ([\d.]+)\s*\n(?:\s*Metadata:\s*\n\s*title\s*:\s*(.*)\n)?", t):
        chapters.append({"start": float(cm.group(1)), "end": float(cm.group(2)),
                         "title": (cm.group(3) or "").strip() or f"Chapter {len(chapters) + 1}"})
    return {"duration": dur, "height": int(hm.group(2)) if hm else 0, "video": bool(hm), "chapters": chapters}


def _free_name(p):
    p = Path(p)
    n = 2
    while p.exists():
        p = p.with_name(f"{re.sub(r' \(\d+\)$', '', p.stem)} ({n}){p.suffix}")
        n += 1
    return p


def tool_plan(req):
    """What a tool job will do: (description, list of (ffmpeg args, output file), duration)."""
    src = Path(str(req.get("input") or ""))
    if not src.is_file():
        raise ValueError("الملف غير موجود — اختر ملفاً من جهازك أو من السجل.")
    if not FFMPEG:
        raise ValueError("الأدوات تحتاج ffmpeg.")
    info = probe_media(src)
    op = req.get("tool")
    stem, d = src.stem, src.parent
    if op == "compress":
        mb = float(req.get("target_mb") or 0)
        if mb <= 0 or not info["duration"]:
            raise ValueError("اكتب الحجم المطلوب بالميغابايت (والملف يجب أن يكون فيديو أو صوتاً له مدة).")
        total_kbps = mb * 8192 * 0.95 / info["duration"]
        a_kbps = 96 if total_kbps < 600 else 128
        v_kbps = int(total_kbps - a_kbps)
        if info["video"] and v_kbps < 80:
            raise ValueError(f"الحجم صغير جداً لمدة هذا الفيديو — أقل حجم معقول تقريباً {int((80 + a_kbps) * info['duration'] / 8192 / 0.95) + 1} ميغابايت.")
        out = _free_name(d / f"{stem} ({int(mb) if mb == int(mb) else mb}MB).mp4")
        args = (["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.3)}k",
                 "-bufsize", f"{v_kbps * 2}k"] if info["video"] else ["-vn"]) + ["-c:a", "aac", "-b:a", f"{a_kbps}k"]
        if not info["video"]:
            out = out.with_suffix(".m4a")
        return f"ضغط إلى {mb:g} ميغابايت", [(args + ["-movflags", "+faststart"], out)], info["duration"]
    if op in ("resize", "preset"):
        if not info["video"]:
            raise ValueError("هذا الملف ليس فيديو.")
        pr = TOOL_PRESETS.get(req.get("preset")) if op == "preset" else None
        h = int(pr["height"] if pr else req.get("height") or 0)
        if h < 144:
            raise ValueError("اختر الدقة.")
        crf, abr = (pr["crf"], pr["abr"]) if pr else (23, "160k")
        label = pr["label"] if pr else f"{h}p"
        out = _free_name(d / f"{stem} ({label}).mp4")
        args = ["-vf", f"scale=-2:'min({h},ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", abr, "-movflags", "+faststart"]
        return ("تحويل " + label), [(args, out)], info["duration"]
    if op == "audio":
        fmt = req.get("format") if req.get("format") in AUDIO_CODECS else "mp3"
        out = _free_name(d / f"{stem}.{fmt}")
        return f"استخراج الصوت ({fmt.upper()})", [(["-vn", *AUDIO_CODECS[fmt]], out)], info["duration"]
    if op == "chapters":
        ch = info["chapters"]
        if len(ch) < 2:
            raise ValueError("لا توجد فصول في هذا الملف.")
        folder = d / f"{stem} - chapters"
        steps = []
        # cut exactly at the chapter times: a plain copy could only cut at the video's key frames (seconds off);
        # audio-only files are copied as they are (audio frames are tiny, so the cut is exact anyway)
        enc = (["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"] if info["video"]
               else ["-map", "0:a", "-c", "copy"])
        ext = ".mp4" if info["video"] else src.suffix
        for i, c in enumerate(ch, 1):
            out = folder / f"{i:03d} - {safe_name(c['title'], 100)}{ext}"
            steps.append((["-ss", f"{c['start']:.3f}", "-t", f"{c['end'] - c['start']:.3f}", *enc], out))
        return f"تقسيم إلى {len(ch)} فصول", steps, info["duration"]
    raise ValueError("أداة غير معروفة.")


def run_tool_job(job, req):
    import subprocess
    desc, steps, dur = tool_plan(req)
    src = Path(req["input"])
    with JOBS_LOCK:
        job.update(status="processing", stage=desc + "…", percent=0, title=src.name, outdir=str(src.parent))
    done_parts, total = 0, max(len(steps), 1)
    for args, out in steps:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".part" + out.suffix)
        seek = args[:4] if args[:1] == ["-ss"] else []
        rest = args[4:] if seek else args
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1", *seek, "-i", str(src), *rest, str(tmp)]
        piece = float(args[3]) if seek else dur
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        job["_proc"] = proc
        err_tail = []
        threading.Thread(target=lambda: err_tail.extend(proc.stderr.read().splitlines()[-15:]), daemon=True).start()
        for line in proc.stdout:
            if job.get("cancel"):
                proc.kill()
                proc.wait()
                tmp.unlink(missing_ok=True)
                raise Cancelled()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    t = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = min(1.0, t / piece) if piece else 0
                with JOBS_LOCK:
                    job["percent"] = (done_parts + frac) / total * 100
        code = proc.wait()
        if code != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise RuntimeError("فشل ffmpeg: " + " ".join(err_tail[-3:]))
        tmp.replace(out)
        done_parts += 1
        history_add({"key": None, "title": out.stem, "file": str(out), "quality": desc,
                     "size": out.stat().st_size, "date": time.time(), "source": "أدوات", "url": None})
    with JOBS_LOCK:
        first = steps[0][1]
        job.update(status="done", stage=desc + " ✓", percent=100, file=str(first) if len(steps) == 1 else None,
                   outdir=str(first.parent), done_count=len(steps), many=len(steps) > 1)


def tool_job(req, label=None, source_label="أدوات"):
    desc, steps, _ = tool_plan(req)  # checks the request now, so the page shows the error at once
    r = {**req, "url": str(req["input"]), "title": Path(req["input"]).name}
    return new_job(r, label=label or desc, source="tool", source_label=source_label)


def after_download(job, req):
    """What the user asked to happen once a single download is finished: a conversion, 3D subtitles."""
    f = job.get("file")
    if not f or not os.path.exists(f):
        return
    after = req.get("after") or {}
    if after.get("tool"):
        try:
            tool_job({**after, "input": f}, source_label="بعد التنزيل")
        except Exception as e:  # noqa: BLE001
            log(f"[after] {e}")
    if req.get("sub3d") and SETTINGS.get("sub3d_cmd"):
        run_sub3d(f)


def run_sub3d(video):
    """The user's own 3D subtitle tool, run on each .srt saved next to the video."""
    import shlex
    import subprocess
    v = Path(video)
    for srt in sorted(v.parent.glob(glob_escape(v.stem) + "*.srt")):
        cmd = SETTINGS["sub3d_cmd"].replace("{file}", f'"{srt}"')
        try:
            r = subprocess.run(cmd if SYSTEM == "Windows" else shlex.split(cmd), capture_output=True, text=True,
                               timeout=600, creationflags=NO_WINDOW, cwd=str(v.parent))
            log(f"[3d] {srt.name}: exit {r.returncode} {(r.stderr or '')[-200:]}")
        except Exception as e:  # noqa: BLE001
            log(f"[3d] {srt.name}: {e}")


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# ---------------------------------------------------------------- notifications on the computer
def notify(title, body):
    import subprocess
    log(f"[notify] {title} — {body}")
    if not SETTINGS.get("notify", True) or SELFTEST:
        return
    try:
        if SYSTEM == "Windows":
            tray = UI.get("tray")
            if tray is not None:
                tray.notify(body[:250], title[:60])
        elif SYSTEM == "Darwin":
            esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            subprocess.Popen(["osascript", "-e", f'display notification "{esc(body[:250])}" with title "{esc(title[:60])}"'])
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, body])
    except Exception as e:  # noqa: BLE001
        log(f"[notify] not shown: {e}")


def job_notify(job):
    st = job.get("status")
    if st not in ("done", "error") or job.get("_notified"):
        return
    job["_notified"] = True
    if st == "done" and job.get("sub_id") and not job.get("done_count"):
        return  # a channel check with nothing new
    name = job.get("title") or job.get("url") or ""
    if st == "error":
        notify(T("تعذّر التنزيل"), f"{name}\n{T((job.get('error') or '').splitlines()[0] if job.get('error') else '')}")
    elif job.get("many") and job.get("done_count"):
        notify(T("اكتمل التنزيل ✓"), f"{name} — {job['done_count']} " + T("ملف"))
    else:
        notify(T("اكتمل التنزيل ✓"), name)


# ---------------------------------------------------------------- watching the clipboard for video links
CLIP = {"last": None, "seq": None, "suggest": None}


def read_clipboard():
    import subprocess
    test = os.environ.get("MO_CLIP_FILE")
    if test:
        try:
            return Path(test).read_text(encoding="utf-8")
        except OSError:
            return ""
    if SYSTEM == "Windows":
        import ctypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        seq = u.GetClipboardSequenceNumber()
        if seq == CLIP["seq"]:
            return CLIP["last"] or ""
        CLIP["seq"] = seq
        u.GetClipboardData.restype = ctypes.c_void_p
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        if not u.OpenClipboard(None):
            return CLIP["last"] or ""
        try:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            ptr = k.GlobalLock(h)
            try:
                return ctypes.wstring_at(ptr) if ptr else ""
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    cmds = [["pbpaste"]] if SYSTEM == "Darwin" else [["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"],
                                                    ["xsel", "-ob"]]
    for c in cmds:
        if shutil.which(c[0]):
            try:
                return subprocess.run(c, capture_output=True, text=True, timeout=2).stdout
            except Exception:  # noqa: BLE001
                return ""
    return ""


def link_worth_suggesting(url):
    if re.search(r"\.(mp4|mkv|webm|mov|m4v|mp3|m4a|flac|wav|ogg|opus|m3u8|mpd)(\?|$)", url, re.I):
        return True
    try:
        for ie in yt_dlp.extractor.gen_extractor_classes():
            if ie.ie_key() != "Generic" and ie.suitable(url):
                return True
    except Exception:  # noqa: BLE001
        pass
    site = gallery_site(url)
    return bool(site and site != "directlink")


def clipboard_loop():
    while True:
        time.sleep(1.5)
        if not SETTINGS.get("clipboard"):
            CLIP["last"] = None
            continue
        try:
            text = (read_clipboard() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if text == CLIP["last"]:
            continue
        first = CLIP["last"] is None
        CLIP["last"] = text
        if first and not os.environ.get("MO_CLIP_FILE"):
            continue  # what was already copied before turning this on is not "new"
        m = re.fullmatch(r"https?://\S+", text)
        if m and len(text) < 2000 and link_worth_suggesting(text):
            CLIP["suggest"] = {"url": text, "ts": time.time(), "id": uuid.uuid4().hex[:8]}
            notify(T("رابط فيديو في الحافظة"), text[:120])


# ---------------------------------------------------------------- links sent from a phone on the same network
PHONE = {"server": None, "port": None, "fails": {}, "lock": threading.Lock()}
PHONE_PORT = int(os.environ.get("MD_PHONE_PORT", 8790))


def lan_ip():
    import socket
    s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s_.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the network card used for the LAN
        return s_.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s_.close()


def phone_url():
    return f"http://{lan_ip()}:{PHONE['port']}/m?k={SETTINGS.get('phone_key')}" if PHONE["port"] else ""


def phone_qr_svg(text):
    segno = ensure("segno", required=False)
    if not segno or not text:
        return ""
    import io
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0e3b2e", xmldecl=False, svgns=True)
    return buf.getvalue().decode()


PHONE_PAGE = """<!doctype html><html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MO Downloader</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f1ea;margin:0;padding:18px;color:#1d2a26}}
.c{{background:#fffdf7;border-radius:14px;padding:18px;max-width:520px;margin:auto;box-shadow:0 2px 10px #0001}}
h1{{font-size:20px;margin:0 0 12px}} textarea,input,select,button{{width:100%;box-sizing:border-box;font-size:16px;
padding:11px;border-radius:10px;border:1px solid #cfc8b8;margin:6px 0}} button{{background:#0f6b54;color:#fff;border:0;
font-weight:700}} #m{{margin-top:10px;font-weight:600}}</style></head><body><div class="c">
<h1>MO Downloader</h1><form id="f"><textarea id="u" rows="3" placeholder="https://…" required dir="ltr"></textarea>
<select id="q"><option value="best">{best}</option><option value="1080">1080p</option><option value="720">720p</option>
<option value="audio">{audio}</option></select><input id="p" type="password" inputmode="numeric" placeholder="{pin}" required>
<button>{send}</button></form><div id="m"></div></div><script>
const k=new URLSearchParams(location.search).get("k");p.value=localStorage.getItem("mo-pin")||"";
f.onsubmit=async e=>{{e.preventDefault();m.textContent="…";const r=await fetch("/m/add",{{method:"POST",
headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{k,pin:p.value,url:u.value,preset:q.value}})}});
const j=await r.json().catch(()=>({{}}));if(r.ok){{localStorage.setItem("mo-pin",p.value);u.value="";m.style.color="#0f6b54";
m.textContent="{ok}"}}else{{m.style.color="#a33";m.textContent=j.error||r.status}}}};</script></body></html>"""


class PhoneHandler(BaseHTTPRequestHandler):
    server_version = "MODownloader-phone"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _key_ok(self, k):
        return bool(SETTINGS.get("phone") and SETTINGS.get("phone_key")) and secrets.compare_digest(str(k or ""), SETTINGS["phone_key"])

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/m" or not self._key_ok(parse_qs(u.query).get("k", [""])[0]):
            return self._send(404, b"not found", "text/plain")
        en = SETTINGS.get("lang") == "en"
        page = PHONE_PAGE.format(lang="en" if en else "ar", dir="ltr" if en else "rtl", best=T("أفضل جودة"),
                                 audio=T("صوت MP3"), pin=T("كلمة المرور"), send=T("أرسل إلى الكمبيوتر"),
                                 ok=T("أُرسل ✓ — بدأ التنزيل على الكمبيوتر"))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/m/add":
            return self._send(404, {"error": "not found"})
        ip = self.client_address[0]
        with PHONE["lock"]:
            f = PHONE["fails"].get(ip, [0, 0])
            if f[0] >= 5 and time.time() - f[1] < 300:
                return self._send(429, {"error": T("محاولات خاطئة كثيرة — انتظر 5 دقائق.")})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad request"})
        if not self._key_ok(req.get("k")):
            return self._send(404, {"error": "not found"})
        if not SETTINGS.get("phone_pin") or not secrets.compare_digest(str(req.get("pin") or ""), str(SETTINGS["phone_pin"])):
            with PHONE["lock"]:
                f = PHONE["fails"].get(ip, [0, 0])
                PHONE["fails"][ip] = [f[0] + 1 if time.time() - f[1] < 300 else 1, time.time()]
            return self._send(403, {"error": T("كلمة المرور غير صحيحة.")})
        PHONE["fails"].pop(ip, None)
        urls = re.findall(r"https?://[^\s<>\"']+", str(req.get("url") or ""))
        if not urls:
            return self._send(400, {"error": T("لم أجد أي رابط.")})
        preset = str(req.get("preset") or "best")
        preset = preset if preset in ("best", "audio") or preset.isdigit() else "best"
        for u in urls[:20]:
            new_job({"url": u, "mode": "preset", "preset": preset, "audio_convert": "mp3" if preset == "audio" else "original"},
                    label=("MP3" if preset == "audio" else "أفضل جودة" if preset == "best" else preset + "p"),
                    source="phone", source_label="من الجوال")
        notify(T("رابط من الجوال"), urls[0][:120])
        self._send(200, {"ok": True, "count": len(urls[:20])})


def phone_apply():
    """Start or stop the phone listener to match the settings."""
    want = bool(SETTINGS.get("phone") and SETTINGS.get("phone_pin"))
    if want and not SETTINGS.get("phone_key"):
        SETTINGS["phone_key"] = secrets.token_urlsafe(12)
        save_settings()
    if want and not PHONE["server"]:
        for port in range(PHONE_PORT, PHONE_PORT + 10):
            try:
                srv = ThreadingHTTPServer(("0.0.0.0", port), PhoneHandler)
            except OSError:
                continue
            PHONE.update(server=srv, port=port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            log(f"[phone] listening on the local network, port {port}")
            break
    elif not want and PHONE["server"]:
        srv = PHONE["server"]
        PHONE.update(server=None, port=None)
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        log("[phone] stopped")


# ---------------------------------------------------------------- previews inside the window
MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
               ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
               ".ogg": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}


def preview_allowed(path):
    """Only files the app downloaded or made (in the history) or anything under the downloads folder."""
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    if not p.is_file() or p.suffix.lower() not in MEDIA_TYPES:
        return False
    roots = {Path(SETTINGS["outdir"]).resolve()}
    if any(r == p or r in p.parents for r in roots):
        return True
    with HISTORY_LOCK:
        return bool(HISTORY.execute("SELECT 1 FROM history WHERE file=? LIMIT 1", (str(path),)).fetchone())


# ---------------------------------------------------------------- export / import
def export_data():
    with HISTORY_LOCK:
        hist = [_history_row(r) for r in HISTORY.execute("SELECT * FROM history ORDER BY n").fetchall()]
    with SUBS_LOCK:
        subs = [{**s_, "_seen": sorted(read_set(sub_archive_path(s_["id"])))} for s_ in SUBS]
    keep = {k: v for k, v in SETTINGS.items() if k not in ("engine_checked", "update_checked", "phone_key")}
    data = {"app": APP_NAME, "version": APP_VERSION, "exported": time.strftime("%Y-%m-%d %H:%M"), "settings": keep,
            "subs": subs, "history": hist, "archive": sorted(ARCHIVE)}
    out = _free_name(Path(SETTINGS["outdir"]) / f"MO-Downloader-backup-{time.strftime('%Y-%m-%d')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    private_file(out)
    return {"ok": True, "file": str(out), "history": len(hist), "subs": len(subs)}


def import_data(data, parts):
    if not isinstance(data, dict) or data.get("app") != APP_NAME:
        raise ValueError("هذا ليس ملف نسخة احتياطية من MO Downloader.")
    got = {}
    if "settings" in parts and isinstance(data.get("settings"), dict):
        for k, v in data["settings"].items():
            if k in DEFAULT_SETTINGS and k not in ("engine_checked", "update_checked", "phone_key") and type(v) in (
                    type(DEFAULT_SETTINGS[k]), int, float, str, bool):
                SETTINGS[k] = v
        save_settings()
        phone_apply()
        got["settings"] = True
    if "subs" in parts:
        n = 0
        with SUBS_LOCK:
            have = {s_["url"] for s_ in SUBS}
            for s_ in data.get("subs") or []:
                if not isinstance(s_, dict) or not s_.get("url") or s_["url"] in have:
                    continue
                seen = s_.pop("_seen", []) or []
                s_["id"] = uuid.uuid4().hex[:8]
                SUBS.append(s_)
                have.add(s_["url"])
                if seen:
                    sub_archive_path(s_["id"]).write_text("".join(x + "\n" for x in seen), encoding="utf-8")
                n += 1
            save_subs()
        got["subs"] = n
    if "history" in parts:
        with HISTORY_LOCK:
            have = {(r["key"], round(r["date"] or 0, 2)) for r in HISTORY.execute("SELECT key, date FROM history")}
            new = [h for h in data.get("history") or [] if isinstance(h, dict)
                   and (h.get("key"), round(h.get("date") or 0, 2)) not in have]
            _history_insert(HISTORY, new)
            HISTORY.commit()
            for k in data.get("archive") or []:
                if isinstance(k, str) and k not in ARCHIVE:
                    ARCHIVE.add(k)
                    append_line(ARCHIVE_FILE, k)
        got["history"] = len(new)
    return {"ok": True, **got}


# ---------------------------------------------------------------- updating the app itself
UPDATE = {"state": "", "msg": "", "latest": None, "file": None, "notes": "", "page": None}


def _asset_for_me(assets):
    import platform
    want = ("-Setup.exe",) if SYSTEM == "Windows" and FROZEN else \
        (("-mac-arm64.dmg",) if platform.machine() == "arm64" else ("-mac-x64.dmg",)) if SYSTEM == "Darwin" and FROZEN else \
        (".zip",)
    for a in assets or []:
        if any(str(a.get("name", "")).endswith(w) for w in want) and "full" not in a.get("name", ""):
            return a
    return None


def update_check(download=True):
    """Look at the latest release; if it is newer, download the file for this computer (and check its fingerprint)."""
    import hashlib
    url = SETTINGS.get("update_url")
    if not url:
        return UPDATE
    UPDATE.update(state="checking", msg="جارٍ البحث عن إصدار جديد…")
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=20) as r:
            rel = json.loads(r.read())
    except urllib.error.HTTPError as e:
        UPDATE.update(state="error", msg="صفحة الإصدارات غير متاحة للبرنامج (المستودع خاص) — حدّث يدوياً من صفحة الإصدارات."
                      if e.code in (401, 403, 404) else f"تعذّر البحث عن تحديث (HTTP {e.code}).")
        return UPDATE
    except Exception as e:  # noqa: BLE001
        UPDATE.update(state="error", msg=f"تعذّر البحث عن تحديث: {e}")
        return UPDATE
    SETTINGS["update_checked"] = time.time()
    save_settings()
    tag = str(rel.get("tag_name") or "").lstrip("vV")
    UPDATE.update(latest=tag, notes=(rel.get("body") or "")[:3000], page=rel.get("html_url"))
    if not tag or _ver_key(tag) <= _ver_key(APP_VERSION):
        UPDATE.update(state="current", msg=f"لديك أحدث إصدار ({APP_VERSION}).")
        return UPDATE
    asset = _asset_for_me(rel.get("assets"))
    if not asset:
        UPDATE.update(state="available", msg=f"يوجد إصدار جديد {tag} — نزّله من صفحة الإصدارات.")
        return UPDATE
    if not download:
        UPDATE.update(state="available", msg=f"يوجد إصدار جديد {tag}.")
        return UPDATE
    d = APP_DATA / "updates"
    d.mkdir(parents=True, exist_ok=True)
    out = d / asset["name"]
    UPDATE.update(state="downloading", msg=f"جارٍ تنزيل الإصدار {tag}…")
    try:
        req = urllib.request.Request(asset.get("browser_download_url") or asset["url"],
                                     headers={"Accept": "application/octet-stream", "User-Agent": APP_NAME})
        h = hashlib.sha256()
        tmp = out.with_suffix(out.suffix + ".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 16), b""):
                f.write(chunk)
                h.update(chunk)
        size, digest = tmp.stat().st_size, str(asset.get("digest") or "")
        if asset.get("size") and size != int(asset["size"]):
            raise ValueError(f"الحجم لا يطابق ({size} بدل {asset['size']})")
        if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
            raise ValueError("البصمة لا تطابق — لن يُثبَّت هذا الملف.")
        tmp.replace(out)
    except Exception as e:  # noqa: BLE001
        Path(str(out) + ".part").unlink(missing_ok=True)
        UPDATE.update(state="error", msg=f"تعذّر تنزيل التحديث: {e}")
        return UPDATE
    UPDATE.update(state="ready", file=str(out), msg=f"الإصدار {tag} جاهز للتثبيت.")
    notify(T("تحديث جاهز"), f"MO Downloader {tag}")
    return UPDATE


def update_install():
    import subprocess
    f = UPDATE.get("file")
    if UPDATE.get("state") != "ready" or not f or not os.path.exists(f):
        raise ValueError("لا يوجد تحديث جاهز.")
    if f.endswith(".exe"):
        save_queue()
        subprocess.Popen([f, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"], creationflags=0x00000008)
        threading.Thread(target=lambda: (time.sleep(1.5), quit_app()), daemon=True).start()
        return {"ok": True, "msg": "يُثبَّت التحديث الآن وسيفتح البرنامج من جديد."}
    open_path(f, play=True)  # a .dmg (drag the app into Applications) or the source .zip
    return {"ok": True, "msg": "فُتح ملف التحديث — اسحب التطبيق إلى مجلد التطبيقات واستبدل القديم." if f.endswith(".dmg")
            else "فُتح ملف التحديث."}


def update_loop():
    time.sleep(60)
    while True:
        if SETTINGS.get("update_url") and time.time() - float(SETTINGS.get("update_checked") or 0) > 86400:
            update_check(download=FROZEN)
        time.sleep(3600)


def pause_job(j):
    if j and j["status"] not in FINISHED and j["status"] != PAUSED and not j.get("live") and not j.get("gallery"):
        j["pause"] = True
        j["cancel"] = True
        if j["status"] == "queued":
            j.update(status=PAUSED, stage="متوقف مؤقتاً")
        return True
    return False


def resume_job(j):
    if j and j["status"] == PAUSED:
        if j.get("many"):
            j["_req"]["_done_keys"] = list(dict.fromkeys((j["_req"].get("_done_keys") or []) + (j.get("done_keys") or [])))
        j.pop("_pause_at", None)
        j.update(status="queued", stage="في الطابور — سيكمل من حيث توقف", cancel=False, pause=False)
        return True
    return False


def pause_all():
    with QUEUE:
        n = sum(pause_job(j) for j in list(JOBS.values()))
    return n


def resume_all():
    with QUEUE:
        n = sum(resume_job(j) for j in list(JOBS.values()))
        QUEUE.notify_all()
    return n


# ---------------------------------------------------------------- the queue survives closing the app
SAVED_JOB_KEYS = ("id", "title", "url", "created", "label", "source", "source_label", "sub_id", "many", "done_keys",
                  "done_count")


def _clean_req(req):
    r = {k: v for k, v in req.items() if k not in ("cookies", "page_cookies")}
    if isinstance(r.get("headers"), dict):
        r["headers"] = {k: v for k, v in r["headers"].items() if k.lower() not in ("cookie", "authorization")}
    return r


def save_queue():
    with JOBS_LOCK:
        items = []
        for j in JOBS.values():
            if j["status"] in FINISHED or j.get("live") or j.get("cancel") and not j.get("pause"):
                continue
            d = {k: j.get(k) for k in SAVED_JOB_KEYS}
            d["paused"] = j["status"] == PAUSED or bool(j.get("pause"))
            d["req"] = _clean_req(j["_req"])
            if j.get("many"):
                d["req"]["_done_keys"] = list(dict.fromkeys((d["req"].get("_done_keys") or []) + (j.get("done_keys") or [])))
            items.append(d)
    data = json.dumps(items, ensure_ascii=False)
    if data != QUEUE_STATE.get("last"):
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        private_file(tmp)
        tmp.replace(QUEUE_FILE)
        QUEUE_STATE["last"] = data


QUEUE_STATE = {}


def restore_queue():
    """Downloads that were waiting, paused or running when the app closed come back: running ones continue where
    their partial files stopped, paused ones stay paused."""
    items = load_json(QUEUE_FILE, [])
    with QUEUE:
        for d in items if isinstance(items, list) else []:
            if not d.get("id") or d["id"] in JOBS or not (d.get("req") or {}).get("url"):
                continue
            j = {k: d.get(k) for k in SAVED_JOB_KEYS if d.get(k) is not None}
            j.update(_req=d["req"], percent=0, restored=True)
            if d.get("paused"):
                j.update(status=PAUSED, stage="متوقف مؤقتاً — من الجلسة السابقة")
            else:
                j.update(status="queued", stage="في الطابور — يكمل من الجلسة السابقة")
            JOBS[d["id"]] = j
        QUEUE.notify_all()
    if items:
        log(f"[queue] restored {len(items)} download(s) from the last session")


def new_job(req, label="", source="single", source_label=""):
    if not req.get("url"):
        raise ValueError("الرابط مفقود.")
    if req.get("clip_start") or req.get("clip_end"):  # validate early so the user sees the error now
        a, b = parse_time(req.get("clip_start")), parse_time(req.get("clip_end"))
        if b is not None and b <= (a or 0):
            raise ValueError("وقت النهاية يجب أن يكون بعد وقت البداية.")
    job_id = uuid.uuid4().hex[:10]
    stage = "في الطابور"
    if req.get("start_at"):
        at = float(req["start_at"])
        if at > time.time() + 2:
            stage = "مجدول — يبدأ " + time.strftime("%Y-%m-%d %H:%M", time.localtime(at))
        else:
            req.pop("start_at", None)
    with QUEUE:
        JOBS[job_id] = {
            "id": job_id, "title": req.get("title") or req["url"], "url": req["url"], "status": "queued",
            "stage": stage, "percent": 0, "created": time.time(), "label": label, "start_at": req.get("start_at"),
            "source": source, "source_label": source_label, "sub_id": req.get("sub_id"),
            "many": bool(req.get("playlist") or req.get("sub_id")), "_req": req,
        }
        QUEUE.notify_all()
    return job_id


def scheduler_loop():
    while True:
        try:
            save_queue()
        except Exception as e:  # noqa: BLE001
            log(f"[queue] not saved: {e}")
        with QUEUE:
            QUEUE.wait(timeout=2)
            running = sum(1 for j in JOBS.values() if j["status"] in RUNNING)
            slots = max(1, min(6, int(SETTINGS.get("max_parallel") or 2))) - running
            if slots <= 0:
                continue
            now = time.time()
            queued = sorted((j for j in JOBS.values() if j["status"] == "queued"
                             and float((j.get("_req") or {}).get("start_at") or 0) <= now), key=lambda j: j["created"])
            for j in queued[:slots]:
                j.update(status="starting", stage="جارٍ التحضير", started=time.time())
                threading.Thread(target=run_job, args=(j["id"],), daemon=True).start()


def batch_jobs(req):
    text = req.get("urls") or ""
    urls, seen = [], set()
    for u in re.findall(r"https?://[^\s<>\"']+", text):
        u = u.rstrip(").,،؛")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        raise ValueError("لم أجد أي رابط. ضع كل رابط في سطر.")
    label = {"best": "أفضل جودة", "audio": "صوت"}.get(str(req.get("preset")), f"حتى {req.get('preset')}p")
    ids = []
    for u in urls:
        r = {k: v for k, v in req.items() if k != "urls"}
        r.update(url=u, mode="preset", batch=True, playlist=bool(req.get("playlist")))
        ids.append(new_job(r, label=f"دفعة · {label}", source="batch", source_label="دفعة روابط"))
    return {"count": len(ids)}


# ---------------------------------------------------------------- subscriptions (follow channels)
YT_CHANNEL = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$")


def normalize_sub_url(url):
    url = url.strip()
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host.endswith("youtube.com"):
        if YT_CHANNEL.match(u.path):  # channel home -> its Videos tab (newest first, no shorts)
            return f"https://www.youtube.com{u.path.rstrip('/')}/videos", True
        if re.match(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/(videos|streams|shorts)/?$", u.path):
            return url, True
        if u.path == "/watch" and "list" in parse_qs(u.query):
            return f"https://www.youtube.com/playlist?list={parse_qs(u.query)['list'][0]}", False
    return url, False


def channel_tab(url, tab):
    return re.sub(r"/(videos|shorts|streams)/?$", "", url.rstrip("/")) + "/" + tab


def seed_keys(url, limit=300):
    """Everything already on a channel tab, so following it only downloads what comes later."""
    opts = base_opts({"playlist": True})
    opts.update(extract_flat="in_playlist", playlistend=limit)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return [k for k in (archive_key(ydl, e) for e in (info.get("entries") or []) if e) if k]
    except Exception as e:  # noqa: BLE001  (a channel without Shorts or streams has no such tab)
        log(f"[subs] {url}: {e}")
        return []


def sub_set_tabs(sub, shorts=None, streams=None):
    """Turn following a channel's Shorts / live streams on or off; turning one on skips what is already there."""
    for tab, on in (("shorts", shorts), ("streams", streams)):
        if on is None or not sub.get("channel"):
            continue
        if on and not sub.get(tab):
            keys = seed_keys(channel_tab(sub["url"], tab))
            if keys:
                with open(sub_archive_path(sub["id"]), "a", encoding="utf-8") as f:
                    f.write("".join(k + "\n" for k in keys))
        sub[tab] = bool(on)


def sub_add(req):
    url, is_channel = normalize_sub_url(req.get("url") or "")
    if not url.startswith("http"):
        raise ValueError("ضع رابط قناة أو قائمة تشغيل.")
    with SUBS_LOCK:
        if any(s["url"] == url for s in SUBS):
            raise ValueError("هذه القناة أو القائمة متابَعة بالفعل.")
    start = str(req.get("start") or "new")
    first_n = 0 if start == "new" else max(1, min(200, int(start)))
    opts = base_opts({"playlist": True})
    opts["extract_flat"] = "in_playlist"
    if is_channel:
        opts["playlistend"] = max(60, first_n + 30)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") != "playlist" and info.get("entries") is None:
            raise ValueError("هذا رابط فيديو واحد. ضع رابط القناة أو قائمة التشغيل.")
        entries = [e for e in (info.get("entries") or []) if e]
        keys = [archive_key(ydl, e) for e in entries]
    if not is_channel and first_n:  # ordinary playlists usually grow at the end: "latest" = last items
        keys_seen = keys[:-first_n] if first_n < len(keys) else []
    else:
        keys_seen = keys[first_n:]
    title = re.sub(r"\s+-\s+(Videos|Shorts|Live|Streams)$", "", info.get("title") or info.get("uploader") or "قناة")
    sid = uuid.uuid4().hex[:8]
    Path(sub_archive_path(sid)).write_text("".join(k + "\n" for k in keys_seen if k), encoding="utf-8")
    sub = {
        "id": sid, "url": url, "title": title, "channel": is_channel,
        "uploader": info.get("uploader") or info.get("channel") or "", "site": info.get("extractor_key"),
        "preset": str(req.get("preset") or "best"), "container": req.get("container") or "mp4",
        "audio_convert": req.get("audio_convert") or "mp3", "outdir": req.get("outdir") or SETTINGS["outdir"],
        "enabled": True, "added": time.time(), "last_check": None, "last_result": "", "last_new": None,
        "total": 0, "first_n": first_n, "count_seen": len(entries),
    }
    with SUBS_LOCK:
        SUBS.append(sub)
        save_subs()
    sub_set_tabs(sub, shorts=bool(req.get("shorts")), streams=bool(req.get("streams")))
    with SUBS_LOCK:
        save_subs()
    if first_n:
        sub_check(sid)
    else:
        sub["last_check"] = time.time()
        sub["last_result"] = "بدأت المتابعة — سيُنزَّل كل فيديو جديد"
        save_subs()
    return {"ok": True, "sub": sub}


def sub_check(sid):
    with SUBS_LOCK:
        sub = find_sub(sid)
        if not sub:
            raise ValueError("المتابعة غير موجودة.")
        with JOBS_LOCK:
            if any(j.get("sub_id") == sid and j["status"] not in FINISHED for j in JOBS.values()):
                return None
        sub["last_check"] = time.time()
        sub["last_result"] = "جارٍ الفحص…"
        save_subs()
        req = {
            "url": sub["url"], "playlist": True, "mode": "preset", "preset": sub["preset"],
            "container": sub["container"], "audio_convert": sub["audio_convert"], "outdir": sub["outdir"],
            "skip": True, "sub_id": sid, "folder": sub["title"], "title": f"متابعة: {sub['title']}", "meta": True,
        }
        if sub.get("channel"):
            req["playlistend"] = 300          # safety cap; reading stops at the first video already downloaded
            req["_break_existing"] = True
            req["extra_urls"] = [channel_tab(sub["url"], t) for t in ("shorts", "streams") if sub.get(t)]
        sub["first_n"] = 0
    q = "صوت" if sub["preset"] == "audio" else ("أفضل جودة" if sub["preset"] == "best" else f"حتى {sub['preset']}p")
    return new_job(req, label=f"متابعة · {q}", source="sub", source_label=f"متابعة: {sub['title']}")


def sub_finished(sid, job):
    with SUBS_LOCK:
        sub = find_sub(sid)
        if not sub:
            return
        n = job.get("done_count", 0)
        if job["status"] == "error":
            sub["last_result"] = "فشل الفحص: " + (job.get("error") or "")[:160]
        elif job["status"] == "cancelled":
            sub["last_result"] = "أُلغي الفحص"
        elif n:
            sub["last_result"] = f"نُزّل {n} جديد"
            sub["last_new"] = time.time()
            sub["total"] = sub.get("total", 0) + n
        else:
            sub["last_result"] = "لا جديد"
        save_subs()


def subs_loop():
    time.sleep(40)
    while True:
        try:
            hours = max(0.5, float(SETTINGS.get("check_hours") or 6))
            for s in list(SUBS):
                if s.get("enabled") and time.time() - float(s.get("last_check") or 0) >= hours * 3600:
                    sub_check(s["id"])
        except Exception as e:  # noqa: BLE001
            log(f"[subs] {e}")
        time.sleep(60)


# ---------------------------------------------------------------- start with the computer
def app_icon_file():
    """An .ico of the logo for the Python version (next to the script, or written from the built-in logo)."""
    f = Path(__file__).with_name("mo_downloader.ico")
    if f.exists():
        return str(f)
    out = APP_DATA / "mo_downloader.ico"
    if not out.exists():
        try:
            import io
            from PIL import Image
            Image.open(io.BytesIO(LOGO_PNG)).convert("RGBA").save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
        except Exception:  # noqa: BLE001
            return None
    return str(out)


def app_icon_path():
    if FROZEN:
        return sys.executable
    return str(Path(__file__).with_name("mo_downloader.ico"))


def mac_bundle():
    b = os.environ.get("MO_APP_BUNDLE")
    if b:
        return b
    m = re.search(r"^(.*?\.app)/Contents/", sys.executable)
    return m.group(1) if (FROZEN and m) else None


def set_autostart(on):
    if SYSTEM == "Windows":
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        lnk = startup / f"{APP_NAME}.lnk"
        if not on:
            lnk.unlink(missing_ok=True)
        else:
            if FROZEN:
                target, args = sys.executable, "--background"
            elif os.environ.get("MO_LAUNCHER"):
                target, args = os.environ["MO_LAUNCHER"], "--background"
            else:
                pyw = Path(sys.executable).with_name("pythonw.exe")
                target, args = str(pyw if pyw.exists() else sys.executable), f'"{Path(__file__).resolve()}" --background'

            def q(s):
                return "'" + str(s).replace("'", "''") + "'"
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
                  f"$s.TargetPath={q(target)};$s.Arguments={q(args)};"
                  f"$s.WorkingDirectory={q(Path(target).parent)};$s.IconLocation={q(app_icon_path())};"
                  f"$s.WindowStyle=7;$s.Save()")
            enc = base64.b64encode(ps.encode("utf-16-le")).decode()
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           check=True, capture_output=True, creationflags=0x08000000)
    elif SYSTEM == "Darwin":
        plist = Path.home() / "Library/LaunchAgents/local.mo.downloader.plist"
        if not on:
            plist.unlink(missing_ok=True)
        else:
            bundle = mac_bundle()
            args = (["/usr/bin/open", "-g", "-a", bundle, "--args", "--background"] if bundle
                    else [sys.executable, str(Path(__file__).resolve()), "--background"])
            import plistlib
            plist.parent.mkdir(parents=True, exist_ok=True)
            with open(plist, "wb") as f:
                plistlib.dump({"Label": "local.mo.downloader", "ProgramArguments": args, "RunAtLoad": True}, f)
    else:
        desk = Path.home() / ".config/autostart/mo-downloader.desktop"
        if not on:
            desk.unlink(missing_ok=True)
        else:
            desk.parent.mkdir(parents=True, exist_ok=True)
            cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" --background'
            desk.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec={cmd}\nX-GNOME-Autostart-enabled=true\n",
                            encoding="utf-8")
    SETTINGS["autostart"] = bool(on)
    save_settings()


# ---------------------------------------------------------------- misc
def native_host_path():
    """The program the browser starts to talk to the app. The installed app answers itself; the Python version
    gets a small launcher script."""
    if FROZEN:
        return sys.executable
    d = APP_DATA / "native"
    d.mkdir(parents=True, exist_ok=True)
    if SYSTEM == "Windows":
        f = d / "mo-native-host.bat"
        f.write_text(f'@echo off\r\n"{sys.executable}" "{os.path.abspath(__file__)}" %*\r\n', encoding="utf-8")
    else:
        f = d / "mo-native-host.sh"
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.abspath(__file__)}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
    return str(f)


CHROMIUM_DIRS = {  # browser -> where it looks for native messaging hosts
    "Darwin": ["Google/Chrome", "Google/Chrome Beta", "Microsoft Edge", "BraveSoftware/Brave-Browser", "Chromium",
               "Vivaldi", "Arc/User Data"],
    "Linux": ["google-chrome", "google-chrome-beta", "microsoft-edge", "BraveSoftware/Brave-Browser", "chromium",
              "vivaldi", "google-chrome-for-testing"],
}
WIN_REG_KEYS = [r"Software\Google\Chrome\NativeMessagingHosts", r"Software\Microsoft\Edge\NativeMessagingHosts",
                r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts", r"Software\Chromium\NativeMessagingHosts",
                r"Software\Vivaldi\NativeMessagingHosts"]


def register_native_host():
    """Let the browser extension start the app without the browser's "Open MO Downloader?" prompt."""
    try:
        path = native_host_path()
        chrome = {"name": NATIVE_HOST, "description": APP_NAME, "path": path, "type": "stdio",
                  "allowed_origins": [f"chrome-extension://{EXT_ID_CHROME}/"]}
        firefox = {"name": NATIVE_HOST, "description": APP_NAME, "path": path, "type": "stdio",
                   "allowed_extensions": [EXT_ID_FIREFOX]}
        done = []
        if SYSTEM == "Windows":
            import winreg
            d = APP_DATA / "native"
            d.mkdir(parents=True, exist_ok=True)
            (d / "chrome.json").write_text(json.dumps(chrome, indent=2), encoding="utf-8")
            (d / "firefox.json").write_text(json.dumps(firefox, indent=2), encoding="utf-8")
            for key, f in [(k, "chrome.json") for k in WIN_REG_KEYS] + [(r"Software\Mozilla\NativeMessagingHosts", "firefox.json")]:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + "\\" + NATIVE_HOST) as h:
                    winreg.SetValueEx(h, "", 0, winreg.REG_SZ, str(d / f))
                done.append(key.split("\\")[1])
        else:
            if SYSTEM == "Darwin":
                root = Path.home() / "Library" / "Application Support"
                targets = [root / b / "NativeMessagingHosts" for b in CHROMIUM_DIRS["Darwin"] if (root / b).exists()]
                ff = root / "Mozilla" / "NativeMessagingHosts"
            else:
                root = Path.home() / ".config"
                targets = [root / b / "NativeMessagingHosts" for b in CHROMIUM_DIRS["Linux"] if (root / b).exists()]
                ff = Path.home() / ".mozilla" / "native-messaging-hosts"
            for t in targets:
                t.mkdir(parents=True, exist_ok=True)
                (t / f"{NATIVE_HOST}.json").write_text(json.dumps(chrome, indent=2), encoding="utf-8")
                done.append(t.parent.name)
            if ff.parent.exists():
                ff.mkdir(parents=True, exist_ok=True)
                (ff / f"{NATIVE_HOST}.json").write_text(json.dumps(firefox, indent=2), encoding="utf-8")
                done.append("Firefox")
        log(f"[native] browser helper registered for: {', '.join(done) or 'no browser found'}")
    except Exception as e:  # noqa: BLE001
        log(f"[native] browser helper not registered: {e}")


class OpenError(Exception):
    pass


def _win_select(p):
    """Open the folder in Explorer with the file selected (the shell's own call: no quoting problems with
    spaces, commas or Arabic names, unlike "explorer /select,")."""
    import ctypes
    from ctypes import wintypes
    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    ole32.CoInitialize(None)
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    pidl = shell32.ILCreateFromPathW(str(p))
    if not pidl:
        raise OSError("ILCreateFromPathW failed")
    try:
        hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        if hr != 0:
            raise OSError(f"SHOpenFolderAndSelectItems 0x{hr & 0xFFFFFFFF:08x}")
    finally:
        shell32.ILFree(pidl)


def open_path(path, select=False, play=False):
    """Show a folder, show a file selected in its folder, or (play) open the file in the system's own player.
    Raises OpenError with an Arabic reason the page shows to the user."""
    p = Path(path) if path else Path(SETTINGS["outdir"])
    if not p.exists():
        if play:
            raise OpenError("الملف غير موجود — ربما نُقل أو حُذف أو غُيّر اسمه.")
        if not path or Path(path) == Path(SETTINGS["outdir"]):
            try:
                p.mkdir(parents=True, exist_ok=True)  # the downloads folder before the first download
            except OSError as e:
                raise OpenError(f"تعذّر إنشاء مجلد التنزيلات: {p} ({e.strerror or e})")
        else:
            p = next((x for x in Path(path).parents if x.exists()), Path(SETTINGS["outdir"]))
            select = False
            if not p.exists():
                raise OpenError("المجلد غير موجود — ربما فُصل القرص أو نُقل المجلد.")
    log(f"[open] {'play' if play else 'select' if select else 'open'}: {p}")
    try:
        if SYSTEM == "Windows":
            try:  # let the window that opens come to the front instead of flashing in the taskbar
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            except Exception:  # noqa: BLE001
                pass
            if play:
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif select and p.is_file():
                try:
                    _win_select(p)
                except Exception as e:  # noqa: BLE001
                    log(f"[open] shell select failed ({e}); using explorer")
                    subprocess.Popen(f'explorer /select,"{p}"')
            else:
                os.startfile(str(p if p.is_dir() else p.parent))  # type: ignore[attr-defined]
        elif SYSTEM == "Darwin":
            subprocess.Popen(["open", str(p)] if play or not (select and p.is_file()) else ["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p if play or p.is_dir() else p.parent)])
    except OSError as e:
        log(f"[open] failed: {e}")
        if play and getattr(e, "winerror", None) in (1155, 1156):
            raise OpenError("لا يوجد على جهازك برنامج يفتح هذا النوع من الملفات.")
        raise OpenError(f"تعذّر الفتح: {e.strerror or e}")
    return {"ok": True}


def history_query(q="", limit=100, offset=0):
    """One page of the history, newest first; q searches title, channel, site, link and source."""
    q = (q or "").strip()
    where, args = "", []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = " WHERE " + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in ("title", "uploader", "site", "url", "source"))
        args = [like] * 5
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    with HISTORY_LOCK:
        total = HISTORY.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        count = HISTORY.execute("SELECT COUNT(*) FROM history" + where, args).fetchone()[0] if q else total
        rows = HISTORY.execute(f"SELECT * FROM history{where} ORDER BY n DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    items = [_history_row(r) for r in rows]
    return {"total": total, "count": count, "items": items, "offset": offset, "more": offset + len(items) < count}


def history_delete(key=None, date=None, everything=False):
    global ARCHIVE
    with HISTORY_LOCK:
        if everything:
            HISTORY.execute("DELETE FROM history")
            ARCHIVE = set()
        else:
            if date is None:
                HISTORY.execute("DELETE FROM history WHERE key=?", (key,))
            else:
                HISTORY.execute("DELETE FROM history WHERE key=? AND abs(date-?)<0.001", (key, float(date)))
            if key and not HISTORY.execute("SELECT 1 FROM history WHERE key=? LIMIT 1", (key,)).fetchone():
                ARCHIVE.discard(key)
        HISTORY.commit()
        ARCHIVE_FILE.write_text("".join(k + "\n" for k in ARCHIVE), encoding="utf-8")
    return {"ok": True}


PICK_LOCK = threading.Lock()


def _pick_tk(initial, title):
    """Modern native folder dialog through Tk (on Windows this is the same window Explorer uses).
    Forced on top of the browser so it can't open hidden behind it."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
        root.lift()
        root.focus_force()
        path = filedialog.askdirectory(parent=root, initialdir=initial, title=title, mustexist=False)
    finally:
        root.destroy()
    return os.path.normpath(path) if path else None


def _pick_powershell(initial, title):
    def q(x):
        return "'" + str(x).replace("'", "''") + "'"
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          # a real (invisible) top-most owner window, shown first, so the dialog appears in front of the browser
          "$o=New-Object System.Windows.Forms.Form;$o.TopMost=$true;$o.ShowInTaskbar=$false;$o.Opacity=0;"
          "$o.StartPosition='CenterScreen';$o.Show();$o.Activate();"
          "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
          f"$f.Description={q(title)};$f.SelectedPath={q(initial)};$f.ShowNewFolderButton=$true;"
          "$r=$f.ShowDialog($o);$o.Close();"
          "if($r -eq 'OK'){Write-Output $f.SelectedPath}")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True, timeout=900, creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:300] or f"powershell exit {r.returncode}")
    out = r.stdout.decode("utf-8", "replace").strip()
    return out or None


def pick_folder(initial=""):
    """Native 'choose folder' dialog on the user's computer. Returns a path, or None if the user cancelled."""
    initial = initial if initial and os.path.isdir(initial) else str(Path.home())
    title = "اختر مجلد حفظ التنزيلات"
    if not PICK_LOCK.acquire(blocking=False):
        raise ValueError("نافذة اختيار المجلد مفتوحة بالفعل. ابحث عنها في شريط المهام إن كانت خلف المتصفح.")
    try:
        if SYSTEM == "Windows":
            try:
                out = _pick_tk(initial, title)
            except Exception as e:  # noqa: BLE001
                log(f"[pick] tk dialog failed ({e}); trying PowerShell")
                out = _pick_powershell(initial, title)
        elif SYSTEM == "Darwin":
            esc = initial.replace('"', '\\"')
            # "activate" brings the dialog in front of the browser
            script = (f'activate\nset f to choose folder with prompt "{title}" default location (POSIX file "{esc}")\n'
                      'POSIX path of f')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=900)
            err = r.stderr.decode("utf-8", "replace")
            if r.returncode != 0 and "-128" not in err and "cancel" not in err.lower():
                raise RuntimeError(err.strip()[:300])
            out = r.stdout.decode("utf-8", "replace").strip() or None
        else:
            out = None
            for cmd in (["zenity", "--file-selection", "--directory", f"--title={title}", f"--filename={initial}/"],
                        ["kdialog", "--getexistingdirectory", initial, "--title", title]):
                if shutil.which(cmd[0]):
                    out = subprocess.run(cmd, capture_output=True, timeout=900).stdout.decode("utf-8", "replace").strip()
                    break
            else:
                try:
                    out = _pick_tk(initial, title)
                except Exception:  # noqa: BLE001
                    raise ValueError("لا تتوفر نافذة اختيار المجلدات على هذا النظام؛ اكتب المسار يدوياً.") from None
    except subprocess.TimeoutExpired:
        return None
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        log(f"[pick] folder dialog failed: {e}")
        raise ValueError("تعذّر فتح نافذة اختيار المجلد. اكتب المسار في الخانة يدوياً.\n" + str(e)[:200]) from e
    finally:
        PICK_LOCK.release()
    if not out:
        return None
    out = out.rstrip("/\\") or out
    if not os.path.isdir(out):
        os.makedirs(out, exist_ok=True)
    return out


def pick_file(initial=""):
    """Native 'choose a file' dialog (for the tools). Returns a path or None."""
    initial = initial if initial and os.path.isdir(initial) else str(Path.home())
    title = "اختر ملف فيديو أو صوت"
    win = UI.get("win")
    if win is not None:
        try:
            import webview
            r = win.create_file_dialog(getattr(webview, "FileDialog", webview).OPEN if hasattr(webview, "FileDialog")
                                       else webview.OPEN_DIALOG, directory=initial)
            return (r[0] if isinstance(r, (list, tuple)) else r) if r else None
        except Exception as e:  # noqa: BLE001
            log(f"[pick] window file dialog failed: {e}")
    if not PICK_LOCK.acquire(blocking=False):
        raise ValueError("نافذة الاختيار مفتوحة بالفعل.")
    try:
        if SYSTEM == "Darwin":
            esc = initial.replace('"', '\\"')
            r = subprocess.run(["osascript", "-e", f'activate\nset f to choose file with prompt "{title}" default location '
                                f'(POSIX file "{esc}")\nPOSIX path of f'], capture_output=True, timeout=900)
            return r.stdout.decode("utf-8", "replace").strip() or None
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            root.update()
            path = filedialog.askopenfilename(parent=root, initialdir=initial, title=title)
        finally:
            root.destroy()
        return os.path.normpath(path) if path else None
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:  # noqa: BLE001
        raise ValueError("تعذّر فتح نافذة اختيار الملف. الصق مسار الملف في الخانة يدوياً.\n" + str(e)[:200]) from e
    finally:
        PICK_LOCK.release()


def proxy_test(proxy):
    proxy = str(proxy or "").strip()
    if not proxy:
        raise ValueError("اكتب عنوان البروكسي أولاً.")
    if not re.match(r"^(https?|socks4a?|socks5h?)://", proxy, re.I):
        raise ValueError("عنوان البروكسي يجب أن يبدأ بـ http:// أو https:// أو socks5://")
    t = time.time()
    try:
        with yt_dlp.YoutubeDL({"proxy": proxy, "quiet": True, "no_warnings": True, "socket_timeout": 15}) as ydl:
            ydl.urlopen("https://www.gstatic.com/generate_204").read()
    except Exception as e:  # noqa: BLE001
        raise ValueError("تعذّر الاتصال عبر هذا البروكسي. تأكد من العنوان والمنفذ (واسم المستخدم وكلمة المرور إن وُجدت) وأنه يعمل.\n"
                         + ANSI.sub("", str(e))[:200]) from e
    return {"ok": True, "ms": int((time.time() - t) * 1000)}


# ---------------------------------------------------------------- the app's own window
UI = {"win": None, "base": None, "gui": None, "tray": None}
SELFTEST = os.environ.get("MO_SELFTEST")  # CI: open the window, check the page really loaded, then quit


def busy():
    with JOBS_LOCK:
        return any(j["status"] in RUNNING + ("queued",) for j in JOBS.values())


def _on_main_thread(fn):
    """macOS UI calls must run on the main thread."""
    if SYSTEM == "Darwin":
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
            return
        except Exception:  # noqa: BLE001
            pass
    fn()


def _mac_bring_front():
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass


def show_window(url=None):
    """Bring the app window back (from the Dock/taskbar icon, a second launch, or the browser extension)."""
    w = UI["win"]
    if not w:
        return False
    try:
        if url:
            w.load_url(url)
        w.show()
        try:
            w.restore()
        except Exception:  # noqa: BLE001
            pass
        if SYSTEM == "Darwin":
            _on_main_thread(_mac_bring_front)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[window] show failed: {e}")
        return False


def quit_app():
    w = UI["win"]
    if w:
        try:
            w.destroy()  # ends webview.start() in main(); main() then stops the server
            return
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=SERVER.shutdown, daemon=True).start()


def _mac_reopen_hook():
    """Clicking the Dock icon while the window is hidden brings it back."""
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def reopen(self, app, has_visible):
            threading.Thread(target=show_window, daemon=True).start()
            return True
        objc.classAddMethods(BrowserView.AppDelegate, [
            objc.selector(reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception as e:  # noqa: BLE001
        log(f"[window] dock reopen hook not installed: {e}")


TRAY_TEXT = {"show": "إظهار MO Downloader", "pause": "إيقاف مؤقت للكل", "resume": "استئناف الكل", "quit": "خروج"}


def _tray_quit():
    save_queue()  # what is still downloading continues at the next start
    quit_app()


def start_windows_tray():
    """An icon next to the clock while the app runs (also when its window is hidden): show / pause all / quit."""
    pystray = ensure("pystray", required=False)
    pil = ensure("pillow", "PIL", required=False)
    if not pystray or not pil:
        return None
    import io
    from PIL import Image
    image = Image.open(io.BytesIO(LOGO_PNG)).convert("RGBA").resize((64, 64))
    menu = pystray.Menu(
        pystray.MenuItem(T(TRAY_TEXT["show"]), lambda *a: show_window(), default=True),
        pystray.MenuItem(T(TRAY_TEXT["pause"]), lambda *a: pause_all()),
        pystray.MenuItem(T(TRAY_TEXT["resume"]), lambda *a: resume_all()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(T(TRAY_TEXT["quit"]), lambda icon, item: (icon.stop(), _tray_quit())),
    )
    icon = pystray.Icon("MO Downloader", image, APP_NAME, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    UI["tray"] = icon
    return icon


MAC_STATUS = {}


def start_mac_status_item():
    """The same menu in the macOS menu bar. Must run on the main thread."""
    import AppKit
    import objc
    from Foundation import NSObject
    if "cls" not in MAC_STATUS:
        class MOStatusTarget(NSObject):
            def show_(self, sender):
                threading.Thread(target=show_window, daemon=True).start()

            def pauseAll_(self, sender):
                threading.Thread(target=pause_all, daemon=True).start()

            def resumeAll_(self, sender):
                threading.Thread(target=resume_all, daemon=True).start()

            def quit_(self, sender):
                threading.Thread(target=_tray_quit, daemon=True).start()
        MAC_STATUS["cls"] = MOStatusTarget
    target = MAC_STATUS["cls"].alloc().init()
    item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    data = AppKit.NSData.dataWithBytes_length_(LOGO_PNG, len(LOGO_PNG))
    img = AppKit.NSImage.alloc().initWithData_(data)
    img.setSize_((18, 18))
    item.button().setImage_(img)
    item.button().setToolTip_(APP_NAME)
    menu = AppKit.NSMenu.alloc().init()
    for title, sel in ((T(TRAY_TEXT["show"]), "show:"), (T(TRAY_TEXT["pause"]), "pauseAll:"),
                       (T(TRAY_TEXT["resume"]), "resumeAll:"), (None, None), (T(TRAY_TEXT["quit"]), "quit:")):
        if title is None:
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            continue
        mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, "")
        mi.setTarget_(target)
        menu.addItem_(mi)
    item.setMenu_(menu)
    MAC_STATUS.update(item=item, target=target, menu=menu)  # keep them alive
    UI["tray"] = item
    return item


def start_tray():
    try:
        if SYSTEM == "Windows":
            start_windows_tray()
        elif SYSTEM == "Darwin":
            _on_main_thread(start_mac_status_item)
    except Exception as e:  # noqa: BLE001
        log(f"[tray] icon not shown: {e}")


def _selftest_dock_reopen(win):
    """CI: hide the window, then send the app the same message macOS sends when its Dock icon is clicked."""
    import AppKit

    def visible():
        return any(w.isVisible() and str(w.title()) == APP_NAME for w in AppKit.NSApplication.sharedApplication().windows())
    win.hide()
    time.sleep(1.5)
    if visible():
        return "FAIL(still-visible)"

    def click_dock():
        app = AppKit.NSApplication.sharedApplication()
        app.delegate().applicationShouldHandleReopen_hasVisibleWindows_(app, False)
    _on_main_thread(click_dock)
    for _ in range(16):
        time.sleep(0.5)
        if visible():
            return "OK"
    return "FAIL(not-shown)"


def window_wanted():
    return SETTINGS.get("ui", "window") == "window" and not os.environ.get("MD_NO_WINDOW")


def run_window(url, hidden):
    """Show the interface in the app's own window (WebView2 on Windows, WebKit on macOS). Blocks until quit.
    Returns False if no native window is possible here, so the caller can use the browser instead."""
    webview = ensure("pywebview", "webview", required=False)
    if not webview:
        return False
    win = webview.create_window(APP_NAME, url, width=1200, height=880, min_size=(760, 560), hidden=hidden,
                                background_color="#f4f1ea", text_select=True)
    UI["win"], UI["base"] = win, url

    def on_closing():
        # downloads running, or the app is meant to stay on (started with the computer): hide, don't quit
        if not SELFTEST and (busy() or BACKGROUND or SETTINGS.get("autostart")):
            threading.Thread(target=win.hide, daemon=True).start()
            tray = UI.get("tray")
            if SYSTEM == "Windows" and tray is not None and not UI.get("told_tray"):
                UI["told_tray"] = True
                try:
                    tray.notify(T("ما زال يعمل — أيقونته بجانب الساعة."), APP_NAME)
                except Exception:  # noqa: BLE001
                    pass
            return False
        return True
    win.events.closing += on_closing

    if SELFTEST:
        def check():
            ok, title = False, None
            for _ in range(60):
                try:
                    title = win.evaluate_js("document.title")
                    if title == APP_NAME and win.evaluate_js("!!document.getElementById('nav')"):
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1)
            extra = f" tray={'OK' if UI.get('tray') is not None else 'none'}"
            if ok and SYSTEM == "Darwin":
                extra += " reopen=" + _selftest_dock_reopen(win)
            Path(SELFTEST).write_text(f"{'OK' if ok else 'FAIL'} gui={UI.get('gui')} title={title!r}{extra}\n", encoding="utf-8")
            win.destroy()
        win.events.loaded += lambda: threading.Thread(target=check, daemon=True).start()

    if SYSTEM == "Darwin":
        _mac_reopen_hook()
    gui = os.environ.get("MO_GUI") or ("edgechromium" if SYSTEM == "Windows" else "cocoa" if SYSTEM == "Darwin" else "qt")
    UI["gui"] = gui or "native"
    (APP_DATA / "webview").mkdir(parents=True, exist_ok=True)
    icon = None
    if SYSTEM == "Windows":
        try:  # own taskbar group + icon (otherwise the Python version shows up as pythonw with Python's logo)
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MO.Downloader")
        except Exception:  # noqa: BLE001
            pass
        if not FROZEN:
            icon = app_icon_file()
    webview.start(start_tray, gui=gui, private_mode=False, storage_path=str(APP_DATA / "webview"),
                  **({"icon": icon} if icon else {}))
    if UI.get("tray") is not None and SYSTEM == "Windows":
        try:
            UI["tray"].stop()
        except Exception:  # noqa: BLE001
            pass
    return True


SITES_CACHE = {}


def supported_sites():
    if "list" in SITES_CACHE:
        return SITES_CACHE["list"]
    out = []
    for ie in yt_dlp.extractor.gen_extractor_classes():
        try:
            name, desc = ie.IE_NAME, getattr(ie, "IE_DESC", None)
            if name == "generic" or desc is False:
                continue
            out.append({"n": name, "d": desc or "", "e": "yt-dlp", "w": bool(ie.working())})
        except Exception:  # noqa: BLE001
            continue
    if gallery_dl:
        cats = {}
        for ex in gallery_dl.extractor.extractors():
            c = getattr(ex, "category", "")
            if not c or c in ("generic", "directlink", "recursive", "oauth", "test", "noop", "ytdl"):
                continue
            cats.setdefault(c, set()).add(getattr(ex, "subcategory", "") or "")
        for c, subs in sorted(cats.items()):
            out.append({"n": c, "d": "، ".join(sorted(x for x in subs if x))[:140], "e": "gallery-dl", "w": True})
    SITES_CACHE["list"] = out
    return out


def test_url(req):
    url = str(req.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("ضع رابطاً يبدأ بـ http أو https.")
    yie = None
    for ie in yt_dlp.extractor.gen_extractor_classes():
        try:
            if ie.IE_NAME != "generic" and ie.suitable(url):
                yie = ie.IE_NAME
                break
        except Exception:  # noqa: BLE001
            continue
    res = {"url": url, "ytdlp": yie, "gallery": gallery_site(url), "probe": None, "error": None}
    try:
        info = get_info({"url": url, "playlist": False})
        n = sum(len(v) for v in (info.get("formats") or {}).values())
        res["probe"] = {"type": info.get("type"), "title": info.get("title"), "method": info.get("method"),
                        "formats": n, "count": info.get("count"), "live": info.get("live_status")}
    except Exception as e:  # noqa: BLE001
        res["error"] = clean_err(e)
    return res


def job_report(job):
    lines = [f"MO Downloader {APP_VERSION} — {SYSTEM} {platform.release()} {platform.machine()}"
             + (" (standalone)" if FROZEN else " (Python)"),
             f"yt-dlp {ENGINE['version']} · gallery-dl {ENGINE.get('gallery') or '-'} · "
             f"impersonation {'yes' if IMPERSONATE else 'no'} · ffmpeg {'yes' if FFMPEG else 'no'} · "
             f"accelerator {'yes' if ARIA2C and not ARIA2_BROKEN['why'] else 'no'}",
             f"link: {redact_url(job.get('url'))}",
             f"label: {job.get('label') or '-'} · source: {job.get('source') or '-'}",
             f"result: {job.get('status')} — {job.get('stage')}", "attempts:"]
    for a in job.get("attempts") or []:
        lines.append(f"  - {a.get('method')}: {redact_text(a.get('error'))}")
    if not job.get("attempts"):
        lines.append("  - (none recorded)")
    lines += ["error:", redact_text(job.get("error") or "-").strip()]
    # never include cookies, headers or full query strings
    return "\n".join(lines)


def public_job(j):
    return {k: v for k, v in j.items() if not k.startswith("_")}


def extension_source():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    for cand in (base / "browser-extension", Path(__file__).parent.parent / "browser-extension"):
        if (cand / "manifest.json").exists():
            return cand
    return None


def export_extension():
    """Copy the browser extension to normal folders the browsers' install dialogs can reach:
    one for Chrome/Edge/Brave/Opera, one adjusted for Safari."""
    src = extension_source()
    if not src:
        raise ValueError("ملفات إضافة المتصفح غير موجودة في هذه النسخة.")
    dst = Path.home() / "MO Downloader Extension"
    safari = Path.home() / "MO Downloader Extension - Safari"
    for d in (dst, safari):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(src, d)
    # Safari: a non-persistent background page instead of a service worker (Safari's service-worker
    # background can't reach this computer's app), and no "downloads" permission (Safari has no such API).
    mf = safari / "manifest.json"
    m = json.loads(mf.read_text(encoding="utf-8"))
    m["background"] = {"scripts": ["background.js"], "persistent": False}
    m["permissions"] = [x for x in m.get("permissions", []) if x != "downloads"]
    m.pop("browser_specific_settings", None)
    mf.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst, safari


def ext_download(req):
    url = str(req.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("هذه الصفحة لا تحتوي رابطاً يمكن تنزيله.")
    kind = req.get("kind") if req.get("kind") in ("page", "media", "file") else "page"
    page = str(req.get("page") or "")
    if req.get("cookies"):
        store_ext_cookies(url, req["cookies"])
    if req.get("page_cookies") and page:
        store_ext_cookies(page, req["page_cookies"])
    headers = clean_headers(req.get("headers"))
    if req.get("referer"):
        headers.setdefault("Referer", str(req["referer"])[:2000])
    if req.get("ua"):
        headers.setdefault("User-Agent", str(req["ua"])[:500])
    title = (req.get("title") or url)[:200]
    alts = []
    for a in (req.get("alt_streams") or [])[:5]:
        au = str((a or {}).get("url") or "")
        if au.startswith(("http://", "https://")):
            alts.append({"url": au, "headers": clean_headers(a.get("headers")), "title": str(a.get("title") or "")[:200]})
    if kind == "file":
        r = {"url": url, "kind": "file", "headers": headers, "filename": req.get("filename") or "",
             "outdir": SETTINGS["outdir"], "title": req.get("filename") or title, "page": page}
        return {"id": new_job(r, label="من المتصفح · ملف", source="ext", source_label="من المتصفح")}
    preset = str(req.get("preset") or "best")
    if preset not in ("best", "audio") and not preset.isdigit():
        preset = "best"
    r = {
        "url": url, "mode": "preset", "preset": preset, "container": "mp4",
        "audio_convert": "mp3" if preset == "audio" else "original", "audio_quality": "0",
        "outdir": SETTINGS["outdir"], "meta": True, "title": title, "headers": headers,
        "playlist": "youtube.com/playlist" in url, "alt_streams": alts,
    }
    if kind == "media":
        r["name"] = req.get("title") or "video"
    q = {"best": "أفضل جودة", "audio": "صوت MP3"}.get(preset, f"{preset}p")
    lab = ("من المتصفح · بث ملتقط · " if kind == "media" else "من المتصفح · ") + q
    return {"id": new_job(r, label=lab, source="ext", source_label="من المتصفح")}


def clean_headers(h):
    """Request headers the extension saw for a stream; cookies travel separately, hop-by-hop ones are dropped."""
    out = {}
    for k, v in (h or {}).items() if isinstance(h, dict) else []:
        k, v = str(k)[:100], str(v)[:4000]
        low = k.lower()
        if low in ("cookie", "host", "content-length", "connection", "accept-encoding", "range", "if-range",
                   "if-none-match", "if-modified-since", "upgrade-insecure-requests", "priority") or low.startswith(("sec-", ":")):
            continue
        out[k] = v
    return out


def ext_formats(b):
    url = str(b.get("url") or "").strip()
    if b.get("cookies"):
        store_ext_cookies(url, b["cookies"])
    info = get_info({"url": url, "headers": clean_headers(b.get("headers")), "playlist": False})
    if info.get("type") != "video":
        return {"type": info.get("type"), "heights": [], "audio": False, "title": info.get("title")}
    f = info["formats"]
    hs = sorted({x["height"] for k in ("av", "video") for x in f[k] if x.get("height")}, reverse=True)
    return {"type": "video", "title": info.get("title"), "heights": hs[:8], "audio": bool(f["audio"] or f["av"]),
            "duration": info.get("duration"), "method": info.get("method")}


class Handler(BaseHTTPRequestHandler):
    server_version = "MODownloader/" + APP_VERSION

    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, p):
        size = p.stat().st_size
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end = 0, size - 1
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _authorized(self):
        return self.headers.get("X-Token") == TOKEN

    def _host_ok(self):
        # blocks DNS-rebinding: a web page can never reach us under its own host name
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _ext_origin(self):
        """Requests from our browser extension. Web pages can't send the X-MO-Ext header without a CORS
        preflight, and we only answer preflights coming from extension origins."""
        origin = self.headers.get("Origin") or ""
        ok_origin = not origin or origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://"))
        return ok_origin and self.headers.get("X-MO-Ext") == "1"

    def _cors(self):
        origin = self.headers.get("Origin") or ""
        if origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "X-MO-Ext, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Vary", "Origin")

    def _send_ext(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):  # noqa: N802
        origin = self.headers.get("Origin") or ""
        if (self._host_ok() and urlparse(self.path).path.startswith("/api/ext/")
                and origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://"))):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(403, {"error": "forbidden"})

    def _ext_api(self, method, u):
        if not self._ext_origin():
            return self._send(403, {"error": "forbidden"})
        try:
            if u.path == "/api/ext/status":
                return self._send_ext(200, {"app": APP_NAME, "version": APP_VERSION,
                                            "url": f"http://127.0.0.1:{self.server.server_address[1]}/"})
            if u.path == "/api/ext/jobs":
                with JOBS_LOCK:
                    jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:6]
                    keep = ("id", "title", "status", "stage", "percent", "label", "source", "error", "speed")
                    return self._send_ext(200, {"jobs": [{k: j.get(k) for k in keep} for j in jobs]})
            if method == "POST" and u.path == "/api/ext/download":
                return self._send_ext(200, ext_download(self._body()))
            if method == "POST" and u.path == "/api/ext/formats":
                return self._send_ext(200, ext_formats(self._body()))
            if method == "POST" and u.path == "/api/ext/cookies":
                b = self._body()
                store_ext_cookies(str(b.get("url") or ""), b.get("cookies"))
                return self._send_ext(200, {"ok": True})
            if method == "POST" and u.path == "/api/ext/show":
                b = self._body()
                base = f"http://127.0.0.1:{self.server.server_address[1]}/"
                target = base + ("?url=" + urllib.request.quote(str(b["url"]), safe="") if b.get("url") else "")
                shown = show_window(target if b.get("url") else None)
                return self._send_ext(200, {"window": shown, "url": target})
            if method == "POST" and u.path == "/api/ext/open_folder":
                open_path(SETTINGS["outdir"])
                return self._send_ext(200, {"ok": True})
            return self._send_ext(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            return self._send_ext(400, {"error": clean_err(e)})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if u.path.startswith("/api/ext/"):
            return self._ext_api("GET", u)
        if u.path in ("/logo.png", "/favicon.ico"):
            return self._send(200, LOGO_PNG, "image/png")
        if u.path == "/api/ping":
            return self._send(200, {"app": APP_NAME, "version": APP_VERSION, "frozen": FROZEN, "busy": busy()})
        if u.path in ("/", "/index.html"):
            html = PAGE.replace("__TOKEN__", TOKEN).replace('"__LANG__"', json.dumps(SETTINGS.get("lang") or "ar"))
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/media" and qs.get("t"):
            pass
        elif not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if u.path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:200]
                return self._send(200, {"jobs": [public_job(j) for j in jobs]})
        if u.path == "/api/status":
            return self._send(200, {
                "app": APP_VERSION, "ytdlp": ENGINE["version"], "pending": ENGINE["pending"], "frozen": FROZEN,
                "ffmpeg": bool(FFMPEG), "js": list(JS_RUNTIMES.keys()), "cookie_file": COOKIE_FILE.exists(),
                "settings": {k: v for k, v in SETTINGS.items() if k != "engine_checked"},
                "system": SYSTEM, "has_extension": bool(extension_source()), "aria2": bool(ARIA2C),
                "aria2_broken": ARIA2_BROKEN["why"], "in_window": bool(UI["win"]),
                "gallery": ENGINE.get("gallery"), "impersonate": IMPERSONATE,
                "engine": {"channel": SETTINGS.get("engine_channel", "stable"), "updating": ENGINE["updating"],
                           "msg": ENGINE["msg"], "pending": ENGINE["pending"]},
                "clip": CLIP["suggest"] if CLIP["suggest"] and time.time() - CLIP["suggest"]["ts"] < 180 else None,
                "phone": {"on": bool(PHONE["server"]), "url": phone_url()},
                "update": {k: v for k, v in UPDATE.items() if k != "file"},
                "tpl_example": tpl_example(SETTINGS.get("name_tpl"), SETTINGS.get("folder_tpl")),
                "presets": {k: v["label"] for k, v in TOOL_PRESETS.items()},
            })
        if u.path == "/api/media":
            # previews: <video> can't send headers, so the token comes in the address
            if not secrets.compare_digest(qs.get("t", ""), TOKEN) or not preview_allowed(qs.get("path", "")):
                return self._send(403, {"error": "forbidden"})
            return self._send_file(Path(qs["path"]))
        if u.path == "/api/clip":
            c = CLIP["suggest"]
            return self._send(200, {"clip": c if c and time.time() - c["ts"] < 180 else None})
        if u.path == "/api/sites":
            return self._send(200, {"sites": supported_sites()})
        if u.path == "/api/history":
            return self._send(200, history_query(qs.get("q"), int(qs.get("limit") or 100), int(qs.get("offset") or 0)))
        if u.path == "/api/subs":
            with SUBS_LOCK:
                return self._send(200, {"subs": SUBS, "hours": SETTINGS.get("check_hours")})
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path.startswith("/api/ext/"):
            return self._ext_api("POST", u)
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        try:
            req = self._body()
            p = self.path
            if p == "/api/info":
                return self._send(200, get_info(req))
            if p == "/api/download":
                src = "playlist" if req.get("playlist") else "single"
                return self._send(200, {"id": new_job(req, label=req.get("label") or "", source=src,
                                                      source_label="قائمة تشغيل" if req.get("playlist") else "")})
            if p == "/api/tool":
                return self._send(200, {"id": tool_job(req)})
            if p == "/api/probe":
                if not Path(str(req.get("path") or "")).is_file():
                    raise ValueError("الملف غير موجود.")
                return self._send(200, {**probe_media(req["path"]), "name": Path(req["path"]).name})
            if p == "/api/pick_file":
                path = pick_file(req.get("initial") or SETTINGS["outdir"])
                return self._send(200, {"path": path} if path else {"cancelled": True})
            if p == "/api/clip_dismiss":
                CLIP["suggest"] = None
                return self._send(200, {"ok": True})
            if p == "/api/phone":
                return self._send(200, {"on": bool(PHONE["server"]), "url": phone_url(), "svg": phone_qr_svg(phone_url())})
            if p == "/api/export":
                return self._send(200, export_data())
            if p == "/api/import":
                return self._send(200, import_data(req.get("data"), set(req.get("parts") or ["settings", "subs", "history"])))
            if p == "/api/update_check":
                threading.Thread(target=update_check, kwargs={"download": True}, daemon=True).start()
                return self._send(200, {"ok": True})
            if p == "/api/update_install":
                return self._send(200, update_install())
            if p == "/api/batch":
                return self._send(200, batch_jobs(req))
            if p == "/api/cancel":
                with JOBS_LOCK:
                    j = JOBS.get(req.get("id"))
                    if j:
                        j["cancel"] = True
                        j["pause"] = False
                        if j["status"] in ("queued", PAUSED):
                            j.update(status="cancelled", stage="أُلغي")
                return self._send(200, {"ok": True})
            if p == "/api/engine_update":
                if "channel" in req and req["channel"] in ("stable", "nightly"):
                    SETTINGS["engine_channel"] = req["channel"]
                    save_settings()
                threading.Thread(target=engine_update_check, kwargs={"force": True}, daemon=True).start()
                return self._send(200, {"ok": True})
            if p == "/api/test_url":
                return self._send(200, test_url(req))
            if p == "/api/report":
                with JOBS_LOCK:
                    j = JOBS.get(req.get("id"))
                if not j:
                    raise ValueError("المهمة غير موجودة.")
                return self._send(200, {"text": job_report(j)})
            if p == "/api/stop_live":
                with JOBS_LOCK:
                    j = JOBS.get(req.get("id"))
                if not j:
                    raise ValueError("المهمة غير موجودة.")
                return self._send(200, {"ok": True, "ffmpeg": stop_live(j)})
            if p == "/api/pause":
                with QUEUE:
                    pause_job(JOBS.get(req.get("id")))
                return self._send(200, {"ok": True})
            if p == "/api/resume":
                with QUEUE:
                    resume_job(JOBS.get(req.get("id")))
                    QUEUE.notify_all()
                return self._send(200, {"ok": True})
            if p == "/api/pause_all":
                return self._send(200, {"ok": True, "paused": pause_all()})
            if p == "/api/resume_all":
                return self._send(200, {"ok": True, "resumed": resume_all()})
            if p == "/api/cancel_all":
                with JOBS_LOCK:
                    for j in JOBS.values():
                        if j["status"] not in FINISHED:
                            j["cancel"] = True
                            if j["status"] == "queued":
                                j.update(status="cancelled", stage="أُلغي")
                return self._send(200, {"ok": True})
            if p == "/api/retry":
                with JOBS_LOCK:
                    j = JOBS.get(req.get("id"))
                if not j:
                    raise ValueError("المهمة غير موجودة.")
                return self._send(200, {"id": new_job(dict(j["_req"]), label=j.get("label", ""), source=j.get("source", "single"),
                                                      source_label=j.get("source_label", ""))})
            if p == "/api/clear":
                with JOBS_LOCK:
                    for k in [k for k, j in JOBS.items() if j["status"] in FINISHED]:
                        del JOBS[k]
                return self._send(200, {"ok": True})
            if p == "/api/open":
                try:
                    return self._send(200, open_path(req.get("path") or SETTINGS["outdir"], select=bool(req.get("select")),
                                                     play=bool(req.get("play"))))
                except OpenError as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            if p == "/api/pick_folder":
                path = pick_folder(req.get("initial") or SETTINGS["outdir"])
                return self._send(200, {"path": path} if path else {"cancelled": True})
            if p == "/api/proxy_test":
                return self._send(200, proxy_test(req.get("proxy")))
            if p == "/api/ext_folder":
                dst, safari = export_extension()
                open_path(safari if req.get("safari") else dst)
                return self._send(200, {"ok": True, "path": str(dst), "safari": str(safari)})
            if p == "/api/open_url":
                u = str(req.get("url") or "")
                if u.startswith(("http://", "https://")):
                    webbrowser.open(u)
                return self._send(200, {"ok": True})
            if p == "/api/cookies":
                if req.get("delete"):
                    COOKIE_FILE.unlink(missing_ok=True)
                    return self._send(200, {"ok": True, "has": False})
                text = req.get("text")
                if not text or "\t" not in text:
                    raise ValueError("هذا ليس ملف كوكيز صالحاً. صدّره بصيغة Netscape (cookies.txt) من إضافة المتصفح.")
                if not text.lstrip().startswith("#"):
                    text = "# Netscape HTTP Cookie File\n" + text
                COOKIE_FILE.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
                private_file(COOKIE_FILE)
                n = sum(1 for ln in text.splitlines() if ln.count("\t") >= 6)
                return self._send(200, {"ok": True, "has": True, "count": n})
            if p == "/api/settings":
                changed = {}
                if "outdir" in req and str(req["outdir"]).strip():
                    changed["outdir"] = str(Path(os.path.expanduser(str(req["outdir"]).strip())))
                if "max_parallel" in req:
                    changed["max_parallel"] = max(1, min(6, int(req["max_parallel"])))
                if "check_hours" in req:
                    changed["check_hours"] = max(0.5, min(168, float(req["check_hours"])))
                if "cookies" in req:
                    changed["cookies"] = str(req["cookies"] or "")
                if "proxy" in req:
                    pv = str(req["proxy"] or "").strip()
                    if pv and not re.match(r"^(https?|socks4a?|socks5h?)://", pv, re.I):
                        raise ValueError("عنوان البروكسي يجب أن يبدأ بـ http:// أو https:// أو socks5://")
                    changed["proxy"] = pv
                if req.get("proxy_mode") in ("fallback", "always"):
                    changed["proxy_mode"] = req["proxy_mode"]
                if "accel" in req:
                    changed["accel"] = bool(req["accel"])
                if "accel_conns" in req:
                    changed["accel_conns"] = max(1, min(16, int(req["accel_conns"])))
                if req.get("ext_cookies") in ("auto", "always", "never"):
                    changed["ext_cookies"] = req["ext_cookies"]
                if req.get("ui") in ("window", "browser"):
                    changed["ui"] = req["ui"]
                if req.get("engine_channel") in ("stable", "nightly"):
                    changed["engine_channel"] = req["engine_channel"]
                if req.get("lang") in ("ar", "en"):
                    changed["lang"] = req["lang"]
                if "rate_limit_kb" in req:
                    changed["rate_limit_kb"] = max(0, min(10_000_000, int(req["rate_limit_kb"] or 0)))
                if "name_tpl" in req:
                    changed["name_tpl"] = tpl_check(req["name_tpl"])
                if "folder_tpl" in req:
                    changed["folder_tpl"] = tpl_check(req["folder_tpl"], folder=True)
                for k in ("notify", "clipboard", "phone"):
                    if k in req:
                        changed[k] = bool(req[k])
                if "phone_pin" in req:
                    pin = str(req["phone_pin"] or "").strip()
                    if pin and not re.fullmatch(r"\d{4,10}", pin):
                        raise ValueError("كلمة مرور الجوال: من 4 إلى 10 أرقام.")
                    changed["phone_pin"] = pin
                if changed.get("phone") and not (changed.get("phone_pin") or SETTINGS.get("phone_pin")):
                    raise ValueError("اختر كلمة مرور للجوال أولاً (من 4 إلى 10 أرقام).")
                if req.get("phone_new_key"):
                    changed["phone_key"] = secrets.token_urlsafe(12)
                if "sub3d_cmd" in req:
                    cmd = str(req["sub3d_cmd"] or "").strip()
                    if cmd and "{file}" not in cmd:
                        raise ValueError("أمر الترجمة ثلاثية الأبعاد يجب أن يحتوي {file} مكان ملف الترجمة.")
                    changed["sub3d_cmd"] = cmd
                if "update_url" in req:
                    uu = str(req["update_url"] or "").strip()
                    if uu and not uu.startswith(("https://", "http://127.0.0.1", "http://localhost")):
                        raise ValueError("عنوان التحديثات يجب أن يبدأ بـ https://")
                    changed["update_url"] = uu
                SETTINGS.update(changed)
                save_settings()
                phone_apply()
                if "autostart" in req:
                    set_autostart(bool(req["autostart"]))
                with QUEUE:
                    QUEUE.notify_all()
                return self._send(200, {"ok": True, "settings": SETTINGS})
            if p == "/api/history/delete":
                return self._send(200, history_delete(req.get("key"), req.get("date")))
            if p == "/api/history/clear":
                return self._send(200, history_delete(everything=True))
            if p == "/api/subs/add":
                return self._send(200, sub_add(req))
            if p == "/api/subs/check":
                ids = [s["id"] for s in SUBS if s.get("enabled")] if req.get("all") else [req.get("id")]
                started = [sub_check(i) for i in ids]
                return self._send(200, {"ok": True, "started": len([s for s in started if s])})
            if p == "/api/subs/update":
                with SUBS_LOCK:
                    s = find_sub(req.get("id"))
                    if not s:
                        raise ValueError("المتابعة غير موجودة.")
                    for k in ("enabled", "preset", "container", "audio_convert", "outdir"):
                        if k in req:
                            s[k] = req[k]
                sub_set_tabs(s, shorts=req.get("shorts"), streams=req.get("streams"))
                with SUBS_LOCK:
                    save_subs()
                return self._send(200, {"ok": True})
            if p == "/api/subs/delete":
                with SUBS_LOCK:
                    SUBS[:] = [s for s in SUBS if s["id"] != req.get("id")]
                    save_subs()
                sub_archive_path(req.get("id")).unlink(missing_ok=True)
                return self._send(200, {"ok": True})
            if p == "/api/shutdown":
                self._send(200, {"ok": True})
                threading.Thread(target=quit_app, daemon=True).start()
                return None
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": clean_err(e)})


def free_port(preferred):
    for port in range(preferred, preferred + PORT_SPAN):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0  # none of the ten free: any port (the extension then learns it from PORT_FILE through its helper)


def already_running(port):
    """If MO Downloader is already open (on any of the ports it may use), return that port."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for p in range(port, port + PORT_SPAN):
        try:
            with opener.open(f"http://127.0.0.1:{p}/api/ping", timeout=1.0) as r:
                if json.loads(r.read()).get("app") == APP_NAME:
                    return p
        except Exception:  # noqa: BLE001
            continue
    return None


def replace_other_copy(port):
    """The installed app was opened while the Python version (or an older installed copy) is running — e.g. one that
    starts with Windows. Close that copy and take its place, unless it is downloading something."""
    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        info = json.loads(o.open(f"http://127.0.0.1:{port}/api/ping", timeout=2).read())
        if info.get("frozen") and info.get("version") == APP_VERSION:
            return False  # the same installed app: just show it
        if info.get("busy"):
            log(f"[start] another copy ({info.get('version') or 'older'}) is downloading - leaving it open")
            return False
        page = o.open(f"http://127.0.0.1:{port}/", timeout=3).read().decode("utf-8", "replace")
        tok = re.search(r'const TOKEN = "([^"]+)"', page).group(1)
        o.open(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", data=b"{}",
                                      headers={"X-Token": tok, "Content-Type": "application/json"}), timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"[start] could not replace the other copy: {e}")
        return False
    for _ in range(40):
        time.sleep(0.25)
        try:
            o.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.5)
        except Exception:  # noqa: BLE001
            log(f"[start] replaced the other copy ({info.get('version') or 'Python version'}) on port {port}")
            return True
    return False


def repair_windows_links():
    """Shortcuts named "MO Downloader" (desktop, Start menu, start-with-Windows) and the mo-downloader:// link
    that the Python version's launcher pointed at itself go back to the installed app."""
    try:
        import winreg
        exe = sys.executable
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\mo-downloader\shell\open\command") as k:
            if exe.lower() not in str(winreg.QueryValueEx(k, "")[0] if _reg_has(k) else "").lower():
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\mo-downloader\DefaultIcon") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}",0')

        def q(x):
            return "'" + str(x).replace("'", "''") + "'"
        ps = ("$ws=New-Object -ComObject WScript.Shell;$n=0;"
              "foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'),"
              "[Environment]::GetFolderPath('Startup'))){"
              "$f=Join-Path $d 'MO Downloader.lnk'; if(Test-Path -LiteralPath $f){ $s=$ws.CreateShortcut($f);"
              f"if($s.TargetPath -ne {q(exe)}){{ $bg=($d -eq [Environment]::GetFolderPath('Startup'));"
              f"$s.TargetPath={q(exe)};$s.Arguments=$(if($bg){{'--background'}}else{{''}});"
              f"$s.WorkingDirectory={q(Path(exe).parent)};$s.IconLocation={q(exe + ',0')};$s.Save();$n++ }} }} }};$n")
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, text=True, creationflags=0x08000000, timeout=60)
        n = (r.stdout or "").strip()
        if n and n != "0":
            log(f"[start] {n} shortcut(s) pointed back at the installed app")
    except Exception as e:  # noqa: BLE001
        log(f"[start] shortcuts not checked: {e}")


def _reg_has(k):
    import winreg
    try:
        winreg.QueryValueEx(k, "")
        return True
    except OSError:
        return False


SERVER = None


def main():
    global SERVER, ARIA2C
    shutil.rmtree(TMP_DIR, ignore_errors=True)  # cookies handed over by the extension are never kept between runs
    ARIA2C = find_aria2()
    try:
        _install_aria2_downloader()
    except Exception as e:  # noqa: BLE001
        log(f"[aria2] progress hook not installed: {e}")
    try:
        _install_live_stop()
    except Exception as e:  # noqa: BLE001
        log(f"[live] stop hook not installed: {e}")
    Path(SETTINGS["outdir"]).mkdir(parents=True, exist_ok=True)
    running_port = already_running(DEFAULT_PORT)
    if running_port and FROZEN and replace_other_copy(running_port):
        running_port = None
    if FROZEN and SYSTEM == "Windows":
        threading.Thread(target=repair_windows_links, daemon=True).start()
    if running_port:
        if not BACKGROUND:
            shown = False
            try:  # ask the running copy to show its window
                r = urllib.request.Request(f"http://127.0.0.1:{running_port}/api/ext/show", data=b"{}",
                                           headers={"X-MO-Ext": "1", "Content-Type": "application/json"})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=3) as resp:
                    shown = bool(json.loads(resp.read()).get("window"))
            except Exception:  # noqa: BLE001
                pass
            if not shown:
                log("MO Downloader is already running - opening it in the browser.")
                webbrowser.open(f"http://127.0.0.1:{running_port}/")
        return
    port = free_port(DEFAULT_PORT)
    server = SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        PORT_FILE.write_text(str(server.server_address[1]))
    except OSError:
        pass
    restore_queue()
    threading.Thread(target=register_native_host, daemon=True).start()
    phone_apply()
    for target in (scheduler_loop, subs_loop, engine_update_check, fetch_aria2_windows, clipboard_loop, update_loop):
        threading.Thread(target=target, daemon=True).start()
    print("=" * 60)
    print(f"  MO Downloader {APP_VERSION} is running:  {url}")
    print(f"  yt-dlp {ENGINE['version']} | ffmpeg: {'OK' if FFMPEG else 'MISSING'}"
          f" | JS runtime: {', '.join(JS_RUNTIMES) or 'MISSING'} | aria2: {'OK' if ARIA2C else 'no'}")
    print(f"  gallery-dl {ENGINE.get('gallery') or 'MISSING'} | impersonation: {'OK' if IMPERSONATE else 'no'}")
    if FROZEN:
        print(f"  engine: {Path(yt_dlp.__file__).parent}")
    print(f"  Downloads folder: {SETTINGS['outdir']}")
    print("  Close this window (or press the stop button in the page) to quit.")
    print("=" * 60, flush=True)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    if window_wanted():
        try:
            if run_window(url, hidden=BACKGROUND):  # returns when the user quits
                server.shutdown()
                log("MO Downloader stopped.")
                return
        except Exception as e:  # noqa: BLE001
            log(f"[window] not available, using the browser instead: {e}")
            UI["win"] = None
    if not BACKGROUND and not os.environ.get("MD_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        while serving.is_alive():
            serving.join(1)
    except KeyboardInterrupt:
        server.shutdown()
    log("MO Downloader stopped.")


# ---------------------------------------------------------------- icon
LOGO_PNG = base64.b64decode("__LOGO_B64__")

# ---------------------------------------------------------------- page
PAGE = r"""__PAGE__"""

if __name__ == "__main__":
    main()
