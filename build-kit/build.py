#!/usr/bin/env python3
"""
Build MO Downloader as a standalone app (no Python needed on the user's computer).

  Windows : dist/MO-Downloader-<version>-Setup.exe   (installer; needs Inno Setup 6, else a .zip)
  macOS   : dist/MO-Downloader-<version>-mac-<arch>.dmg
  Linux   : dist/MO-Downloader-<version>-linux-<arch>.tar.gz

Run it on the system you are building for:  python build-kit/build.py
"""
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
ROOT = KIT.parent
APP = ROOT / "app" / "mo_downloader.py"
ICO = ROOT / "app" / "mo_downloader.ico"
ICNS = KIT / "AppIcon.icns"
DIST = ROOT / "dist"
WORK = ROOT / "build-tmp"
NAME = "MO Downloader"
SYSTEM = platform.system()
ARCH = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine())


def run(*cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.check_call(list(map(str, cmd)), **kw)


def app_version():
    for line in APP.read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_VERSION"):
            return line.split("=")[1].strip().strip('"')
    return "0"


def main():
    run(sys.executable, "-m", "pip", "install", "-U", "--quiet", "pip")
    run(sys.executable, "-m", "pip", "install", "-U", "--quiet",
        "pyinstaller", "yt-dlp[default,curl-cffi]", "yt-dlp-ejs", "gallery-dl", "imageio-ffmpeg", "deno", "certifi", "pywebview")
    import importlib.metadata as md
    import deno as deno_pkg
    deno_bin = Path(deno_pkg.find_deno_bin())
    print("deno:", deno_bin)
    # aria2 (multi-connection downloads): CI puts the official Windows build / Homebrew's build in MO_ARIA2C
    aria2 = os.environ.get("MO_ARIA2C") or shutil.which("aria2c")
    print("aria2:", aria2 or "not bundled")
    # Homebrew's aria2 uses OpenSSL 3, which loads a 'legacy' module from a Homebrew path at start-up.
    # Ship that module next to aria2 (the app points OPENSSL_MODULES at it).
    ossl_legacy = os.environ.get("MO_OSSL_LEGACY")
    if SYSTEM == "Darwin" and aria2 and not ossl_legacy:
        try:
            prefix = subprocess.check_output(["brew", "--prefix", "openssl@3"], text=True).strip()
            cand = Path(prefix) / "lib" / "ossl-modules" / "legacy.dylib"
            ossl_legacy = str(cand) if cand.exists() else None
        except Exception:
            ossl_legacy = None
    print("openssl legacy module:", ossl_legacy or "-")
    versions = {"yt-dlp": md.version("yt-dlp"), "yt-dlp-ejs": md.version("yt-dlp-ejs"),
                "gallery-dl": md.version("gallery-dl"), "app": app_version()}
    print("engine:", versions)

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)
    (WORK / "engine_version.json").write_text(json.dumps(versions), encoding="utf-8")

    args = [
        sys.executable, "-m", "PyInstaller", str(APP), "--noconfirm", "--clean", "--onedir",
        "--name", NAME, "--distpath", WORK / "dist", "--workpath", WORK / "work", "--specpath", WORK,
        "--add-data", f"{WORK / 'engine_version.json'}{os.pathsep}.",
        "--add-data", f"{ROOT / 'browser-extension'}{os.pathsep}browser-extension",
        "--collect-all", "yt_dlp_ejs", "--collect-all", "imageio_ffmpeg",
        "--collect-all", "gallery_dl", "--collect-all", "curl_cffi",
        "--add-binary", f"{deno_bin}{os.pathsep}deno_bin",
        "--collect-data", "certifi", "--hidden-import", "certifi",
        "--hidden-import", "tkinter", "--hidden-import", "tkinter.filedialog",
        *(["--add-binary", f"{aria2}{os.pathsep}aria2", "--add-data", f"{KIT / 'licenses'}{os.pathsep}licenses"] if aria2 else []),
        *(["--add-binary", f"{ossl_legacy}{os.pathsep}aria2/ossl-modules"] if ossl_legacy else []),
    ]
    # the app's own window: the engine for each system is loaded at runtime, so name it explicitly
    if SYSTEM == "Windows":
        args += ["--windowed", "--icon", ICO,
                 "--hidden-import", "webview.platforms.winforms", "--hidden-import", "webview.platforms.edgechromium",
                 "--hidden-import", "clr", "--collect-all", "clr_loader", "--collect-all", "pythonnet"]
    elif SYSTEM == "Darwin":
        args += ["--windowed", "--icon", ICNS, "--osx-bundle-identifier", "local.mo.downloader", "--argv-emulation",
                 "--hidden-import", "webview.platforms.cocoa", "--hidden-import", "WebKit", "--hidden-import", "Quartz",
                 "--hidden-import", "Security", "--hidden-import", "UniformTypeIdentifiers", "--hidden-import", "PyObjCTools.AppHelper"]
    run(*args)

    out = WORK / "dist"
    if SYSTEM == "Windows":
        iscc = shutil.which("iscc") or next((str(p) for p in [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"] if p.exists()), None)
        if iscc:
            wv2 = os.environ.get("MO_WEBVIEW2_SETUP", "")
            run(iscc, f"/DMyAppVersion={versions['app']}", f"/DSourceDir={out / NAME}", f"/DIconFile={ICO}",
                *([f"/DWebView2Setup={wv2}"] if wv2 and Path(wv2).exists() else []),
                f"/O{DIST}", KIT / "installer.iss")
            print("\nDONE:", DIST / f"MO-Downloader-{versions['app']}-Setup.exe")
        else:
            z = shutil.make_archive(str(DIST / f"MO-Downloader-{versions['app']}-windows-{ARCH}"), "zip", out, NAME)
            print("\nInno Setup not found -> portable zip instead:", z)
    elif SYSTEM == "Darwin":
        app = out / f"{NAME}.app"
        info = app / "Contents" / "Info.plist"
        with open(info, "rb") as f:
            pl = plistlib.load(f)
        pl.update(LSUIElement=True, CFBundleDisplayName=NAME, CFBundleShortVersionString=versions["app"],
                  CFBundleVersion=versions["app"], NSHighResolutionCapable=True,
                  CFBundleURLTypes=[{"CFBundleURLName": "MO Downloader", "CFBundleURLSchemes": ["mo-downloader"]}])
        with open(info, "wb") as f:
            plistlib.dump(pl, f)
        # ad-hoc sign every executable inside (ffmpeg, deno, libraries), then the app (required on Apple Silicon)
        magics = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
        for f in app.rglob("*"):
            if f.is_file() and not f.is_symlink():
                with open(f, "rb") as fh:
                    if fh.read(4) in magics:
                        os.chmod(f, f.stat().st_mode | 0o755)
                        subprocess.call(["codesign", "--force", "--sign", "-", str(f)])
        run("codesign", "--force", "--sign", "-", app)
        stage = WORK / "dmg"
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        os.symlink("/Applications", stage / "Applications")
        dmg = DIST / f"MO-Downloader-{versions['app']}-mac-{ARCH}.dmg"
        dmg.unlink(missing_ok=True)
        run("hdiutil", "create", "-volname", NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", dmg)
        print("\nDONE:", dmg)
    else:
        t = shutil.make_archive(str(DIST / f"MO-Downloader-{versions['app']}-linux-{ARCH}"), "gztar", out, NAME)
        print("\nDONE:", t)


if __name__ == "__main__":
    main()
