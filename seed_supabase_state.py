"""
seed_supabase_state.py
One-time local script: migrates your existing local token_state.json
expiry (and current .env IG_ACCESS_TOKEN) into Supabase's app_state
table, so the GitHub Actions setup starts with an accurate token expiry
instead of treating the token as "never checked before" on its first run.

Run this ONCE, locally, before your first GitHub Actions run:
    python seed_supabase_state.py

Safe to re-run - it just overwrites the same app_state row.
"""
import json
import os
import time

from dotenv import load_dotenv
load_dotenv()

from token_refresh import seed_token_state

TOKEN_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_state.json")


def main():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        print("IG_ACCESS_TOKEN not found in your local .env - nothing to seed.")
        return

    expires_at = None
    if os.path.exists(TOKEN_STATE_PATH):
        try:
            with open(TOKEN_STATE_PATH) as f:
                expires_at = json.load(f).get("expires_at")
        except (json.JSONDecodeError, OSError):
            pass

    if expires_at is None:
        # No prior local record - assume a fresh 60-day long-lived token
        # starting now. Slightly conservative (may trigger one extra
        # refresh sooner than strictly necessary), never wrong in the
        # dangerous direction.
        expires_at = time.time() + 60 * 86400
        print("No local token_state.json found - seeding a conservative 60-day expiry from now.")

    seed_token_state(token, expires_at)
    print("Done. This value now lives in Supabase (app_state, key 'ig_token_state') "
          "and GitHub Actions runs will read/refresh it from there.")


if __name__ == "__main__":
    main()
