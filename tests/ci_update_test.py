"""Automatic update of the built app, on a GitHub Windows or Mac machine (run in CI after the build).

    Windows: python tests/ci_update_test.py dist/MO-Downloader-X.Y-Setup.exe
    macOS:   python tests/ci_update_test.py dist/MO-Downloader-X.Y-mac-<arch>.dmg "<path to MO Downloader.app>"

A local stand-in for GitHub's "latest release" answer offers version 9.9 (the file is this same build, renamed).
The app must find it, download it, check its fingerprint, install it by itself (nothing is downloading), close and
open again. On Windows the app is first installed with the real installer; on the Mac a copy of the app bundle is
used and must be replaced by the one inside the .dmg.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PKG = Path(sys.argv[1]).resolve()
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WIN = sys.platform == "win32"
RESULTS = []
SERVED = {"downloads": 0}


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail else ""), flush=True)


# ---- the stand-in releases page
data = PKG.read_bytes()
if WIN:
    asset = "MO-Downloader-9.9-Setup.exe"
else:
    asset = f"MO-Downloader-9.9-mac-{'arm64' if platform.machine() == 'arm64' else 'x64'}.dmg"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/release.json":
            body = json.dumps({"tag_name": "v9.9", "html_url": "http://127.0.0.1/", "body": "test", "assets": [{
                "name": asset, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "browser_download_url": f"http://127.0.0.1:{self.server.server_address[1]}/{asset}"}]}).encode()
        elif self.path == "/" + asset:
            SERVED["downloads"] += 1
            body = data
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
REL = f"http://127.0.0.1:{srv.server_address[1]}/release.json"


def find_app():
    for p in range(8765, 8775):
        try:
            j = json.loads(O.open(f"http://127.0.0.1:{p}/api/ping", timeout=0.5).read())
            if j.get("app") == "MO Downloader":
                return p
        except Exception:
            pass
    return None


def wait(pred, secs):
    for _ in range(int(secs * 2)):
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    return None


home = Path(os.environ.get("USERPROFILE") or os.environ["HOME"])
log_file = home / ".media_downloader" / "log.txt"

# ---- install / prepare the app
if WIN:
    subprocess.run(["taskkill", "/F", "/IM", "MO Downloader.exe"], capture_output=True)
    r = subprocess.run([str(PKG), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=600)
    exe = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "MO Downloader" / "MO Downloader.exe"
    check("installed with the real installer", r.returncode == 0 and exe.exists(), str(exe))
    # the installer rewrites its uninstall record on every install: proof that it ran again
    record = exe.parent / "unins000.dat"
    record_before = record.stat().st_mtime if record.exists() else 0
    cmd = [str(exe), "--background"]
else:
    src = Path(sys.argv[2]).resolve()
    apps = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "Apps"
    apps.mkdir(exist_ok=True)
    bundle = apps / "MO Downloader.app"
    shutil.rmtree(bundle, ignore_errors=True)
    subprocess.run(["ditto", str(src), str(bundle)], check=True)
    marker = bundle / "Contents" / "Resources" / "old-copy-marker.txt"
    marker.write_text("x")
    cmd = ["open", "-n", str(bundle), "--args", "--background"]

env = {**os.environ, "MO_UPDATE_DELAY": "3600"}  # no automatic daily check during the test: we start it ourselves
env.pop("MD_PORT", None)
subprocess.Popen(cmd, env=env)
port = wait(find_app, 120)
check("app running", port, str(port))
base = f"http://127.0.0.1:{port}"
tok = re.search(r'const TOKEN = "([^"]+)"', O.open(base + "/").read().decode()).group(1)


def api(path, body):
    rq = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                headers={"X-Token": tok, "Content-Type": "application/json"})
    return json.loads(O.open(rq, timeout=30).read())


api("/api/settings", {"update_url": REL, "auto_update": True})
api("/api/update_check", {})
went = wait(lambda: find_app() is None, 240)
check("app found version 9.9, downloaded it and closed to install it", went and SERVED["downloads"] == 1,
      f"downloads served: {SERVED['downloads']}")
back = wait(find_app, 300)
check("app opened again by itself after the update", back, str(back))
if WIN:
    after = record.stat().st_mtime if record.exists() else 0
    check("the installer ran again (silently)", after > record_before, f"{record_before} → {after}")
else:
    check("the old copy was replaced by the one in the .dmg", not marker.exists(), str(marker))
log = log_file.read_text(errors="replace") if log_file.exists() else ""
last = {}
if back:
    try:
        b2 = f"http://127.0.0.1:{back}"
        t2 = re.search(r'const TOKEN = "([^"]+)"', O.open(b2 + "/").read().decode()).group(1)
        last = json.loads(O.open(urllib.request.Request(b2 + "/api/status", headers={"X-Token": t2})).read())["settings"].get("last_update") or {}
    except Exception as e:
        last = {"error": str(e)}
check("the app records that it installed 9.9 by itself", last.get("to") == "9.9" and last.get("auto") is True, str(last))
if not WIN:
    check("the new copy is not blocked by macOS (no quarantine mark)",
          "com.apple.quarantine" not in subprocess.run(["xattr", str(bundle)], capture_output=True, text=True).stdout, "")

if back:
    try:
        t2 = re.search(r'const TOKEN = "([^"]+)"', O.open(f"http://127.0.0.1:{back}/").read().decode()).group(1)
        O.open(urllib.request.Request(f"http://127.0.0.1:{back}/api/shutdown", data=b"{}",
                                      headers={"X-Token": t2, "Content-Type": "application/json"}), timeout=5)
    except Exception:
        pass
time.sleep(3)
if not all(RESULTS):
    print(log[-3000:])
print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
sys.exit(0 if all(RESULTS) else 1)
