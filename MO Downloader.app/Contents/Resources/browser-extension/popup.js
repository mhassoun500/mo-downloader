const ext = globalThis.browser ?? chrome;
const $ = (id) => document.getElementById(id);
const DEFAULTS = { capture: false, videoButton: true, imageButton: true, sendCookies: true };
let tab = null, running = false;

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

async function refreshMedia() {
  if (!tab) return;
  const r = await send({ type: "media", tabId: tab.id });
  const box = $("media");
  box.textContent = "";
  const list = r.media || [];
  if (!list.length) { box.append(el("div", "empty", "شغّل الفيديو في الصفحة وسيظهر هنا.")); return; }
  for (const m of list) {
    const row = el("div", "m");
    const name = decodeURIComponent(m.url.split(/[?#]/)[0].split("/").pop() || m.url).slice(0, 80);
    row.append(el("span", "k", m.kind), el("span", "n", name + (m.size ? "  ·  " + fmtSize(m.size) : "")));
    const b1 = el("button", "mini", "تنزيل"), b2 = el("button", "mini", "صوت");
    b1.title = m.url;
    b1.onclick = () => grab(m.url, "best");
    b2.onclick = () => grab(m.url, "audio");
    row.append(b1, b2);
    box.append(row);
  }
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
    box.checked = !!opts[k];
    box.onchange = () => ext.storage.local.set({ [k]: box.checked });
  }

  await refreshStatus();
  await refreshMedia();
  setInterval(() => { if (running) refreshJobs(); refreshMedia(); }, 1200);
})();
