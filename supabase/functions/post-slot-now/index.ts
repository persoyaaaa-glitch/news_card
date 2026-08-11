// post-slot-now
//
// Called by the PWA's "Post now" button. Fires post-now.yml's
// workflow_dispatch event on GitHub - same GITHUB_DISPATCH_TOKEN/
// GITHUB_REPO_OWNER/GITHUB_REPO_NAME secrets trigger-schedule-check and
// generate-slot already use, so no new Supabase secrets need to be
// added for this to work. That workflow runs daily_scheduler.py's
// post_now(), which publishes ONE slot/language immediately, ignoring
// its planned_time and the MIN_GAP_MINUTES cooldown.
//
// Before spending an Actions run, this checks the slot against today's
// real schedule AND the slot_overrides Manual flag (using the
// service_role key, same as save-slot-selection/generate-slot), so an
// already-posted slot, a stale date, or a slot you've taken over
// manually gets rejected immediately with a clear error instead of
// silently kicking off a wasted/unwanted Actions run. post_now() on
// the Python side re-checks both of these itself before actually
// publishing - this is a UX shortcut, not the only safety net.

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "apikey, authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

Deno.serve(async (req) => {
  // Browsers preflight this because the request carries custom headers
  // (apikey/Authorization) - same reasoning as trigger-schedule-check.
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return jsonResponse({ ok: false, error: "Method not allowed" }, 405);
  }

  let body: { slot_date?: string; slot_index?: number; lang?: string };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid JSON body" }, 400);
  }

  const { slot_date, slot_index, lang } = body;
  if (!slot_date || typeof slot_index !== "number" || (lang !== "en" && lang !== "hi")) {
    return jsonResponse(
      { ok: false, error: "slot_date, slot_index, and lang ('en' or 'hi') are required" },
      400
    );
  }

  const SUPABASE_URL = Deno.env.get("SUPABASE_URL");
  const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const token = Deno.env.get("GITHUB_DISPATCH_TOKEN");
  const owner = Deno.env.get("GITHUB_REPO_OWNER");
  const repo = Deno.env.get("GITHUB_REPO_NAME");

  if (!SUPABASE_URL || !SERVICE_ROLE_KEY) {
    return jsonResponse({ ok: false, error: "Missing SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY" }, 500);
  }
  if (!token || !owner || !repo) {
    return jsonResponse(
      { ok: false, error: "Missing GITHUB_DISPATCH_TOKEN/GITHUB_REPO_OWNER/GITHUB_REPO_NAME secret(s)" },
      500
    );
  }

  const headers = { apikey: SERVICE_ROLE_KEY, Authorization: `Bearer ${SERVICE_ROLE_KEY}` };

  // Sanity-check against the real schedule first.
  const getResp = await fetch(
    `${SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots:${slot_date}&select=value`,
    { headers }
  );
  if (!getResp.ok) {
    return jsonResponse({ ok: false, error: `failed to read daily_slots (${getResp.status})` }, 502);
  }
  const rows = await getResp.json();
  const state = rows.length ? rows[0].value : null;
  if (!state || state.date !== slot_date) {
    return jsonResponse(
      { ok: false, error: "slot_date doesn't match today's schedule - it may have rolled over" },
      409
    );
  }
  const slots = state.slots || [];
  if (slot_index < 0 || slot_index >= slots.length) {
    return jsonResponse({ ok: false, error: "slot_index is out of range for today's schedule" }, 400);
  }
  const existing = slots.find((s: any) => s.index === slot_index);
  const postedField = lang === "hi" ? "posted_hi" : "posted";
  if (existing && existing[postedField]) {
    return jsonResponse({ ok: false, error: "this slot is already posted" }, 409);
  }

  // Refuse a slot flagged Manual for this language - see post_now()'s
  // docstring for why. Best-effort: if this lookup itself fails, fall
  // through and let the dispatch happen - post_now() re-checks the
  // flag server-side before ever publishing, so this pre-check is only
  // a faster UX shortcut, not the sole safety net.
  const manualColumn = lang === "hi" ? "manual_hi" : "manual_en";
  const manualResp = await fetch(
    `${SUPABASE_URL}/rest/v1/slot_overrides?slot_date=eq.${slot_date}&slot_index=eq.${slot_index}&select=${manualColumn}`,
    { headers }
  );
  if (manualResp.ok) {
    const manualRows = await manualResp.json();
    if (manualRows.length && manualRows[0][manualColumn]) {
      return jsonResponse(
        {
          ok: false,
          error: "this slot is flagged Manual - you said you'd post it yourself, so Post now is disabled for it",
        },
        409
      );
    }
  }

  const resp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/post-now.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { slot_index: String(slot_index), lang } }),
    }
  );

  // GitHub returns 204 with no body on success.
  if (resp.status === 204) {
    return jsonResponse({ ok: true });
  }

  const text = await resp.text();
  return jsonResponse({ ok: false, status: resp.status, detail: text }, 502);
});
