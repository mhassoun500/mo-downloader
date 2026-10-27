// MO Downloader — floating "download" button over videos (quality menu) and images (one click) on any page.
(() => {
  if (window.__moDownloader) return;
  window.__moDownloader = true;
  // never on MO Downloader's own page
  if (/^(127\.0\.0\.1|localhost)$/.test(location.hostname) && +location.port >= 8765 && +location.port <= 8770) return;

  const ext = globalThis.browser ?? chrome;
  const MIN_W = 220, MIN_H = 120, IMG_MIN = 180;
  let opt = { videoButton: true, imageButton: true };
  try {
    ext.storage.local.get(opt).then((o) => (opt = o));
    ext.storage.onChanged.addListener((ch) => { for (const k in ch) if (k in opt) opt[k] = ch[k].newValue; });
  } catch (e) {}
  const OPTIONS = [
    ["best", "⚡ أفضل جودة"],
    ["1080", "1080p"],
    ["720", "720p"],
    ["audio", "🎵 صوت MP3"],
    ["choose", "اختيار الجودة…"],
  ];

  const host = document.createElement("mo-downloader-ui");
  host.style.cssText = "all:initial;position:fixed;inset:0 auto auto 0;z-index:2147483647;pointer-events:none";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `
  <style>
    *{box-sizing:border-box;font-family:"Segoe UI",Tahoma,"Noto Sans Arabic",system-ui,sans-serif}
    .btn{position:fixed;display:none;align-items:center;gap:6px;pointer-events:auto;cursor:pointer;
      background:rgba(15,107,92,.92);color:#fff;border:1px solid rgba(255,255,255,.35);border-radius:999px;
      padding:6px 12px 6px 10px;font-size:13px;font-weight:700;box-shadow:0 4px 14px rgba(0,0,0,.35);
      backdrop-filter:blur(4px);transition:transform .12s,background .12s;direction:ltr}
    .btn:hover{background:#0f6b5c;transform:scale(1.05)}
    .btn svg{width:16px;height:16px}
    .menu{position:fixed;display:none;pointer-events:auto;direction:rtl;background:#fffdf8;color:#1d1b16;
      border:1px solid #e3ddcf;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.3);padding:6px;min-width:170px}
    .menu button{display:block;width:100%;text-align:right;background:none;border:0;border-radius:8px;
      padding:8px 10px;font-size:14px;color:inherit;cursor:pointer}
    .menu button:hover{background:#dcefe9;color:#0f6b5c}
    .menu .hd{font-size:12px;color:#6b665b;padding:4px 10px 6px;border-bottom:1px solid #e3ddcf;margin-bottom:4px}
    .toast{position:fixed;display:none;pointer-events:auto;direction:rtl;max-width:340px;
      background:#1d1b16;color:#fff;border-radius:12px;padding:10px 14px;font-size:14px;line-height:1.5;
      box-shadow:0 10px 30px rgba(0,0,0,.35)}
    .toast.ok{background:#0f6b5c} .toast.err{background:#9c2a22}
    @media (prefers-color-scheme: dark){
      .menu{background:#1d1b16;color:#ece7dc;border-color:#332f27}
      .menu .hd{color:#a39d8f;border-color:#332f27}
      .menu button:hover{background:#1d332d;color:#3fb59c}
    }
  </style>
  <div class="btn" title="تنزيل بواسطة MO Downloader">
    <svg viewBox="0 0 24 24" fill="none" stroke="#ffc94d" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3v12M6.5 10l5.5 5.5L17.5 10"/><path d="M4 16v4h16v-4" stroke="#fff" stroke-width="2.6"/></svg>
    <span class="lbl">تنزيل</span>
  </div>
  <div class="menu"><div class="hd">MO Downloader</div></div>
  <div class="toast"></div>`;
  const btn = root.querySelector(".btn"), lbl = root.querySelector(".lbl"), menu = root.querySelector(".menu"), toastEl = root.querySelector(".toast");
  for (const [id, label] of OPTIONS) {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", (e) => { e.stopPropagation(); choose(id); });
    menu.append(b);
  }

  let attached = false;
  function attach() {
    const parent = document.fullscreenElement || document.webkitFullscreenElement || document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
    attached = true;
  }
  document.addEventListener("fullscreenchange", () => attached && attach());
  document.addEventListener("webkitfullscreenchange", () => attached && attach());

  let current = null, currentKind = "video", hideTimer = null, menuOpen = false;

  const onScreen = (r) => r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  function visibleVideos() {
    if (!opt.videoButton) return [];
    return [...document.querySelectorAll("video")].filter((v) => {
      const r = v.getBoundingClientRect();
      return r.width >= MIN_W && r.height >= MIN_H && onScreen(r) && getComputedStyle(v).visibility !== "hidden";
    });
  }
  function visibleImages() {
    if (!opt.imageButton) return [];
    return [...document.images].filter((im) => {
      const r = im.getBoundingClientRect();
      return r.width >= IMG_MIN && r.height >= IMG_MIN * 0.7 && onScreen(r) && /^https?:/.test(im.currentSrc || im.src || "");
    });
  }

  function place() {
    if (!current) return;
    const r = current.getBoundingClientRect();
    const top = Math.max(8, r.top + 12), left = Math.max(8, r.left + 12);
    btn.style.top = top + "px";
    btn.style.left = left + "px";
    if (menuOpen) {
      menu.style.top = top + 38 + "px";
      menu.style.left = left + "px";
    }
  }

  function show(v, kind) {
    clearTimeout(hideTimer);
    if (!attached) attach();
    if (current !== v) { menuOpen = false; menu.style.display = "none"; }
    current = v;
    currentKind = kind;
    lbl.textContent = kind === "image" ? "تنزيل الصورة" : "تنزيل";
    btn.style.display = "flex";
    place();
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (menuOpen) return;
      btn.style.display = "none";
      current = null;
    }, 900);
  }

  let last = 0;
  document.addEventListener("pointermove", (e) => {
    const now = performance.now();
    if (now - last < 120) return;
    last = now;
    const inside = (el) => {
      const r = el.getBoundingClientRect();
      return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    };
    const v = visibleVideos().find(inside);
    const im = v ? null : visibleImages().find(inside);
    if (v) show(v, "video");
    else if (im) show(im, "image");
    else if (current && !menuOpen) scheduleHide();
  }, { passive: true, capture: true });
  addEventListener("scroll", place, { passive: true, capture: true });
  addEventListener("resize", place, { passive: true });

  btn.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (currentKind === "image") { saveImage(current); return; }
    menuOpen = !menuOpen;
    menu.style.display = menuOpen ? "block" : "none";
    place();
  });
  menu.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  document.addEventListener("pointerdown", (e) => {
    if (menuOpen && !e.composedPath().includes(host)) closeMenu();
  }, true);
  function closeMenu() {
    menuOpen = false;
    menu.style.display = "none";
    scheduleHide();
  }

  let toastTimer = null;
  function toast(text, kind, ms = 3500) {
    if (!attached) attach();
    toastEl.textContent = text;
    toastEl.className = "toast " + (kind || "");
    toastEl.style.display = "block";
    const r = current ? current.getBoundingClientRect() : { top: 16, left: 16 };
    toastEl.style.top = Math.max(8, r.top + 56) + "px";
    toastEl.style.left = Math.max(8, r.left + 12) + "px";
    clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(() => (toastEl.style.display = "none"), ms);
  }

  function explain(err) {
    if (err === "NOT_RUNNING")
      return "MO Downloader غير مشغّل. افتحه من أيقونته ثم أعد المحاولة.";
    return "تعذّر الإرسال: " + err;
  }

  function send(msg) {
    return new Promise((resolve) => {
      try {
        ext.runtime.sendMessage(msg, (res) => resolve(res || { error: (ext.runtime.lastError || {}).message || "no response" }));
      } catch (e) {
        resolve({ error: "أعد تحميل الصفحة (تم تحديث الإضافة)." });
      }
    });
  }

  async function saveImage(img) {
    const url = img && (img.currentSrc || img.src);
    if (!url) return;
    toast("جارٍ إرسال الصورة إلى MO Downloader…", "", 0);
    const name = decodeURIComponent((url.split(/[?#]/)[0].split("/").pop() || "")).slice(0, 120);
    const res = await send({ type: "downloadFile", url, page: location.href, filename: /\.\w{2,5}$/.test(name) ? name : "" });
    if (res.error) toast(explain(res.error), "err", 6000);
    else toast("✓ أُضيفت الصورة إلى التنزيلات", "ok");
  }

  async function choose(id) {
    closeMenu();
    // Big sites (YouTube…) stream through blob: URLs -> send the page. On simple pages with several
    // videos, send the clicked video's own file so only that one is downloaded.
    const src = current && (current.currentSrc || current.src);
    const several = document.querySelectorAll("video").length > 1;
    const url = several && /^https?:/.test(src || "") ? src : location.href;
    const title = document.title;
    toast("جارٍ الإرسال إلى MO Downloader…", "", 0);
    const res = id === "choose" ? await send({ type: "openUI", url }) : await send({ type: "download", url, title, preset: id });
    if (res.error) toast(explain(res.error), "err", 6000);
    else toast(id === "choose" ? "فُتحت صفحة اختيار الجودة ✓" : "✓ أُضيف إلى قائمة التنزيل في MO Downloader", "ok");
  }

  ext.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "toast" && window === window.top) {
      if (msg.error) toast(explain(msg.error), "err", 6000);
      else toast("✓ أُضيف إلى قائمة التنزيل في MO Downloader", "ok");
    }
  });
})();
