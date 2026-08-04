"""
fetch_repo_traffic.py
Pulls this repo's public stats and stores them in Supabase so the
companion PWA can display them, refreshed on every workflow run:
  - views + clones (last 14 days, GitHub's Traffic API)
  - stars, forks, watchers, open issues (live counts, public repo data)

Two different token requirements are in play here:
  - Traffic API (views/clones) requires actual push access to the repo -
    the workflow's own built-in GITHUB_TOKEN can NEVER satisfy this, no
    matter what `permissions:` block the job declares (contents/issues/etc
    scopes don't map to it). It needs a real Personal Access Token.
  - The plain repo endpoint (stars/forks/watchers/open_issues) is public
    data and works fine with the default GITHUB_TOKEN, or even
    unauthenticated - kept on GITHUB_TOKEN just to get the higher rate
    limit.

Set REPO_TRAFFIC_PAT (classic PAT, 'repo' scope, belonging to someone
with push access to this repo) as a GitHub Actions secret to enable the
views/clones half. Without it, this script still saves the always-available
stars/forks/watchers/open_issues stats, and just logs why traffic is being
skipped.
"""
import os
import requests

from supabase_client import save_state

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
REPO_TRAFFIC_PAT = os.environ.get("REPO_TRAFFIC_PAT")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", auto-set by Actions
TRAFFIC_KEY = "repo_traffic"

API_BASE = "https://api.github.com"


def _get(url: str, token: str):
    return requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )


def _fetch_traffic(repo: str) -> dict:
    """Views + clones over the trailing 14 days. Needs a real PAT with
    push access - the Actions-provided GITHUB_TOKEN can't do this no
    matter what `permissions:` are granted to it."""
    if not REPO_TRAFFIC_PAT:
        print("[fetch_repo_traffic] REPO_TRAFFIC_PAT not set - skipping views/clones "
              "(stars/forks/watchers will still be updated). To enable: create a classic "
              "Personal Access Token with 'repo' scope on an account with push access to "
              "this repo, then add it as the REPO_TRAFFIC_PAT secret.")
        return {}

    result = {}
    views_resp = _get(f"{API_BASE}/repos/{repo}/traffic/views", REPO_TRAFFIC_PAT)
    if views_resp.status_code == 403:
        print("[fetch_repo_traffic] 403 on traffic/views - REPO_TRAFFIC_PAT doesn't have "
              "push access to this repo, or the token/scope is wrong.")
    elif not views_resp.ok:
        print(f"[fetch_repo_traffic] traffic/views request failed: "
              f"{views_resp.status_code} {views_resp.text[:200]}")
    else:
        data = views_resp.json()
        result["count_14d"] = data.get("count", 0)
        result["uniques_14d"] = data.get("uniques", 0)

    clones_resp = _get(f"{API_BASE}/repos/{repo}/traffic/clones", REPO_TRAFFIC_PAT)
    if clones_resp.status_code == 403:
        print("[fetch_repo_traffic] 403 on traffic/clones - same permission issue as views.")
    elif not clones_resp.ok:
        print(f"[fetch_repo_traffic] traffic/clones request failed: "
              f"{clones_resp.status_code} {clones_resp.text[:200]}")
    else:
        data = clones_resp.json()
        result["clones_14d"] = data.get("count", 0)
        result["clone_uniques_14d"] = data.get("uniques", 0)

    return result


def _fetch_repo_stats(repo: str) -> dict:
    """Stars/forks/watchers/open issues - public data, works with the
    default GITHUB_TOKEN (or even no token, just at a lower rate limit)."""
    token = GITHUB_TOKEN or REPO_TRAFFIC_PAT
    resp = _get(f"{API_BASE}/repos/{repo}", token) if token else requests.get(
        f"{API_BASE}/repos/{repo}", headers={"Accept": "application/vnd.github+json"}, timeout=15
    )
    if not resp.ok:
        print(f"[fetch_repo_traffic] repo stats request failed: {resp.status_code} {resp.text[:200]}")
        return {}
    data = resp.json()
    return {
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "watchers": data.get("subscribers_count", data.get("watchers_count", 0)),
        "open_issues": data.get("open_issues_count", 0),
    }


def fetch_and_store():
    if not GITHUB_REPOSITORY:
        print("[fetch_repo_traffic] GITHUB_REPOSITORY not set - skipping "
              "(expected when run outside GitHub Actions).")
        return

    stats = {}
    stats.update(_fetch_repo_stats(GITHUB_REPOSITORY))
    stats.update(_fetch_traffic(GITHUB_REPOSITORY))

    if not stats:
        print("[fetch_repo_traffic] nothing could be fetched this run - leaving previous "
              "stats in place.")
        return

    save_state(TRAFFIC_KEY, stats)

    parts = []
    if "count_14d" in stats:
        parts.append(f"{stats['count_14d']} views / {stats['uniques_14d']} unique visitors (14d)")
    if "clones_14d" in stats:
        parts.append(f"{stats['clones_14d']} clones / {stats['clone_uniques_14d']} unique cloners (14d)")
    if "stars" in stats:
        parts.append(f"{stats['stars']} stars, {stats['forks']} forks, {stats['watchers']} watchers")
    print(f"[fetch_repo_traffic] saved: {'; '.join(parts)}")


if __name__ == "__main__":
    fetch_and_store()
