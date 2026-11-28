// BSR Corridors admin — small progressive enhancements (pages work without JS too).
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);

  // Confirm before submitting forms marked data-confirm="…"
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    // Check in / out: open the welcome / departure message in WhatsApp in the same tap, in the "whatsapp"
    // tab (reused for every message, so WhatsApp Web isn't opened twice), while the form still goes through.
    const wa = e.target.dataset && e.target.dataset.waOpen;
    if (wa) window.open(wa, "whatsapp");
    if (e.target.dataset && e.target.dataset.busy) busy(e.target.dataset.busy, false);  // the page itself moves on to the result
  });

  // Record payment (stay page): record it, then open the "Payment done" thanks in WhatsApp with the amount. The
  // WhatsApp tab is opened in the tap itself (browsers block it later) and pointed at the message once the payment
  // is saved, so the message shows the new paid / balance. Without JS the form posts normally.
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.matches || !form.matches("form[data-pay-wa]") || e.defaultPrevented) return;
    e.preventDefault();
    const win = window.open("", "whatsapp");
    busy("Recording the payment…", false);
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } })
      .then((r) => r.json())
      .then((d) => {
        if (d.wa && win) win.location.href = d.wa;
        else if (win) win.close();
        location.href = location.pathname + "#payment";
        location.reload();
      })
      .catch(() => { if (win) win.close(); form.removeAttribute("data-pay-wa"); form.submit(); });
  });

  // Extend stay: extend it, then open the "Stay extended" message (new dates, amount, total to pay) in WhatsApp in the
  // same tap. If there's something to choose or fix (another room, a wrong date), the form is sent again normally
  // so the page shows it — nothing was changed by the first try.
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!form.matches || !form.matches("form[data-extend-wa]") || e.defaultPrevented) return;
    e.preventDefault();
    const win = window.open("", "whatsapp");
    busy(form.dataset.busyText || "Extending the stay…", false);
    fetch(form.action || location.href, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } })
      .then((r) => r.json())
      .then((d) => {
        if (!d.ok) throw new Error("show the page");
        if (d.wa && win) win.location.href = d.wa;
        else if (win) win.close();
        location.href = d.next;
      })
      .catch(() => { if (win) win.close(); form.removeAttribute("data-extend-wa"); form.submit(); });
  });

  // "Generating the invoice…": shown while Check out / a message with the invoice link makes the invoice (a few
  // seconds — it's saved in Google Drive). A message opens WhatsApp in another tab/app, so this page then reloads
  // to show the invoice as generated.
  // The loader: the property's logo with a ring turning around it, and what is happening.
  let busyEl, busyTimer = null;
  function busy(text, reloadAfter) {
    clearTimeout(busyTimer);
    busyTimer = null;
    if (!busyEl) {
      busyEl = document.createElement("div");
      busyEl.className = "busy-overlay";
      busyEl.setAttribute("role", "status");
      busyEl.setAttribute("aria-live", "polite");
      const icon = document.body.dataset.icon || "/static/img/icon.png";
      busyEl.innerHTML = '<div class="busy-box"><span class="busy-logo" aria-hidden="true"><img alt=""><span class="busy-ring">' +
        '</span></span><span class="busy-text"></span></div>';
      const img = busyEl.querySelector("img");
      img.addEventListener("error", () => { if (!img.src.endsWith(icon)) img.src = icon; });  // logo unavailable
      img.src = document.body.dataset.logo || icon;
      document.body.append(busyEl);
    }
    busyEl.querySelector(".busy-text").textContent = text || "Please wait…";
    busyEl.hidden = false;
    if (reloadAfter) setTimeout(() => location.reload(), 6000);
  }
  function unbusy() {
    clearTimeout(busyTimer);
    busyTimer = null;
    if (busyEl) busyEl.hidden = true;
  }
  // Only when it takes a moment: quick answers never flash the loader.
  let busyGiveUp = null;
  function busySoon(text, ms) {
    clearTimeout(busyTimer);
    clearTimeout(busyGiveUp);
    busyTimer = setTimeout(() => {
      busy(text, false);
      busyGiveUp = setTimeout(unbusy, 30000);  // e.g. a download: the page stays, so don't keep the loader up
    }, ms === undefined ? 250 : ms);
  }
  window.app = Object.assign(window.app || {}, { busy, unbusy, busySoon });
  // The property's logo in the header: if it can't be loaded (Drive unreachable), the BSR icon instead.
  document.querySelectorAll("img.brand-logo").forEach((img) => {
    const icon = document.body.dataset.icon;
    const fallback = () => { if (icon && !img.src.endsWith(icon)) img.src = icon; };
    if (img.complete && !img.naturalWidth) fallback();
    else img.addEventListener("error", fallback);
  });
  window.addEventListener("pageshow", unbusy);  // back button
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[data-busy]");
    if (a) busy(a.dataset.busy, a.target === "whatsapp");
  });

  // Every button that sends a form (save, check in, record, move out…) shows the loader until the next page comes —
  // unless the form opens elsewhere (WhatsApp, a PDF in a new tab) or was cancelled at "Are you sure?".
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (e.defaultPrevented || !form.matches || form.dataset.busy || form.matches("[data-no-busy]")) return;
    if (e.submitter && e.submitter.matches("[data-no-busy]")) return;  // e.g. Download: the page stays
    const target = (e.submitter && e.submitter.getAttribute("formtarget")) || form.getAttribute("target");
    if (target && target !== "_self") return;
    const label = (e.submitter && (e.submitter.dataset.busyText || e.submitter.textContent.trim())) || "";
    const get = (form.getAttribute("method") || "get").toLowerCase() === "get";
    const text = form.dataset.busyText || (get ? "Loading…" : (label && label.length < 30 ? label + "…" : "Saving…"));
    setTimeout(() => { if (!e.defaultPrevented) busySoon(text); }, 0);  // after any other handler took it over
  });

  // Going to another page of the app: the loader if the page takes a moment (e.g. the server waking up).
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.dataset.busy || a.matches("[data-no-busy], [download], [data-photo], [data-share-pdf]")) return;
    if (a.target && a.target !== "_self") return;
    const href = a.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript:")) return;
    let url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search)) return;
    if (/\.(pdf|xlsx|csv|zip|json|gz)$|\/(receipt|invoice|export|download)\b/i.test(url.pathname)) return;  // files
    setTimeout(() => { if (!e.defaultPrevented) busySoon("Loading…", 400); }, 0);  // the photo viewer etc. stay here
  });

  // htmx parts of a page (when they're used): the loader if the answer takes a moment.
  document.addEventListener("htmx:beforeRequest", () => busySoon("Loading…", 400));
  document.addEventListener("htmx:afterRequest", unbusy);

  // Keep the server awake while this page is open: Render's free plan puts it to sleep after 15 minutes without
  // requests, and waking takes ~50 s. If the page hasn't talked to the server for 10 minutes (left idle), send a
  // quiet ping — the page itself is never reloaded, so nothing being typed is lost.
  const AWAKE_MS = 10 * 60 * 1000;
  let lastContact = Date.now();
  document.addEventListener("htmx:afterRequest", () => { lastContact = Date.now(); });
  setInterval(() => {
    if (Date.now() - lastContact < AWAKE_MS) return;
    lastContact = Date.now();
    fetch("/healthz/", { cache: "no-store" }).catch(() => {});
  }, 60 * 1000);

  // File viewer: ID files, receipts and documents open in the page — photos (tap to zoom), PDFs and videos — with
  // ‹ › / arrow keys / swipe between the files in the same group. Other files (and phones' PDFs) get an Open button.
  // Without JS the links still open the file in a new tab.
  let viewer, shown = [], at = 0;
  const preloaded = new Map();
  const kindOf = (a) => a.dataset.kind || "image";
  const preload = (a) => {
    if (a && kindOf(a) === "image" && !preloaded.has(a.href)) { const i = new Image(); i.src = a.href; preloaded.set(a.href, i); }
  };
  const showPhoto = () => {
    const a = shown[at], kind = kindOf(a);
    const stage = viewer.querySelector(".viewer-stage"), img = viewer.querySelector("img"), status = viewer.querySelector(".viewer-status");
    stage.querySelectorAll("iframe, video, .viewer-file").forEach((el) => el.remove());
    // Clear the previous file first: files come from Google Drive and take a moment.
    img.hidden = true;
    img.classList.remove("zoomed");
    img.removeAttribute("src");
    status.hidden = false;
    const openButton = (text) => {
      status.hidden = true;
      const box = document.createElement("div");
      box.className = "viewer-file";
      box.innerHTML = `<p>${text}</p><a class="btn btn-primary" target="_blank" rel="noopener"></a>`;
      box.querySelector("a").href = a.href;
      box.querySelector("a").textContent = kind === "pdf" ? "Open the PDF" : "Open / download";
      stage.append(box);
    };
    if (kind === "image") {
      status.textContent = "Loading…";
      img.onload = () => { if (img.src === a.href) { img.hidden = false; status.hidden = true; } };
      img.onerror = () => { if (img.src === a.href) openButton("This file can’t be shown here."); };
      img.src = a.href;
    } else if (kind === "pdf" && !MOBILE_VIEW) {
      status.textContent = "Loading the PDF…";
      const frame = document.createElement("iframe");
      frame.title = a.title || "PDF";
      frame.addEventListener("load", () => { status.hidden = true; });
      frame.src = a.href;
      stage.append(frame);
    } else if (kind === "video") {
      status.textContent = "Loading the video…";
      const video = document.createElement("video");
      video.controls = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.addEventListener("loadedmetadata", () => { status.hidden = true; });
      video.addEventListener("error", () => { video.remove(); openButton("This video can’t play here."); });
      video.src = a.href;
      stage.append(video);
    } else {
      openButton(kind === "pdf" ? "PDF" : "This file opens outside the app.");
    }
    if (shown.length > 1) { preload(shown[(at + 1) % shown.length]); preload(shown[(at - 1 + shown.length) % shown.length]); }
    viewer.querySelector(".viewer-title").textContent = a.title || a.textContent.trim();
    viewer.querySelector(".viewer-count").textContent = shown.length > 1 ? `${at + 1} / ${shown.length}` : "";
    viewer.querySelectorAll(".viewer-nav").forEach((b) => { b.hidden = shown.length < 2; });
  };
  const MOBILE_VIEW = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);  // phones show PDFs outside
  const step = (d) => { if (shown.length > 1) { at = (at + d + shown.length) % shown.length; showPhoto(); } };
  const makeViewer = () => {
    viewer = document.createElement("dialog");
    viewer.className = "photo-viewer";
    viewer.innerHTML = `<div class="viewer-bar"><span class="viewer-title"></span><span class="viewer-count"></span>
        <a class="viewer-open" target="_blank" rel="noopener" title="Open in a new tab">↗</a>
        <button type="button" class="viewer-close" aria-label="Close">×</button></div>
      <div class="viewer-stage"><img alt="Photo" hidden><p class="viewer-status"></p></div>
      <button type="button" class="viewer-nav viewer-prev" aria-label="Previous file">‹</button>
      <button type="button" class="viewer-nav viewer-next" aria-label="Next file">›</button>`;
    document.body.append(viewer);
    viewer.querySelector(".viewer-close").addEventListener("click", () => viewer.close());
    viewer.querySelector(".viewer-prev").addEventListener("click", () => step(-1));
    viewer.querySelector(".viewer-next").addEventListener("click", () => step(1));
    viewer.querySelector(".viewer-open").addEventListener("click", (e) => { e.currentTarget.href = shown[at].href; });
    const img = viewer.querySelector("img");
    img.addEventListener("click", () => img.classList.toggle("zoomed"));
    viewer.addEventListener("click", (e) => { if (e.target === viewer || e.target.classList.contains("viewer-stage")) viewer.close(); });
    viewer.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft") step(-1);
      if (e.key === "ArrowRight") step(1);
    });
    let x0 = null;
    viewer.addEventListener("touchstart", (e) => {
      const onMedia = e.target.closest && e.target.closest("video, iframe");
      x0 = e.touches.length === 1 && !img.classList.contains("zoomed") && !onMedia ? e.touches[0].clientX : null;
    }, { passive: true });
    viewer.addEventListener("touchend", (e) => {
      if (x0 === null) return;
      const dx = e.changedTouches[0].clientX - x0;
      if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
      x0 = null;
    });
    viewer.addEventListener("close", () => {
      viewer.querySelector("img").removeAttribute("src");
      viewer.querySelectorAll("iframe, video, .viewer-file").forEach((el) => el.remove());  // stops a playing video
      preloaded.clear();
    });
  };
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-viewer]");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || !window.HTMLDialogElement) return;
    e.preventDefault();
    if (!viewer) makeViewer();
    const seen = new Set();
    shown = Array.from(document.querySelectorAll(`a[data-viewer="${a.dataset.viewer}"]`))
      .filter((l) => !seen.has(l.href) && seen.add(l.href));
    at = Math.max(0, shown.findIndex((l) => l.href === a.href));
    showPhoto();
    viewer.showModal();
  });

  // Choosing ID files: they wait in a list (add more, remove any) until "Upload all", which sends them in one go.
  const fmtSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
  const MAX_FILE = 25 * 1048576, MAX_TOTAL = 60 * 1048576, MAX_FILES = 10;
  document.querySelectorAll("form[data-stage]").forEach((form) => {
    const input = form.querySelector("[data-stage-input]"), list = form.querySelector("[data-stage-list]");
    if (!input || !list || typeof DataTransfer === "undefined") return;
    let picked = [];
    const render = () => {
      const dt = new DataTransfer();
      picked.forEach((f) => dt.items.add(f));
      input.files = dt.files;
      list.hidden = picked.length === 0;
      list.innerHTML = "";
      const total = picked.reduce((n, f) => n + f.size, 0);
      picked.forEach((f, i) => {
        const row = document.createElement("div");
        row.className = "stage-item" + (f.size > MAX_FILE ? " is-bad" : "");
        const icon = f.type.startsWith("image/") ? "🖼" : f.type === "application/pdf" ? "📄" : f.type.startsWith("video/") ? "🎬" : "📎";
        row.innerHTML = `<span class="stage-icon" aria-hidden="true">${icon}</span><span class="stage-name"></span>
          <small class="muted"></small><button type="button" class="btn btn-ghost btn-sm" aria-label="Remove">×</button>`;
        row.querySelector(".stage-name").textContent = f.name;
        row.querySelector("small").textContent = fmtSize(f.size) + (f.size > MAX_FILE ? " — over 25 MB" : "");
        row.querySelector("button").addEventListener("click", () => { picked.splice(i, 1); render(); });
        list.append(row);
      });
      const problem = picked.some((f) => f.size > MAX_FILE) ? "Remove the files over 25 MB."
        : picked.length > MAX_FILES ? `Up to ${MAX_FILES} files at a time.`
        : total > MAX_TOTAL ? "Together over 60 MB — upload in two goes." : "";
      const bar = document.createElement("div");
      bar.className = "stage-actions";
      bar.innerHTML = `<button class="btn btn-primary btn-sm">Upload all (${picked.length})</button>
        <button type="button" class="btn btn-ghost btn-sm" data-stage-clear>Clear</button><small class="stage-problem"></small>`;
      bar.querySelector(".stage-problem").textContent = problem;
      bar.querySelector("button.btn-primary").disabled = Boolean(problem);
      bar.querySelector("[data-stage-clear]").addEventListener("click", () => { picked = []; render(); });
      if (picked.length) list.append(bar);
    };
    input.addEventListener("change", () => {
      const added = Array.from(input.files).filter((f) => !picked.some((p) => p.name === f.name && p.size === f.size));
      picked = picked.concat(added);
      render();
    });
  });

  // "Select all" for the earlier ID files.
  document.querySelectorAll("[data-check-all]").forEach((all) => all.addEventListener("change", () => {
    all.closest("form").querySelectorAll('input[type="checkbox"][name="photo"]').forEach((c) => { c.checked = all.checked; });
  }));

  // Close the mobile "More" sheet when tapping elsewhere
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.tab-more[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");

  // ── Extend page: quick +N night buttons and running total ──
  const ext = $(".extend-form");
  if (ext) {
    const date = ext.elements["new_check_out"];
    ext.querySelectorAll("[data-set-date]").forEach((b) => b.addEventListener("click", () => {
      date.value = b.dataset.setDate;
      ext.querySelectorAll("[data-set-date]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      ext.elements["amount"].focus();
      summary();
    }));
    const summary = () => {
      if (ext.dataset.total === "") { $("#extend-summary").textContent = "Amounts are hidden — unlock money to see the new total."; return; }
      const total = parseFloat(ext.dataset.total) + (parseFloat(ext.elements["amount"].value) || 0);
      const paid = parseFloat(ext.dataset.paid) + (parseFloat(ext.elements["paid_now"].value) || 0);
      $("#extend-summary").innerHTML = `New total <b>${inr(total)}</b> · Paid <b>${inr(paid)}</b> · Balance <b>${inr(total - paid)}</b>`;
    };
    ["amount", "paid_now"].forEach((n) => ext.elements[n].addEventListener("input", summary));
    summary();
  }

  // Room clash on the stay form: show the choices as a popup (CSS makes it a bottom drawer on phones).
  const clash = $("#clash");
  if (clash && clash.showModal) {
    clash.close();
    clash.showModal();
    clash.addEventListener("click", (e) => {
      if (e.target === clash || e.target.closest("[data-close-dialog]")) clash.close();  // tap outside / Close
    });
  }

  // WhatsApp on a computer: get the message first, copy it, then open WhatsApp Web in the one "whatsapp" tab. If
  // WhatsApp Web is already open (or installed as an app) it sometimes doesn't fill the message in — then Ctrl+V.
  // Phones open the WhatsApp app directly, as before (links work without JS too).
  const MOBILE = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
  let toastEl, toastTimer;
  function toast(html) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "wa-toast";
      toastEl.setAttribute("role", "status");
      document.body.append(toastEl);
    }
    toastEl.innerHTML = html;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 9000);
  }
  function openWhatsApp(url, init, fallback, after) {
    busySoon("Preparing the message…");
    fetch(url, Object.assign({ headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" }, init))
      .then((r) => ((r.headers.get("content-type") || "").includes("json") ? r.json() : Promise.reject(r)))
      .finally(unbusy)
      .then((d) => {
        const go = () => {
          const win = window.open(d.url, "whatsapp");
          if (!win) toast(`<a class="btn btn-primary btn-sm" href="${d.url}" target="whatsapp">Open WhatsApp</a>`);
        };
        const copied = d.text && navigator.clipboard ? navigator.clipboard.writeText(d.text) : Promise.reject();
        copied.then(() => { toast("Message copied — if WhatsApp doesn’t fill it in, press <b>Ctrl+V</b> in the chat."); go(); }, go)
          .then(() => { if (after) setTimeout(after, 1500); });
      })
      .catch(fallback);
  }
  if (!MOBILE) {
    document.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest('a[target="whatsapp"]');
      if (!a || !/\/stays\/\d+\/whatsapp\//.test(a.pathname) || e.ctrlKey || e.metaKey || e.shiftKey) return;
      e.preventDefault();
      openWhatsApp(a.href, {}, () => window.open(a.href, "whatsapp"));
    });
    document.addEventListener("submit", (e) => {
      const form = e.target;
      if (e.defaultPrevented || form.target !== "whatsapp") return;
      e.preventDefault();
      if (form.method.toLowerCase() === "post") {
        // Then reload, e.g. for the "Payment links" list on the stay page.
        openWhatsApp(form.action, { method: "POST", body: new FormData(form) }, () => location.reload(), () => location.reload());
      } else {
        const url = form.action + "?" + new URLSearchParams(new FormData(form)).toString();
        openWhatsApp(url, {}, () => window.open(url, "whatsapp"));
      }
    });
  }

  // Message all in-house guests: "Send to next guest" opens the next chat; each opened chat is ticked off.
  const bulk = $("#wa-bulk");
  if (bulk) {
    const links = Array.from(bulk.querySelectorAll("[data-wa-send]"));
    const next = $("#wa-next");
    const refresh = () => {
      const left = links.filter((a) => !a.classList.contains("is-sent"));
      if (!next) return;
      next.disabled = !left.length;
      next.textContent = left.length
        ? `Send to ${left[0].dataset.name} (${links.length - left.length + 1} of ${links.length})`
        : `All ${links.length} sent ✓`;
    };
    links.forEach((a) => a.addEventListener("click", () => {
      a.classList.add("is-sent");
      a.textContent = "Opened ✓";
      setTimeout(refresh);
    }));
    if (next) next.addEventListener("click", () => { const a = links.find((x) => !x.classList.contains("is-sent")); if (a) a.click(); });
    refresh();
  }

  const form = $(".stay-form");
  if (!form) return;

  const f = (name) => form.elements[name];

  // ── Foreign national fields ──
  const foreign = $("#foreign");
  const toggleForeign = () => {
    const n = (f("g-nationality").value || "").trim().toLowerCase();
    foreign.hidden = !n || n === "indian" || n === "india";
  };
  f("g-nationality").addEventListener("input", toggleForeign);
  toggleForeign();

  // ── Room → capacity ──
  const roomSel = f("s-room");
  const roomMax = Object.fromEntries(
    (roomSel.dataset.rooms || "").split(",").filter(Boolean).map((x) => x.split(":"))
  );
  roomSel.addEventListener("change", () => { if (roomMax[roomSel.value]) f("s-num_guests").max = roomMax[roomSel.value]; });

  // ── Monthly (open-ended) stays: show rent & deposit, check-out optional ──
  const kind = f("s-kind");
  const monthly = $("#monthly-fields");
  const syncKind = () => {
    const isMonthly = kind.value === "monthly";
    monthly.hidden = !isMonthly;
    const lbl = form.querySelector('label[for="id_s-check_out"]');
    if (lbl) lbl.firstChild.textContent = isMonthly ? "Check out (leave empty until they leave) " : "Check out ";
  };
  kind.addEventListener("change", () => {
    if (kind.value === "monthly") f("s-check_out").value = "";  // open-ended until they leave
    syncKind();
    update();
  });
  syncKind();

  // ── Airbnb etc.: nights extended directly with us (invoiced) ──
  const source = f("s-source");
  const direct = $("#direct-fields");
  if (source && direct) {
    // Online bookings need a host: mark the Host label required.
    const hostLabel = form.querySelector('label[for="id_s-host"]');
    const star = document.createElement("span");
    star.className = "req";
    star.setAttribute("aria-hidden", "true");
    star.textContent = " *";
    const syncSource = () => {
      const online = !["walk_in", "direct", ""].includes(source.value);
      direct.hidden = !online;
      if (hostLabel) { if (online) hostLabel.append(star); else star.remove(); }
    };
    source.addEventListener("change", syncSource);
    syncSource();
  }

  // ── Other guests: one row per guest beyond the first ──
  const guestsInput = f("s-num_guests");
  const rows = Array.from(document.querySelectorAll(".other-guest"));
  const syncRows = () => {
    const wanted = Math.max(0, (parseInt(guestsInput.value, 10) || 1) - 1);
    rows.forEach((r, i) => { r.hidden = i >= wanted; });
    const none = $("#other-guests-none");
    if (none) none.hidden = wanted > 0;
  };
  guestsInput.addEventListener("input", syncRows);
  syncRows();

  // ── Nights / total / balance ──
  const ci = f("s-check_in"), co = f("s-check_out");
  const update = () => {
    const a = ci.valueAsDate, b = co.valueAsDate;
    const nights = a && b ? Math.round((b - a) / 86400000) : 0;
    const total = parseFloat(f("s-total_amount").value) || 0;
    const paid = parseFloat(f("s-amount_paid").value) || 0;
    $("#nights-line").textContent = nights > 0 ? `${nights} night${nights === 1 ? "" : "s"}` : (a && b ? "Check-out must be after check-in" : "");
    $("#total-line").innerHTML = `Total <b>${inr(total)}</b> · Paid <b>${inr(paid)}</b> · Balance <b>${inr(total - paid)}</b>`;
  };
  ci.addEventListener("change", () => {
    if (ci.value && kind.value !== "monthly" && (!co.value || co.value <= ci.value)) {
      const d = ci.valueAsDate; d.setUTCDate(d.getUTCDate() + 1); co.valueAsDate = d;
    }
    co.min = ci.value;
    update();
  });
  ["s-check_out", "s-total_amount", "s-amount_paid"].forEach((n) => f(n).addEventListener("input", update));
  update();

  // ── Returning guest autofill (new stays only) ──
  const guestId = f("g-guest_id");
  if (guestId.value) return; // editing an existing stay's guest
  const box = $("#returning");
  const phone = f("g-phone");
  let timer, lastLookup = "";

  const idField = f("g-id_number");
  const idPlaceholder = idField.placeholder;

  const clearReturning = () => {
    guestId.value = "";
    box.hidden = true;
    idField.placeholder = idPlaceholder;
  };

  const lookup = async () => {
    const digits = phone.value.replace(/\D/g, "");
    if (digits.length < 10) { clearReturning(); return; }
    if (digits === lastLookup) return;
    lastLookup = digits;
    try {
      const res = await fetch(`${document.body.dataset.base || "/"}guests/lookup/?phone=${encodeURIComponent(phone.value)}`, { headers: { Accept: "application/json" } });
      const g = await res.json();
      if (!g.found) { clearReturning(); return; }
      box.innerHTML = "";
      const text = document.createElement("span");
      text.textContent = `Returning guest: ${g.name} — ${g.stays} previous stay${g.stays === 1 ? "" : "s"}${g.last_stay ? ", last on " + g.last_stay : ""}. `;
      const use = document.createElement("button");
      use.type = "button";
      use.className = "btn btn-sm btn-primary";
      use.textContent = "Use saved details";
      use.addEventListener("click", () => {
        guestId.value = g.guest_id;
        f("g-name").value = g.name;
        f("g-address").value = g.address;
        f("g-nationality").value = g.nationality;
        f("g-id_type").value = g.id_type;
        idField.value = "";
        idField.placeholder = `Saved: ${g.id_masked} — leave blank to keep`;
        f("g-visa_number").value = g.visa_number;
        f("g-visa_type").value = g.visa_type;
        f("g-arrival_in_india").value = g.arrival_in_india;
        if (g.passport_masked) f("g-passport_number").placeholder = `Saved: ${g.passport_masked} — leave blank to keep`;
        toggleForeign();
        text.textContent = `Using saved details for ${g.name}. Edit anything that has changed.`;
        use.remove();
      });
      box.append(text, use);
      box.hidden = false;
    } catch (_) { /* offline: just type the details */ }
  };
  phone.addEventListener("input", () => {
    if (guestId.value && phone.value.replace(/\D/g, "") !== lastLookup) clearReturning();
    clearTimeout(timer);
    timer = setTimeout(lookup, 350);
  });
  phone.addEventListener("blur", lookup);
})();

// Share invoice PDF (phones): the share sheet sends the file itself, e.g. to the guest on WhatsApp.
// The PDF is fetched when the page loads, because phones only allow sharing straight after the tap.
(function () {
  var buttons = document.querySelectorAll("[data-share-pdf]");
  if (!buttons.length || !navigator.canShare) return;
  var probe;
  try { probe = new File([""], "x.pdf", { type: "application/pdf" }); } catch (e) { return; }
  if (!navigator.canShare({ files: [probe] })) return;
  buttons.forEach(function (btn) {
    var file = null;
    fetch(btn.dataset.sharePdf, { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.blob(); })
      .then(function (blob) {
        file = new File([blob], btn.dataset.filename, { type: "application/pdf" });
        btn.hidden = false;
      })
      .catch(function () {});
    btn.addEventListener("click", function () {
      if (!file) return;
      navigator.share({ files: [file], title: btn.dataset.title }).catch(function (e) {
        if (e && e.name !== "AbortError") alert("Couldn’t share the invoice. Use Download instead.");
      });
    });
  });
})();

// Cluster item theme: live preview while picking (nothing is saved until "Save theme").
(() => {
  const picker = document.querySelector("[data-theme-picker]");
  if (!picker) return;
  const preview = picker.querySelector("[data-preview]");
  picker.addEventListener("change", (e) => {
    const choice = e.target.closest(".theme-choice");
    if (!choice || !preview) return;
    preview.style.setProperty("--p-accent", choice.dataset.accent);
    preview.style.setProperty("--p-dark", choice.dataset.dark);
  });
})();

// PG / co-living pages.
(() => {
  // PG move-in: returning resident by phone; the date-of-month only for a fixed due date.
  const pgForm = document.querySelector("form[data-pg-move-in]");
  if (pgForm) {
    const $ = (n) => pgForm.querySelector(`[name="${n}"]`);
    const phone = $("r-phone"), cycle = $("t-cycle"), rule = $("t-due_rule");
    const dayWrap = pgForm.querySelector(".due-day-wrap"), note = pgForm.querySelector("[data-returning]");
    const syncDue = () => { if (dayWrap) dayWrap.hidden = !(rule && rule.value === "fixed" && cycle.value === "monthly"); };
    if (cycle) cycle.addEventListener("change", syncDue);
    if (rule) rule.addEventListener("change", syncDue);
    syncDue();
    if (phone) phone.addEventListener("change", async () => {
      const digits = phone.value.replace(/\D/g, "");
      if (digits.length < 10) return;
      try {
        const r = await fetch(pgForm.dataset.lookup + "?phone=" + encodeURIComponent(phone.value));
        const d = await r.json();
        $("resident_id").value = d.found ? d.id : "";
        if (!d.found) { note.hidden = true; return; }
        const fill = { "r-name": d.name, "r-address": d.address, "r-id_type": d.id_type, "r-organisation": d.organisation,
          "r-occupation": d.occupation, "r-emergency_name": d.emergency_name, "r-emergency_relation": d.emergency_relation,
          "r-emergency_phone": d.emergency_phone, "r-vehicle": d.vehicle };
        for (const [k, v] of Object.entries(fill)) { const el = $(k); if (el && v && !el.value) el.value = v; }
        note.textContent = `Returning resident: ${d.name}` + (d.id_masked ? ` · ID ${d.id_masked} saved (leave ID empty to keep it)` : "") +
          (d.open ? ` · already ${d.open} — shift them instead` : "");
        note.hidden = false;
      } catch (e) { /* offline: they type the details */ }
    });
  }

  // PG move-out: the refund as you type (advance − unpaid rent − deductions).
  const settle = document.querySelector("form[data-settle]");
  if (settle) {
    const out = settle.querySelector("[data-settle-total]");
    const num = (v) => Number(String(v).replace(/[₹,\s]/g, "")) || 0;
    const calc = () => {
      let left = num(settle.dataset.deposit);
      settle.querySelectorAll("[data-settle-amount]").forEach((i) => { left -= num(i.value); });
      settle.querySelectorAll("[data-settle-add]").forEach((i) => { left += Math.min(num(i.value), num(i.dataset.max)); });
      const fmt = (n) => "₹" + Math.round(Math.abs(n)).toLocaleString("en-IN");
      out.textContent = fmt(left);
      const label = settle.querySelector("[data-settle-label]");
      if (label) label.textContent = left >= 0 ? "Refund" : "Still to be paid by the resident";
    };
    settle.addEventListener("input", calc);
    calc();
  }

  // Copy a text box (e.g. the vacancy message) to the clipboard.
  document.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    const box = document.querySelector(btn.dataset.copy);
    if (!box) return;
    try { await navigator.clipboard.writeText(box.value); } catch (e) { box.select(); document.execCommand("copy"); }
    const was = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = was; }, 1500);
  }));

  // PG move-out: changing the date works the rent out again for that day.
  const reloadDay = document.querySelector("[data-reload-day]");
  if (reloadDay) reloadDay.addEventListener("change", () => {
    if (reloadDay.value) location.href = location.pathname + "?day=" + encodeURIComponent(reloadDay.value);
  });
})();

// File pickers open on photos and PDFs (on phones: the gallery, where WhatsApp's images are). "Video / other file"
// opens the same picker once with every kind of file.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-any-file]");
  if (!btn) return;
  const input = btn.closest("form").querySelector('input[type="file"]');
  if (!input) return;
  const accept = input.getAttribute("accept");
  input.removeAttribute("accept");
  const restore = () => { if (accept) input.setAttribute("accept", accept); };
  input.addEventListener("change", restore, { once: true });
  window.addEventListener("focus", () => setTimeout(restore, 800), { once: true });
  input.click();
});
