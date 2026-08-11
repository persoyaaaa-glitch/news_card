const IST_OFFSET_MIN = 5.5 * 60;

// Must match STORIES_PER_POST in daily_scheduler.py - also enforced
// server-side by save-slot-selection, this is just so the UI can cap
// selection and grey out "add" before hitting that 400.
const MAX_SELECTED_STORIES = 5;

function nowIST() {
  const now = new Date();
  const utcMs = now.getTime() + now.getTimezoneOffset() * 60000;
  return new Date(utcMs + IST_OFFSET_MIN * 60000);
}

// "YYYY-MM-DD" for an IST-shifted Date (e.g. from nowIST()) - matches
// the date strings daily_scheduler.py uses as slots_key() suffixes, so
// the client can compute which per-date keys to ask Supabase for
// without needing a round trip just to find out what "today" is.
function isoDateStrIST(d) {
  return d.toISOString().slice(0, 10);
}

function todayStrIST() {
  return isoDateStrIST(nowIST());
}

// [today, yesterday, ...] date strings, oldest last - matches the
// order daily_slots rows are shown/swiped through in the app.
function recentDateStrsIST(count) {
  const out = [];
  const d = nowIST();
  for (let i = 0; i < count; i++) {
    out.push(isoDateStrIST(d));
    d.setDate(d.getDate() - 1);
  }
  return out;
}

function fmtTime(d) {
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

function fmtDate(d) {
  return d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

// ---- Accounts (home screen) ----
//
// Every Instagram account this app manages. logo is a path relative to
// this file - drop the matching image next to index.html/app.js. "en"
// doesn't have a real logo yet (placeholder = icon-192.png) until one's
// uploaded; swap ACCOUNTS[0].logo once you have it, nothing else needs
// to change.
const ACCOUNTS = [
  { lang: "en", handle: "timely.brought", label: "ENGLISH", logo: "logo-en.png" },
  { lang: "hi", handle: "timely.samachar.hindi", label: "HINDI", logo: "logo-hi.png" },
];

function accountFor(lang) {
  return ACCOUNTS.find((a) => a.lang === lang) || ACCOUNTS[0];
}

// Slot content (stories/captions/images) is shared 1:1 by index between
// languages, but each language has its OWN clock time and its OWN
// posted-flag (see daily_scheduler.py's dual-track scheduling) - this
// picks the right field for whichever account is currently open.
function plannedTimeOf(slot, lang) {
  const iso = lang === "hi" ? slot.planned_time_hi : slot.planned_time;
  return new Date(iso || slot.planned_time);
}

// ---- Supabase (read-only anon access, RLS-restricted - see supabase_app_additions.sql) ----

// DAYS_KEPT must match daily_scheduler.py's DAILY_SLOTS_KEEP_DAYS - how
// many per-date daily_slots:* rows the backend keeps around (and the
// anon RLS policy allows reading - see migration_daily_slots_history.sql).
// Swiping further back than this just has nothing to show.
const DAYS_KEPT = 2;

async function fetchDailySlotsFor(dateStr) {
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots:${dateStr}&select=value`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) throw new Error(`Failed to load schedule (${resp.status})`);
  const rows = await resp.json();
  return rows.length ? rows[0].value : null;
}

// Today's schedule specifically - used by the home-screen "next up"
// badge, which should always reflect today regardless of which day the
// open schedule view is currently swiped to.
async function fetchDailySlots() {
  const dateStr = todayStrIST();
  return (await fetchDailySlotsFor(dateStr)) || { date: dateStr, slots: [] };
}

// Fetches the last `count` days' daily_slots rows in ONE request (keys
// daily_slots:D0,daily_slots:D1,... via the REST `in.()` filter) rather
// than one round trip per day - this is what powers the swipe-between-
// days view. Returns an array the same length as `count`, oldest last,
// with { date, slots: [] } placeholders for any day that has no row yet
// (e.g. day 1 of ever running this, or a gap).
async function fetchRecentDaysSlots(count) {
  const dateStrs = recentDateStrsIST(count);
  const keys = dateStrs.map((d) => `daily_slots:${d}`).join(",");
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/app_state?key=in.(${keys})&select=value`;
  const resp = await fetch(url, {
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
    },
  });
  if (!resp.ok) throw new Error(`Failed to load schedule (${resp.status})`);
  const rows = await resp.json();
  const byDate = {};
  rows.forEach((r) => { byDate[r.value.date] = r.value; });
  return dateStrs.map((d) => byDate[d] || { date: d, slots: [] });
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

// Reads the per-language manual flag (manual_en / manual_hi - added by
// migration_hi_manual_flag.sql, which renamed the original single
// `manual` column). Also now actually filters on the flag's value
// (=eq.true) instead of returning every row that exists for the date
// regardless of its value, which the original version did.
function _manualColumn(lang) {
  return lang === "hi" ? "manual_hi" : "manual_en";
}

async function fetchManualIndices(dateStr, lang) {
  const column = _manualColumn(lang);
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/slot_overrides?slot_date=eq.${dateStr}&${column}=eq.true&select=slot_index`;
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

async function markSlotManual(dateStr, slotIndex, lang) {
  const column = _manualColumn(lang);
  const url = `${CONFIG.SUPABASE_URL}/rest/v1/slot_overrides`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      apikey: CONFIG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates",
    },
    body: JSON.stringify({ slot_date: dateStr, slot_index: slotIndex, [column]: true }),
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

// Fires the generate-slot Edge Function, which dispatches
// generate-slot.yml on GitHub to force-build ONE slot's content right
// now instead of waiting for the rolling 30-min-ahead build. Only
// builds content - the slot still posts at its normal fixed time.
async function triggerGenerateSlot(dateStr, slotIndex) {
  try {
    const resp = await fetch(`${CONFIG.SUPABASE_URL}/functions/v1/generate-slot`, {
      method: "POST",
      headers: {
        apikey: CONFIG.SUPABASE_ANON_KEY,
        Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ slot_date: dateStr, slot_index: slotIndex }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.ok) {
      return { ok: false, error: (data && data.error) || `couldn't start (${resp.status})` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: "check your connection" };
  }
}

// Fires the post-slot-now Edge Function, which dispatches post-now.yml
// on GitHub to publish ONE specific slot/language immediately -
// ignoring its planned_time and the MIN_GAP_MINUTES cooldown. This is
// the "Post now" button: both a manual override for a slot you just
// want out right away, and the rescue path for one that's overdue
// because an earlier auto-post attempt failed - instead of waiting for
// the next scheduler.yml tick to retry it, this fires an attempt now.
async function triggerPostNow(dateStr, slotIndex, lang) {
  try {
    const resp = await fetch(`${CONFIG.SUPABASE_URL}/functions/v1/post-slot-now`, {
      method: "POST",
      headers: {
        apikey: CONFIG.SUPABASE_ANON_KEY,
        Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ slot_date: dateStr, slot_index: slotIndex, lang }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.ok) {
      return { ok: false, error: (data && data.error) || `couldn't start (${resp.status})` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: "check your connection" };
  }
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

// ---- Home screen / navigation ----

let currentAccountLang = null; // null = on the home screen
let lastBackPressAt = 0;

// Remembers which screen (home, or a specific account's schedule) was
// showing so a page refresh/reopen lands back there instead of always
// resetting to the account picker.
const ACTIVE_ACCOUNT_KEY = "infact_active_account";

function renderAccountList() {
  const list = document.getElementById("accountList");
  list.innerHTML = "";
  ACCOUNTS.forEach((acc) => {
    const row = document.createElement("div");
    row.className = "account-row";
    row.dataset.lang = acc.lang;
    row.innerHTML = `
      <img class="account-logo" src="${acc.logo}" alt="">
      <div class="account-main">
        <div class="account-handle">${escapeHtml(acc.handle)}</div>
        <div class="account-label">${acc.label}</div>
      </div>
      <span class="account-arrow">&rsaquo;</span>
    `;
    row.addEventListener("click", () => selectAccount(acc.lang));
    list.appendChild(row);
  });
}

function updateAccountHeader(lang) {
  const acc = accountFor(lang);
  const logo = document.getElementById("headerLogo");
  const eyebrow = document.getElementById("accountEyebrow");
  logo.src = acc.logo;
  logo.hidden = false;
  eyebrow.textContent = acc.handle.toUpperCase();
}

function selectAccount(lang) {
  currentAccountLang = lang;
  viewedDayOffset = 0; // always land on today's schedule when opening/switching an account
  document.getElementById("homeScreen").hidden = true;
  document.getElementById("app").hidden = false;
  updateAccountHeader(lang);
  history.pushState({ screen: "schedule", lang }, "");
  localStorage.setItem(ACTIVE_ACCOUNT_KEY, lang);
  refresh();
}

function showHomeScreen() {
  currentAccountLang = null;
  document.getElementById("app").hidden = true;
  document.getElementById("homeScreen").hidden = false;
  localStorage.removeItem(ACTIVE_ACCOUNT_KEY);
  refreshHomeScreenBadge();
}

// Figures out, across both accounts, which one's next NOT-YET-POSTED
// slot is soonest, and drops a "NEXT UP" badge on that account's row -
// so from the picker alone you can see which account needs attention
// first without opening it.
function nextUpcomingTime(data, lang) {
  const now = nowIST();
  let soonest = null;
  (data.slots || []).forEach((s) => {
    const posted = lang === "hi" ? s.posted_hi : s.posted;
    if (posted) return;
    const t = plannedTimeOf(s, lang);
    if (t > now && (!soonest || t < soonest)) soonest = t;
  });
  return soonest;
}

function markNextUpAccount(data) {
  const withTimes = ACCOUNTS
    .map((acc) => ({ lang: acc.lang, time: nextUpcomingTime(data, acc.lang) }))
    .filter((t) => t.time);
  withTimes.sort((a, b) => a.time - b.time);
  const nextLang = withTimes.length ? withTimes[0].lang : null;

  document.querySelectorAll(".account-row").forEach((row) => {
    const isNext = row.dataset.lang === nextLang;
    row.classList.toggle("account-row-next", isNext);
    const existingBadge = row.querySelector(".account-next-badge");
    if (existingBadge) existingBadge.remove();
    if (isNext) {
      const badge = document.createElement("span");
      badge.className = "account-next-badge";
      badge.textContent = "NEXT UP";
      row.querySelector(".account-main").appendChild(badge);
    }
  });
}

async function refreshHomeScreenBadge() {
  try {
    const data = await fetchDailySlots();
    markNextUpAccount(data);
  } catch (e) {
    console.error(e);
  }
}

function showExitToast() {
  const toast = document.getElementById("exitToast");
  toast.hidden = false;
  setTimeout(() => {
    toast.hidden = true;
  }, 1800);
}

document.getElementById("backBtn").addEventListener("click", () => history.back());

window.addEventListener("popstate", () => {
  if (currentAccountLang !== null) {
    // Was viewing a schedule - this pop takes us back to the home screen.
    showHomeScreen();
    return;
  }
  // Already on the home screen and got a back press with nothing above
  // it in our own stack - double-back-to-exit, not a real navigation.
  const now = Date.now();
  if (now - lastBackPressAt < 2000) {
    return; // second press within the window - let the browser actually exit/navigate away
  }
  lastBackPressAt = now;
  showExitToast();
  history.pushState({ screen: "home" }, ""); // re-arm so the next back press is caught here too
});

// ---- Rendering ----

let currentSlots = [];
let currentManualIndices = new Set();
let currentDateStr = null;

// Which day of the DAYS_KEPT-day rolling window is currently shown -
// 0 = today, 1 = yesterday, etc. dayDataCache holds whatever
// fetchRecentDaysSlots() last returned, in the same order, so swiping
// between already-fetched days is instant (no re-fetch, just re-render
// from cache) - see goToDay().
let viewedDayOffset = 0;
let dayDataCache = [];

function isViewingHistorical() {
  // Based on the actual rendered date, not viewedDayOffset - covers
  // every render() call path (swipe, poll refresh, post-action
  // re-render after marking a slot manual, etc.) with one check.
  return !!currentDateStr && currentDateStr !== todayStrIST();
}

// slot.index values with a "Generate now" build currently in flight (this
// browser tab only - just drives the "Generating..." UI state, the real
// source of truth is whether the slot has content in Supabase).
const generatingSlotIndices = new Set();

// Same idea as generatingSlotIndices, but for a "Post now" publish
// currently in flight - drives the "Posting..." UI state while
// post-now.yml runs on GitHub. Real source of truth is still the
// posted/posted_hi flag from Supabase.
const postingSlotIndices = new Set();

// Status now reflects the REAL posted/posted_hi flag from the backend
// (daily_scheduler.py writes this the moment a post is CONFIRMED to
// have gone out - see _mark_slot_posted_in_skeleton), not just whether
// the clock has passed the planned time. A slot whose time has passed
// but that isn't actually confirmed posted (failed publish, still
// retrying) now shows as "overdue" instead of being falsely marked
// "past"/posted.
function statusOf(slot, allSorted, index, lang) {
  const now = nowIST();
  const planned = plannedTimeOf(slot, lang);
  const isPosted = lang === "hi" ? slot.posted_hi : slot.posted;
  if (isPosted) return "past";
  if (planned <= now) return "overdue";
  const nextUpcoming = allSorted.find((s) => {
    const sPosted = lang === "hi" ? s.posted_hi : s.posted;
    return !sPosted && plannedTimeOf(s, lang) > now;
  });
  if (nextUpcoming && nextUpcoming.index === slot.index) return "next";
  return "pending";
}

function render(data, manualIndices) {
  const lang = currentAccountLang || "en";
  currentDateStr = data.date;
  currentManualIndices = manualIndices || new Set();
  document.getElementById("dateLabel").textContent = data.date
    ? fmtDate(new Date(data.date))
    : "No schedule yet";

  const list = document.getElementById("list");
  const empty = document.getElementById("emptyState");
  // Sort by scheduled time, not by static creation index - a slot's
  // position (and its displayed 1/2/3... number, which comes from array
  // position, not slot.index) should always follow its planned time, so
  // adding more posts or editing a time re-orders the list instead of
  // just appending new slots to the bottom regardless of when they fire.
  const slots = (data.slots || []).slice().sort(
    (a, b) => plannedTimeOf(a, lang) - plannedTimeOf(b, lang)
  );
  currentSlots = slots;

  if (!slots.length) {
    empty.hidden = false;
    list.querySelectorAll(".slot-row").forEach((el) => el.remove());
    updateProgress(0, 0);
    return;
  }
  empty.hidden = true;

  const now = nowIST();
  const pastCount = slots.filter((s) => (lang === "hi" ? s.posted_hi : s.posted)).length;
  updateProgress(pastCount, slots.length);

  list.querySelectorAll(".slot-row").forEach((el) => el.remove());

  slots.forEach((slot, i) => {
    const planned = plannedTimeOf(slot, lang);
    const status = statusOf(slot, slots, i, lang);
    const isManual = currentManualIndices.has(slot.index);
    const row = document.createElement("div");
    row.className = `slot-row ${status}${isManual ? " manual" : ""}`;
    const topStory = (slot.stories && slot.stories[0]) || null;
    const otherLangHasContent = lang === "hi"
      ? (slot.image_urls || []).length > 0
      : (slot.image_urls_hi || []).length > 0;
    const otherLangBadge = lang === "hi" ? "EN" : "HI";

    row.innerHTML = `
      <span class="slot-num">${String(i + 1).padStart(2, "0")}</span>
      <span class="slot-dot ${status === "past" ? "posted" : status}"></span>
      <div class="slot-main">
        <div class="slot-time">${fmtTime(planned)}${isManual ? ' <span class="manual-tag">MANUAL</span>' : ""}</div>
        <div class="slot-headline">${topStory ? escapeHtml(topStory.title) : "Pending"}</div>
      </div>
      <span class="slot-meta">${(slot.stories || []).length}&middot;${(lang === "hi" ? (slot.image_urls_hi || []) : (slot.image_urls || [])).length}${otherLangHasContent ? ` <span class="hi-badge">${otherLangBadge}</span>` : ""}</span>
    `;
    row.addEventListener("click", () => openModal(slot));

    // No content built for this slot yet (and it hasn't already posted) -
    // offer to build it now instead of waiting for the rolling 30-min
    // pre-build window to reach it. Tapping this never opens the modal.
    // Never offered on a historical (non-today) day - "generate now"
    // only makes sense for a slot that hasn't fired yet.
    if (!topStory && status !== "past" && !isViewingHistorical()) {
      const isGenerating = generatingSlotIndices.has(slot.index);
      const genBtn = document.createElement("button");
      genBtn.type = "button";
      genBtn.className = "slot-generate-btn";
      genBtn.textContent = isGenerating ? "Generating..." : "Generate";
      genBtn.disabled = isGenerating;
      genBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        generateSlotNow(slot);
      });
      row.appendChild(genBtn);
    }

    // Content's built (or the slot's overdue/due) but not posted yet -
    // offer to publish it right now instead of waiting for the normal
    // scheduled time / the next 30-min scheduler tick to retry it.
    // Never offered for a slot flagged Manual (you said you'd post it
    // yourself) or on a historical day.
    if (topStory && status !== "past" && !isManual && !isViewingHistorical()) {
      const isPosting = postingSlotIndices.has(slot.index);
      const postBtn = document.createElement("button");
      postBtn.type = "button";
      postBtn.className = "slot-postnow-btn";
      postBtn.textContent = isPosting ? "Posting..." : "Post now";
      postBtn.disabled = isPosting;
      postBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        postSlotNow(slot, lang);
      });
      row.appendChild(postBtn);
    }

    list.appendChild(row);
  });
}

// ---- Generate now (force-build a not-yet-built slot's content) ----

// Shows/hides the modal's "Generate content now" block for whichever
// slot the modal currently has open, and (re)wires its click handler.
function updateGenerateBlock(slot) {
  const block = document.getElementById("generateBlock");
  const btn = document.getElementById("generateBtn");
  const status = document.getElementById("generateStatus");
  const hasContent = (slot.stories || []).length > 0;
  const isPast = slotStatus(slot) === "past";

  if (hasContent || isPast || isViewingHistorical()) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const isGenerating = generatingSlotIndices.has(slot.index);
  btn.disabled = isGenerating;
  btn.textContent = isGenerating ? "Generating..." : "Generate content now";
  // Only clear the status line when nothing has been said yet (e.g. the
  // modal just opened). Don't wipe an error message that generateSlotNow
  // just set on a failed start - that was clobbering "Couldn't start: ..."
  // with an empty string the instant the button reverted, so the error
  // never had a chance to be seen.
  if (!isGenerating && !status.dataset.errorShown) status.textContent = "";
  btn.onclick = () => generateSlotNow(slot, status);
}

// Kicks off a manual build for one slot, then polls every 15s (up to
// ~5 min) until the slot actually has content, refreshing the list and
// (if still open) the modal as soon as it lands. This only builds
// content - it never posts; the slot still fires at its normal fixed
// time via the regular scheduler.
async function generateSlotNow(slot, statusEl) {
  if (!currentDateStr || generatingSlotIndices.has(slot.index)) return;

  generatingSlotIndices.add(slot.index);
  if (statusEl) {
    statusEl.textContent = "Starting...";
    delete statusEl.dataset.errorShown;
  }
  render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
  if (currentModalSlot && currentModalSlot.index === slot.index) updateGenerateBlock(currentModalSlot);

  const result = await triggerGenerateSlot(currentDateStr, slot.index);
  if (!result.ok) {
    generatingSlotIndices.delete(slot.index);
    if (statusEl) {
      statusEl.textContent = `Couldn't start: ${result.error}`;
      statusEl.dataset.errorShown = "1";
    }
    render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    if (currentModalSlot && currentModalSlot.index === slot.index) updateGenerateBlock(currentModalSlot);
    return;
  }

  if (statusEl) statusEl.textContent = "Generating - takes a minute or two, checking automatically...";
  pollForSlotContent(slot.index);
}

function pollForSlotContent(slotIndex, attemptsLeft = 20) {
  if (!generatingSlotIndices.has(slotIndex)) return; // cancelled/superseded elsewhere
  if (attemptsLeft <= 0) {
    generatingSlotIndices.delete(slotIndex);
    render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    if (currentModalSlot && currentModalSlot.index === slotIndex) {
      updateGenerateBlock(currentModalSlot);
      document.getElementById("generateStatus").textContent =
        "Still building - this is taking longer than usual. It'll appear here once it's ready.";
    }
    return;
  }
  setTimeout(async () => {
    await refresh(); // re-fetches and calls render() itself
    const slot = currentSlots.find((s) => s.index === slotIndex);
    const built = slot && (slot.stories || []).length > 0;
    if (built) {
      generatingSlotIndices.delete(slotIndex);
      render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
      if (currentModalSlot && currentModalSlot.index === slotIndex) {
        Object.assign(currentModalSlot, slot);
        renderModalLang(currentModalLang);
        populateStoryList(currentModalSlot);
        updateGenerateBlock(currentModalSlot);
      }
      return;
    }
    pollForSlotContent(slotIndex, attemptsLeft - 1);
  }, 15000);
}

// ---- Post now (force-publish a not-yet-posted slot immediately) ----

// Shows/hides the modal's "Post now" block for whichever slot the
// modal currently has open, and (re)wires its click handler. Belongs
// to whichever language tab is currently open in the modal (unlike the
// manual-takeover button, which always belongs to the account you
// opened the modal from) - posting is a per-language action.
function updatePostNowBlock(slot) {
  const block = document.getElementById("postNowBlock");
  const btn = document.getElementById("postNowBtn");
  const status = document.getElementById("postNowStatus");
  const lang = currentModalLang;
  const hasContent = (lang === "hi" ? (slot.image_urls_hi || []) : (slot.image_urls || [])).length > 0;
  const isPosted = lang === "hi" ? slot.posted_hi : slot.posted;
  const isManual = currentManualIndices.has(slot.index);

  if (!hasContent || isPosted || isManual || isViewingHistorical()) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  const isPosting = postingSlotIndices.has(slot.index);
  btn.disabled = isPosting;
  btn.textContent = isPosting ? "Posting..." : "Post now";
  if (!isPosting && !status.dataset.errorShown) status.textContent = "";
  btn.onclick = () => postSlotNow(slot, lang, status);
}

// Kicks off an immediate publish for one slot/language, then polls
// every 15s (up to ~5 min) until the posted/posted_hi flag actually
// flips, refreshing the list and (if still open) the modal as soon as
// it does. Ignores the slot's planned_time and the normal
// MIN_GAP_MINUTES cooldown - see post_now() in daily_scheduler.py.
async function postSlotNow(slot, lang, statusEl) {
  if (!currentDateStr || postingSlotIndices.has(slot.index)) return;

  postingSlotIndices.add(slot.index);
  if (statusEl) {
    statusEl.textContent = "Starting...";
    delete statusEl.dataset.errorShown;
  }
  render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
  if (currentModalSlot && currentModalSlot.index === slot.index) updatePostNowBlock(currentModalSlot);

  const result = await triggerPostNow(currentDateStr, slot.index, lang);
  if (!result.ok) {
    postingSlotIndices.delete(slot.index);
    if (statusEl) {
      statusEl.textContent = `Couldn't start: ${result.error}`;
      statusEl.dataset.errorShown = "1";
    }
    render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    if (currentModalSlot && currentModalSlot.index === slot.index) updatePostNowBlock(currentModalSlot);
    return;
  }

  if (statusEl) statusEl.textContent = "Posting - takes a minute or two, checking automatically...";
  pollForSlotPosted(slot.index, lang);
}

function pollForSlotPosted(slotIndex, lang, attemptsLeft = 20) {
  if (!postingSlotIndices.has(slotIndex)) return; // cancelled/superseded elsewhere
  if (attemptsLeft <= 0) {
    postingSlotIndices.delete(slotIndex);
    render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    if (currentModalSlot && currentModalSlot.index === slotIndex) {
      updatePostNowBlock(currentModalSlot);
      document.getElementById("postNowStatus").textContent =
        "Still working - this is taking longer than usual. It'll flip to posted here once it lands.";
    }
    return;
  }
  setTimeout(async () => {
    await refresh(); // re-fetches and calls render() itself
    const slot = currentSlots.find((s) => s.index === slotIndex);
    const posted = slot && (lang === "hi" ? slot.posted_hi : slot.posted);
    if (posted) {
      postingSlotIndices.delete(slotIndex);
      render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
      if (currentModalSlot && currentModalSlot.index === slotIndex) {
        Object.assign(currentModalSlot, slot);
        renderModalLang(currentModalLang);
        populateStoryList(currentModalSlot);
        updatePostNowBlock(currentModalSlot);
      }
      return;
    }
    pollForSlotPosted(slotIndex, lang, attemptsLeft - 1);
  }, 15000);
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
  currentModalLang = currentAccountLang || "en"; // default to whichever account you opened this from
  const modal = document.getElementById("modal");
  const planned = plannedTimeOf(slot, currentModalLang);
  document.getElementById("modalTime").textContent = fmtTime(planned) + " IST";

  const hasHindi = (slot.image_urls_hi || []).length > 0;
  const langTabs = document.getElementById("langTabs");
  langTabs.hidden = !hasHindi;
  document.getElementById("langTabEn").classList.toggle("active", currentModalLang === "en");
  document.getElementById("langTabHi").classList.toggle("active", currentModalLang === "hi");

  renderModalLang(currentModalLang);
  populateStoryList(slot);
  delete document.getElementById("generateStatus").dataset.errorShown;
  updateGenerateBlock(slot);
  delete document.getElementById("postNowStatus").dataset.errorShown;
  updatePostNowBlock(slot);

  const reviewBtn = document.getElementById("reviewBtn");
  const hasCandidates = (slot.candidates || []).length > 0;
  const alreadyPast = slotStatus(slot) === "past";
  // Reviewing/reordering only makes sense for a slot that hasn't
  // posted yet - never offered when looking back at a past day.
  reviewBtn.hidden = !hasCandidates || isViewingHistorical();
  reviewBtn.disabled = alreadyPast;
  reviewBtn.textContent = alreadyPast
    ? "Already posted \u2014 can't review"
    : `Review & reorder stories (${(slot.candidates || []).length} candidates)`;
  reviewBtn.onclick = () => openReviewModal(slot);

  const manualBtn = document.getElementById("manualBtn");
  const manualStatus = document.getElementById("manualStatus");
  // The manual flag/button always belongs to the ACCOUNT you opened this
  // modal from (currentAccountLang), not whichever language tab you're
  // currently previewing - "take over" means "I'll post this account's
  // version myself," regardless of which tab you're glancing at.
  const takeoverLang = currentAccountLang || "en";
  const alreadyManual = currentManualIndices.has(slot.index);
  // "Take over and post manually" is a same-day action - hide it
  // entirely on a historical day rather than let someone tap it for a
  // slot that's already long past.
  manualBtn.hidden = isViewingHistorical();
  manualStatus.hidden = isViewingHistorical();
  manualBtn.disabled = alreadyManual;
  manualBtn.textContent = alreadyManual
    ? "Taken over \u2014 auto-post skipped for this one"
    : "Take over \u2014 post this one manually";
  manualStatus.textContent = alreadyManual
    ? "Download the slides above and post it yourself; the app will pick it up once it's live."
    : "";
  manualBtn.onclick = async () => {
    if (!currentDateStr || isViewingHistorical()) return;
    manualBtn.disabled = true;
    manualBtn.textContent = "Marking...";
    const ok = await markSlotManual(currentDateStr, slot.index, takeoverLang);
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

// Fills the slides scroller, caption, and share/download button for
// whichever language tab is active (slot's English fields have no
// suffix; Hindi fields are the same shape with an "_hi" suffix - see
// content_pregen.py / hourly_run.run_combined). Falls back to "no
// Hindi content" copy if a slot with the tab visible somehow has no
// Hindi images (e.g. every story in that slot failed translation -
// see hourly_run._build_hindi_slides).
function renderModalLang(lang) {
  currentModalLang = lang;
  const slot = currentModalSlot;
  if (!slot) return;

  const imageUrls = lang === "hi" ? (slot.image_urls_hi || []) : (slot.image_urls || []);
  const caption = lang === "hi" ? (slot.caption_hi || "") : (slot.caption || "");

  document.getElementById("modalSub").textContent =
    `${(slot.stories || []).length} stories \u00b7 ${imageUrls.length} slides`;

  const scroller = document.getElementById("slidesScroller");
  scroller.innerHTML = "";
  const shareBtn = document.getElementById("downloadAllBtn");

  if (!imageUrls.length && lang === "hi") {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "Hindi translation didn't come through for this slot's stories - only the English post will go out.";
    scroller.appendChild(p);
    shareBtn.hidden = true;
  } else {
    imageUrls.forEach((url) => {
      const img = document.createElement("img");
      img.src = url;
      img.loading = "lazy";
      scroller.appendChild(img);
    });
    shareBtn.hidden = false;
    shareBtn.disabled = false;
    shareBtn.textContent = shareSupportsFiles() ? "Share all slides" : "Download all slides";
  }

  document.getElementById("captionText").textContent = caption;

  document.getElementById("downloadStatus").textContent = "";
  shareBtn.onclick = () => shareOrDownloadSlides(imageUrls, lang, caption);
  document.getElementById("copyBtn").onclick = () => copyCaption(caption);
  document.getElementById("copyBtn").classList.remove("copied");
  document.getElementById("copyBtn").textContent = "Copy";

  // Post now belongs to whichever language tab is now active, so it
  // has to be recomputed every time the tab (and thus `lang`) changes.
  updatePostNowBlock(slot);
}

function populateStoryList(slot) {
  const storyList = document.getElementById("storyList");
  storyList.innerHTML = "";
  (slot.stories || []).forEach((s) => {
    const li = document.createElement("li");
    if (s.is_sensitive) li.className = "sensitive";
    li.textContent = `${s.title} — ${s.source || ""}`;
    storyList.appendChild(li);
  });
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  currentModalSlot = null;
}

// A slot isn't stored with its own status - it's derived from the current
// clock + its position among today's slots (see statusOf), same as render()
// uses for the list rows. Recomputed on demand for whichever slot the
// review/manual buttons are currently looking at.
function slotStatus(slot) {
  const lang = currentAccountLang || "en";
  const idx = currentSlots.indexOf(slot);
  return statusOf(slot, currentSlots, idx, lang);
}

// ---- Review modal (pick + reorder which candidate stories actually post) ----
//
// A slot is built with CANDIDATE_STORY_COUNT candidates (see
// hourly_run.build_candidates / content_pregen.py) but only
// STORIES_PER_POST of them go out. Until this is reviewed, the top
// STORIES_PER_POST by priority are what's selected (slot.selected_story_ids),
// which is exactly what would've posted before this feature existed.
// Saving here calls the save-slot-selection Edge Function, which
// recomputes slot.image_urls/caption/stories from the chosen subset+order
// and writes it back - see that function for details.

let reviewSlot = null;
let reviewSelected = [];   // ordered candidate ids that will post
let reviewUnselected = []; // remaining candidate ids, original priority order

function openReviewModal(slot) {
  reviewSlot = slot;
  const candidateIds = (slot.candidates || []).map((c) => c.id);
  const chosen = (slot.selected_story_ids || []).filter((id) => candidateIds.includes(id));
  reviewSelected = (chosen.length ? chosen : candidateIds).slice(0, MAX_SELECTED_STORIES);
  reviewUnselected = candidateIds.filter((id) => !reviewSelected.includes(id));
  document.getElementById("reviewSaveStatus").textContent = "";
  document.getElementById("reviewModal").hidden = false;
  renderReviewModal();
}

function closeReviewModal() {
  document.getElementById("reviewModal").hidden = true;
  reviewSlot = null;
}

function reviewCandidateById(id) {
  return (reviewSlot.candidates || []).find((c) => c.id === id);
}

function reviewRowEl(c, actionsHtml, extraClass, draggable) {
  const thumb = (c.image_urls && c.image_urls[0]) || "";
  const handleHtml = draggable
    ? '<span class="review-drag-handle" aria-label="Drag to reorder">&#8942;&#8942;</span>'
    : "";
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <div class="review-row${extraClass ? " " + extraClass : ""}" data-id="${c.id}">
      ${handleHtml}
      ${thumb ? `<img class="review-thumb" src="${thumb}" loading="lazy">` : '<div class="review-thumb review-thumb-empty"></div>'}
      <div class="review-main">
        <div class="review-title${c.is_sensitive ? " sensitive" : ""}">${escapeHtml(c.title)}</div>
        <div class="review-source">${escapeHtml(c.source || "")}${c.priority_rank ? ` &middot; #${c.priority_rank}` : ""}</div>
      </div>
      <div class="review-actions">${actionsHtml}</div>
    </div>
  `;
  const rowEl = wrap.firstElementChild;
  // Tap anywhere on the row except the action buttons / drag handle to
  // preview this story's own image(s) - hook + description if it has
  // two, just the hook if it only has one.
  rowEl.addEventListener("click", (e) => {
    if (e.target.closest(".review-actions") || e.target.closest(".review-drag-handle")) return;
    openStoryPreview(c);
  });
  return rowEl;
}

// ---- Story preview (tap a review row to see its hook/description image(s)) ----

function openStoryPreview(c) {
  document.getElementById("storyPreviewTitle").textContent = c.title || "Story";
  document.getElementById("storyPreviewSource").textContent = c.source || "";
  const scroller = document.getElementById("storyPreviewScroller");
  scroller.innerHTML = "";
  const urls = c.image_urls || [];
  if (!urls.length) {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "No preview image available for this story yet.";
    scroller.appendChild(p);
  } else {
    urls.forEach((url) => {
      const img = document.createElement("img");
      img.src = url;
      img.loading = "lazy";
      scroller.appendChild(img);
    });
  }
  document.getElementById("storyPreviewModal").hidden = false;
}

function closeStoryPreview() {
  document.getElementById("storyPreviewModal").hidden = true;
}

// ---- Drag to reorder ("Going out" list in the review modal) ----
//
// Pointer-events based (not native HTML5 drag-and-drop, which doesn't
// work reliably on touch) so it works with both mouse and touch. Only
// the drag handle initiates a drag; tapping the rest of the row still
// opens the preview, and the up/down/remove buttons still work as a
// fallback/for accessibility.

function findSelectedRowEl(id) {
  return Array.from(document.querySelectorAll("#reviewSelectedList .review-row"))
    .find((r) => r.dataset.id === String(id));
}

function attachDragHandle(handleEl, id) {
  if (!handleEl) return;
  handleEl.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    let baseY = e.clientY;
    document.body.style.userSelect = "none";
    const startEl = findSelectedRowEl(id);
    if (startEl) startEl.classList.add("dragging");

    function onMove(ev) {
      const el = findSelectedRowEl(id);
      if (!el) return;
      const dy = ev.clientY - baseY;
      el.style.transform = `translateY(${dy}px)`;

      const list = document.getElementById("reviewSelectedList");
      const siblings = Array.from(list.querySelectorAll(".review-row")).filter((r) => r !== el);
      const draggedRect = el.getBoundingClientRect();
      const draggedMid = draggedRect.top + draggedRect.height / 2;

      for (const sib of siblings) {
        const sibRect = sib.getBoundingClientRect();
        const sibMid = sibRect.top + sibRect.height / 2;
        const fromIdx = reviewSelected.indexOf(id);
        const toIdx = reviewSelected.findIndex((x) => String(x) === sib.dataset.id);
        if (fromIdx === -1 || toIdx === -1) continue;
        const movingDown = fromIdx < toIdx;
        if ((movingDown && draggedMid > sibMid) || (!movingDown && draggedMid < sibMid)) {
          reviewSelected.splice(fromIdx, 1);
          reviewSelected.splice(toIdx, 0, id);
          renderReviewModal();
          const newEl = findSelectedRowEl(id);
          if (newEl) {
            newEl.classList.add("dragging");
            newEl.style.transform = "translateY(0px)";
          }
          baseY = ev.clientY; // re-baseline so the row keeps tracking the pointer from its new slot
          break;
        }
      }
    }

    function onUp() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.body.style.userSelect = "";
      const el = findSelectedRowEl(id);
      if (el) {
        el.classList.remove("dragging");
        el.style.transform = "";
      }
      renderReviewModal();
    }

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  });
}

function renderReviewModal() {
  document.getElementById("reviewModalSub").textContent =
    `${reviewSelected.length}/${MAX_SELECTED_STORIES} selected`;

  const selectedList = document.getElementById("reviewSelectedList");
  selectedList.innerHTML = "";
  if (!reviewSelected.length) {
    selectedList.innerHTML = '<p class="field-hint">Nothing selected yet - add at least one story below.</p>';
  }
  reviewSelected.forEach((id, i) => {
    const c = reviewCandidateById(id);
    if (!c) return;
    const upDisabled = i === 0 ? "disabled" : "";
    const downDisabled = i === reviewSelected.length - 1 ? "disabled" : "";
    const actions = `
      <button class="review-icon-btn" data-action="up" ${upDisabled} aria-label="Move up">&uarr;</button>
      <button class="review-icon-btn" data-action="down" ${downDisabled} aria-label="Move down">&darr;</button>
      <button class="review-icon-btn review-remove" data-action="remove" aria-label="Remove">&times;</button>
    `;
    const el = reviewRowEl(c, actions, "", true);
    el.querySelector('[data-action="up"]').onclick = () => moveReviewSelected(id, -1);
    el.querySelector('[data-action="down"]').onclick = () => moveReviewSelected(id, 1);
    el.querySelector('[data-action="remove"]').onclick = () => deselectReviewCandidate(id);
    selectedList.appendChild(el);
    attachDragHandle(el.querySelector(".review-drag-handle"), id);
  });

  const unselectedList = document.getElementById("reviewUnselectedList");
  unselectedList.innerHTML = "";
  if (!reviewUnselected.length) {
    unselectedList.innerHTML = '<p class="field-hint">No other candidates left.</p>';
  }
  const atMax = reviewSelected.length >= MAX_SELECTED_STORIES;
  reviewUnselected.forEach((id) => {
    const c = reviewCandidateById(id);
    if (!c) return;
    const actions = `<button class="review-icon-btn review-add" data-action="add" ${atMax ? "disabled" : ""} aria-label="Add">&plus;</button>`;
    const el = reviewRowEl(c, actions, "unselected");
    const addBtn = el.querySelector('[data-action="add"]');
    if (addBtn) addBtn.onclick = () => selectReviewCandidate(id);
    unselectedList.appendChild(el);
  });

  document.getElementById("saveReviewBtn").disabled = !reviewSelected.length;
}

function selectReviewCandidate(id) {
  if (reviewSelected.includes(id) || reviewSelected.length >= MAX_SELECTED_STORIES) return;
  reviewSelected.push(id);
  reviewUnselected = reviewUnselected.filter((x) => x !== id);
  renderReviewModal();
}

function deselectReviewCandidate(id) {
  reviewSelected = reviewSelected.filter((x) => x !== id);
  const candidateIds = (reviewSlot.candidates || []).map((c) => c.id);
  reviewUnselected.push(id);
  reviewUnselected.sort((a, b) => candidateIds.indexOf(a) - candidateIds.indexOf(b));
  renderReviewModal();
}

function moveReviewSelected(id, delta) {
  const i = reviewSelected.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= reviewSelected.length) return;
  [reviewSelected[i], reviewSelected[j]] = [reviewSelected[j], reviewSelected[i]];
  renderReviewModal();
}

async function saveSlotSelection(dateStr, slotIndex, selectedIds) {
  try {
    const resp = await fetch(`${CONFIG.SUPABASE_URL}/functions/v1/save-slot-selection`, {
      method: "POST",
      headers: {
        apikey: CONFIG.SUPABASE_ANON_KEY,
        Authorization: `Bearer ${CONFIG.SUPABASE_ANON_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ slot_date: dateStr, slot_index: slotIndex, selected_story_ids: selectedIds }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.ok) {
      return { ok: false, error: (data && data.error) || `save failed (${resp.status})` };
    }
    return { ok: true, slot: data.slot };
  } catch (e) {
    return { ok: false, error: "check your connection" };
  }
}

async function saveReviewSelection() {
  if (!reviewSlot || !currentDateStr || !reviewSelected.length) return;
  const saveBtn = document.getElementById("saveReviewBtn");
  const status = document.getElementById("reviewSaveStatus");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving...";
  status.textContent = "";

  const result = await saveSlotSelection(currentDateStr, reviewSlot.index, reviewSelected);
  if (result.ok) {
    // Merge the recomputed fields straight into the in-memory slot so the
    // list row and the (still-open) slot modal reflect the new selection
    // without a full refetch.
    Object.assign(reviewSlot, result.slot);
    if (currentModalSlot && currentModalSlot.index === reviewSlot.index) {
      Object.assign(currentModalSlot, result.slot);
      renderModalLang(currentModalLang);
      populateStoryList(currentModalSlot);
    }
    render({ date: currentDateStr, slots: currentSlots }, currentManualIndices);
    status.textContent = "Saved.";
    saveBtn.textContent = "Save selection";
    saveBtn.disabled = false;
    setTimeout(closeReviewModal, 900);
  } else {
    status.textContent = `Couldn't save: ${result.error}`;
    saveBtn.textContent = "Save selection";
    saveBtn.disabled = false;
  }
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
  const lang = currentAccountLang || "en";
  const postedSlots = currentSlots.filter((s) => (lang === "hi" ? s.posted_hi : s.posted));
  const pendingSlots = currentSlots.filter((s) => !(lang === "hi" ? s.posted_hi : s.posted));

  document.getElementById("scheduleModalSub").textContent =
    `${postedSlots.length} posted \u00b7 ${pendingSlots.length} remaining`;

  const countInput = document.getElementById("postsCountInput");
  countInput.min = Math.max(1, postedSlots.length);
  const currentTotal = pendingOverride.target_count || currentSlots.length;
  countInput.value = currentTotal;
  document.getElementById("postsCountHint").textContent =
    `Already-posted slots (${postedSlots.length}) can't be removed, so the lowest you can go is ${countInput.min}. Max 25/day. This only edits the ${accountFor(lang).handle} schedule.`;

  const list = document.getElementById("pendingTimesList");
  list.innerHTML = "";
  if (!pendingSlots.length) {
    const p = document.createElement("p");
    p.className = "field-hint";
    p.textContent = "Nothing left to schedule today.";
    list.appendChild(p);
    return;
  }
  pendingSlots.forEach((slot, i) => {
    const planned = plannedTimeOf(slot, lang);
    const row = document.createElement("div");
    row.className = "pending-time-row";
    row.innerHTML = `
      <span class="slot-num">${String(i + 1).padStart(2, "0")}</span>
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
  const lang = currentAccountLang || "en";
  const saveBtn = document.getElementById("saveScheduleBtn");
  const status = document.getElementById("scheduleSaveStatus");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving...";
  status.textContent = "";

  const targetCount = parseInt(document.getElementById("postsCountInput").value, 10);

  // time_edits is keyed by slot index -> {"en": "HH:MM", "hi": "HH:MM"}
  // (see daily_scheduler.py's _apply_time_edits). Only touch this
  // account's own language key per index, preserving whatever's already
  // there for the OTHER language on the same slot.
  const timeEdits = {};
  Object.entries(pendingOverride.time_edits || {}).forEach(([idx, val]) => {
    timeEdits[idx] = typeof val === "object" && val !== null ? { ...val } : { en: val };
  });
  document.querySelectorAll("#pendingTimesList input[type='time']").forEach((input) => {
    if (!input.value) return;
    const idx = input.dataset.index;
    timeEdits[idx] = { ...(timeEdits[idx] || {}), [lang]: input.value };
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

// Feature-checks Web Share API Level 2 (file sharing) support. Only
// checked with a dummy JPEG File, since navigator.share exists on more
// browsers than actually accept `files` (e.g. some desktop browsers
// have share() for text/url only) - canShare({files}) is the real test.
function shareSupportsFiles() {
  if (!navigator.canShare) return false;
  const probe = new File([""], "probe.jpg", { type: "image/jpeg" });
  return navigator.canShare({ files: [probe] });
}

// Primary path: hand every slide straight to the OS share sheet in one
// go, so the user taps Instagram (or whatever) and posts - no copy
// ever touches their Gallery, so there's nothing to clean up
// afterward. Falls back to the old save-each-file-to-Downloads flow on
// browsers without file-sharing support (desktop, some older mobile
// browsers) or if the user cancels/it fails for another reason.
async function shareOrDownloadSlides(imageUrls, lang, caption) {
  const urls = imageUrls || [];
  if (!urls.length) {
    document.getElementById("downloadStatus").textContent = "Nothing to share.";
    return;
  }
  if (shareSupportsFiles()) {
    const ok = await shareAllSlides(urls, lang, caption);
    if (ok) return;
    // shareAllSlides already reported cancellation/failure in the
    // status line; only fall through to downloading if it was an
    // actual failure, not a deliberate user cancel.
    if (lastShareWasUserCancel) return;
  }
  await downloadAllSlides(urls, lang);
}

let lastShareWasUserCancel = false;

async function shareAllSlides(urls, lang, caption) {
  const btn = document.getElementById("downloadAllBtn");
  const status = document.getElementById("downloadStatus");
  lastShareWasUserCancel = false;
  btn.disabled = true;
  status.textContent = `Preparing ${urls.length} slide(s) to share...`;

  try {
    const prefix = lang === "hi" ? "slide-hi" : "slide";
    const files = await Promise.all(
      urls.map(async (url, i) => {
        const resp = await fetch(url);
        const blob = await resp.blob();
        return new File([blob], `${prefix}-${String(i + 1).padStart(2, "0")}.jpg`, { type: blob.type || "image/jpeg" });
      })
    );

    if (!navigator.canShare({ files })) {
      // Individual images were shareable in the probe check, but this
      // exact set (count/total size) isn't - e.g. some browsers cap
      // how many files can go in one share. Let the caller fall back.
      status.textContent = "Can't share this many slides at once - falling back to download.";
      btn.disabled = false;
      return false;
    }

    await navigator.share({ files, text: caption || "" });
    status.textContent = `Sent ${urls.length} slide(s) to share.`;
    btn.disabled = false;
    return true;
  } catch (e) {
    btn.disabled = false;
    if (e && e.name === "AbortError") {
      // User backed out of the share sheet - not an error, just don't
      // fall back to auto-downloading behind their back.
      lastShareWasUserCancel = true;
      status.textContent = "Share cancelled.";
      return true;
    }
    status.textContent = "Sharing failed - falling back to download.";
    return false;
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
  if (!currentAccountLang) return; // nothing to load until an account is picked on the home screen
  const lang = currentAccountLang;
  try {
    const days = await fetchRecentDaysSlots(DAYS_KEPT);
    dayDataCache = days;
    // Re-fetching (e.g. the polling interval below) shouldn't yank the
    // user back to "today" if they're mid-swipe on a previous day -
    // just refresh whichever day they're currently viewing, in place.
    const data = dayDataCache[viewedDayOffset] || dayDataCache[0];
    const manualIndices = data.date ? await fetchManualIndices(data.date, lang) : new Set();
    render(data, manualIndices);
    updateDaySwipeUI();
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

// Re-renders from the already-fetched dayDataCache - no network call,
// so swiping feels instant. Clamped to whatever range actually came
// back (a brand-new account might not have DAYS_KEPT days of history
// yet).
function goToDay(offset) {
  const clamped = Math.max(0, Math.min(offset, dayDataCache.length - 1));
  if (clamped === viewedDayOffset || !dayDataCache.length) return;
  viewedDayOffset = clamped;
  const lang = currentAccountLang || "en";
  const data = dayDataCache[viewedDayOffset];
  const list = document.getElementById("list");
  list.classList.add("swiping");
  (data.date ? fetchManualIndices(data.date, lang) : Promise.resolve(new Set())).then((manualIndices) => {
    render(data, manualIndices);
    updateDaySwipeUI();
    list.classList.remove("swiping");
  });
}

function updateDaySwipeUI() {
  const row = document.getElementById("daySwipeRow");
  const dots = document.getElementById("daySwipeDots");
  if (!row || !dots) return;
  if (dayDataCache.length <= 1) {
    row.hidden = true;
    return;
  }
  row.hidden = false;
  dots.innerHTML = dayDataCache
    .map((_, i) => `<span class="${i === viewedDayOffset ? "active" : ""}"></span>`)
    .join("");
}

// ---- Swipe between days ----
//
// Attached to #list rather than the whole app so it doesn't fight with
// vertical scrolling of the slot rows themselves - a swipe only fires
// when the horizontal movement clearly dominates the vertical one.
(function setupDaySwipe() {
  const list = document.getElementById("list");
  let startX = null;
  let startY = null;
  list.addEventListener("touchstart", (e) => {
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
  }, { passive: true });
  list.addEventListener("touchend", (e) => {
    if (startX === null) return;
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    startX = null;
    const SWIPE_THRESHOLD = 55;
    if (Math.abs(dx) < SWIPE_THRESHOLD || Math.abs(dx) < Math.abs(dy) * 1.3) return;
    // Swipe left (finger moves right-to-left, dx < 0) -> go back a day.
    // Swipe right -> go forward toward today.
    goToDay(viewedDayOffset + (dx < 0 ? 1 : -1));
  }, { passive: true });
})();

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
document.getElementById("closeReviewModalBtn").addEventListener("click", closeReviewModal);
document.getElementById("reviewModal").addEventListener("click", (e) => {
  if (e.target.id === "reviewModal") closeReviewModal();
});
document.getElementById("saveReviewBtn").addEventListener("click", saveReviewSelection);
document.getElementById("closeStoryPreviewBtn").addEventListener("click", closeStoryPreview);
document.getElementById("storyPreviewModal").addEventListener("click", (e) => {
  if (e.target.id === "storyPreviewModal") closeStoryPreview();
});
document.getElementById("postsCountMinus").addEventListener("click", () => {
  const input = document.getElementById("postsCountInput");
  input.value = Math.max(parseInt(input.min, 10) || 1, (parseInt(input.value, 10) || 1) - 1);
});
document.getElementById("postsCountPlus").addEventListener("click", () => {
  const input = document.getElementById("postsCountInput");
  input.value = Math.min(25, (parseInt(input.value, 10) || 1) + 1);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  if (currentAccountLang) {
    refresh();
  } else {
    refreshHomeScreenBadge();
  }
});

history.replaceState({ screen: "home" }, "");
renderAccountList();

// Restore whichever screen was showing before the last refresh/close,
// instead of always dropping back to the account picker.
const savedLang = localStorage.getItem(ACTIVE_ACCOUNT_KEY);
if (savedLang && ACCOUNTS.some((a) => a.lang === savedLang)) {
  currentAccountLang = savedLang;
  document.getElementById("homeScreen").hidden = true;
  document.getElementById("app").hidden = false;
  updateAccountHeader(savedLang);
  history.pushState({ screen: "schedule", lang: savedLang }, "");
  refresh();
} else {
  refreshHomeScreenBadge();
}

tickClock();
setInterval(tickClock, 1000 * 30);
setInterval(() => {
  refresh();
  if (!currentAccountLang) refreshHomeScreenBadge();
}, 1000 * 60 * 5);
setupPush().catch((e) => console.error("[push] setupPush failed:", e));
