"""Assemble the single-file app: server.py + page.html + logo -> mo_downloader.py

The version number lives only in the VERSION file at the top of the project; this script writes it into the app,
the macOS bundle (Info.plist) and the read-me, and build-kit/build.py reads it from there too."""
import base64, re, sys
from pathlib import Path
src = Path(__file__).parent
root = src.parent
version = (root / "VERSION").read_text(encoding="utf-8").strip()
assert re.fullmatch(r"\d+(\.\d+){1,3}", version), version
page = (src / "page.html").read_text(encoding="utf-8")
assert '"""' not in page
code = (src / "server.py").read_text(encoding="utf-8")
code, n = re.subn(r'^APP_VERSION = "[^"]*"', f'APP_VERSION = "{version}"', code, count=1, flags=re.M)
assert n == 1, "APP_VERSION line not found"
code = code.replace("__LOGO_B64__", base64.b64encode((src / "logo128.png").read_bytes()).decode(), 1)
code = code.replace('r"""__PAGE__"""', 'r"""' + page + '"""', 1)
out = Path(sys.argv[1])
out.write_text(code, encoding="utf-8")

plist = root / "MO Downloader.app" / "Contents" / "Info.plist"
if plist.exists():
    t = plist.read_text(encoding="utf-8")
    t2 = re.sub(r"(<key>CFBundle(?:Short)?Version(?:String)?</key><string>)[^<]*(</string>)", rf"\g<1>{version}\g<2>", t)
    if t2 != t:
        plist.write_text(t2, encoding="utf-8")
readme = root / "اقرأني.txt"
if readme.exists():
    t = readme.read_text(encoding="utf-8")
    t2 = re.sub(r"\AMO Downloader [\d.]+", f"MO Downloader {version}", t)
    if t2 != t:
        readme.write_text(t2, encoding="utf-8")
print("built", out, len(code), "version", version)
