const ext = globalThis.browser ?? chrome;
const $ = (id) => document.getElementById(id);
const DEFAULTS = {
  capture: false, videoButton: true, imageButton: true, sendCookies: true,
  captureMinMB: 2,
  captureExts: "mp4 mkv webm mov avi m4v ts mp3 m4a aac flac wav ogg opus zip rar 7z iso dmg exe msi apk",
  captureSkip: "",
};
let tab = null, running = false, sniffOff = false;

function send(msg) {
  return new Promise((resolve) =>
    ext.runtime.sendMessage(msg, (res) => resolve(res || { error: (ext.runtime.lastError || {}).message || "no response" })));
}
function show(text, kind) {
  const m = $("msg");
  m.textContent = text;
  m.className = "msg " + kind;
}
const explain = (e) => e === "NOT_RUNNING" ? "MO Downloader غير مشغّل. اضغط «تشغيل البرنامج» بالأسفل أو افتحه من أيقونته." : e;
const usable = () => tab && /^https?:/.test(tab.url || "");
const fmtSize = (b) => {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i > 1 ? 1 : 0) + " " + u[i];
};
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

async function refreshStatus() {
  const s = await send({ type: "status" });
  running = !!s.running;
  // features this browser doesn't offer (e.g. Safari has no downloads API)
  if (s.canCapture === false) $("o-capture").closest("label").style.display = "none";
  if (s.canSniff === false) {
    $("media").textContent = "";
    $("media").append(el("div", "empty", "التقاط الفيديو من الصفحة غير متاح في هذا المتصفح."));
    $("clearMedia").style.display = "none";
    sniffOff = true;
  }
  $("dot").className = "dot " + (running ? "on" : "off");
  $("dotText").textContent = running ? "يعمل" : "غير مشغّل";
  $("launch").style.display = running ? "none" : "inline";
  if (running) refreshJobs();
}

async function refreshJobs() {
  const r = await send({ type: "jobs" });
  if (!r.jobs) return;
  const box = $("jobs");
  box.textContent = "";
  $("jobsBox").style.display = r.jobs.length ? "block" : "none";
  for (const j of r.jobs.slice(0, 4)) {
    const d = el("div", "job");
    d.append(el("div", "t", j.title));
    let st = j.stage || "";
    if (j.status === "downloading" && j.percent != null) st += " · " + j.percent.toFixed(0) + "%";
    if (j.status === "downloading" && j.speed) st += " · " + fmtSize(j.speed) + "/s";
    d.append(el("div", "s", st));
    if (!["done", "error", "cancelled", "paused"].includes(j.status)) {
      const bar = el("div", "bar"), i = el("i");
      i.style.width = (j.percent || 0) + "%";
      bar.append(i); d.append(bar);
    }
    box.append(d);
  }
}

const qualities = new Map(); // url -> {loading} | {heights, audio} | {error}   (kept across list refreshes)
const KIND_NAMES = { EMBED: "مشغّل مضمّن" };

async function refreshMedia() {
  if (!tab || sniffOff) return;
  const r = await send({ type: "media", tabId: tab.id });
  const box = $("media");
  const list = r.media || [];
  const sig = JSON.stringify(list.map((m) => m.url)) + JSON.stringify([...qualities.entries()]);
  if (box.dataset.sig === sig) return; // nothing changed: don't rebuild (keeps buttons stable under the mouse)
  box.dataset.sig = sig;
  box.textContent = "";
  if (!list.length) { box.append(el("div", "empty", "شغّل الفيديو في الصفحة وسيظهر هنا.")); return; }
  for (const m of list) {
    const row = el("div", "m");
    let name = decodeURIComponent(m.url.split(/[?#]/)[0].split("/").pop() || m.url);
    if (m.kind === "EMBED") { try { name = new URL(m.url).hostname + new URL(m.url).pathname; } catch (e) {} }
    row.append(el("span", "k", KIND_NAMES[m.kind] || m.kind), el("span", "n", name.slice(0, 80) + (m.size ? "  ·  " + fmtSize(m.size) : "")));
    row.title = m.url;
    const q = qualities.get(m.url);
    if (q && q.heights) {
      const wrap = el("div", "qs");
      for (const h of q.heights) { const b = el("button", "mini", h + "p"); b.onclick = () => grab(m.url, String(h)); wrap.append(b); }
      if (q.audio) { const b = el("button", "mini", "صوت"); b.onclick = () => grab(m.url, "audio"); wrap.append(b); }
      if (!q.heights.length) { const b = el("button", "mini", "تنزيل"); b.onclick = () => grab(m.url, "best"); wrap.append(b); }
      row.append(wrap);
    } else {
      const bq = el("button", "mini", q && q.loading ? "…" : "الجودات");
      bq.disabled = !!(q && q.loading);
      bq.title = q && q.error ? q.error : "اعرض الجودات المتاحة لهذا الفيديو";
      bq.onclick = () => loadQualities(m.url);
      const b1 = el("button", "mini", "تنزيل"), b2 = el("button", "mini", "صوت");
      b1.onclick = () => grab(m.url, "best");
      b2.onclick = () => grab(m.url, "audio");
      row.append(bq, b1, b2);
    }
    box.append(row);
  }
}

async function loadQualities(url) {
  qualities.set(url, { loading: true });
  refreshMedia();
  const r = await send({ type: "formats", tabId: tab.id, url });
  if (r.error) { qualities.set(url, { error: explain(r.error) }); show(explain(r.error), "err"); }
  else qualities.set(url, { heights: r.heights || [], audio: !!r.audio });
  refreshMedia();
}

async function grab(url, preset) {
  show("جارٍ الإرسال…", "ok");
  const r = await send({ type: "downloadMedia", tabId: tab.id, url, preset, title: tab.title });
  if (r.error) show(explain(r.error), "err"); else { show("✓ أُضيف إلى قائمة التنزيل", "ok"); refreshStatus(); }
}

async function go(preset) {
  if (!usable()) return;
  show("جارٍ الإرسال…", "ok");
  const r = await send({ type: "download", url: tab.url, title: tab.title, preset, tabId: tab.id, page: tab.url });
  if (r.error) show(explain(r.error), "err");
  else { show("✓ أُضيف إلى قائمة التنزيل", "ok"); refreshStatus(); }
}

(async () => {
  [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  $("page").textContent = usable() ? tab.title || tab.url : "افتح صفحة فيها فيديو أو صوت";
  document.querySelectorAll("[data-p]").forEach((b) => {
    b.disabled = !usable();
    b.onclick = () => go(b.dataset.p);
  });
  $("choose").disabled = !usable();
  $("choose").onclick = async () => {
    const r = await send({ type: "openUI", url: tab.url });
    if (r.error) show(explain(r.error), "err"); else window.close();
  };
  $("open").onclick = async () => {
    const r = await send({ type: "openUI" });
    if (r.error) show(explain(r.error), "err"); else window.close();
  };
  $("folder").onclick = async () => {
    const r = await send({ type: "openFolder" });
    if (r.error) show(explain(r.error), "err");
  };
  $("launch").onclick = async () => {
    show("جارٍ تشغيل MO Downloader… (وافق على رسالة المتصفح إن ظهرت)", "ok");
    const r = await send({ type: "launch" });
    if (r.running) { show("✓ البرنامج يعمل الآن", "ok"); refreshStatus(); }
    else show("لم يبدأ البرنامج. افتحه من أيقونته على الجهاز.", "err");
  };
  $("clearMedia").onclick = async () => { await send({ type: "clearMedia", tabId: tab.id }); refreshMedia(); };

  const opts = await ext.storage.local.get(DEFAULTS);
  for (const k of Object.keys(DEFAULTS)) {
    const box = $("o-" + k);
    if (!box) continue;
    if (typeof DEFAULTS[k] === "boolean") {
      box.checked = !!opts[k];
      box.onchange = () => { ext.storage.local.set({ [k]: box.checked }); if (k === "capture") showCapture(); };
    } else {
      box.value = opts[k];
      box.onchange = () => ext.storage.local.set({ [k]: typeof DEFAULTS[k] === "number" ? Math.max(0, +box.value || 0) : box.value.trim() });
    }
  }
  const showCapture = () => { $("captureOpts").style.display = $("o-capture").checked ? "" : "none"; };
  showCapture();

  await refreshStatus();
  await refreshMedia();
  setInterval(() => { if (running) refreshJobs(); refreshMedia(); }, 1200);
})();
