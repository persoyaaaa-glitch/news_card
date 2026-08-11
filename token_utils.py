"""
token_utils.py
Meta's Graph API tokens come in two flavors:
  - Short-lived (from Graph API Explorer): ~1 hour, useless for automation
  - Long-lived: ~60 days, refreshable before it expires

Step 1 (one-time, manual): exchange your short-lived token for a
long-lived one using this script.

Step 2 (recurring, ~every 50 days): refresh the long-lived token before
it expires, using the same exchange endpoint. Set a personal reminder
for this - or later we can wire it into its own Railway cron.
"""
import sys
import requests

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def exchange_for_long_lived_token(app_id: str, app_secret: str, short_lived_token: str) -> dict:
    resp = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()  # {"access_token": "...", "token_type": "bearer", "expires_in": 5184000}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python token_utils.py <app_id> <app_secret> <short_lived_token>")
        sys.exit(1)

    result = exchange_for_long_lived_token(sys.argv[1], sys.argv[2], sys.argv[3])
    print("\nLong-lived token (valid ~60 days) - save this as IG_ACCESS_TOKEN:\n")
    print(result.get("access_token"))
    print(f"\nExpires in ~{result.get('expires_in', 0) // 86400} days from now.")
