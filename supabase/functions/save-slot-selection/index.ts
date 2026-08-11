// save-slot-selection
//
// Called by the PWA's review screen after the user checks/unchecks and
// reorders candidate stories for a not-yet-posted slot. Recomputes that
// slot's image_urls/caption/image_urls_hi/caption_hi from the chosen
// subset + order of slot.candidates (each candidate's images were
// already built and uploaded by content_pregen.py - see
// hourly_run.build_candidates) and writes the updated slot back into
// app_state.daily_slots. Uses the service_role key (never shipped to
// the browser, auto-provided to every Edge Function) so it can write
// app_state, which the anon key can only read (see
// supabase_app_additions.sql).
//
// No AI call happens here on purpose - re-running Gemini every time
// someone toggles a checkbox or drags a reorder handle would be slow
// and easy to rate-limit. Instead, each candidate already carries its
// own detailed per-story write-up - caption_paragraph / caption_paragraph_hi
// (~90-150 words, generated once when the candidate was built - see
// ai_text.generate_hook_and_detail / translate_story_to_hindi via
// hourly_run.build_candidates) - and this function just assembles those
// into one caption, in the EXACT order `selected` is given. Since
// `selected` is built from the client's selected_story_ids array (see
// below), whatever order the reviewer dragged the stories into IS the
// order they appear in the caption - mirrors
// hourly_run.build_combined_caption/_hindi, which do the identical
// assembly for the untouched default (see content_pregen.py).

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "apikey, authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const MAX_IMAGES = 10;   // Instagram's per-carousel cap - see hourly_run.py
const MAX_STORIES = 5;   // STORIES_PER_POST in daily_scheduler.py
const IG_CAPTION_CHAR_LIMIT = 2200; // Instagram's hard cap on caption length, hashtags included

const HASHTAGS_EN = "#IndiaNews #TopStories #NewsRoundup #Trending #BreakingNews " +
  "#DailyNews #WorldNews #NewsUpdate #CurrentAffairs #NewsToday";
const HASHTAGS_HI = "#IndiaNews #HindiNews #आजकीखबर #Trending #BreakingNews " +
  "#DailyNews #WorldNews #NewsUpdate #CurrentAffairs #NewsToday";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

// Assembles intro + numbered story blocks + hashtags, trimming from the
// END (lowest-priority story) if it would exceed IG_CAPTION_CHAR_LIMIT -
// mirrors hourly_run._fit_caption exactly, so the same slot's caption
// comes out the same whether it was built by content_pregen.py or
// re-saved here after a reorder.
function fitCaption(intro: string, blocks: string[], hashtagLine: string): string {
  let remaining = blocks.slice();
  while (remaining.length > 0) {
    const lines = [intro, ""];
    remaining.forEach((block, i) => {
      lines.push(`${i + 1}. ${block}`);
      lines.push("");
    });
    const caption = lines.join("\n").replace(/\n+$/, "") + "\n\n" + hashtagLine;
    if (caption.length <= IG_CAPTION_CHAR_LIMIT) return caption;
    if (remaining.length === 1) {
      const overflow = caption.length - IG_CAPTION_CHAR_LIMIT;
      const keep = Math.max(0, remaining[0].length - overflow - 1);
      const trimmed = remaining[0].slice(0, keep).trimEnd() + "…";
      return [intro, "", `1. ${trimmed}`].join("\n") + "\n\n" + hashtagLine;
    }
    remaining = remaining.slice(0, -1); // drop the lowest-priority story and retry
  }
  return (intro + "\n\n" + hashtagLine).slice(0, IG_CAPTION_CHAR_LIMIT);
}

function templatedCaption(selected: any[]): string {
  const intro = `Today's top ${selected.length} stories - here's what's happening:`;
  const blocks = selected.map((c) => {
    const body = c.caption_paragraph || c.detail_text || "";
    const headlinePart = body ? `${c.title} — ${body}` : c.title;
    return `${headlinePart} (Source: ${c.source})`;
  });
  return fitCaption(intro, blocks, HASHTAGS_EN);
}

function templatedCaptionHindi(selected: any[]): string {
  const intro = `आज की ${selected.length} बड़ी खबरें:`;
  const blocks = selected.map((c) => {
    const headline = c.title_hi || c.title;
    const body = c.caption_paragraph_hi || "";
    const headlinePart = body ? `${headline} — ${body}` : headline;
    return `${headlinePart} (Source: ${c.source})`;
  });
  return fitCaption(intro, blocks, HASHTAGS_HI);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405, headers: CORS_HEADERS });
  }

  const SUPABASE_URL = Deno.env.get("SUPABASE_URL");
  const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!SUPABASE_URL || !SERVICE_ROLE_KEY) {
    return jsonResponse({ ok: false, error: "Missing SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY" }, 500);
  }

  let body: { slot_date?: string; slot_index?: number; selected_story_ids?: string[] };
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ ok: false, error: "invalid JSON body" }, 400);
  }

  const { slot_date, slot_index, selected_story_ids } = body;
  if (!slot_date || typeof slot_index !== "number" ||
      !Array.isArray(selected_story_ids) || !selected_story_ids.length) {
    return jsonResponse({
      ok: false,
      error: "slot_date, slot_index, and a non-empty selected_story_ids array are required",
    }, 400);
  }
  if (selected_story_ids.length > MAX_STORIES) {
    return jsonResponse({
      ok: false,
      error: `at most ${MAX_STORIES} stories can be selected (Instagram's ${MAX_IMAGES}-image carousel cap)`,
    }, 400);
  }

  const headers = {
    apikey: SERVICE_ROLE_KEY,
    Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
    "Content-Type": "application/json",
  };

  const getResp = await fetch(`${SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots:${slot_date}&select=value`, { headers });
  if (!getResp.ok) {
    return jsonResponse({ ok: false, error: `failed to read daily_slots (${getResp.status})` }, 502);
  }
  const rows = await getResp.json();
  if (!rows.length) {
    return jsonResponse({ ok: false, error: "no daily_slots row yet" }, 404);
  }
  const state = rows[0].value;
  if (state.date !== slot_date) {
    return jsonResponse({ ok: false, error: "slot_date doesn't match today's schedule - it may have rolled over" }, 409);
  }

  const slot = (state.slots || []).find((s: any) => s.index === slot_index);
  if (!slot) {
    return jsonResponse({ ok: false, error: "slot not found" }, 404);
  }
  if (!slot.candidates || !slot.candidates.length) {
    return jsonResponse({ ok: false, error: "this slot has no candidates to select from yet" }, 409);
  }

  const byId = new Map(slot.candidates.map((c: any) => [c.id, c]));
  const selected = selected_story_ids.map((id) => byId.get(id)).filter(Boolean) as any[];
  if (!selected.length) {
    return jsonResponse({ ok: false, error: "none of the selected ids matched this slot's candidates" }, 400);
  }

  const image_urls = selected.flatMap((c) => c.image_urls || []).slice(0, MAX_IMAGES);
  const image_urls_hi = selected.flatMap((c) => c.image_urls_hi || []).slice(0, MAX_IMAGES);
  const caption = templatedCaption(selected);
  const caption_hi = image_urls_hi.length ? templatedCaptionHindi(selected) : "";
  const stories = selected.map((c) => ({
    title: c.title, source: c.source, is_sensitive: !!c.is_sensitive, title_hi: c.title_hi || "",
  }));

  slot.selected_story_ids = selected_story_ids;
  slot.image_urls = image_urls;
  slot.caption = caption;
  slot.image_urls_hi = image_urls_hi;
  slot.caption_hi = caption_hi;
  slot.stories = stories;

  const patchResp = await fetch(`${SUPABASE_URL}/rest/v1/app_state?key=eq.daily_slots:${slot_date}`, {
    method: "PATCH",
    headers: { ...headers, Prefer: "return=minimal" },
    body: JSON.stringify({ value: state, updated_at: new Date().toISOString() }),
  });
  if (!patchResp.ok) {
    const detail = await patchResp.text();
    return jsonResponse({ ok: false, error: `failed to save (${patchResp.status})`, detail }, 502);
  }

  return jsonResponse({ ok: true, slot });
});