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
import urllib.parse
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
    # a longer stream in 1-second pieces (pause / resume of a stream that comes in pieces)
    sh(ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
       "-i", "sine=frequency=600", "-t", "24", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-b:v", "900k",
       "-c:a", "aac", "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
       "-hls_segment_filename", str(root / "long_%03d.ts"), str(root / "long.m3u8"))
    sh(ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi",
       "-i", "sine=frequency=700", "-t", "10", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "1500k",
       "-c:a", "aac", str(root / "big.mp4"))
    for i in range(4, 8):
        shutil.copy(root / "clip.mp4", root / f"clip{i}.mp4")
    (root / "list-ch.txt").write_text("clip4.mp4 clip5.mp4", encoding="utf-8")
    (root / "list-pl.txt").write_text("/slow/big.mp4 /slow/clip6.mp4 /slow/clip7.mp4", encoding="utf-8")
    # a video with 3 chapters (tools: split by chapters)
    (root / "chapters.txt").write_text(";FFMETADATA1\n" + "".join(
        f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={i * 4000}\nEND={(i + 1) * 4000}\ntitle=Part {i + 1}\n" for i in range(3)),
        encoding="utf-8")
    sh(ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
       "-i", "sine=frequency=500", "-i", str(root / "chapters.txt"), "-map_metadata", "2", "-map", "0:v", "-map", "1:a",
       "-t", "12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(root / "chaptered.mp4"))
    # a page with a video and an English subtitle track (subtitles)
    (root / "subs.vtt").write_text("WEBVTT\n\n00:00:00.500 --> 00:00:03.000\nHello subtitles\n\n"
                                   "00:00:03.500 --> 00:00:06.000\nSecond line\n", encoding="utf-8")
    (root / "subpage.html").write_text('<html><head><title>Subtitle page</title></head><body><video controls>'
                                       '<source src="/files/clip.mp4" type="video/mp4">'
                                       '<track kind="subtitles" srclang="en" label="English" src="/files/subs.vtt">'
                                       '</video></body></html>', encoding="utf-8")
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
    def __init__(self, home, extra_env=None, script=None):
        env = os.environ.copy()
        env.update(HOME=str(home), USERPROFILE=str(home), MD_PORT=str(APP_PORT), MD_NO_WINDOW="1", MD_NO_BROWSER="1",
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        env.update(extra_env or {})
        self.log = open(home / "app.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-u", str(script or APP)], env=env, stdout=self.log, stderr=subprocess.STDOUT)
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

    def kill(self):  # like a crash or a forced quit
        self.proc.kill()
        self.proc.wait(10)

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


def slow_hits():
    return json.loads(OPENER.open(SITE + "/slow-count").read())


def files_in(d):
    return sorted(str(p.relative_to(d)) for p in Path(d).rglob("*") if p.is_file()) if Path(d).exists() else []


def run_26_tests(app, home, tmp):
    data = home / ".media_downloader"
    out = home / "Downloads" / "MO Downloader"

    # --- the version number has one source
    ver = (REPO / "VERSION").read_text().strip()
    check("version comes from the VERSION file", app.api("/api/status").get("app") == ver, app.api("/api/status").get("app"))

    # --- error classes (also unit-tested in tests/test_errors.py)
    r = subprocess.run([sys.executable, str(REPO / "tests" / "test_errors.py")], capture_output=True, text=True)
    check("error classes: login / network / blocked recognised precisely", r.returncode == 0, (r.stdout + r.stderr).strip()[-300:])

    # --- history moved into the database, read in pages, delete one
    h = app.api("/api/history?limit=2")
    check("old history carried over into the database", any(x.get("title") == "Old download 1" for x in
          app.api("/api/history?limit=100&q=Old")["items"]) and not (data / "history.jsonl").exists(), f"total {h.get('total')}")
    check("history is read in pages", len(h["items"]) == 2 and h["more"] and h["total"] >= 5, f"{len(h['items'])} of {h['total']}")
    page2 = app.api("/api/history?limit=2&offset=2")["items"]
    check("history next page continues", page2 and page2[0].get("date") != h["items"][0].get("date"), "")
    n0 = app.api("/api/history?limit=1")["total"]
    app.api("/api/history/delete", {"key": "old 0"})
    check("history delete removes one entry", app.api("/api/history?limit=1")["total"] == n0 - 1, "")

    # --- cookies handed over by the extension are not left on disk
    left = [p.name for p in (data / "tmp").glob("ck-*")] if (data / "tmp").exists() else []
    check("temporary cookie files deleted after the download", not left, str(left))

    # --- clip: only part of a video
    j = app.wait(app.api("/api/download", {"url": SITE + "/files/clip2.mp4", "mode": "preset", "preset": "best",
                                           "clip_start": "1", "clip_end": "4"})["id"])
    d = duration(j["file"]) if j.get("file") else 0
    check("clip: only the chosen part", j["status"] == "done" and 2.5 <= d <= 4.5, f"{j['status']} · {d:.1f}s")

    # --- batch + skip what was downloaded before
    r = app.api("/api/batch", {"urls": f"{SITE}/files/clip3.mp4\n{SITE}/files/clip.mp4\n", "preset": "best", "skip": True})
    time.sleep(1)
    bj = [j for j in app.api("/api/jobs")["jobs"] if j.get("source") == "batch"]
    bj = [app.wait(j["id"]) for j in bj]
    skipped = [j["url"] for j in bj if "سابقاً" in (j.get("stage") or "")]
    check("batch of links queued", r.get("count") == 2 and len(bj) == 2, str(r))
    check("batch skips only what was already downloaded", skipped == [f"{SITE}/files/clip.mp4"],
          " | ".join(f"{j['url'].rsplit('/', 1)[1]}: {j['stage']}" for j in bj))

    # --- following: only new videos are downloaded
    s = app.api("/api/subs/add", {"url": SITE + "/list/ch.html", "start": "new", "preset": "best"})
    sid = s.get("sub", {}).get("id")
    check("follow: list added, nothing old downloaded", sid and s["sub"]["count_seen"] == 2, str(s)[:150])
    (tmp / "site" / "list-ch.txt").write_text("clip4.mp4 clip5.mp4 clip6.mp4", encoding="utf-8")
    app.api("/api/subs/check", {"id": sid})
    time.sleep(1)
    sj = [j for j in app.api("/api/jobs")["jobs"] if j.get("sub_id") == sid][-1]
    sj = app.wait(sj["id"])
    check("follow: the new video (only) downloaded", sj["status"] == "done" and sj.get("done_count") == 1,
          f"{sj['status']} · {sj.get('done_count')} · {sj['stage']}")

    # --- proxy: always through the user's proxy
    app.api("/api/settings", {"proxy": SITE, "proxy_mode": "always"})
    before = int(OPENER.open(SITE + "/proxy-count").read())
    j = app.wait(app.api("/api/download", {"url": f"http://localhost:{SITE_PORT}/files/clip7.mp4", "mode": "preset", "preset": "best"})["id"])
    hits = int(OPENER.open(SITE + "/proxy-count").read()) - before
    app.api("/api/settings", {"proxy": "", "proxy_mode": "fallback"})
    check("proxy: download goes through the proxy", j["status"] == "done" and hits >= 1, f"{j['status']} · {hits} proxied requests")

    # --- pause / resume a stream that comes in pieces: continues, does not start over
    jid = app.api("/api/download", {"url": SITE + "/slow/long.m3u8", "mode": "preset", "preset": "best", "title": "pause test"})["id"]
    for _ in range(240):
        j = app.job(jid)
        if (j.get("percent") or 0) >= 30:
            break
        time.sleep(0.25)
    app.api("/api/pause", {"id": jid})
    j = app.wait(jid, 30, until=("paused", "done", "error"))
    mid = sum(v for k, v in slow_hits().items() if k.startswith("long_"))
    app.api("/api/resume", {"id": jid})
    j = app.wait(jid, 180)
    hits = {k: v for k, v in slow_hits().items() if k.startswith("long_")}
    again = sum(v - 1 for v in hits.values() if v > 1)
    check("pause a stream in pieces", mid > 3, f"paused after {mid} pieces")
    check("resume continues where it stopped (pieces not fetched again)",
          j["status"] == "done" and len(hits) == 24 and again <= 3 and duration(j["file"]) > 22,
          f"{j['status']} · {len(hits)} pieces · {again} fetched again · {duration(j['file']) if j.get('file') else 0:.1f}s")

    # --- pause / resume a list: finished items are not downloaded again
    jid = app.api("/api/download", {"url": SITE + "/list/pl.html", "playlist": True, "mode": "preset", "preset": "best"})["id"]
    for _ in range(400):
        j = app.job(jid)
        if (j.get("done_count") or 0) >= 1 or j["status"] in ("done", "error"):
            break
        time.sleep(0.25)
    app.api("/api/pause", {"id": jid})
    j = app.wait(jid, 60, until=("paused", "done", "error"))
    paused_ok = j["status"] == "paused"
    first = dict(slow_hits())
    app.api("/api/resume", {"id": jid})
    j = app.wait(jid, 240)
    check("pause a list", paused_ok, j["status"])
    hits = slow_hits()
    check("resumed list finishes without downloading finished items again",
          j["status"] == "done" and (j.get("done_count") or 0) == 3 and hits.get("big.mp4") == first.get("big.mp4"),
          f"{j['status']} · {j.get('done_count')} items · big.mp4 requested {first.get('big.mp4')} → {hits.get('big.mp4')}")

    # --- the queue survives the app being closed (even forcibly)
    jid = app.api("/api/download", {"url": SITE + "/slow/big.mp4", "mode": "preset", "preset": "best", "title": "survives restart"})["id"]
    pid = app.api("/api/download", {"url": SITE + "/files/clip.mp4", "mode": "preset", "preset": "720", "title": "stays paused"})["id"]
    app.api("/api/pause", {"id": pid})
    for _ in range(120):
        if (app.job(jid).get("percent") or 0) > 10:
            break
        time.sleep(0.25)
    time.sleep(3)  # the queue is written every 2 seconds
    qmode = oct((data / "queue.json").stat().st_mode & 0o777) if (data / "queue.json").exists() else "missing"
    app.kill()
    app2 = App(home)
    try:
        jobs = {j["id"]: j for j in app2.api("/api/jobs")["jobs"]}
        check("queue saved privately (owner only)", qmode == "0o600", qmode)
        check("after restart: the running download is back", jid in jobs, str(list(jobs))[:100])
        check("after restart: the paused download is still paused", jobs.get(pid, {}).get("status") == "paused",
              jobs.get(pid, {}).get("status"))
        j = app2.wait(jid, 240) if jid in jobs else {}
        check("after restart: the download completes", j.get("status") == "done" and duration(j.get("file") or "") > 9,
              f"{j.get('status')} · {(j.get('error') or '')[:100]}")

        # --- the browser helper (native messaging): reports the port, and starts the app when it is not running
        ext_id = re.search(r'EXT_ID_CHROME = "([a-p]{32})"', APP.read_text()).group(1)
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "MD_PORT": str(APP_PORT), "MD_NO_WINDOW": "1",
               "MD_NO_BROWSER": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

        def native(msg):
            import struct
            raw = json.dumps(msg).encode()
            p = subprocess.run([sys.executable, str(APP), f"chrome-extension://{ext_id}/"], input=struct.pack("<I", len(raw)) + raw,
                               capture_output=True, env=env, timeout=60)
            n = struct.unpack("<I", p.stdout[:4])[0] if len(p.stdout) >= 4 else 0
            return json.loads(p.stdout[4:4 + n]) if n else {"raw": p.stdout[:200], "err": p.stderr[-300:]}
        port = int(app2.base.rsplit(":", 1)[1])
        r = native({"type": "ping"})
        check("browser helper reports the app's port", r.get("port") == port, str(r)[:200])
        reg = list((home / ".config").rglob("com.mo.downloader.json")) if (home / ".config").exists() else []
    finally:
        app2.stop()
    time.sleep(2)
    r = native({"type": "launch"})
    started = r.get("port")
    check("browser helper starts the app when it is closed (no browser prompt)", bool(started), str(r)[:200])
    if started:
        try:
            tok = re.search(r'const TOKEN = "([^"]+)"', OPENER.open(f"http://127.0.0.1:{started}/").read().decode()).group(1)
            OPENER.open(urllib.request.Request(f"http://127.0.0.1:{started}/api/shutdown", data=b"{}",
                        headers={"X-Token": tok, "Content-Type": "application/json"}), timeout=5)
        except Exception:
            pass
        time.sleep(2)


def ffinfo(fp):
    return subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_type,height", "-of", "json", fp], capture_output=True, text=True).stdout


def run_27_tests(tmp):
    """2.7: tools, after-download, subtitles, speed limit, scheduling, templates, notifications, preview,
    clipboard, phone, backup, self-update, English messages."""
    home = tmp / "home27"
    home.mkdir()
    clip_file = tmp / "clipboard.txt"
    clip_file.write_text("nothing yet", encoding="utf-8")
    site = tmp / "site"
    app = App(home, {"MO_CLIP_FILE": str(clip_file), "MD_PHONE_PORT": "8977"})
    out = home / "Downloads" / "MO Downloader"
    log = lambda: (home / "app.log").read_text(errors="replace")  # noqa: E731
    try:
        # --- tools on a file already on disk
        src = out / "source big.mp4"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(site / "big.mp4", src)
        before = src.stat().st_size
        j = app.wait(app.api("/api/tool", {"tool": "compress", "input": str(src), "target_mb": 1})["id"])
        size = os.path.getsize(j["file"]) if j.get("file") and os.path.exists(j["file"]) else 0
        check("tools: compress to a set size", j["status"] == "done" and 0 < size <= 1.1 * 1048576 and duration(j["file"]) > 9,
              f"{j['status']} · {before} → {size} bytes · {(j.get('error') or '')[:120]}")
        check("tools: the original file is not changed", src.stat().st_size == before, "")
        j = app.wait(app.api("/api/tool", {"tool": "resize", "input": str(src), "height": 360})["id"])
        check("tools: change resolution", j["status"] == "done" and '"height": 360' in ffinfo(j["file"]), (j.get("error") or "")[:120])
        j = app.wait(app.api("/api/tool", {"tool": "preset", "input": str(src), "preset": "mobile"})["id"])
        check("tools: ready-made target (phone)", j["status"] == "done" and '"height": 720' in ffinfo(j["file"]), (j.get("error") or "")[:120])
        j = app.wait(app.api("/api/tool", {"tool": "audio", "input": str(src), "format": "mp3"})["id"])
        check("tools: extract audio", j["status"] == "done" and j["file"].endswith(".mp3") and '"video"' not in ffinfo(j["file"]),
              (j.get("error") or "")[:120])
        ch = out / "chaptered.mp4"
        shutil.copy(site / "chaptered.mp4", ch)
        pr = app.api("/api/probe", {"path": str(ch)})
        check("tools: chapters read from the file", len(pr.get("chapters") or []) == 3, str(pr)[:150])
        j = app.wait(app.api("/api/tool", {"tool": "chapters", "input": str(ch)})["id"])
        parts = sorted((out / "chaptered - chapters").glob("*.mp4")) if (out / "chaptered - chapters").exists() else []
        check("tools: split by chapters", j["status"] == "done" and len(parts) == 3 and all(3 < duration(str(x)) < 5.5 for x in parts),
              f"{j['status']} · {[x.name for x in parts]}")
        r = app.api("/api/tool", {"tool": "compress", "input": str(out / "missing.mp4"), "target_mb": 5})
        check("tools: a missing file is refused with a reason", "غير موجود" in (r.get("error") or ""), str(r))

        # --- split by chapters while downloading
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/chaptered.mp4", "mode": "preset", "preset": "best",
                                               "split_chapters": True})["id"])
        parts = sorted(Path(j["outdir"]).rglob("* - chapters/*.mp4")) if j.get("outdir") else []
        check("download: split by chapters", j["status"] == "done" and len(parts) == 3 and all(3.5 < duration(str(x)) < 4.6 for x in parts),
              f"{j['status']} · {[(x.name, round(duration(str(x)), 1)) for x in parts]}")

        # --- after download: convert automatically
        jid = app.api("/api/download", {"url": SITE + "/files/clip3.mp4", "mode": "preset", "preset": "best",
                                        "after": {"tool": "audio", "format": "m4a"}})["id"]
        app.wait(jid)
        time.sleep(1)
        conv = [x for x in app.api("/api/jobs")["jobs"] if x.get("source") == "tool" and x.get("source_label") == "بعد التنزيل"]
        c = app.wait(conv[0]["id"]) if conv else {}
        check("after download: converted automatically", c.get("status") == "done" and (c.get("file") or "").endswith(".m4a"),
              f"{c.get('status')} · {c.get('file')}")

        # --- subtitles: language, separate file, embedded, the 3D tool
        tool = tmp / "fake3d.py"
        tool.write_text("import sys, pathlib\np = pathlib.Path(sys.argv[1])\n"
                        "p.with_suffix('.3d.ass').write_text('[Script Info]\\n' + p.read_text(encoding='utf-8'), encoding='utf-8')\n")
        app.api("/api/settings", {"sub3d_cmd": f'"{sys.executable}" "{tool}" {{file}}'})
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/subpage.html", "mode": "preset", "preset": "best",
                                               "subs": True, "sub_langs": "en", "sub_mode": "both", "sub3d": True})["id"])
        srts = list(Path(j["outdir"]).glob("*.en.srt")) if j.get("outdir") else []
        ass = list(Path(j["outdir"]).glob("*.3d.ass")) if j.get("outdir") else []
        check("subtitles: saved as a separate .srt file", j["status"] == "done" and srts and "Hello subtitles" in srts[0].read_text(),
              f"{j['status']} · {[x.name for x in srts]} · {(j.get('error') or '')[:120]}")
        check("subtitles: embedded in the video", j.get("file") and '"subtitle"' in ffinfo(j["file"]), "")
        check("subtitles: the 3D subtitle tool ran on the .srt", bool(ass), str([x.name for x in ass]))

        # --- speed limit
        app.api("/api/settings", {"rate_limit_kb": 150})
        t0 = time.time()
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/big.mp4?limit", "mode": "preset", "preset": "best",
                                               "title": "limited"})["id"], 180)
        took = time.time() - t0
        size = os.path.getsize(j["file"]) if j.get("file") else 0
        app.api("/api/settings", {"rate_limit_kb": 0})
        check("speed limit respected", j["status"] == "done" and took >= size / (150 * 1024) * 0.8,
              f"{size} bytes in {took:.1f}s (limit 150 KB/s → at least {size / 153600:.1f}s)")

        # --- start at a set time
        at = time.time() + 8
        jid = app.api("/api/download", {"url": SITE + "/files/clip4.mp4", "mode": "preset", "preset": "best", "start_at": at})["id"]
        time.sleep(3)
        early = app.job(jid)
        j = app.wait(jid, 60)
        check("scheduled download waits for its time", early["status"] == "queued" and "مجدول" in early["stage"]
              and j["status"] == "done" and j.get("started", at) >= at - 1,
              f"{early['status']} · {early['stage']} → {j['status']}")

        # --- file name and folder templates
        bad = app.api("/api/settings", {"name_tpl": "{uploader}"})
        check("template without {title} or {id} refused", "{title}" in (bad.get("error") or ""), str(bad)[:120])
        app.api("/api/settings", {"name_tpl": "{site} - {title}", "folder_tpl": "{site}/test-{year}"})
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/clip5.mp4", "mode": "preset", "preset": "best"})["id"])
        rel = os.path.relpath(j.get("file") or "", out)
        app.api("/api/settings", {"name_tpl": "", "folder_tpl": ""})
        check("file name and folder templates applied", j["status"] == "done" and re.match(r"Generic[\\/]test-[^\\/]+[\\/]Generic - clip5", rel),
              rel)

        # --- notifications
        check("notification sent when a download finishes", "[notify]" in log() and "clip5" in log(), "")

        # --- preview inside the window
        f = j["file"]
        q = urllib.parse.urlencode({"t": app.token, "path": f})
        r = OPENER.open(urllib.request.Request(f"{app.base}/api/media?{q}", headers={"Range": "bytes=0-99"}))
        check("preview: file served in parts (seeking works)", r.status == 206 and len(r.read()) == 100
              and r.headers.get("Content-Type") == "video/mp4", f"{r.status} {r.headers.get('Content-Range')}")
        outside = tmp / "secret.mp4"
        outside.write_bytes(b"x" * 10)
        try:
            code = OPENER.open(f"{app.base}/api/media?" + urllib.parse.urlencode({"t": app.token, "path": str(outside)})).status
        except urllib.error.HTTPError as e:
            code = e.code
        try:
            code2 = OPENER.open(f"{app.base}/api/media?" + urllib.parse.urlencode({"t": "wrong", "path": f})).status
        except urllib.error.HTTPError as e:
            code2 = e.code
        check("preview: other files and a wrong token are refused", code == 403 and code2 == 403, f"{code} {code2}")

        # --- clipboard
        app.api("/api/settings", {"clipboard": True})
        time.sleep(2)
        clip_file.write_text(SITE + "/files/clip6.mp4", encoding="utf-8")
        sug = None
        for _ in range(20):
            sug = app.api("/api/clip").get("clip")
            if sug:
                break
            time.sleep(0.5)
        clip_file.write_text("just some text", encoding="utf-8")
        time.sleep(2)
        app.api("/api/clip_dismiss", {})
        clip_file.write_text("https://example.com/about-us", encoding="utf-8")
        time.sleep(3)
        check("clipboard: a copied video link is suggested", (sug or {}).get("url") == SITE + "/files/clip6.mp4", str(sug))
        check("clipboard: plain text and ordinary pages are not suggested", app.api("/api/clip").get("clip") is None, "")
        app.api("/api/settings", {"clipboard": False})

        # --- links from a phone
        r = app.api("/api/settings", {"phone": True})
        check("phone: needs a password first", "كلمة مرور" in (r.get("error") or ""), str(r)[:100])
        app.api("/api/settings", {"phone": True, "phone_pin": "4321"})
        ph = app.api("/api/phone", {})
        key = re.search(r"k=([^&]+)", ph.get("url", "")).group(1) if "k=" in ph.get("url", "") else ""
        port = int(re.search(r":(\d+)/m", ph.get("url", ":0/m")).group(1))
        check("phone: listening, with a QR code", ph.get("on") and key and "<svg" in (ph.get("svg") or ""), ph.get("url", "")[:80])
        page = OPENER.open(f"http://127.0.0.1:{port}/m?k={key}").read().decode()

        def send(pin, k=key):
            rq = urllib.request.Request(f"http://127.0.0.1:{port}/m/add", data=json.dumps(
                {"k": k, "pin": pin, "url": SITE + "/files/clip7.mp4", "preset": "best"}).encode(),
                headers={"Content-Type": "application/json"})
            try:
                return OPENER.open(rq).status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            nokey = OPENER.open(f"http://127.0.0.1:{port}/m?k=wrong").status
        except urllib.error.HTTPError as e:
            nokey = e.code
        check("phone: page opens only with the code from the QR", "MO Downloader" in page and nokey == 404, str(nokey))
        wrong, right = send("0000"), send("4321")
        time.sleep(1)
        got = [x for x in app.api("/api/jobs")["jobs"] if x.get("source") == "phone"]
        check("phone: wrong password refused, right one starts the download", wrong == 403 and right == 200 and len(got) == 1,
              f"{wrong} {right} · {len(got)} job(s)")
        codes = [send("1111") for _ in range(6)]
        check("phone: too many wrong passwords → locked for a while", codes[-1] == 429 and send("4321") == 429, str(codes))
        app.api("/api/settings", {"phone": False})
        time.sleep(1)
        try:
            OPENER.open(f"http://127.0.0.1:{port}/m?k={key}", timeout=3)
            stopped = False
        except Exception:
            stopped = True
        check("phone: turning it off stops listening", stopped, "")

        # --- backup: export and import
        app.api("/api/subs/add", {"url": SITE + "/list/ch.html", "start": "new", "preset": "best"})
        ex = app.api("/api/export", {})
        data = json.loads(Path(ex["file"]).read_text(encoding="utf-8"))
        check("backup: exported settings, channels and history", ex.get("ok") and data["subs"] and len(data["history"]) >= 5
              and "phone_key" not in data["settings"], f"{ex.get('history')} history · {ex.get('subs')} channels")
        h = app.api("/api/history?limit=1")["items"][0]
        app.api("/api/history/delete", {"key": h.get("key"), "date": h.get("date")})
        app.api("/api/subs/delete", {"id": app.api("/api/subs")["subs"][0]["id"]})
        r = app.api("/api/import", {"data": data, "parts": ["history", "subs"]})
        check("backup: import restores what was removed and skips what is there", r.get("history") == 1 and r.get("subs") == 1, str(r))
        r = app.api("/api/import", {"data": {"hello": 1}, "parts": ["settings"]})
        check("backup: a file that isn't a backup is refused", "نسخة احتياطية" in (r.get("error") or ""), str(r)[:100])

        # --- updating the app itself (a local stand-in for the releases page)
        def wait_update():
            for _ in range(60):
                u = app.api("/api/status")["update"]
                if u.get("state") not in ("checking", "downloading", ""):
                    return u
                time.sleep(0.5)
            return app.api("/api/status")["update"]
        app.api("/api/settings", {"update_url": SITE + "/release/old.json"})
        app.api("/api/update_check", {})
        u = wait_update()
        check("update: says when this is the latest version", u.get("state") == "current", u.get("msg"))
        app.api("/api/settings", {"update_url": SITE + "/release/bad.json"})
        app.api("/api/update_check", {})
        u = wait_update()
        check("update: a file whose fingerprint doesn't match is rejected", u.get("state") == "error" and "البصمة" in (u.get("msg") or ""),
              u.get("msg"))
        app.api("/api/settings", {"update_url": SITE + "/release/new.json"})
        app.api("/api/update_check", {"auto": False})
        u = wait_update()
        upd = home / ".media_downloader" / "updates" / "MO-Downloader-9.9.zip"
        check("update: a newer version is downloaded and checked", u.get("state") == "ready" and u.get("latest") == "9.9"
              and upd.exists() and upd.read_bytes() == (site / "clip.mp4").read_bytes(), u.get("msg"))

        # --- automatic update: a copy of the app replaces itself with a newer version and starts again
        import zipfile
        newer = APP.read_text(encoding="utf-8").replace(f'APP_VERSION = "{app.api("/api/status")["app"]}"', 'APP_VERSION = "9.9"', 1)
        with zipfile.ZipFile(site / "update.zip", "w") as z:
            z.writestr("MO Downloader 9.9/app/mo_downloader.py", newer)
        copy = tmp / "appcopy" / "app" / "mo_downloader.py"
        copy.parent.mkdir(parents=True)
        shutil.copy(APP, copy)
        home_u = tmp / "home-update"
        home_u.mkdir()
        old = App(home_u, {"MD_PORT": "8955"}, script=copy)
        port = int(old.base.rsplit(":", 1)[1])
        old.api("/api/settings", {"update_url": SITE + "/release/zip.json"})
        old.api("/api/update_check", {})
        new, newport = None, None
        for _ in range(120):
            time.sleep(0.5)
            for pp in range(port, port + 10):
                try:
                    p = json.loads(OPENER.open(f"http://127.0.0.1:{pp}/api/ping", timeout=0.5).read())
                    if p.get("version") == "9.9":
                        new, newport = p, pp
                        break
                except Exception:
                    pass
            if new:
                break
        check("automatic update: downloaded, installed and reopened as the new version", bool(new)
              and old.proc.poll() is not None and 'APP_VERSION = "9.9"' in copy.read_text(encoding="utf-8"),
              f"{new} · port {port} → {newport} · old process exit {old.proc.poll()}")
        check("automatic update: back on the same port", newport == port, f"{port} → {newport}")
        try:
            tok = re.search(r'const TOKEN = "([^"]+)"', OPENER.open(f"http://127.0.0.1:{newport}/").read().decode()).group(1)
            OPENER.open(urllib.request.Request(f"http://127.0.0.1:{newport}/api/shutdown", data=b"{}",
                        headers={"X-Token": tok, "Content-Type": "application/json"}), timeout=5)
        except Exception:
            pass
        if old.proc.poll() is None:
            old.proc.kill()

        # --- English messages
        app.api("/api/settings", {"lang": "en"})
        page = OPENER.open(app.base + "/").read().decode()
        j = app.wait(app.api("/api/download", {"url": SITE + "/files/clip2.mp4", "mode": "preset", "preset": "720"})["id"])
        time.sleep(0.5)
        check("English: the page starts in English", 'const LANG = "en"' in page, "")
        check("English: notifications in English", "[notify] Download finished ✓" in log(), log().strip().splitlines()[-1][:120])
        app.api("/api/settings", {"lang": "ar"})
    finally:
        app.stop()


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
    # history kept by an earlier version (one JSON line per download) must be carried into the new database
    data = home / ".media_downloader"
    data.mkdir()
    (data / "history.jsonl").write_text("".join(json.dumps({"key": f"old {i}", "title": f"Old download {i}",
                                                            "date": 1700000000 + i, "quality": "720p · mp4"}) + "\n"
                                                for i in range(3)), encoding="utf-8")
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

        run_26_tests(app, home, tmp)
        run_27_tests(tmp)

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
