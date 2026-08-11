"""
seed_supabase_state.py
One-time local script: pushes your current .env token(s) into Supabase's
app_state table as the starting point for token_refresh.py's tracking,
so the GitHub Actions / Railway setup starts with an accurate expiry
instead of treating the token as "never checked before" on its first run.

Handles BOTH accounts - reads IG_ACCESS_TOKEN (English) and, if present,
IG_ACCESS_TOKEN_HI (Hindi) - and seeds whichever ones are actually set.
Missing one is fine; it's just skipped with a note.

Run this ONCE, locally, per account, right after you generate that
account's long-lived token:
    python seed_supabase_state.py

Safe to re-run - it just overwrites the same app_state row(s).
"""
import json
import os
import time

from dotenv import load_dotenv
load_dotenv()

from token_refresh import seed_token_state, ACCOUNTS

TOKEN_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_state.json")


def _seed_one(account: str):
    cfg = ACCOUNTS[account]
    token = os.environ.get(cfg["token_env"])
    if not token:
        print(f"[{account}] {cfg['token_env']} not found in your local .env - skipping.")
        return

    expires_at = None
    if account == "en" and os.path.exists(TOKEN_STATE_PATH):
        # Legacy local file from before Supabase-backed tracking existed -
        # only ever applies to the original English account.
        try:
            with open(TOKEN_STATE_PATH) as f:
                expires_at = json.load(f).get("expires_at")
        except (json.JSONDecodeError, OSError):
            pass

    if expires_at is None:
        # No prior record - assume a fresh 60-day long-lived token
        # starting now. Slightly conservative (may trigger one extra
        # refresh sooner than strictly necessary), never wrong in the
        # dangerous direction.
        expires_at = time.time() + 60 * 86400
        print(f"[{account}] no prior expiry on record - seeding a conservative 60-day expiry from now.")

    seed_token_state(token, expires_at, account=account)
    print(f"[{account}] done. Now tracked in Supabase (app_state, key '{cfg['state_key']}') - "
          f"every run will read/refresh it from there.")


def main():
    for account in ACCOUNTS:
        _seed_one(account)


if __name__ == "__main__":
    main()
