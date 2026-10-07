// BSR Corridors admin — small progressive enhancements (pages work without JS too).
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);

  // Confirm before submitting forms marked data-confirm="…"
  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Close the mobile "More" sheet when tapping elsewhere
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.tab-more[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
  });

  const form = $(".stay-form");
  if (!form) return;

  const f = (name) => form.elements[name];
  const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");

  // ── Foreign national fields ──
  const foreign = $("#foreign");
  const toggleForeign = () => {
    const n = (f("g-nationality").value || "").trim().toLowerCase();
    foreign.hidden = !n || n === "indian" || n === "india";
  };
  f("g-nationality").addEventListener("input", toggleForeign);
  toggleForeign();

  // ── Room → default rate & capacity ──
  const roomSel = f("s-room");
  const roomInfo = Object.fromEntries(
    (roomSel.dataset.rooms || "").split(",").filter(Boolean).map((x) => {
      const [id, rate, max] = x.split(":");
      return [id, { rate: parseFloat(rate), max: parseInt(max, 10) }];
    })
  );
  let lastDefault = roomInfo[roomSel.value] ? roomInfo[roomSel.value].rate : null;
  roomSel.addEventListener("change", () => {
    const info = roomInfo[roomSel.value];
    if (!info) return;
    const rate = f("s-nightly_rate");
    if (!rate.value || parseFloat(rate.value) === 0 || parseFloat(rate.value) === lastDefault) rate.value = info.rate;
    lastDefault = info.rate;
    f("s-num_guests").max = info.max;
    update();
  });

  // ── Nights / total / balance ──
  const ci = f("s-check_in"), co = f("s-check_out");
  const update = () => {
    const a = ci.valueAsDate, b = co.valueAsDate;
    const nights = a && b ? Math.round((b - a) / 86400000) : 0;
    const rate = parseFloat(f("s-nightly_rate").value) || 0;
    const paid = parseFloat(f("s-amount_paid").value) || 0;
    $("#nights-line").textContent = nights > 0 ? `${nights} night${nights === 1 ? "" : "s"}` : (a && b ? "Check-out must be after check-in" : "");
    const total = nights > 0 ? nights * rate : 0;
    $("#total-line").innerHTML = nights > 0
      ? `Total <b>${inr(total)}</b> · Paid <b>${inr(paid)}</b> · Balance <b>${inr(total - paid)}</b>`
      : "";
  };
  ci.addEventListener("change", () => {
    if (ci.value && (!co.value || co.value <= ci.value)) {
      const d = ci.valueAsDate; d.setUTCDate(d.getUTCDate() + 1); co.valueAsDate = d;
    }
    co.min = ci.value;
    update();
  });
  ["s-check_out", "s-nightly_rate", "s-amount_paid"].forEach((n) => f(n).addEventListener("input", update));
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
      const res = await fetch(`/guests/lookup/?phone=${encodeURIComponent(phone.value)}`, { headers: { Accept: "application/json" } });
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
