"""Assemble the single-file app: server.py + page.html + logo -> mo_downloader.py"""
import base64, sys
from pathlib import Path
src = Path(__file__).parent
page = (src / "page.html").read_text(encoding="utf-8")
assert '"""' not in page
code = (src / "server.py").read_text(encoding="utf-8")
code = code.replace("__LOGO_B64__", base64.b64encode((src / "logo128.png").read_bytes()).decode(), 1)
code = code.replace('r"""__PAGE__"""', 'r"""' + page + '"""', 1)
out = Path(sys.argv[1])
out.write_text(code, encoding="utf-8")
print("built", out, len(code))
