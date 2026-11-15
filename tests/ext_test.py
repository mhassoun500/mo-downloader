#!/usr/bin/env python3
"""MO Downloader browser-extension tests in a real (automated) Chromium.

    pip install playwright && python -m playwright install chromium
    python tests/ext_test.py

Loads browser-extension/ into Chromium, starts the local test site and the app (own temporary home folder), and
checks what the extension does in a browser: its fixed ID, starting the app through the native helper (no
"Open MO Downloader?" prompt), the download button over videos and images, which browser downloads are handed to
the app, and caught streams (their headers, qualities, embedded players, and the fallback for a page without video).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tests as rt  # noqa: E402  (shared helpers: media, test site, the App driver)

EXT = rt.REPO / "browser-extension"
SITE = rt.SITE


def page_files(site):
    pages = site / "pages"
    pages.mkdir()
    (pages / "player.html").write_text("""<html><head><meta charset="utf-8"><title>Hidden Player Page</title></head><body>
<div id="player" style="width:640px;height:360px;background:#000"></div>
<script>fetch("/hdr-only/master.m3u8", {headers: {"X-Auth": "letmein"}}).then(r => r.text())
  .then(t => document.getElementById("player").textContent = "stream loaded");</script></body></html>""", encoding="utf-8")
    (pages / "embed.html").write_text("""<html><head><meta charset="utf-8"><title>Embed Page</title>
<meta property="og:video" content="/files/clip2.mp4"></head><body>
<iframe src="/pages/inner.html" width="640" height="360"></iframe></body></html>""", encoding="utf-8")
    (pages / "inner.html").write_text("""<html><head><title>Inner</title></head><body style="margin:0">
<video src="/files/clip.mp4" width="620" height="340" controls></video></body></html>""", encoding="utf-8")
    (pages / "hover.html").write_text("""<html><head><meta charset="utf-8"><title>Hover</title></head><body style="margin:0">
<video id="v" src="/files/clip.mp4" width="640" height="360" style="position:absolute;top:20px;left:20px"></video>
<div style="height:3000px"></div>
<img id="later" src="/files/pic.png" width="400" height="300" style="position:absolute;top:2200px;left:20px">
</body></html>""", encoding="utf-8")
    (pages / "links.html").write_text("""<html><head><meta charset="utf-8"><title>Links</title></head><body>
<a id="mp4" href="/files/clip3.mp4" download>video</a> <a id="txt" href="/files/note.txt" download>text</a></body></html>""",
                                      encoding="utf-8")
    (site / "note.txt").write_text("hello", encoding="utf-8")
    subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=400x300",
                    "-frames:v", "1", str(site / "pic.png")], check=True)


async def main_async(app, home):
    from playwright.async_api import async_playwright
    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    expected_id = rt.re.search(r'EXT_ID_CHROME = "([a-p]{32})"', rt.APP.read_text()).group(1)
    env = {**os.environ, "HOME": str(home), "MD_PORT": str(rt.APP_PORT), "MD_NO_WINDOW": "1", "MD_NO_BROWSER": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    async with async_playwright() as p:
        # Chromium reads user-level native helpers from its profile folder (~/.config/chromium normally); the test
        # browser uses a temporary profile, so put the manifest the app registered there
        udd = Path(tempfile.mkdtemp())
        (udd / "NativeMessagingHosts").mkdir()
        for f in (home / ".config" / "chromium" / "NativeMessagingHosts").glob("*.json"):
            shutil.copy(f, udd / "NativeMessagingHosts" / f.name)
        ctx = await p.chromium.launch_persistent_context(
            str(udd), headless=True, channel="chromium", env=env, accept_downloads=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--autoplay-policy=no-user-gesture-required"])
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=20000)
        eid = sw.url.split("/")[2]
        rt.check("extension has its fixed ID (needed by the native helper)", eid == expected_id, eid)

        pop = await ctx.new_page()
        await pop.goto(f"chrome-extension://{eid}/popup.html")
        await pop.wait_for_timeout(600)

        async def msg(m):
            return await pop.evaluate(f"() => new Promise(r => chrome.runtime.sendMessage({json.dumps(m)}, r))")

        # --- the native helper: with the app closed, the extension starts it without any browser prompt
        app.stop()
        time.sleep(2)
        r = await pop.evaluate("() => chrome.runtime.sendNativeMessage('com.mo.downloader', {type: 'ping'}).catch(e => ({error: String(e)}))")
        rt.check("native helper answers (app closed: no port)", r.get("ok") is False and not r.get("port"), str(r)[:200])
        r = await msg({"type": "launch"})  # what the extension does when the app is needed and closed
        st = await msg({"type": "status"})
        port = int(st["url"].rstrip("/").rsplit(":", 1)[1]) if st.get("url") else None
        rt.check("extension starts the closed app through its helper (no 'Open?' prompt)", bool(port), str(r)[:200])
        rt.check("extension finds the app it started", st.get("running"), str(st)[:150])
        if port:
            app.base = f"http://127.0.0.1:{port}"
            html = o.open(app.base + "/").read().decode()
            app.token = rt.re.search(r'const TOKEN = "([^"]+)"', html).group(1)

        # --- which browser downloads go to the app
        cases = await sw.evaluate("""() => {
          const o = {captureMinMB: 2, captureExts: "mp4 zip", captureSkip: "drive.google.com"};
          const t = (name, url, size, ref) => wantDownload({filename: name, totalBytes: size, referrer: ref || ""}, url, o);
          return {
            bigVideo: t("/d/a.mp4", "https://x.com/a.mp4", 50e6),
            smallVideo: t("/d/a.mp4", "https://x.com/a.mp4", 100e3),
            unknownSize: t("/d/a.mp4", "https://x.com/a.mp4", -1),
            otherType: t("/d/a.pdf", "https://x.com/a.pdf", 50e6),
            skippedSite: t("/d/a.zip", "https://drive.google.com/u/a.zip", 50e6),
            skippedReferrer: t("/d/a.zip", "https://cdn.x.com/a.zip", 50e6, "https://drive.google.com/file/1"),
          };
        }""")
        want = {"bigVideo": True, "smallVideo": False, "unknownSize": True, "otherType": False, "skippedSite": False,
                "skippedReferrer": False}
        rt.check("browser downloads: size / type / site filters", cases == want, json.dumps(cases))

        await pop.evaluate("() => chrome.storage.local.set({capture: true, captureMinMB: 0, captureExts: 'mp4'})")
        lp = await ctx.new_page()
        await lp.goto(SITE + "/pages/links.html")
        before = {j["id"] for j in app.api("/api/jobs")["jobs"]}
        in_browser = []
        lp.on("download", lambda d: in_browser.append(d.suggested_filename))
        await lp.click("#mp4")
        await lp.click("#txt")
        await lp.wait_for_timeout(4000)
        txt_in_browser = "note.txt" in in_browser
        new = [j for j in app.api("/api/jobs")["jobs"] if j["id"] not in before]
        rt.check("browser download of a video handed to the app", any("clip3" in (j.get("url") or "") for j in new),
                 str([j.get("url") for j in new]))
        rt.check("other file types stay in the browser", txt_in_browser and not any("note.txt" in (j.get("url") or "") for j in new), "")
        await pop.evaluate("() => chrome.storage.local.set({capture: false})")

        # --- download button over videos and images (on-screen elements tracked by an IntersectionObserver)
        hp = await ctx.new_page()
        await hp.set_viewport_size({"width": 1000, "height": 700})
        await hp.goto(SITE + "/pages/hover.html")
        await hp.wait_for_timeout(800)
        attached = "() => !!document.querySelector('mo-downloader-ui')"
        await hp.mouse.move(300, 200)
        await hp.wait_for_timeout(400)
        on_video = await hp.evaluate(attached)
        await hp.mouse.move(300, 200 + 1)
        await hp.evaluate("() => window.scrollTo(0, 2100)")
        await hp.wait_for_timeout(500)
        await hp.mouse.move(200, 250)
        await hp.wait_for_timeout(500)
        shot = home / "hover.png"
        await hp.screenshot(path=str(shot))
        rt.check("download button appears over a video", on_video, "")
        rt.check("…and over an image scrolled into view later", await hp.evaluate(attached), str(shot))

        # --- caught streams: the player's own header, qualities, embedded players, fallback for the page
        pg = await ctx.new_page()
        await pg.goto(SITE + "/pages/player.html")
        await pg.wait_for_timeout(2500)
        em = await ctx.new_page()
        await em.goto(SITE + "/pages/embed.html")
        await em.wait_for_timeout(3500)
        tid = lambda u: pop.evaluate(f"async () => (await chrome.tabs.query({{url: '{u}'}}))[0].id")
        t1, t2 = await tid(SITE + "/pages/player.html"), await tid(SITE + "/pages/embed.html")
        m1 = (await msg({"type": "media", "tabId": t1}))["media"]
        hls = next((m for m in m1 if m["kind"] == "HLS"), {})
        rt.check("stream caught with the player's header", (hls.get("headers") or {}).get("X-Auth") == "letmein", str(hls)[:200])
        m2 = (await msg({"type": "media", "tabId": t2}))["media"]
        kinds = sorted({m["kind"] for m in m2})
        rt.check("embedded player and page video found", "EMBED" in kinds, str(kinds))
        f = await msg({"type": "formats", "tabId": t1, "url": hls.get("url")})
        rt.check("qualities of a caught stream", f.get("heights") == [720, 360], str(f)[:150])
        before = {j["id"] for j in app.api("/api/jobs")["jobs"]}
        await msg({"type": "download", "tabId": t1, "url": SITE + "/pages/player.html", "title": "Hidden Player Page",
                   "preset": "best", "page": SITE + "/pages/player.html"})
        await msg({"type": "downloadMedia", "tabId": t1, "url": hls.get("url"), "preset": "360"})
        await ctx.close()
    time.sleep(1)
    jobs = [app.wait(j["id"], 120) for j in app.api("/api/jobs")["jobs"] if j["id"] not in before]
    page_job = next((j for j in jobs if "player.html" in (j.get("url") or "")), {})
    q_job = next((j for j in jobs if "master.m3u8" in (j.get("url") or "")), {})
    rt.check("page without video → the stream caught on it is downloaded", page_job.get("status") == "done"
             and "البث الملتقط" in (page_job.get("method") or ""), f"{page_job.get('status')} · {page_job.get('method')}")
    rt.check("chosen quality of a caught stream downloaded", q_job.get("status") == "done" and "360" in (q_job.get("file") or ""),
             f"{q_job.get('status')} · {os.path.basename(q_job.get('file') or '')}")


def main():
    rt.APP_PORT = 8768  # one of the ports the extension looks at
    tmp = Path(tempfile.mkdtemp(prefix="mo-ext-tests-"))
    site_root, home = tmp / "site", tmp / "home"
    site_root.mkdir()
    home.mkdir()
    (home / ".config" / "chromium").mkdir(parents=True)  # so the app registers its native helper for this browser
    rt.make_media(site_root)
    page_files(site_root)
    site = subprocess.Popen([sys.executable, str(Path(__file__).with_name("testsite.py")), str(site_root), str(rt.SITE_PORT)])
    assert rt.wait_url(SITE + "/files/clip.mp4")
    app = rt.App(home)
    try:
        time.sleep(1.5)
        reg = list((home / ".config" / "chromium").rglob("com.mo.downloader.json"))
        rt.check("app registers its native helper for the browser", bool(reg), str(reg))
        asyncio.run(main_async(app, home))
    finally:
        app.stop()
        site.terminate()
    failed = [r for r in rt.RESULTS if not r[1]]
    print(f"\n{len(rt.RESULTS) - len(failed)}/{len(rt.RESULTS)} passed")
    if failed:
        print("app log tail:\n" + (home / "app.log").read_text(errors="replace")[-2000:])
    shutil.rmtree(tmp, ignore_errors=True) if not failed else print("kept:", tmp)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
