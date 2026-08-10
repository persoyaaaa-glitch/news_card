// generate-slot
//
// Called by the PWA's "Generate now" button on a slot that hasn't had
// its content built yet. Fires generate-slot.yml's workflow_dispatch
// event on GitHub - same GITHUB_DISPATCH_TOKEN/GITHUB_REPO_OWNER/
// GITHUB_REPO_NAME secrets trigger-schedule-check already uses, so no
// new Supabase secrets need to be added for this to work. That
// workflow runs content_pregen.py --slot <index>, which builds and
// saves that ONE slot's candidates/images/caption immediately,
// ignoring the normal 30-min-ahead build window.
//
// This does NOT post anything and does NOT touch planned_time - the
// slot still only actually gets published by the normal scheduler.yml
// cron once its fixed time arrives, exactly like every other slot.
//
// Before spending an Actions run, this checks the slot against today's
// real schedule (using the service_role key, same as save-slot-selection)
// so a stale date or an already-built slot gets rejected with a clear
// error instead of silently kicking off a wasted/duplicate build.

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

  let body: { slot_date?: string; slot_index?: number };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid JSON body" }, 400);
  }

  const { slot_date, slot_index } = body;
  if (!slot_date || typeof slot_index !== "number") {
    return jsonResponse({ ok: false, error: "slot_date and slot_index are required" }, 400);
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

  // Sanity-check against the real schedule first.
  const getResp = await fetch(
    `${SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots&select=value`,
    { headers: { apikey: SERVICE_ROLE_KEY, Authorization: `Bearer ${SERVICE_ROLE_KEY}` } }
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
  if (existing && existing.image_urls && existing.image_urls.length) {
    return jsonResponse({ ok: false, error: "this slot already has content built" }, 409);
  }

  const resp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/generate-slot.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { slot_index: String(slot_index) } }),
    }
  );

  // GitHub returns 204 with no body on success.
  if (resp.status === 204) {
    return jsonResponse({ ok: true });
  }

  const text = await resp.text();
  return jsonResponse({ ok: false, status: resp.status, detail: text }, 502);
});
