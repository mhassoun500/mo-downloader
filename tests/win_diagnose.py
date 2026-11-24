"""Diagnose the installed Windows app on a GitHub runner: icons (exe, shortcuts, window, tray) and opening files.
Prints screenshots / icons as base64 between markers so they can be read from the job log."""
import base64, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

APP = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "MO Downloader" / "MO Downloader.exe"
O = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def dump(name, path):
    p = Path(path)
    if p.exists():
        print(f"=====BEGIN {name}=====\n{base64.b64encode(p.read_bytes()).decode()}\n=====END {name}=====")
    else:
        print(f"[no {name}: {path}]")


def shot(name):
    f = Path(os.environ["RUNNER_TEMP"]) / f"{name}.png"
    ps("Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
       "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
       "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
       "$g=[System.Drawing.Graphics]::FromImage($i);$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
       f"$s=New-Object System.Drawing.Bitmap $i,($b.Width/2),($b.Height/2);$s.Save('{f}',[System.Drawing.Imaging.ImageFormat]::Png)")
    dump(name, f)


def icon_of(name, target):
    f = Path(os.environ["RUNNER_TEMP"]) / f"{name}.png"
    print(name, ps("Add-Type -AssemblyName System.Drawing;"
                   f"$ic=[System.Drawing.Icon]::ExtractAssociatedIcon('{target}');$ic.ToBitmap().Save('{f}');'ok'"))
    dump(name, f)


print("installed:", APP.exists())
desk = Path(os.environ["USERPROFILE"]) / "Desktop" / "MO Downloader.lnk"
public = Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Desktop" / "MO Downloader.lnk"
for l in (desk, public):
    print("shortcut", l, l.exists(), ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{l}');"
                                         "$s.TargetPath+' | '+$s.IconLocation") if l.exists() else "")
icon_of("icon_exe", APP)
if desk.exists():
    icon_of("icon_desktop_lnk", desk)

subprocess.Popen([str(APP)])
port = None
for _ in range(120):
    for p in range(8765, 8775):
        try:
            if json.loads(O.open(f"http://127.0.0.1:{p}/api/ping", timeout=1).read()).get("app"):
                port = p
                break
        except Exception:
            pass
    if port:
        break
    time.sleep(1)
print("app port:", port)
time.sleep(8)
shot("screen_app")
print("windows:", ps("Get-Process | ?{$_.MainWindowTitle} | %{ $_.ProcessName + ' | ' + $_.MainWindowTitle + ' | ' + $_.Path }"))
base = f"http://127.0.0.1:{port}"
tok = re.search(r'const TOKEN = "([^"]+)"', O.open(base + "/").read().decode()).group(1)


def api(path, body):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"X-Token": tok, "Content-Type": "application/json"})
    try:
        with O.open(r, timeout=20) as x:
            return x.status, x.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


st = json.loads(O.open(urllib.request.Request(base + "/api/status", headers={"X-Token": tok})).read())
outdir = Path(st["settings"]["outdir"])
print("outdir:", outdir, outdir.exists())
explorer = "(New-Object -ComObject Shell.Application).Windows() | %{ $_.LocationURL + ' | ' + $_.LocationName }"
print("explorer before:", ps(explorer))
print("open outdir:", api("/api/open", {"path": str(outdir)}))
time.sleep(4)
print("explorer after open folder:", ps(explorer))
outdir.mkdir(parents=True, exist_ok=True)
f = outdir / "test video.mp4"
f.write_bytes(b"\0" * 1000)
print("show file:", api("/api/open", {"path": str(f), "select": True}))
time.sleep(4)
print("explorer after show file:", ps(explorer))
shot("screen_after_open")
log = Path(os.environ["USERPROFILE"]) / ".media_downloader" / "log.txt"
print("----- app log tail -----")
print(log.read_text(errors="replace")[-4000:] if log.exists() else "no log")
