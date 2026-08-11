"""
token_refresh.py
Tracks each Instagram account's current long-lived token expiry locally
(Meta doesn't expose a simple "check remaining lifetime" call on
graph.instagram.com), and refreshes it via the ig_refresh_token grant
when it's getting close.

Multi-account: two independent accounts are tracked - the English page
("en") and the Hindi page ("hi") - each with its own env vars and its
own Supabase app_state key, so refreshing one never touches the other.
Add a third account later by adding one entry to ACCOUNTS below.

State is kept in Supabase app_state (see supabase_client.get_state /
save_state), one row per account: {"access_token": str, "expires_at":
float}.
"""
import os
import time
import requests
from dotenv import load_dotenv

from supabase_client import get_state, save_state

load_dotenv()

GRAPH_BASE = "https://graph.instagram.com"

# Per-account config: env var holding the seed/current token, the env
# var this module writes the live token back into (same var here -
# instagram_publish.py always reads it fresh), and the Supabase
# app_state key that persists the tracked expiry + refreshed token
# across process restarts / GitHub Actions runs.
#
# "ig_token_state" (no suffix) is kept as the English key so existing
# Supabase rows from before the Hindi page existed keep working without
# a migration.
ACCOUNTS = {
    "en": {"token_env": "IG_ACCESS_TOKEN", "state_key": "ig_token_state"},
    "hi": {"token_env": "IG_ACCESS_TOKEN_HI", "state_key": "ig_token_state_hi"},
}

# Refresh once we're within this many days of expiry. Tokens need to be at
# least 24h old before they're refreshable, and long-lived tokens last ~60
# days, so 10 days of buffer is comfortable without wasting refreshes.
REFRESH_THRESHOLD_DAYS = 10


def _load_token_state(account: str) -> dict:
    return get_state(ACCOUNTS[account]["state_key"], default={})


def _save_token_state(account: str, access_token: str, expires_at: float):
    save_state(ACCOUNTS[account]["state_key"], {"access_token": access_token, "expires_at": expires_at})


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


def seed_token_state(access_token: str, expires_at: float, account: str = "en"):
    """One-time manual seed - call with the current token and a known Unix
    expiry timestamp (e.g. from the Meta Access Token Debugger) so the very
    first run for this account has something to compare against."""
    _save_token_state(account, access_token, expires_at)
    print(f"[token_refresh] seeded token state for account='{account}', expiry {expires_at} "
          f"(~{(expires_at - time.time()) / 86400:.1f} days from now)")


def ensure_token_fresh(account: str = "en") -> str:
    """
    Call once near the top of hourly_run.py's run() for EACH account in
    use. Resolves the current token from Supabase if one's been stored
    there (i.e. a previous run already refreshed it); otherwise falls
    back to that account's env var (the GitHub Actions / Railway secret)
    as the initial seed. If the resolved token is within
    REFRESH_THRESHOLD_DAYS of expiry (or has no tracked expiry yet),
    refreshes it and saves the new token+expiry back to Supabase so
    every subsequent run - including ones on a fresh GitHub Actions
    container - picks up the refreshed token automatically.

    Returns the token to actually use for this run, and also sets it on
    os.environ[<that account's token env var>] so the rest of the
    pipeline (e.g. instagram_publish.py) doesn't need to change how it
    reads the token.
    """
    cfg = ACCOUNTS[account]
    token_env = cfg["token_env"]

    state = _load_token_state(account)
    token = state.get("access_token") or os.environ.get(token_env)
    expires_at = state.get("expires_at")

    if not token:
        print(f"[token_refresh] {token_env} not set anywhere (account='{account}'), skipping check")
        return ""

    if expires_at is not None:
        days_left = (expires_at - time.time()) / 86400
        print(f"[token_refresh] account='{account}' token has ~{days_left:.1f} days left (tracked in Supabase)")
        if days_left > REFRESH_THRESHOLD_DAYS:
            os.environ[token_env] = token
            return token  # plenty of runway, nothing to do
    else:
        print(f"[token_refresh] account='{account}' - no tracked expiry yet - refreshing now to establish one")

    print(f"[token_refresh] account='{account}' - refreshing token...")
    result = _refresh_token(token)
    if result and "access_token" in result:
        new_token = result["access_token"]
        new_expires_at = time.time() + result.get("expires_in", 5184000)
        _save_token_state(account, new_token, new_expires_at)
        os.environ[token_env] = new_token
        expires_in_days = result.get("expires_in", 0) / 86400
        print(f"[token_refresh] account='{account}' refreshed successfully, new token valid ~{expires_in_days:.0f} days")
        return new_token

    print(f"[token_refresh] account='{account}' refresh failed - current token still in use. "
          f"If it's actually close to expiry, investigate before it lapses "
          f"(no recovery path once expired - full re-auth required).")
    os.environ[token_env] = token
    return token


def ensure_all_tokens_fresh() -> dict:
    """Convenience for callers that want every configured account refreshed
    in one call. Skips (rather than raising) any account with no token set
    at all, e.g. before the Hindi page's env vars have been added yet."""
    results = {}
    for account, cfg in ACCOUNTS.items():
        if not os.environ.get(cfg["token_env"]) and not _load_token_state(account).get("access_token"):
            print(f"[token_refresh] account='{account}' has no token configured yet - skipping")
            continue
        results[account] = ensure_token_fresh(account)
    return results


if __name__ == "__main__":
    ensure_all_tokens_fresh()
