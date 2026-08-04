// trigger-schedule-check
//
// Called by the PWA right after it saves a schedule_overrides row, so
// GitHub doesn't have to wait for its next */30 cron tick to notice the
// change. This is the ONLY place the GitHub dispatch token lives - it's
// a Supabase secret, set via `supabase secrets set`, never shipped to
// the browser. The anon key (public, in docs/config.js) is enough to
// call this function; it does NOT authorize the caller to do anything
// on GitHub directly.
//
// All this does is fire scheduler.yml's workflow_dispatch event - the
// exact same "Run workflow" button you already have in the Actions tab.
// It does not touch anything else on the repo.

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const token = Deno.env.get("GITHUB_DISPATCH_TOKEN");
  const owner = Deno.env.get("GITHUB_REPO_OWNER");
  const repo = Deno.env.get("GITHUB_REPO_NAME");

  if (!token || !owner || !repo) {
    return new Response("Missing GITHUB_DISPATCH_TOKEN/GITHUB_REPO_OWNER/GITHUB_REPO_NAME secret(s)", { status: 500 });
  }

  const resp = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/scheduler.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );

  // GitHub returns 204 with no body on success.
  if (resp.status === 204) {
    return new Response(JSON.stringify({ ok: true }), {
      headers: { "Content-Type": "application/json" },
    });
  }

  const text = await resp.text();
  return new Response(JSON.stringify({ ok: false, status: resp.status, detail: text }), {
    status: 502,
    headers: { "Content-Type": "application/json" },
  });
});
