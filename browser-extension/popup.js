const ext = globalThis.browser ?? chrome;
const $ = (id) => document.getElementById(id);
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
    const d = document.createElement("div");
    d.className = "job";
    const t = document.createElement("div"); t.className = "t"; t.textContent = j.title;
    const s = document.createElement("div"); s.className = "s";
    s.textContent = (j.stage || "") + (j.status === "downloading" && j.percent != null ? " · " + j.percent.toFixed(0) + "%" : "");
    d.append(t, s);
    if (!["done", "error", "cancelled"].includes(j.status)) {
      const bar = document.createElement("div"); bar.className = "bar";
      const i = document.createElement("i"); i.style.width = (j.percent || 0) + "%";
      bar.append(i); d.append(bar);
    }
    box.append(d);
  }
}

async function go(preset) {
  if (!usable()) return;
  show("جارٍ الإرسال…", "ok");
  const r = await send({ type: "download", url: tab.url, title: tab.title, preset, tabId: tab.id });
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
  $("launch").onclick = async () => {
    show("جارٍ تشغيل MO Downloader… (وافق على رسالة المتصفح إن ظهرت)", "ok");
    const r = await send({ type: "launch" });
    if (r.running) { show("✓ البرنامج يعمل الآن", "ok"); refreshStatus(); }
    else show("لم يبدأ البرنامج. افتحه من أيقونته على الجهاز.", "err");
  };
  await refreshStatus();
  setInterval(() => running && refreshJobs(), 1200);
})();
