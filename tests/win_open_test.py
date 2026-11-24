"""Windows-only checks against the built app (run in CI after the build):
- the installed app replaces a running Python-version copy, and repairs a desktop shortcut pointing elsewhere
- "open folder", "show file" (Arabic name with a comma and spaces), "play" and the error for a missing file

    python tests/win_open_test.py "<path to MO Downloader.exe>"
"""
import json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

EXE = Path(sys.argv[1]).resolve()
REPO = Path(__file__).resolve().parent.parent
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail else ""), flush=True)


def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, encoding="utf-8")
    return (r.stdout + r.stderr).strip()


def ping(port=8765):
    try:
        return json.loads(O.open(f"http://127.0.0.1:{port}/api/ping", timeout=1).read())
    except Exception:
        return None


def wait(pred, secs):
    for _ in range(secs * 4):
        v = pred()
        if v:
            return v
        time.sleep(0.25)
    return None


env = {**os.environ, "MD_PORT": "8765", "MD_NO_WINDOW": "1", "MD_NO_BROWSER": "1"}

# --- a Python-version copy is running (like one that starts with Windows); a desktop shortcut points to a .bat
py = subprocess.Popen([sys.executable, str(REPO / "app" / "mo_downloader.py"), "--background"], env=env)
first = wait(lambda: ping(), 120)
check("Python-version copy running first", bool(first) and first.get("frozen") is False, str(first))
desk = Path(ps("[Environment]::GetFolderPath('Desktop')"))
lnk = desk / "MO Downloader.lnk"
dummy = Path(os.environ["RUNNER_TEMP"]) / "old launcher.bat"
dummy.write_text("@echo off\r\n")
ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{dummy}';$s.Save()")

app = subprocess.Popen([str(EXE)], env=env)
now = wait(lambda: (lambda p: p if p and p.get("frozen") else None)(ping()), 120)
check("installed app replaces the Python-version copy", bool(now), str(now))
check("Python-version copy closed", wait(lambda: py.poll() is not None, 20) is not None, f"exit {py.poll()}")
target = wait(lambda: (lambda t: t if t.lower() == str(EXE).lower() else None)(
    ps(f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath")), 60)
check("desktop shortcut points back at the installed app", bool(target),
      ps(f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"))

base = "http://127.0.0.1:8765"
tok = re.search(r'const TOKEN = "([^"]+)"', O.open(base + "/").read().decode()).group(1)


def api(path, body):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                               headers={"X-Token": tok, "Content-Type": "application/json"})
    return json.loads(O.open(r, timeout=30).read())


outdir = Path(json.loads(O.open(urllib.request.Request(base + "/api/status", headers={"X-Token": tok})).read())["settings"]["outdir"])
windows = "(New-Object -ComObject Shell.Application).Windows() | %{ [uri]::UnescapeDataString($_.LocationURL) }"
ps("(New-Object -ComObject Shell.Application).Windows() | %{ $_.Quit() }")

r = api("/api/open", {"path": str(outdir)})
shown = wait(lambda: outdir.name in ps(windows), 15)
check("open the downloads folder", r.get("ok") and bool(shown), f"{r} · {ps(windows)}")

ps("(New-Object -ComObject Shell.Application).Windows() | %{ $_.Quit() }")
sub = outdir / "مجلد, تجربة"
sub.mkdir(parents=True, exist_ok=True)
f = sub / "فيديو, تجربة (1).mp4"
f.write_bytes(b"\0" * 2048)
r = api("/api/open", {"path": str(f), "select": True})
sel = "(New-Object -ComObject Shell.Application).Windows() | %{ $_.Document.SelectedItems() | %{ $_.Name } }"
selected = wait(lambda: (lambda t: t if "تجربة" in t else None)(ps(sel)), 15)
check("show a file with an Arabic name, comma and spaces (selected in its folder)", r.get("ok") and bool(selected),
      f"{r} · selected: {ps(sel)!r}")

t = outdir / "note.txt"
t.write_text("MO Downloader", encoding="utf-8")
ps("Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force")
r = api("/api/open", {"path": str(t), "play": True})
opened = wait(lambda: "notepad" in ps("Get-Process notepad -ErrorAction SilentlyContinue | %{ $_.ProcessName }").lower(), 20)
check("play: the file opens in its program", r.get("ok") and bool(opened), str(r))
ps("Get-Process notepad -ErrorAction SilentlyContinue | Stop-Process -Force")

r = api("/api/open", {"path": str(outdir / "gone.mp4"), "play": True})
check("missing file: a clear reason instead of nothing", r.get("ok") is False and "غير موجود" in (r.get("error") or ""), str(r))

ps("(New-Object -ComObject Shell.Application).Windows() | %{ $_.Quit() }")
try:
    api("/api/shutdown", {})
except Exception:
    pass
time.sleep(2)
for p in (app, py):
    if p.poll() is None:
        p.kill()
print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
sys.exit(0 if all(RESULTS) else 1)
