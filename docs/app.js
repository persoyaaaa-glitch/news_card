const IST_OFFSET_MIN = 5.5 * 60;

function nowIST() {
  const now = new Date();
  const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
  return new Date(utcMs + IST_OFFSET_MIN * 60000);
}

function fmtTime(d) {
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function fmtDate(d) {
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

// ---- Supabase (read-only anon access, RLS-restricted - see supabase_app_additions.sql) ----

async function fetchDailySlots() {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots&select=value`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) throw new Error(`Failed to load schedule (${resp.status})`);
  const rows = await resp.json();
  if (!rows.length) return { date: null, slots: [] };
  return rows[0].value;
}

async function fetchRepoTraffic() {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/app_state?key=eq.repo_traffic&select=value`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) return null;
  const rows = await resp.json();
  return rows.length ? rows[0].value : null;
}

async function fetchManualIndices(dateStr) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/slot_overrides?slot_date=eq.${dateStr}&select=slot_index`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) return new Set();
  const rows = await resp.json();
  return new Set(rows.map((r) => r.slot_index));
}

async function markSlotManual(dateStr, slotIndex) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/slot_overrides`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates",
    },
    body: JSON.stringify({ slot_date: dateStr, slot_index: slotIndex, manual: true }),
  });
  return resp.ok;
}

async function fetchScheduleOverride(dateStr) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/schedule_overrides?slot_date=eq.${dateStr}&select=*`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) return null; // e.g. schedule_overrides doesn't exist yet - treat as "no override"
  const rows = await resp.json();
  return rows.length ? rows[0] : null;
}

async function saveScheduleOverride(dateStr, targetCount, timeEdits) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/schedule_overrides`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates",
    },
    body: JSON.stringify({ slot_date: dateStr, target_count: targetCount, time_edits: timeEdits }),
  });
  return resp.ok;
}

async function saveSubscription(sub) {
  const json = sub.toJSON();
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/push_subscriptions`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates",
    },
    body: JSON.stringify({
      endpoint: json.endpoint,
      p256dh: json.keys.p256dh,
      auth: json.keys.auth,
    }),
  });
  if (!resp.ok) {
    console.error(`[push] failed to save subscription (${resp.status}): ${await resp.text()}`);
  }
}

// ---- Rendering ----

let currentSlots = [];
let currentManualIndices = new Set();
let currentDateStr = null;

function statusOf(slot, allSorted, index) {
  const now = nowIST();
  const planned = new Date(slot.planned_time);
  if (planned <= now) return "past";
  const nextUpcoming = allSorted.find((s) => new Date(s.planned_time) > now);
  if (nextUpcoming && nextUpcoming.index === slot.index) return "next";
  return "pending";
}

function render(data, manualIndices) {
  currentDateStr = data.date;
  currentManualIndices = manualIndices || new Set();
  document.getElementById("dateLabel").textContent = data.date
    ? fmtDate(new Date(data.date))
    : "No schedule yet";

  const list = document.getElementById("list");
  const empty = document.getElementById("emptyState");
  const slots = (data.slots || []).slice().sort((a, b) => a.index - b.index);
  currentSlots = slots;

  if (!slots.length) {
    empty.hidden = false;
    list.querySelectorAll(".slot-row").forEach((el) => el.remove());
    updateProgress(0, 0);
    return;
  }
  empty.hidden = true;

  const now = nowIST();
  const pastCount = slots.filter((s) => new Date(s.planned_time) <= now).length;
  updateProgress(pastCount, slots.length);

  list.querySelectorAll(".slot-row").forEach((el) => el.remove());

  slots.forEach((slot, i) => {
    const planned = new Date(slot.planned_time);
    const status = statusOf(slot, slots, i);
    const isManual = currentManualIndices.has(slot.index);
    const row = document.createElement("div");
    row.className = `slot-row ${status}${isManual ? " manual" : ""}`;
    const topStory = (slot.stories && slot.stories[0]) || null;

    row.innerHTML = `
      <span class="slot-num">${String(i + 1).padStart(2, "0")}</span>
      <span class="slot-dot ${status === "past" ? "posted" : status}"></span>
      <div class="slot-main">
        <div class="slot-time">${fmtTime(planned)}${isManual ? ' <span class="manual-tag">MANUAL</span>' : ""}</div>
        <div class="slot-headline">${topStory ? escapeHtml(topStory.title) : "Pending"}</div>
      </div>
      <span class="slot-meta">${(slot.stories || []).length}&middot;${(slot.image_urls || []).length}${(slot.image_urls_hi || []).length ? ' <span class="hi-badge">HI</span>' : ""}</span>
    `;
    row.addEventListener("click", () => openModal(slot));
    list.appendChild(row);
  });
}

function updateProgress(done, total) {
  document.getElementById("progressLabel").textContent = `${done}/${total}`;
  document.getElementById("progressFill").style.width = total ? `${(done / total) * 100}%` : "0%";
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function tickClock() {
  document.getElementById("clock").textContent = fmtTime(nowIST());
}

// ---- Modal ----

let currentModalSlot = null;
let currentModalLang = "en";

function openModal(slot) {
  currentModalSlot = slot;
  currentModalLang = "en";
  const modal = document.getElementById("modal");
  const planned = new Date(slot.planned_time);
  document.getElementById("modalTime").textContent = fmtTime(planned) + " IST";
  document.getElementById("modalSub").textContent =
    `${(slot.stories || []).length} stories · ${(slot.image_urls || []).length} slides`;

  const hasHindi = (slot.image_urls_hi || []).length > 0;
  const langTabs = document.getElementById("langTabs");
  langTabs.hidden = !hasHindi;
  document.getElementById("langTabEn").classList.add("active");
  document.getElementById("langTabHi").classList.remove("active");

  renderModalLang("en");

  const storyList = document.getElementById("storyList");
  storyList.innerHTML = "";
  (slot.stories || []).forEach((s) => {
    const li = document.createElement("li");
    if (s.is_sensitive) li.className = "sensitive";
    li.textContent = `${s.title} — ${s.source || ""}`;
    storyList.appendChild(li);
  });

  const manualBtn = document.getElementById("manualBtn");
  const manualStatus = document.getElementById("manualStatus");
  const alreadyManual = currentManualIndices.has(slot.index);
  manualBtn.disabled = alreadyManual;
  manualBtn.textContent = alreadyManual
    ? "Taken over \u2014 auto-post skipped for this one"
    : "Take over \u2014 post this one manually";
  manualStatus.textContent = alreadyManual
    ? "Download the slides above and post it yourself; the app will pick it up once it's live."
    : "";
  manualBtn.onclick = async () => {
    if (!currentDateStr) return;
    manualBtn.disabled = true;
    manualBtn.textContent = "Marking...";
    const ok = await markSlotManual(currentDateStr, slot.index);
    if (ok) {
      currentManualIndices.add(slot.index);
      manualBtn.textContent = "Taken over \u2014 auto-post skipped for this one";
      manualStatus.textContent = "Download the slides above and post it yourself; the app will pick it up once it's live.";
      render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    } else {
      manualBtn.disabled = false;
      manualBtn.textContent = "Take over \u2014 post this one manually";
      manualStatus.textContent = "Couldn't save that - check your connection and try again.";
    }
  };

  modal.hidden = false;
}

// Fills the slides scroller, caption, and download button for whichever
// language tab is active (slot's English fields have no suffix; Hindi
// fields are the same shape with an "_hi" suffix - see content_pregen.py /
// hourly_run.run_combined). Falls back to "no Hindi content" copy if a
// slot with the tab visible somehow has no Hindi images (e.g. every
// story in that slot failed translation - see hourly_run._build_hindi_slides).
function renderModalLang(lang) {
  currentModalLang = lang;
  const slot = currentModalSlot;
  if (!slot) return;

  const imageUrls = lang === "hi" ? (slot.image_urls_hi || []) : (slot.image_urls || []);
  const caption = lang === "hi" ? (slot.caption_hi || "") : (slot.caption || "");

  const scroller = document.getElementById("slidesScroller");
  scroller.innerHTML = "";
  if (!imageUrls.length && lang === "hi") {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "Hindi translation didn't come through for this slot's stories - only the English post will go out.";
    scroller.appendChild(p);
  } else {
    imageUrls.forEach((url) => {
      const img = document.createElement("img");
      img.src = url;
      img.loading = "lazy";
      scroller.appendChild(img);
    });
  }

  document.getElementById("captionText").textContent = caption;

  document.getElementById("downloadStatus").textContent = "";
  document.getElementById("downloadAllBtn").onclick = () => downloadAllSlides(imageUrls, lang);
  document.getElementById("copyBtn").onclick = () => copyCaption(caption);
  document.getElementById("copyBtn").classList.remove("copied");
  document.getElementById("copyBtn").textContent = "Copy";
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  currentModalSlot = null;
}

// ---- Schedule editor modal ----

let pendingOverride = null;

async function openScheduleModal() {
  if (!currentDateStr) return;
  const modal = document.getElementById("scheduleModal");
  const saveBtn = document.getElementById("saveScheduleBtn");
  const status = document.getElementById("scheduleSaveStatus");
  status.textContent = "";
  saveBtn.disabled = true;
  saveBtn.textContent = "Loading...";
  modal.hidden = false;

  pendingOverride = (await fetchScheduleOverride(currentDateStr)) || { target_count: null, time_edits: {} };
  renderScheduleModal();
  saveBtn.disabled = false;
  saveBtn.textContent = "Save changes";
}

function closeScheduleModal() {
  document.getElementById("scheduleModal").hidden = true;
}

function renderScheduleModal() {
  const now = nowIST();
  const postedSlots = currentSlots.filter((s) => new Date(s.planned_time) <= now);
  const pendingSlots = currentSlots.filter((s) => new Date(s.planned_time) > now);

  document.getElementById("scheduleModalSub").textContent =
    `${postedSlots.length} posted \u00b7 ${pendingSlots.length} remaining`;

  const countInput = document.getElementById("postsCountInput");
  countInput.min = Math.max(1, postedSlots.length);
  const currentTotal = pendingOverride.target_count || currentSlots.length;
  countInput.value = currentTotal;
  document.getElementById("postsCountHint").textContent =
    `Already-posted slots (${postedSlots.length}) can't be removed, so the lowest you can go is ${countInput.min}.`;

  const list = document.getElementById("pendingTimesList");
  list.innerHTML = "";
  if (!pendingSlots.length) {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "Nothing left to schedule today.";
    list.appendChild(p);
    return;
  }
  pendingSlots.forEach((slot) => {
    const planned = new Date(slot.planned_time);
    const row = document.createElement("div");
    row.className = "pending-time-row";
    row.innerHTML = `
      <span class="slot-num">${String(slot.index + 1).padStart(2, "0")}</span>
      <span>${(slot.stories && slot.stories[0]) ? escapeHtml(slot.stories[0].title).slice(0, 40) : "Pending"}</span>
      <input type="time" data-index="${slot.index}" value="${fmtTime(planned)}">
    `;
    list.appendChild(row);
  });
}

async function triggerScheduleCheck() {
  // Best-effort - if this fails, the schedule_overrides row is still
  // saved and the next */30 cron tick will pick it up regardless.
  try {
    const resp = await fetch(`${CONFIG.SUPABASE_URL}/functions/v1/trigger-schedule-check`, {
      method: "POST",
      headers: {
        apikey: CONFIG.SUPABASE_ANON_KEY,
        Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
      },
    });
    return resp.ok;
  } catch (e) {
    console.error(e);
    return false;
  }
}

async function saveScheduleChanges() {
  const saveBtn = document.getElementById("saveScheduleBtn");
  const status = document.getElementById("scheduleSaveStatus");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving...";
  status.textContent = "";

  const targetCount = parseInt(document.getElementById("postsCountInput").value, 10);
  const timeEdits = { ...(pendingOverride.time_edits || {}) };
  document.querySelectorAll("#pendingTimesList input[type='time']").forEach((input) => {
    if (input.value) timeEdits[input.dataset.index] = input.value;
  });

  const ok = await saveScheduleOverride(currentDateStr, isNaN(targetCount) ? null : targetCount, timeEdits);
  if (ok) {
    status.textContent = "Saved. Triggering an immediate check...";
    const triggered = await triggerScheduleCheck();
    status.textContent = triggered
      ? "Saved and a check is running now - refresh in a minute or two."
      : "Saved, but couldn't trigger an immediate check - it'll still apply on the next scheduled check (~30 min).";
    saveBtn.textContent = "Save changes";
    saveBtn.disabled = false;
  } else {
    status.textContent = "Couldn't save that - check your connection and try again.";
    saveBtn.textContent = "Save changes";
    saveBtn.disabled = false;
  }
}

async function downloadAllSlides(imageUrls, lang) {
  const btn = document.getElementById("downloadAllBtn");
  const status = document.getElementById("downloadStatus");
  const urls = imageUrls || [];
  const prefix = lang === "hi" ? "slide-hi" : "slide";
  btn.disabled = true;

  for (let i = 0; i < urls.length; i++) {
    status.textContent = `Downloading ${i + 1}/${urls.length}...`;
    try {
      const resp = await fetch(urls[i]);
      const blob = await resp.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `${prefix}-${String(i + 1).padStart(2, "0")}.jpg`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(objectUrl);
      // small pause so the browser doesn't drop rapid-fire downloads
      await new Promise((r) => setTimeout(r, 300));
    } catch (e) {
      status.textContent = `Slide ${i + 1} failed to download.`;
    }
  }
  status.textContent = urls.length ? `Saved ${urls.length} slide(s) to your downloads.` : "Nothing to download.";
  btn.disabled = false;
}

function copyCaption(text) {
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById("copyBtn");
    btn.textContent = "Copied";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = "Copy";
      btn.classList.remove("copied");
    }, 1500);
  });
}

// ---- Push notifications ----

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

async function setupPush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    console.warn("[push] serviceWorker or PushManager not supported in this browser");
    return;
  }

  const reg = await navigator.serviceWorker.register("sw.js");
  const existing = await reg.pushManager.getSubscription();

  if (existing) {
    // Already subscribed on this device - but the Supabase row may have
    // been deleted/reset since (table wipe, reseed, etc). Re-save it every
    // load so a locally-cached subscription always has a matching server
    // row. Cheap no-op if the row is already there (unique endpoint +
    // merge-duplicates upsert).
    await saveSubscription(existing);
    return;
  }

  if (Notification.permission === "granted") {
    await subscribeAndSave(reg);
    return;
  }
  if (Notification.permission === "denied") {
    console.warn("[push] Notification permission is denied - reset site permissions to retry");
    return;
  }

  // Not yet decided - show the banner and let the person tap to opt in.
  const banner = document.getElementById("notifyBanner");
  banner.hidden = false;
  document.getElementById("enableNotifyBtn").onclick = async () => {
    const perm = await Notification.requestPermission();
    if (perm === "granted") {
      await subscribeAndSave(reg);
      banner.hidden = true;
    } else {
      console.warn(`[push] permission not granted: ${perm}`);
    }
  };
}

async function subscribeAndSave(reg) {
  try {
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(CONFIG.VAPID_PUBLIC_KEY),
    });
    await saveSubscription(sub);
  } catch (e) {
    console.error("[push] subscribeAndSave failed:", e);
  }
}

// ---- Boot ----

async function refresh() {
  try {
    const data = await fetchDailySlots();
    const manualIndices = data.date ? await fetchManualIndices(data.date) : new Set();
    render(data, manualIndices);
  } catch (e) {
    console.error(e);
  }
  try {
    const traffic = await fetchRepoTraffic();
    renderTraffic(traffic);
  } catch (e) {
    console.error(e);
  }
}

function renderTraffic(traffic) {
  const el = document.getElementById("trafficLabel");
  if (!el) return;
  if (!traffic) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = `${traffic.count_14d} views · ${traffic.uniques_14d} visitors (14d)`;
}

document.getElementById("closeModalBtn").addEventListener("click", closeModal);
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") closeModal();
});
document.getElementById("langTabEn").addEventListener("click", () => {
  document.getElementById("langTabEn").classList.add("active");
  document.getElementById("langTabHi").classList.remove("active");
  renderModalLang("en");
});
document.getElementById("langTabHi").addEventListener("click", () => {
  document.getElementById("langTabHi").classList.add("active");
  document.getElementById("langTabEn").classList.remove("active");
  renderModalLang("hi");
});
document.getElementById("openScheduleBtn").addEventListener("click", openScheduleModal);
document.getElementById("closeScheduleModalBtn").addEventListener("click", closeScheduleModal);
document.getElementById("scheduleModal").addEventListener("click", (e) => {
  if (e.target.id === "scheduleModal") closeScheduleModal();
});
document.getElementById("saveScheduleBtn").addEventListener("click", saveScheduleChanges);
document.getElementById("postsCountMinus").addEventListener("click", () => {
  const input = document.getElementById("postsCountInput");
  input.value = Math.max(parseInt(input.min, 10) || 1, (parseInt(input.value, 10) || 1) - 1);
});
document.getElementById("postsCountPlus").addEventListener("click", () => {
  const input = document.getElementById("postsCountInput");
  input.value = Math.min(15, (parseInt(input.value, 10) || 1) + 1);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

tickClock();
setInterval(tickClock, 1000 * 30);
refresh();
setInterval(refresh, 1000 * 60 * 5);
setupPush().catch((e) => console.error("[push] setupPush failed:", e));
