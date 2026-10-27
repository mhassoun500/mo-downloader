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
APP_VERSION = "2.3"
FROZEN = bool(getattr(sys, "frozen", False))
PROTOCOL = "mo-downloader"
# started in the background (with the computer) or by the browser extension through mo-downloader://
BACKGROUND = "--background" in sys.argv or any(a.lower().startswith(PROTOCOL + ":") for a in sys.argv[1:])
DEFAULT_PORT = int(os.environ.get("MD_PORT", 8765))
SYSTEM = platform.system()

APP_DATA = Path.home() / ".media_downloader"
APP_DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = APP_DATA / "settings.json"
HISTORY_FILE = APP_DATA / "history.jsonl"
ARCHIVE_FILE = APP_DATA / "archive.txt"
SUBS_FILE = APP_DATA / "subscriptions.json"
SUBS_DIR = APP_DATA / "subscriptions"
ENGINE_DIR = APP_DATA / "engine"
COOKIE_FILE = APP_DATA / "cookies.txt"
LOG_FILE = APP_DATA / "log.txt"
TMP_DIR = APP_DATA / "tmp"
TOOLS_DIR = APP_DATA / "tools"

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
    "engine_checked": 0,
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
    for prefix, key in (("yt_dlp_ejs", "yt-dlp-ejs"), ("yt_dlp", "yt-dlp")):
        wheels = sorted(ENGINE_DIR.glob(f"{prefix}-[0-9]*.whl"), key=lambda p: _ver_key(_wheel_version(p)))
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

ENGINE = {"version": yt_dlp.version.__version__, "pending": None}


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
TOKEN = secrets.token_urlsafe(16)
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


def engine_update_check():
    """Standalone builds: fetch the newest yt-dlp from PyPI (at most once a day); used from next start."""
    if not FROZEN:
        return
    if time.time() - float(SETTINGS.get("engine_checked") or 0) < 20 * 3600:
        return
    try:
        ENGINE_DIR.mkdir(parents=True, exist_ok=True)
        pending = None
        for name in ("yt-dlp", "yt-dlp-ejs"):
            meta = json.loads(https_get(f"https://pypi.org/pypi/{name}/json"))
            latest = meta["info"]["version"]
            current = ENGINE["version"] if name == "yt-dlp" else bundled_versions().get(name, "0")
            prefix = name.replace("-", "_")
            have = [_wheel_version(p) for p in ENGINE_DIR.glob(f"{prefix}-[0-9]*.whl")]
            if _ver_key(latest) <= _ver_key(current) or latest in have:
                continue
            files = [u for u in meta["urls"] if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-none-any.whl")]
            if not files:
                continue
            data = https_get(files[0]["url"], timeout=120)
            tmp = ENGINE_DIR / (files[0]["filename"] + ".part")
            tmp.write_bytes(data)
            tmp.replace(ENGINE_DIR / files[0]["filename"])
            if name == "yt-dlp":
                pending = latest
        ENGINE["pending"] = pending
        SETTINGS["engine_checked"] = time.time()
        save_settings()
        if pending:
            log(f"[engine] downloaded yt-dlp {pending}; it will be used after restart")
    except Exception as e:  # noqa: BLE001
        log(f"[engine] update check failed: {e}")


# ---------------------------------------------------------------- state
JOBS = {}
JOBS_LOCK = threading.RLock()
QUEUE = threading.Condition(JOBS_LOCK)
RUNNING = ("starting", "downloading", "processing")
PAUSED = "paused"
FINISHED = ("done", "error", "cancelled")

HISTORY_LOCK = threading.Lock()
HISTORY = []
for _line in (HISTORY_FILE.read_text(encoding="utf-8").splitlines() if HISTORY_FILE.exists() else []):
    try:
        HISTORY.append(json.loads(_line))
    except ValueError:
        pass
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


class Cancelled(Exception):
    pass


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


def accel_on(proxy=None):
    if not (ARIA2C and SETTINGS.get("accel", True)):
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
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
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
    f.write_text(text, encoding="utf-8")
    return str(f)


AUTH_ERRORS = ("sign in", "log in", "login", "logged in", "cookies", "not a bot", "age-restricted", "age restricted",
               "inappropriate for some users", "private video", "members only", "members-only", "premium",
               "registered users", "subscription", "http error 401", "http error 403", "account",
               "status=401", "status=403", "401 unauthorized", "403 forbidden")


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

    def prog(done, total, speed, path):
        with JOBS_LOCK:
            if path and not job.get("many"):
                job["title"] = Path(path).name or job["title"]
            job.update(status="downloading", stage="جارٍ التنزيل" + (" (مسرّع)" if accel_on(proxy) else ""),
                       downloaded=done, total=total, speed=speed,
                       eta=int((total - done) / speed) if (total and speed) else None,
                       percent=(done / total * 100) if total else None)

    stop = lambda: bool(job.get("cancel"))  # noqa: E731
    try:
        if accel_on(proxy):
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
    with HISTORY_LOCK:
        HISTORY.append(entry)
        append_line(HISTORY_FILE, json.dumps(entry, ensure_ascii=False))
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
    if "unsupported url" in low:
        hints.append("الرابط غير مدعوم أو لا يحتوي على وسائط قابلة للاستخراج.")
    if "js runtime" in low or "javascript runtime" in low:
        hints.append("ثبّت Deno ثم أعد تشغيل الأداة.")
    if "requested format is not available" in low:
        hints.append("هذه الجودة لم تعد متاحة. اضغط «عرض الجودات» مجدداً واختر غيرها.")
    return msg + ("\n\n" + "\n".join(hints) if hints else "")


NET_ERRORS = (
    "failed to resolve", "getaddrinfo", "name or service not known", "nodename nor servname", "no address associated",
    "temporary failure in name resolution", "connection refused", "connection reset", "connection aborted",
    "remote end closed", "timed out", "timeout", "network is unreachable", "unable to connect", "eof occurred",
    "ssl: ", "certificate verify failed", "not available in your country", "geo restrict", "geo-restrict",
    "blocked", "http error 451", "http error 403: forbidden", "proxy",
)


def is_network_error(msg):
    low = str(msg).lower()
    return any(k in low for k in NET_ERRORS)


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
        for h in reversed(HISTORY):
            if h.get("key") == key:
                return h
    return None


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


def get_info(req):
    flags = {}
    for _ in range(3):
        try:
            info = _get_info({**req, **flags})
            if flags.get("_use_proxy"):
                info["via_proxy"] = True
            if flags.get("_use_cookies"):
                info["via_cookies"] = True
            return info
        except Exception as e:  # noqa: BLE001
            if "_use_cookies" not in flags and is_auth_error(e) and ext_cookies_for(req.get("url") or ""):
                log("[cookies] info needs login, retrying with the browser's cookies")
                flags["_use_cookies"] = True
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
    }
    with HISTORY_LOCK:
        HISTORY.append(entry)
        append_line(HISTORY_FILE, json.dumps(entry, ensure_ascii=False))
        if key and key not in ARCHIVE and not key.startswith(("generic ", "html5mediaembed ")):
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
    if accel_on(opts.get("proxy")):
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
        opts["outtmpl"] = {"default": name}

    if req.get("playlist") or req.get("sub_id") or req.get("batch"):
        opts["ignoreerrors"] = "only_download"  # a private/removed item must not stop the rest
    if req.get("playlistend"):
        opts["playlistend"] = int(req["playlistend"])
    if req.get("sub_id"):
        opts["match_filter"] = match_filter_func("!is_live & live_status!=?is_upcoming & live_status!=?is_live")

    # --- skip what was downloaded before
    if req.get("skip"):
        archive = set(ARCHIVE)
        if req.get("sub_id"):
            archive |= read_set(sub_archive_path(req["sub_id"]))
        opts["download_archive"] = archive  # a set: yt-dlp checks it but we write the files ourselves

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
            opts.update({
                "writesubtitles": True, "writeautomaticsub": False,
                "subtitleslangs": req.get("sub_langs") or ["ar", "ar-.*", "en", "en-.*"],
                "subtitlesformat": "srt/vtt/best",
            })
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
            if final_ext in ("mp4", "mkv"):
                pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})

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


def run_job(job_id):
    job = JOBS[job_id]
    req = job["_req"]

    def check_cancel():
        if job.get("cancel"):
            raise Cancelled()

    def progress_hook(d):
        check_cancel()
        info = d.get("info_dict") or {}
        with JOBS_LOCK:
            if info.get("title"):
                job["current"] = info["title"]
                if not job.get("many"):
                    job["title"] = info["title"]
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                job.update(status="downloading", stage="جارٍ التنزيل", downloaded=done, total=total,
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
        for attempt in range(3):
            attempt_req = {**req, **flags}
            if flags and job.get("many"):
                attempt_req["skip"] = True  # only what failed; finished items are in the archive
            logger = JobLogger(job)
            crash = None
            try:
                if req.get("kind") == "file":
                    code = run_file_job(job, attempt_req, flags)
                else:
                    opts = build_download_opts(attempt_req, job, logger)
                    opts["progress_hooks"] = [progress_hook]
                    opts["postprocessor_hooks"] = [pp_hook]
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.add_post_processor(HistoryPP(ydl, job), when="after_move")
                        code = ydl.download([attempt_req["url"]])
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                if job.get("cancel") or "Cancelled" in type(e).__name__:
                    raise Cancelled() from e
                code, crash = 1, e
            if not code and not job.get("failed"):
                break
            failed_text = " ".join(job.get("errors") or []) + " " + str(crash or "")
            if "_use_cookies" not in flags and is_auth_error(failed_text) and ext_cookies_for(req["url"]):
                flags["_use_cookies"] = True
                stage = "الموقع يطلب تسجيل الدخول — إعادة المحاولة بكوكيز المتصفح…"
            elif "_use_proxy" not in flags and proxy_fallback_on() and is_network_error(failed_text):
                flags["_use_proxy"] = True
                stage = "فشل الاتصال المباشر — إعادة المحاولة عبر البروكسي…"
            else:
                break
            log(f"[retry] job {job_id}: {stage}")
            with JOBS_LOCK:
                job.update(status="starting", stage=stage, failed=0, errors=[], percent=0, speed=None, eta=None)
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
            else:
                job.update(status="done", stage="اكتمل ✓", percent=100, speed=None, eta=None)
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
        if req.get("sub_id") and job["status"] != PAUSED:
            sub_finished(req["sub_id"], job)
        with QUEUE:
            QUEUE.notify_all()


def new_job(req, label="", source="single", source_label=""):
    if not req.get("url"):
        raise ValueError("الرابط مفقود.")
    if req.get("clip_start") or req.get("clip_end"):  # validate early so the user sees the error now
        a, b = parse_time(req.get("clip_start")), parse_time(req.get("clip_end"))
        if b is not None and b <= (a or 0):
            raise ValueError("وقت النهاية يجب أن يكون بعد وقت البداية.")
    job_id = uuid.uuid4().hex[:10]
    with QUEUE:
        JOBS[job_id] = {
            "id": job_id, "title": req.get("title") or req["url"], "url": req["url"], "status": "queued",
            "stage": "في الطابور", "percent": 0, "created": time.time(), "label": label,
            "source": source, "source_label": source_label, "sub_id": req.get("sub_id"),
            "many": bool(req.get("playlist") or req.get("sub_id")), "_req": req,
        }
        QUEUE.notify_all()
    return job_id


def scheduler_loop():
    while True:
        with QUEUE:
            QUEUE.wait(timeout=2)
            running = sum(1 for j in JOBS.values() if j["status"] in RUNNING)
            slots = max(1, min(6, int(SETTINGS.get("max_parallel") or 2))) - running
            if slots <= 0:
                continue
            queued = sorted((j for j in JOBS.values() if j["status"] == "queued"), key=lambda j: j["created"])
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
            req["playlistend"] = max(15, int(sub.get("first_n") or 0))
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
def open_path(path, select=False):
    p = Path(path)
    if not p.exists():
        p = p.parent if p.parent.exists() else Path(SETTINGS["outdir"])
    if SYSTEM == "Windows":
        if select and p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p if p.is_dir() else p.parent))  # type: ignore[attr-defined]
    elif SYSTEM == "Darwin":
        subprocess.Popen(["open", "-R", str(p)] if select and p.is_file() else ["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])


def history_query(q="", limit=300):
    q = (q or "").strip().lower()
    with HISTORY_LOCK:
        items = list(reversed(HISTORY))
    if q:
        items = [h for h in items if q in " ".join(str(h.get(k) or "") for k in ("title", "uploader", "site", "url", "source")).lower()]
    return {"total": len(HISTORY), "count": len(items), "items": items[:limit]}


def history_delete(key=None, date=None, everything=False):
    global HISTORY, ARCHIVE
    with HISTORY_LOCK:
        if everything:
            HISTORY, ARCHIVE = [], set()
        else:
            HISTORY = [h for h in HISTORY if not (h.get("key") == key and (date is None or h.get("date") == date))]
            if key and not any(h.get("key") == key for h in HISTORY):
                ARCHIVE.discard(key)
        HISTORY_FILE.write_text("".join(json.dumps(h, ensure_ascii=False) + "\n" for h in HISTORY), encoding="utf-8")
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


def public_job(j):
    return {k: v for k, v in j.items() if not k.startswith("_")}


def extension_source():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    for cand in (base / "browser-extension", Path(__file__).parent.parent / "browser-extension"):
        if (cand / "manifest.json").exists():
            return cand
    return None


def export_extension():
    """Copy the browser extension to a normal folder the browser's 'Load unpacked' dialog can reach."""
    src = extension_source()
    if not src:
        raise ValueError("ملفات إضافة المتصفح غير موجودة في هذه النسخة.")
    dst = Path.home() / "MO Downloader Extension"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    return dst


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
    headers = {}
    if req.get("referer"):
        headers["Referer"] = str(req["referer"])[:2000]
    if req.get("ua"):
        headers["User-Agent"] = str(req["ua"])[:500]
    title = (req.get("title") or url)[:200]
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
        "playlist": "youtube.com/playlist" in url,
    }
    if kind == "media":
        r["name"] = req.get("title") or "video"
    q = {"best": "أفضل جودة", "audio": "صوت MP3"}.get(preset, f"{preset}p")
    lab = ("من المتصفح · بث ملتقط · " if kind == "media" else "من المتصفح · ") + q
    return {"id": new_job(r, label=lab, source="ext", source_label="من المتصفح")}


class Handler(BaseHTTPRequestHandler):
    server_version = "MODownloader/2.3"

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
            if method == "POST" and u.path == "/api/ext/cookies":
                b = self._body()
                store_ext_cookies(str(b.get("url") or ""), b.get("cookies"))
                return self._send_ext(200, {"ok": True})
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
            return self._send(200, {"app": APP_NAME})
        if u.path in ("/", "/index.html"):
            html = PAGE.replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
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
            })
        if u.path == "/api/history":
            return self._send(200, history_query(qs.get("q"), int(qs.get("limit") or 300)))
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
            if p == "/api/pause":
                with QUEUE:
                    j = JOBS.get(req.get("id"))
                    if j and j["status"] not in FINISHED and j["status"] != PAUSED:
                        j["pause"] = True
                        j["cancel"] = True
                        if j["status"] == "queued":
                            j.update(status=PAUSED, stage="متوقف مؤقتاً")
                return self._send(200, {"ok": True})
            if p == "/api/resume":
                with QUEUE:
                    j = JOBS.get(req.get("id"))
                    if j and j["status"] == PAUSED:
                        j.update(status="queued", stage="في الطابور — سيكمل من حيث توقف", cancel=False, pause=False)
                        QUEUE.notify_all()
                return self._send(200, {"ok": True})
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
                open_path(req.get("path") or SETTINGS["outdir"], select=bool(req.get("select")))
                return self._send(200, {"ok": True})
            if p == "/api/pick_folder":
                path = pick_folder(req.get("initial") or SETTINGS["outdir"])
                return self._send(200, {"path": path} if path else {"cancelled": True})
            if p == "/api/proxy_test":
                return self._send(200, proxy_test(req.get("proxy")))
            if p == "/api/ext_folder":
                dst = export_extension()
                open_path(dst)
                return self._send(200, {"ok": True, "path": str(dst)})
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
                SETTINGS.update(changed)
                save_settings()
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
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return None
            self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": clean_err(e)})


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 30)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def already_running(port):
    """If MO Downloader is already open, just show it instead of starting a second copy."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == APP_NAME
    except Exception:  # noqa: BLE001
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
    Path(SETTINGS["outdir"]).mkdir(parents=True, exist_ok=True)
    if already_running(DEFAULT_PORT):
        if not BACKGROUND:
            log("MO Downloader is already running - opening it in the browser.")
            webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}/")
        return
    port = free_port(DEFAULT_PORT)
    server = SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    for target in (scheduler_loop, subs_loop, engine_update_check, fetch_aria2_windows):
        threading.Thread(target=target, daemon=True).start()
    print("=" * 60)
    print(f"  MO Downloader {APP_VERSION} is running:  {url}")
    print(f"  yt-dlp {ENGINE['version']} | ffmpeg: {'OK' if FFMPEG else 'MISSING'}"
          f" | JS runtime: {', '.join(JS_RUNTIMES) or 'MISSING'} | aria2: {'OK' if ARIA2C else 'no'}")
    if FROZEN:
        print(f"  engine: {Path(yt_dlp.__file__).parent}")
    print(f"  Downloads folder: {SETTINGS['outdir']}")
    print("  Close this window (or press the stop button in the page) to quit.")
    print("=" * 60, flush=True)
    if not BACKGROUND and not os.environ.get("MD_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log("MO Downloader stopped.")


# ---------------------------------------------------------------- icon
LOGO_PNG = base64.b64decode("__LOGO_B64__")

# ---------------------------------------------------------------- page
PAGE = r"""__PAGE__"""

if __name__ == "__main__":
    main()
