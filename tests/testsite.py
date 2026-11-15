"""Local test site for MO Downloader's automated tests.

Serves generated media files and simulates the situations the app must handle:
  /files/<f>          plain files (with Range support, so pause/resume and the accelerator work)
  /protected/<f>      only with the cookie auth=1 (a site that needs a login)
  /browser-only/<f>   403 unless the request looks like a real Chrome (sec-ch-ua header) — a site behind bot protection
  /hdr-only/<f>       403 unless the request carries the player's header X-Auth: letmein
  /nomedia.html       a page with no video at all
  /pages/<f>          test web pages (browser-extension tests)
  /live/index.m3u8    a live HLS stream (written by ffmpeg while the tests run)
  /slow/<f>           like /files/ but slow (pause / resume / closing the app mid-download)
  /list/<name>.html   a page listing the videos named in ROOT/list-<name>.txt (a "channel" to follow)
  any absolute URL    answered as an HTTP proxy, counted in /proxy-count (the proxy test)
"""
import time
import urllib.request

PROXY_HITS = [0]
SLOW_HITS = {}  # file -> how many times it was requested through /slow/ (pause/resume must not start over)
import http.server
import os
import re
import socketserver
import sys

ROOT = sys.argv[1]
PORT = int(sys.argv[2])


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def deny(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self, head=False):
        if self.path.startswith("http://"):  # we are being used as a proxy
            PROXY_HITS[0] += 1
            req = urllib.request.Request(self.path, headers={k: v for k, v in self.headers.items()
                                                             if k.lower() not in ("proxy-connection", "connection")})
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
                    body = r.read()
                    self.send_response(r.status)
                    for k, v in r.headers.items():
                        if k.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    if not head:
                        self.wfile.write(body)
            except urllib.error.HTTPError as e:
                self.deny(e.code, "upstream error")
            return
        path = self.path.split("?")[0]
        if path == "/proxy-count":
            return self.deny(200, str(PROXY_HITS[0]))
        if path == "/slow-count":
            import json
            return self.deny(200, json.dumps(SLOW_HITS))
        if path.startswith("/list/"):
            name = path[len("/list/"):].replace(".html", "")
            names = open(os.path.join(ROOT, f"list-{name}.txt")).read().split()
            body = "<html><head><title>Test channel %s</title></head><body>%s</body></html>" % (name, "".join(
                f'<video src="{n if n.startswith("/") else "/files/" + n}" controls></video>' for n in names))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            return self.wfile.write(body.encode())
        slow = path.startswith("/slow/")
        if slow:
            path = "/files/" + path[len("/slow/"):]
            self.slow = True
            if not head:
                SLOW_HITS[path[7:]] = SLOW_HITS.get(path[7:], 0) + 1
        if path.startswith("/protected/"):
            if "auth=1" not in (self.headers.get("Cookie") or ""):
                return self.deny(403, "Forbidden: please sign in")
            path = "/files/" + path[len("/protected/"):]
        elif path.startswith("/hdr-only/"):
            # a CDN that wants the player's own header (like players that sign their requests)
            if self.headers.get("X-Auth") != "letmein":
                return self.deny(403, "Forbidden: missing player token")
            path = "/files/" + path[len("/hdr-only/"):]
        elif path.startswith("/browser-only/"):
            if not self.headers.get("sec-ch-ua"):
                return self.deny(403, "Forbidden (Cloudflare challenge)")
            path = "/files/" + path[len("/browser-only/"):]
        if path == "/nomedia.html":
            return self.send_file(os.path.join(ROOT, "nomedia.html"), head)
        if path.startswith("/pages/"):
            return self.send_file(os.path.join(ROOT, "pages", path[len("/pages/"):]), head)
        if path.startswith("/files/") or path.startswith("/live/"):
            rel = path.lstrip("/").split("/", 1)[1]
            base = ROOT if path.startswith("/files/") else os.path.join(ROOT, "live")
            return self.send_file(os.path.join(base, rel), head)
        self.deny(404, "not found")

    def send_file(self, fp, head):
        if not os.path.isfile(fp):
            return self.deny(404, "not found")
        size = os.path.getsize(fp)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if m:
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        ext = os.path.splitext(fp)[1].lower()
        ctype = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
                 ".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".png": "image/png"}.get(ext, "application/octet-stream")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head:
            return
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            if getattr(self, "slow", False):
                time.sleep(0.25)
            while left > 0:
                if getattr(self, "slow", False):
                    time.sleep(0.1)  # about 160 KB/s
                chunk = f.read(min(16384 if getattr(self, "slow", False) else 65536, left))
                if not chunk:
                    break
                left -= len(chunk)
                try:
                    self.wfile.write(chunk)
                except Exception:
                    return


class T(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # a client hanging up mid-file (pause, cancel) is expected here


T(("127.0.0.1", PORT), H).serve_forever()
