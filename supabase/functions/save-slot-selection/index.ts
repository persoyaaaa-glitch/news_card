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
// image_urls/image_urls_hi are bookended with slot.hook_slide_url(_hi)
// and slot.follow_slide_url(_hi) - the ultimate-hook collage and
// follow-for-more end card, both built/uploaded once by
// hourly_run.build_candidates at content_pregen.py build time. This
// function can't regenerate the collage itself (no PIL/image-rendering
// in Deno), so if the reviewer's final selection differs from the
// top-priority default the collage was built from, the collage photos
// stay as originally built rather than reflecting the new selection -
// see build_candidates()'s docstring for that trade-off.
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
const MAX_STORIES = 4;   // STORIES_PER_POST in daily_scheduler.py
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

// Assembles intro + numbered story blocks + hashtags. If it would
// exceed IG_CAPTION_CHAR_LIMIT, TRIMS each story's paragraph down to a
// fair share of the remaining space rather than dropping whole stories
// - every story selected always keeps its headline and at least a
// shortened write-up in the caption. Bodies already shorter than their
// fair share keep their full text; the space they don't use is handed
// to the longer bodies (processed shortest-first). Only if bare
// titles/sources alone don't fit do we fall back to dropping the
// lowest-priority story entirely. Mirrors hourly_run._fit_caption
// exactly, so the same slot's caption comes out the same whether it
// was built by content_pregen.py or re-saved here after a reorder.
type CaptionPart = { title: string; body: string; source: string };

function buildCaption(intro: string, parts: CaptionPart[], hashtagLine: string): string {
  const lines = [intro, ""];
  parts.forEach((p, i) => {
    const block = (p.body ? `${p.title} — ${p.body}` : p.title) + ` (Source: ${p.source})`;
    lines.push(`${i + 1}. ${block}`);
    lines.push("");
  });
  return lines.join("\n").replace(/\n+$/, "") + "\n\n" + hashtagLine;
}

function fitCaption(intro: string, parts: CaptionPart[], hashtagLine: string): string {
  const full = buildCaption(intro, parts, hashtagLine);
  if (full.length <= IG_CAPTION_CHAR_LIMIT) return full;

  // Every story is assumed to end up with a non-empty (if shortened)
  // body, which costs an extra " — " separator each vs. the bare title
  // - reserve that too, or the final caption can come out a few
  // characters over the limit.
  const separator = " — ";
  const bareParts = parts.map((p) => ({ ...p, body: "" }));
  const overhead = buildCaption(intro, bareParts, hashtagLine).length;
  const n = parts.length;
  const available = IG_CAPTION_CHAR_LIMIT - overhead - n * separator.length;

  if (available <= 0 || n === 0) {
    // Bare titles alone don't fit - fall back to dropping stories from
    // the end (old behavior) as a last resort.
    let remaining = parts.slice();
    while (remaining.length > 0) {
      const trial = buildCaption(intro, remaining, hashtagLine);
      if (trial.length <= IG_CAPTION_CHAR_LIMIT) return trial;
      if (remaining.length === 1) break;
      remaining = remaining.slice(0, -1);
    }
    return (intro + "\n\n" + hashtagLine).slice(0, IG_CAPTION_CHAR_LIMIT);
  }

  // Distribute `available` characters of body text across all n
  // stories, shortest-body-first, so short paragraphs keep their full
  // text and unused space passes on to the longer ones.
  const order = parts.map((_, i) => i).sort((a, b) => parts[a].body.length - parts[b].body.length);
  const trimmedBodies: string[] = new Array(n);
  let remainingBudget = available;
  let remainingN = n;
  for (const idx of order) {
    const body = parts[idx].body;
    const share = remainingN > 0 ? Math.floor(remainingBudget / remainingN) : 0;
    if (body.length <= share) {
      trimmedBodies[idx] = body;
      remainingBudget -= body.length;
    } else {
      const keep = Math.max(0, share - 1); // leave room for the "…"
      trimmedBodies[idx] = keep > 0 ? body.slice(0, keep).trimEnd() + "…" : "";
      remainingBudget -= share;
    }
    remainingN -= 1;
  }

  const trimmedParts = parts.map((p, i) => ({ ...p, body: trimmedBodies[i] }));
  return buildCaption(intro, trimmedParts, hashtagLine);
}

function templatedCaption(selected: any[]): string {
  const intro = `Today's top ${selected.length} stories - here's what's happening:`;
  const parts: CaptionPart[] = selected.map((c) => ({
    title: c.title,
    body: c.caption_paragraph || c.detail_text || "",
    source: c.source,
  }));
  return fitCaption(intro, parts, HASHTAGS_EN);
}

function templatedCaptionHindi(selected: any[]): string {
  const intro = `आज की ${selected.length} बड़ी खबरें:`;
  const parts: CaptionPart[] = selected.map((c) => ({
    title: c.title_hi || c.title,
    body: c.caption_paragraph_hi || "",
    source: c.source,
  }));
  return fitCaption(intro, parts, HASHTAGS_HI);
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

  // Ultimate-hook collage + follow-for-more end card: built once in
  // Python (hourly_run.build_candidates, at content_pregen.py build
  // time) and persisted on the slot as hook_slide_url(_hi)/
  // follow_slide_url(_hi) - this function has no image-rendering
  // capability (it's a Deno Edge Function, not Python/PIL), so it
  // reuses those already-uploaded URLs as-is rather than regenerating
  // the collage against the reviewer's possibly-changed selection. See
  // build_candidates()'s docstring for that trade-off. Missing/blank
  // URLs (e.g. an older slot built before this feature existed, or
  // Hindi disabled) are simply skipped, same as the Python side.
  const hookUrl = slot.hook_slide_url || "";
  const hookUrlHi = slot.hook_slide_url_hi || "";
  const followUrl = slot.follow_slide_url || "";
  const followUrlHi = slot.follow_slide_url_hi || "";

  function assembleImages(hookUrl: string, storyUrls: string[], followUrl: string): string[] {
    const bookends = (hookUrl ? 1 : 0) + (followUrl ? 1 : 0);
    const budget = MAX_IMAGES - bookends;
    return [
      ...(hookUrl ? [hookUrl] : []),
      ...storyUrls.slice(0, budget),
      ...(followUrl ? [followUrl] : []),
    ];
  }

  const storyImageUrls = selected.flatMap((c) => c.image_urls || []);
  const storyImageUrlsHi = selected.flatMap((c) => c.image_urls_hi || []);
  const image_urls = assembleImages(hookUrl, storyImageUrls, followUrl);
  const image_urls_hi = assembleImages(hookUrlHi, storyImageUrlsHi, followUrlHi);
  const caption = templatedCaption(selected);
  const caption_hi = storyImageUrlsHi.length ? templatedCaptionHindi(selected) : "";
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