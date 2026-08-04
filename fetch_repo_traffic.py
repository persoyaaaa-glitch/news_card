"""
fetch_repo_traffic.py
Pulls this repo's last-14-days view count from GitHub's Traffic API and
stores it in Supabase so the companion PWA can display it. Runs inside
GitHub Actions using the workflow's own built-in GITHUB_TOKEN - no extra
secret to create.

GitHub's traffic numbers only cover a rolling 14-day window (GitHub
doesn't offer longer history), and only count views from people other
than the repo owner while logged in.
"""
import os
import requests

from supabase_client import save_state

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", auto-set by Actions
TRAFFIC_KEY = "repo_traffic"


def fetch_and_store():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[fetch_repo_traffic] GITHUB_TOKEN/GITHUB_REPOSITORY not set - skipping "
              "(expected when run outside GitHub Actions).")
        return

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/traffic/views"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )

    if resp.status_code == 403:
        print("[fetch_repo_traffic] 403 - the workflow's GITHUB_TOKEN doesn't have enough "
              "permission for the Traffic API. Add 'permissions: contents: write' to this "
              "job in scheduler.yml, or use a classic Personal Access Token with 'repo' "
              "scope as a GITHUB_TRAFFIC_TOKEN secret instead.")
        return
    if not resp.ok:
        print(f"[fetch_repo_traffic] request failed: {resp.status_code} {resp.text[:200]}")
        return

    data = resp.json()
    save_state(TRAFFIC_KEY, {
        "count_14d": data.get("count", 0),
        "uniques_14d": data.get("uniques", 0),
    })
    print(f"[fetch_repo_traffic] saved: {data.get('count', 0)} views / "
          f"{data.get('uniques', 0)} unique visitors (last 14 days)")


if __name__ == "__main__":
    fetch_and_store()
