"""
token_refresh.py
Tracks the current long-lived Instagram token's expiry locally (Meta doesn't
expose a simple "check remaining lifetime" call on graph.instagram.com), and
refreshes the token via the ig_refresh_token grant when it's getting close.

State is kept in token_state.json (just one field: expires_at, a Unix
timestamp). On refresh, expires_at is recomputed from the fresh expires_in
Meta returns, so it stays accurate from then on automatically.
"""
import os
import time
import requests
from dotenv import load_dotenv

from supabase_client import get_state, save_state

load_dotenv()

GRAPH_BASE = "https://graph.instagram.com"

# Supabase app_state keys. Both the expiry AND the live token value are
# stored remotely - not just the expiry - because on GitHub Actions each
# run starts from a clean checkout with no memory of a previous refresh.
# Rewriting a local .env file (the old approach) does nothing useful
# there: the file doesn't exist beyond that one run's container, and the
# GitHub Actions secret that seeded IG_ACCESS_TOKEN can't be edited by
# the workflow itself without extra GitHub API/PAT plumbing. Storing the
# refreshed token in Supabase sidesteps that entirely - every run just
# asks Supabase for the current token first, GitHub secret only as a
# one-time seed.
TOKEN_STATE_KEY = "ig_token_state"  # {"access_token": str, "expires_at": float}

# Refresh once we're within this many days of expiry. Tokens need to be at
# least 24h old before they're refreshable, and long-lived tokens last ~60
# days, so 10 days of buffer is comfortable without wasting refreshes.
REFRESH_THRESHOLD_DAYS = 10


def _load_token_state() -> dict:
    return get_state(TOKEN_STATE_KEY, default={})


def _save_token_state(access_token: str, expires_at: float):
    save_state(TOKEN_STATE_KEY, {"access_token": access_token, "expires_at": expires_at})


def _refresh_token(token: str) -> dict | None:
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()  # {"access_token": "...", "token_type": "bearer", "expires_in": 5184000}
    except requests.RequestException as e:
        print(f"[token_refresh] refresh request failed: {e}")
        return None


def seed_token_state(access_token: str, expires_at: float):
    """One-time manual seed - call with the current token and a known Unix
    expiry timestamp (e.g. from the Meta Access Token Debugger) so the very
    first run has something to compare against."""
    _save_token_state(access_token, expires_at)
    print(f"[token_refresh] seeded token state, expiry {expires_at} "
          f"(~{(expires_at - time.time()) / 86400:.1f} days from now)")


def ensure_token_fresh() -> str:
    """
    Call once near the top of hourly_run.py's run(). Resolves the current
    token from Supabase if one's been stored there (i.e. a previous run
    already refreshed it); otherwise falls back to the IG_ACCESS_TOKEN
    env var (the GitHub Actions secret) as the initial seed. If the
    resolved token is within REFRESH_THRESHOLD_DAYS of expiry (or has no
    tracked expiry yet), refreshes it and saves the new token+expiry back
    to Supabase so every subsequent run - including ones on a fresh
    GitHub Actions container - picks up the refreshed token automatically.

    Returns the token to actually use for this run, and also sets it on
    os.environ["IG_ACCESS_TOKEN"] so the rest of the pipeline (e.g.
    instagram_publish.py) doesn't need to change how it reads the token.
    """
    state = _load_token_state()
    token = state.get("access_token") or os.environ.get("IG_ACCESS_TOKEN")
    expires_at = state.get("expires_at")

    if not token:
        print("[token_refresh] IG_ACCESS_TOKEN not set anywhere, skipping check")
        return ""

    if expires_at is not None:
        days_left = (expires_at - time.time()) / 86400
        print(f"[token_refresh] token has ~{days_left:.1f} days left (tracked in Supabase)")
        if days_left > REFRESH_THRESHOLD_DAYS:
            os.environ["IG_ACCESS_TOKEN"] = token
            return token  # plenty of runway, nothing to do
    else:
        print("[token_refresh] no tracked expiry yet - refreshing now to establish one")

    print("[token_refresh] refreshing token...")
    result = _refresh_token(token)
    if result and "access_token" in result:
        new_token = result["access_token"]
        new_expires_at = time.time() + result.get("expires_in", 5184000)
        _save_token_state(new_token, new_expires_at)
        os.environ["IG_ACCESS_TOKEN"] = new_token
        expires_in_days = result.get("expires_in", 0) / 86400
        print(f"[token_refresh] refreshed successfully, new token valid ~{expires_in_days:.0f} days")
        return new_token

    print("[token_refresh] refresh failed - current token still in use. "
          "If it's actually close to expiry, investigate before it lapses "
          "(no recovery path once expired - full re-auth required).")
    os.environ["IG_ACCESS_TOKEN"] = token
    return token


if __name__ == "__main__":
    ensure_token_fresh()
