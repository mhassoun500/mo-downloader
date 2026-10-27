// MO Downloader browser extension — talks to the MO Downloader app running on this computer.
const ext = globalThis.browser ?? chrome;
const PORTS = [8765, 8766, 8767, 8768, 8769, 8770];
const HEADERS = { "X-MO-Ext": "1", "Content-Type": "application/json" };
const DEFAULTS = { capture: false, videoButton: true, imageButton: true, sendCookies: true };
let base = null;

const PRESETS = [
  ["best", "⚡ أفضل جودة"],
  ["1080", "1080p"],
  ["720", "720p"],
  ["audio", "🎵 صوت MP3"],
];

const options = () => ext.storage.local.get(DEFAULTS);

// ---------------------------------------------------------------- talking to the app
async function probe(url) {
  try {
    const r = await fetch(url + "api/ext/status", { headers: HEADERS, signal: AbortSignal.timeout(900) });
    const j = await r.json();
    return j.app === "MO Downloader" ? j : null;
  } catch (e) {
    return null;
  }
}

async function findServer() {
  if (base && (await probe(base))) return base;
  base = null;
  for (const p of PORTS) {
    const u = `http://127.0.0.1:${p}/`;
    if (await probe(u)) return (base = u);
  }
  return null;
}

async function call(path, body) {
  const b = await findServer();
  if (!b) throw new Error("NOT_RUNNING");
  const r = await fetch(b + path, {
    method: body ? "POST" : "GET",
    headers: HEADERS,
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || "HTTP " + r.status);
  return j;
}

// Start the app through the mo-downloader:// link registered by its installer, then wait for it.
async function launchApp() {
  let tab = null;
  try {
    tab = await ext.tabs.create({ url: "mo-downloader://start", active: true });
  } catch (e) {}
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 750));
    if (await findServer()) {
      if (tab) ext.tabs.remove(tab.id).catch(() => {});
      return true;
    }
  }
  if (tab) ext.tabs.remove(tab.id).catch(() => {});
  return false;
}

async function ensureApp() {
  if (await findServer()) return true;
  return await launchApp();
}

// ---------------------------------------------------------------- cookies (so sites that need a login work)
async function netscapeCookies(url) {
  if (!url || !/^https?:/.test(url) || !(await options()).sendCookies) return "";
  try {
    const list = await ext.cookies.getAll({ url });
    if (!list.length) return "";
    const now = Math.floor(Date.now() / 1000);
    const lines = ["# Netscape HTTP Cookie File"];
    for (const c of list) {
      const domain = c.hostOnly ? c.domain : (c.domain.startsWith(".") ? c.domain : "." + c.domain);
      const exp = c.session || !c.expirationDate ? now + 86400 : Math.floor(c.expirationDate);
      lines.push([(c.httpOnly ? "#HttpOnly_" : "") + domain, c.hostOnly ? "FALSE" : "TRUE", c.path || "/",
        c.secure ? "TRUE" : "FALSE", exp, c.name, c.value].join("\t"));
    }
    return lines.join("\n") + "\n";
  } catch (e) {
    return "";
  }
}

const sameHost = (a, b) => { try { return new URL(a).hostname === new URL(b).hostname; } catch (e) { return false; } };

// one entry point for every kind of download: page (yt-dlp), media (a stream caught on the page), file
async function send(job, tabId) {
  if (!(await ensureApp())) throw new Error("NOT_RUNNING");
  const body = { ua: navigator.userAgent, ...job };
  body.cookies = await netscapeCookies(job.url);
  if (job.page && !sameHost(job.page, job.url)) body.page_cookies = await netscapeCookies(job.page);
  const res = await call("api/ext/download", body);
  flashBadge(tabId);
  return res;
}

function flashBadge(tabId) {
  if (tabId == null || tabId < 0) return;
  ext.action.setBadgeBackgroundColor({ color: "#0f6b5c", tabId }).catch(() => {});
  ext.action.setBadgeText({ text: "✓", tabId }).catch(() => {});
  setTimeout(() => updateBadge(tabId), 3500);
}

async function openUI(url) {
  if (!(await ensureApp())) throw new Error("NOT_RUNNING");
  const b = await findServer();
  if (url) {
    const cookies = await netscapeCookies(url);
    if (cookies) await call("api/ext/cookies", { url, cookies }).catch(() => {});
  }
  await ext.tabs.create({ url: b + (url ? "?url=" + encodeURIComponent(url) : "") });
  return { ok: true };
}

// ---------------------------------------------------------------- streams caught on the page
const MEDIA_TYPES = ["media", "xmlhttprequest", "other", "object"];
const found = new Map(); // tabId -> [{url, kind, size, ts, page, title}]
const key = (tabId) => "media:" + tabId;
// one change at a time per tab: two streams arriving together must not overwrite each other's entry
const tabLocks = new Map();
function withTab(tabId, fn) {
  const next = (tabLocks.get(tabId) || Promise.resolve()).then(fn, fn);
  tabLocks.set(tabId, next.catch(() => {}));
  return next;
}

async function mediaFor(tabId) {
  if (found.has(tabId)) return found.get(tabId);
  const got = (await ext.storage.session.get(key(tabId)).catch(() => ({})))[key(tabId)] || [];
  found.set(tabId, got);
  return got;
}

async function saveMedia(tabId, list) {
  found.set(tabId, list);
  ext.storage.session.set({ [key(tabId)]: list }).catch(() => {});
  updateBadge(tabId);
}

async function updateBadge(tabId) {
  const n = (await mediaFor(tabId)).length;
  ext.action.setBadgeBackgroundColor({ color: "#9a5b00", tabId }).catch(() => {});
  ext.action.setBadgeText({ text: n ? String(n) : "", tabId }).catch(() => {});
}

function classify(url, headers, statusCode) {
  const h = {};
  for (const x of headers || []) h[x.name.toLowerCase()] = x.value || "";
  const ct = (h["content-type"] || "").toLowerCase();
  const path = url.split(/[?#]/)[0].toLowerCase();
  let size = +h["content-length"] || 0;
  const cr = h["content-range"];
  if (cr && /\/(\d+)$/.test(cr)) size = +cr.match(/\/(\d+)$/)[1];
  if (/\.m3u8$/.test(path) || ct.includes("mpegurl")) return { kind: "HLS", size: 0 };
  if (/\.mpd$/.test(path) || ct.includes("dash+xml")) return { kind: "DASH", size: 0 };
  if (/\.(ts|m4s|aac|vtt|srt)$/.test(path)) return null; // pieces of a stream, not the stream
  const isAV = ct.startsWith("video/") || ct.startsWith("audio/") || /\.(mp4|webm|mkv|mov|m4v|mp3|m4a|ogg|flac|wav)$/.test(path);
  if (isAV && (size === 0 || size > 400 * 1024) && statusCode < 400)
    return { kind: (path.match(/\.(\w+)$/) || [, ct.split("/")[1] || "media"])[1].toUpperCase(), size };
  return null;
}

ext.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || !/^https?:/.test(d.url) || /^https?:\/\/(127\.0\.0\.1|localhost):87\d\d\//.test(d.url)) return;
    const c = classify(d.url, d.responseHeaders, d.statusCode);
    if (!c) return;
    withTab(d.tabId, async () => {
      const list = await mediaFor(d.tabId);
      const bare = d.url.split("#")[0];
      if (list.some((m) => m.url === bare)) return;
      let page = "", title = "";
      try { const t = await ext.tabs.get(d.tabId); page = t.url; title = t.title; } catch (e) {}
      list.unshift({ url: bare, kind: c.kind, size: c.size, ts: Date.now(), page, title });
      await saveMedia(d.tabId, list.slice(0, 25));
    });
  },
  { urls: ["<all_urls>"], types: MEDIA_TYPES },
  ["responseHeaders"]
);

// a new page starts loading in the tab: forget the previous page's streams. (Done on the page request itself,
// which always comes before the new page's own requests, so nothing caught early gets wiped.)
ext.webRequest.onBeforeRequest.addListener(
  (d) => { if (d.tabId >= 0 && d.frameId === 0) withTab(d.tabId, () => saveMedia(d.tabId, [])); },
  { urls: ["<all_urls>"], types: ["main_frame"] }
);
ext.tabs.onRemoved.addListener((tabId) => {
  found.delete(tabId);
  ext.storage.session.remove(key(tabId)).catch(() => {});
});

// ---------------------------------------------------------------- hand browser downloads to the app
ext.downloads.onCreated.addListener(async (item) => {
  const { capture } = await options();
  const url = item.finalUrl || item.url;
  if (!capture || !/^https?:/.test(url || "") || item.byExtensionId === ext.runtime.id) return;
  if (!(await findServer())) return; // app closed: the browser downloads it normally
  try {
    await ext.downloads.cancel(item.id);
    await ext.downloads.erase({ id: item.id });
  } catch (e) {}
  const filename = (item.filename || "").split(/[\\/]/).pop();
  try {
    await send({ url, kind: "file", filename, referer: item.referrer || "", page: item.referrer || "" }, item.tabId ?? -1);
  } catch (e) {
    // could not hand it over: let the browser do it after all
    ext.downloads.download({ url }).catch(() => {});
  }
});

// ---------------------------------------------------------------- messages from the page button and the popup
ext.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab ? sender.tab.id : msg.tabId;
  const work = async () => {
    switch (msg.type) {
      case "status": {
        const b = await findServer();
        return { running: !!b, url: b };
      }
      case "jobs":
        return await call("api/ext/jobs");
      case "download": // the page itself (YouTube, any supported site)
        return await send({ url: msg.url, title: msg.title, preset: msg.preset, kind: "page", page: msg.page || msg.url, referer: msg.page || "" }, tabId);
      case "downloadFile": // an image or a direct file link
        return await send({ url: msg.url, kind: "file", filename: msg.filename || "", referer: msg.page || "", page: msg.page || "" }, tabId);
      case "downloadMedia": { // a stream caught on the page
        const m = (await mediaFor(msg.tabId)).find((x) => x.url === msg.url) || {};
        return await send({ url: msg.url, kind: "media", preset: msg.preset, title: m.title || msg.title || "video",
                            referer: m.page || "", page: m.page || "" }, msg.tabId);
      }
      case "media":
        return { media: await mediaFor(msg.tabId) };
      case "clearMedia":
        await withTab(msg.tabId, () => saveMedia(msg.tabId, []));
        return { ok: true };
      case "openUI":
        return await openUI(msg.url);
      case "openFolder":
        return await call("api/ext/open_folder", {});
      case "launch":
        return { running: await launchApp() };
      default:
        return { error: "unknown" };
    }
  };
  work().then(sendResponse, (e) => sendResponse({ error: e.message }));
  return true; // async response
});

// ---------------------------------------------------------------- keyboard shortcut (Alt+Shift+S)
ext.commands.onCommand.addListener(async (command) => {
  if (command !== "mo-download-page") return;
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return;
  try {
    await send({ url: tab.url, title: tab.title, preset: "best", kind: "page", page: tab.url }, tab.id);
    ext.tabs.sendMessage(tab.id, { type: "toast" }).catch(() => {});
  } catch (e) {
    ext.tabs.sendMessage(tab.id, { type: "toast", error: e.message }).catch(() => {});
  }
});

// ---------------------------------------------------------------- right-click menu
function buildMenus() {
  const ctx = ["page", "link", "video", "audio"];
  ext.contextMenus.removeAll(() => {
    ext.contextMenus.create({ id: "mo", title: "MO Downloader", contexts: [...ctx, "image"] });
    for (const [id, t] of PRESETS) ext.contextMenus.create({ id: "mo:" + id, parentId: "mo", title: "تنزيل — " + t, contexts: ctx });
    ext.contextMenus.create({ id: "mo:image", parentId: "mo", title: "تنزيل الصورة", contexts: ["image"] });
    ext.contextMenus.create({ id: "mo:file", parentId: "mo", title: "تنزيل الرابط كملف", contexts: ["link"] });
    ext.contextMenus.create({ id: "mo:sep", parentId: "mo", type: "separator", contexts: ctx });
    ext.contextMenus.create({ id: "mo:choose", parentId: "mo", title: "اختيار الجودة…", contexts: ctx });
  });
}
ext.runtime.onInstalled.addListener(buildMenus);
ext.runtime.onStartup && ext.runtime.onStartup.addListener(buildMenus);

ext.contextMenus.onClicked.addListener(async (info, tab) => {
  const id = String(info.menuItemId);
  if (!id.startsWith("mo:")) return;
  const page = info.frameUrl || info.pageUrl || (tab && tab.url);
  const url = info.linkUrl || page;
  const tabId = tab ? tab.id : -1;
  try {
    if (id === "mo:choose") await openUI(url);
    else if (id === "mo:image") await send({ url: info.srcUrl, kind: "file", referer: page, page }, tabId);
    else if (id === "mo:file") await send({ url: info.linkUrl, kind: "file", referer: page, page }, tabId);
    else await send({ url, title: info.linkUrl ? info.linkUrl : tab && tab.title, preset: id.slice(3), kind: "page", page, referer: page }, tabId);
    if (id !== "mo:choose" && tabId >= 0) ext.tabs.sendMessage(tabId, { type: "toast" }).catch(() => {});
  } catch (e) {
    if (tabId >= 0) ext.tabs.sendMessage(tabId, { type: "toast", error: e.message }).catch(() => {});
  }
});
