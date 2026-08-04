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

async function saveSubscription(sub) {
  const json = sub.toJSON();
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/push_subscriptions`;
  await fetch(url, {
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
      <span class="slot-meta">${(slot.stories || []).length}&middot;${(slot.image_urls || []).length}</span>
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

function openModal(slot) {
  const modal = document.getElementById("modal");
  const planned = new Date(slot.planned_time);
  document.getElementById("modalTime").textContent = fmtTime(planned) + " IST";
  document.getElementById("modalSub").textContent =
    `${(slot.stories || []).length} stories · ${(slot.image_urls || []).length} slides`;

  const scroller = document.getElementById("slidesScroller");
  scroller.innerHTML = "";
  (slot.image_urls || []).forEach((url) => {
    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    scroller.appendChild(img);
  });

  document.getElementById("captionText").textContent = slot.caption || "";

  const storyList = document.getElementById("storyList");
  storyList.innerHTML = "";
  (slot.stories || []).forEach((s) => {
    const li = document.createElement("li");
    if (s.is_sensitive) li.className = "sensitive";
    li.textContent = `${s.title} — ${s.source || ""}`;
    storyList.appendChild(li);
  });

  document.getElementById("downloadStatus").textContent = "";
  document.getElementById("downloadAllBtn").onclick = () => downloadAllSlides(slot);
  document.getElementById("copyBtn").onclick = () => copyCaption(slot.caption || "");
  document.getElementById("copyBtn").classList.remove("copied");
  document.getElementById("copyBtn").textContent = "Copy";

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

function closeModal() {
  document.getElementById("modal").hidden = true;
}

async function downloadAllSlides(slot) {
  const btn = document.getElementById("downloadAllBtn");
  const status = document.getElementById("downloadStatus");
  const urls = slot.image_urls || [];
  btn.disabled = true;

  for (let i = 0; i < urls.length; i++) {
    status.textContent = `Downloading ${i + 1}/${urls.length}...`;
    try {
      const resp = await fetch(urls[i]);
      const blob = await resp.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `slide-${String(i + 1).padStart(2, "0")}.jpg`;
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
  status.textContent = `Saved ${urls.length} slide(s) to your downloads.`;
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
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;

  const reg = await navigator.serviceWorker.register("sw.js");
  const existing = await reg.pushManager.getSubscription();

  if (existing) return; // already subscribed on this device

  if (Notification.permission === "granted") {
    await subscribeAndSave(reg);
    return;
  }
  if (Notification.permission === "denied") return;

  // Not yet decided - show the banner and let the person tap to opt in.
  const banner = document.getElementById("notifyBanner");
  banner.hidden = false;
  document.getElementById("enableNotifyBtn").onclick = async () => {
    const perm = await Notification.requestPermission();
    if (perm === "granted") {
      await subscribeAndSave(reg);
      banner.hidden = true;
    }
  };
}

async function subscribeAndSave(reg) {
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(CONFIG.VAPID_PUBLIC_KEY),
  });
  await saveSubscription(sub);
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
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

tickClock();
setInterval(tickClock, 1000 * 30);
refresh();
setInterval(refresh, 1000 * 60 * 5);
setupPush();
