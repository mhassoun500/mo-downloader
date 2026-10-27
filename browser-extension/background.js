// MO Downloader browser extension — talks to the MO Downloader app running on this computer.
const ext = globalThis.browser ?? chrome;
const PORTS = [8765, 8766, 8767, 8768, 8769, 8770];
const HEADERS = { "X-MO-Ext": "1", "Content-Type": "application/json" };
let base = null;

const PRESETS = [
  ["best", "⚡ أفضل جودة"],
  ["1080", "1080p"],
  ["720", "720p"],
  ["audio", "🎵 صوت MP3"],
];

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

async function download(url, title, preset, tabId) {
  let res;
  try {
    res = await call("api/ext/download", { url, title, preset });
  } catch (e) {
    if (e.message !== "NOT_RUNNING") throw e;
    if (!(await launchApp())) throw new Error("NOT_RUNNING");
    res = await call("api/ext/download", { url, title, preset });
  }
  if (tabId != null) {
    ext.action.setBadgeBackgroundColor({ color: "#0f6b5c", tabId }).catch(() => {});
    ext.action.setBadgeText({ text: "✓", tabId }).catch(() => {});
    setTimeout(() => ext.action.setBadgeText({ text: "", tabId }).catch(() => {}), 4000);
  }
  return res;
}

async function openUI(url) {
  let b = await findServer();
  if (!b && (await launchApp())) b = await findServer();
  if (!b) throw new Error("NOT_RUNNING");
  await ext.tabs.create({ url: b + (url ? "?url=" + encodeURIComponent(url) : "") });
  return { ok: true };
}

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
      case "download":
        return await download(msg.url, msg.title, msg.preset, tabId);
      case "openUI":
        return await openUI(msg.url);
      case "launch":
        return { running: await launchApp() };
      default:
        return { error: "unknown" };
    }
  };
  work().then(sendResponse, (e) => sendResponse({ error: e.message }));
  return true; // async response
});

// ---- right-click menu
function buildMenus() {
  ext.contextMenus.removeAll(() => {
    ext.contextMenus.create({ id: "mo", title: "MO Downloader", contexts: ["page", "link", "video", "audio"] });
    for (const [id, t] of PRESETS)
      ext.contextMenus.create({ id: "mo:" + id, parentId: "mo", title: "تنزيل — " + t, contexts: ["page", "link", "video", "audio"] });
    ext.contextMenus.create({ id: "mo:sep", parentId: "mo", type: "separator", contexts: ["page", "link", "video", "audio"] });
    ext.contextMenus.create({ id: "mo:choose", parentId: "mo", title: "اختيار الجودة…", contexts: ["page", "link", "video", "audio"] });
  });
}
ext.runtime.onInstalled.addListener(buildMenus);
ext.runtime.onStartup && ext.runtime.onStartup.addListener(buildMenus);

ext.contextMenus.onClicked.addListener(async (info, tab) => {
  const id = String(info.menuItemId);
  if (!id.startsWith("mo:")) return;
  // a link the user right-clicked wins; otherwise the page (or the frame that holds the player)
  const url = info.linkUrl || info.frameUrl || info.pageUrl || (tab && tab.url);
  const title = info.linkUrl ? info.linkUrl : tab && tab.title;
  try {
    if (id === "mo:choose") await openUI(url);
    else {
      await download(url, title, id.slice(3), tab && tab.id);
      if (tab && tab.id != null) ext.tabs.sendMessage(tab.id, { type: "toast" }).catch(() => {});
    }
  } catch (e) {
    if (tab && tab.id != null) {
      ext.tabs.sendMessage(tab.id, { type: "toast", error: e.message }).catch(() => {});
    }
  }
});
