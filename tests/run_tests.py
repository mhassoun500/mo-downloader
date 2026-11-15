#!/usr/bin/env python3
"""MO Downloader automated tests — run against the source version of the app.

    python tests/run_tests.py            # local tests only (no internet needed)
    python tests/run_tests.py --network  # also tests that use real public sites (gallery engine)

Needs ffmpeg on PATH. Starts a local test site and the app (in its own temporary home folder), drives the
app through its local API exactly like the interface does, and checks the files it produces.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app" / "mo_downloader.py"
SITE_PORT, APP_PORT = 8931, 8795
SITE = f"http://127.0.0.1:{SITE_PORT}"
NETWORK = "--network" in sys.argv
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local calls never go through a proxy
RESULTS = []


def sh(*cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def make_media(root):
    ff = shutil.which("ffmpeg") or sys.exit("ffmpeg is required for the tests")
    sh(ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi",
       "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
       str(root / "clip.mp4"))
    shutil.copy(root / "clip.mp4", root / "clip2.mp4")
    shutil.copy(root / "clip.mp4", root / "clip3.mp4")
    sh(ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi",
       "-i", "sine=frequency=330", "-t", "8", "-filter_complex", "[0:v]split=2[a][b];[b]scale=640:360[b2]",
       "-map", "[a]", "-map", "[b2]", "-map", "1:a", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a", "aac", "-b:v:0", "2000k", "-b:v:1", "600k", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
       "-master_pl_name", "master.m3u8", "-var_stream_map", "v:0,a:0 v:1,a:1", str(root / "hls_%v.m3u8"))
    (root / "nomedia.html").write_text("<html><head><title>No media here</title></head><body><p>Just text.</p></body></html>",
                                       encoding="utf-8")
    (root / "live").mkdir()


def start_live(root):
    ff = shutil.which("ffmpeg")
    return subprocess.Popen([ff, "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                             "-f", "lavfi", "-i", "sine=frequency=520", "-c:v", "libx264", "-preset", "ultrafast",
                             "-g", "25", "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
                             "-hls_flags", "delete_segments", str(root / "live" / "index.m3u8")],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_url(url, secs=40):
    for _ in range(secs * 4):
        try:
            OPENER.open(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


class App:
    def __init__(self, home):
        env = os.environ.copy()
        env.update(HOME=str(home), USERPROFILE=str(home), MD_PORT=str(APP_PORT), MD_NO_WINDOW="1", MD_NO_BROWSER="1",
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        self.log = open(home / "app.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-u", str(APP)], env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.base = None
        for _ in range(360):  # the app may pick the next free port; read it from its start-up line
            m = re.search(r"is running:\s+(http://127\.0\.0\.1:\d+)/", (home / "app.log").read_text(errors="replace"))
            if m:
                self.base = m.group(1)
                break
            time.sleep(0.25)
        if not self.base or not wait_url(self.base + "/api/ping", 30):
            print((home / "app.log").read_text()[-3000:])
            sys.exit("the app did not start")
        html = OPENER.open(self.base + "/").read().decode()
        self.token = re.search(r'const TOKEN = "([^"]+)"', html).group(1)

    def api(self, path, body=None, ext=False):
        hdr = {"Content-Type": "application/json", **({"X-MO-Ext": "1"} if ext else {"X-Token": self.token})}
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=hdr)
        try:
            return json.loads(OPENER.open(r, timeout=300).read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")

    def job(self, jid):
        return next(j for j in self.api("/api/jobs")["jobs"] if j["id"] == jid)

    def wait(self, jid, secs=120, until=("done", "error", "cancelled", "paused")):
        for _ in range(secs * 4):
            j = self.job(jid)
            if j["status"] in until:
                return j
            time.sleep(0.25)
        return self.job(jid)

    def stop(self):
        try:
            self.api("/api/shutdown", {})
        except Exception:
            pass
        try:
            self.proc.wait(10)
        except Exception:
            self.proc.kill()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail else ""), flush=True)


def duration(fp):
    out = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", fp], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mo-tests-"))
    site_root, home = tmp / "site", tmp / "home"
    site_root.mkdir()
    home.mkdir()
    print("preparing media…", flush=True)
    make_media(site_root)
    site = subprocess.Popen([sys.executable, str(Path(__file__).with_name("testsite.py")), str(site_root), str(SITE_PORT)])
    live = start_live(site_root)
    assert wait_url(SITE + "/files/clip.mp4")
    app = App(home)
    try:
        st = app.api("/api/status")
        print(f"app {st['app']} · yt-dlp {st['ytdlp']} · gallery-dl {st.get('gallery')} · impersonation {st.get('impersonate')}")
        check("engines: gallery-dl available", st.get("gallery"))
        check("engines: browser impersonation available", st.get("impersonate"))

        # 1) plain download still works
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/clip.mp4", "mode": "preset", "preset": "best"})["id"])
        check("direct download", j["status"] == "done" and j.get("file") and duration(j["file"]) > 5, j.get("error") or j.get("method"))

        # 2) bot protection: refused until the request looks like a real browser
        j = app.wait(app.api("/api/ext/download", {"url": SITE + "/browser-only/clip2.mp4", "kind": "page", "preset": "best"}, ext=True)["id"])
        check("blocked site -> retried as a real browser", j["status"] == "done" and "انتحال" in (j.get("method") or ""),
              f"{j['status']} · {j.get('method')} · {(j.get('error') or '')[:120]}")
        info = app.api("/api/info", {"url": SITE + "/browser-only/clip3.mp4"})
        check("blocked site -> quality list read as a real browser", info.get("type") == "video" and "انتحال" in (info.get("method") or ""),
              str(info.get("method") or info.get("error", ""))[:150])

        # 3) login needed: the browser's cookies are used only after the first try fails
        ck = f"# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t1893456000\tauth\t1\n"
        j = app.wait(app.api("/api/ext/download", {"url": SITE + "/protected/clip.mp4", "kind": "page", "preset": "best",
                                                   "cookies": ck}, ext=True)["id"])
        check("login needed -> retried with the browser's login", j["status"] == "done" and "تسجيل" in (j.get("method") or ""),
              f"{j['status']} · {j.get('method')}")

        # 3b) a player that signs its requests with its own header: the header the extension saw is sent along
        f = app.api("/api/ext/formats", {"url": SITE + "/hdr-only/master.m3u8", "headers": {"X-Auth": "letmein"}}, ext=True)
        check("caught stream qualities read with the player's header", f.get("heights") == [720, 360], str(f)[:150])
        j = app.wait(app.api("/api/ext/download", {"url": SITE + "/hdr-only/master.m3u8", "kind": "media", "preset": "360",
                                                   "title": "Header test", "headers": {"X-Auth": "letmein", "Cookie": "x=1"}}, ext=True)["id"])
        check("caught stream downloaded with the player's header", j["status"] == "done" and j.get("file") and duration(j["file"]) > 5,
              f"{j['status']} · {(j.get('error') or '')[:150]}")

        # 4) page without a video, but the extension caught a stream on it -> the caught stream is used
        j = app.wait(app.api("/api/ext/download", {
            "url": SITE + "/nomedia.html", "kind": "page", "preset": "best", "title": "Caught stream test",
            "alt_streams": [{"url": SITE + "/files/master.m3u8", "headers": {"Referer": SITE + "/nomedia.html"}, "title": "x"}]},
            ext=True)["id"])
        check("no video on page -> stream caught by the extension used", j["status"] == "done" and "البث الملتقط" in (j.get("method") or ""),
              f"{j['status']} · {j.get('method')} · {(j.get('error') or '')[:120]}")

        # 5) page without any video and nothing caught -> a clear failure + an error report without secrets
        j = app.wait(app.api("/api/ext/download", {"url": SITE + "/nomedia.html?token=SECRET123", "kind": "page",
                                                   "cookies": "# x\n127.0.0.1\tFALSE\t/\tFALSE\t1893456000\tsid\tCOOKIEVALUE\n"}, ext=True)["id"])
        rep = app.api("/api/report", {"id": j["id"]}).get("text", "")
        check("no video anywhere -> clear failure", j["status"] == "error" and "فيديو" in (j.get("error") or ""), (j.get("error") or "")[:160])
        check("error report has attempts, no cookies or tokens",
              "attempts:" in rep and "SECRET123" not in rep and "COOKIEVALUE" not in rep, rep.replace("\n", " | ")[:200])

        # 6) live stream: record from now, stop, file is kept and playable
        info = app.api("/api/info", {"url": SITE + "/live/index.m3u8"})
        check("live stream detected", info.get("is_live"), str(info.get("live_status") or info.get("error", ""))[:120])
        jid = app.api("/api/download", {"url": SITE + "/live/index.m3u8", "mode": "preset", "preset": "best", "live": "now",
                                        "title": "live test"})["id"]
        time.sleep(9)
        mid = app.job(jid)
        app.api("/api/stop_live", {"id": jid})
        j = app.wait(jid, 60)
        d = duration(j["file"]) if j.get("file") and os.path.exists(j["file"]) else 0
        check("live recording -> stop keeps a playable file", j["status"] == "done" and d >= 3,
              f"{mid['status']} → {j['status']} · {j['stage']} · {d:.1f}s · {(j.get('error') or '')[:100]}")

        # 7) supported sites + link tester
        sites = app.api("/api/sites").get("sites", [])
        ng = sum(1 for x in sites if x["e"] == "gallery-dl")
        check("supported sites list (both engines)", len(sites) > 1500 and ng > 100, f"{len(sites)} total · {ng} gallery-dl")
        t = app.api("/api/test_url", {"url": SITE + "/browser-only/clip.mp4"})
        check("link tester reports the method that worked", (t.get("probe") or {}).get("method", "").startswith("انتحال"),
              json.dumps(t, ensure_ascii=False)[:200])

        if NETWORK:
            # 8) a public image album: the first engine finds nothing, the gallery engine downloads the files
            url = "https://imgur.com/a/TcBmP"
            info = app.api("/api/info", {"url": url})
            check("[network] gallery link recognised", info.get("type") == "gallery", str(info)[:150])
            j = app.wait(app.api("/api/ext/download", {"url": url, "kind": "page", "preset": "best"}, ext=True)["id"], 180)
            check("[network] gallery downloaded by the second engine",
                  j["status"] == "done" and (j.get("done_count") or 0) >= 2 and "gallery" in (j.get("method") or ""),
                  f"{j['status']} · {j.get('done_count')} files · {j.get('method')} · {(j.get('error') or '')[:120]}")
        if os.environ.get("MO_TEST_DEBUG"):
            print("REPORT:\n" + rep)
            print("FILES:", [str(p) for p in (home / "Downloads").rglob("*")])
            print("LIVEJOB:", json.dumps(app.job(jid), ensure_ascii=False)[:1500])
    finally:
        app.stop()
        live.terminate()
        site.terminate()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("app log tail:\n" + (home / "app.log").read_text(errors="replace")[-2500:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
