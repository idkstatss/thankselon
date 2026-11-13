#!/usr/bin/env python3
"""
check_x.py

Polls a list of X (Twitter) accounts via cookie-authenticated session (twikit),
and pings a Discord webhook when a new original post/quote is found.

Designed to run unattended via GitHub Actions on a schedule. Behavior:
- Never raises on a per-user failure; logs and moves on so one bad account
  doesn't block the rest or corrupt state.
- Never writes partial/corrupt state.json (atomic write via temp file + rename).
- First run for any given user stores the current latest id WITHOUT posting
  to Discord (so you don't get a flood of historical tweets).
- Skips retweets and (by default) replies; only original posts and quote
  tweets trigger a notification. Configurable via INCLUDE_REPLIES env var.

Environment variables (all set as GitHub Secrets / workflow env):
  X_AUTH_TOKEN      - value of the 'auth_token' cookie from a logged-in X session
  X_CT0             - value of the 'ct0' cookie from the same session
  TARGET_USERS      - comma-separated list of X usernames (no @), e.g. "nasa,spacex"
  DISCORD_WEBHOOK_URL - Discord incoming webhook URL
  DISCORD_ROLE_ID   - (optional) numeric Discord role ID to ping. If unset, no role ping.
  INCLUDE_REPLIES   - (optional) "true" to also notify on replies. Default: "false".
  STATE_FILE        - (optional) path to state file. Default: "state.json".
"""

import asyncio
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import requests
from twikit import Client

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
INCLUDE_REPLIES = os.environ.get("INCLUDE_REPLIES", "false").strip().lower() == "true"

X_AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN", "").strip()
X_CT0 = os.environ.get("X_CT0", "").strip()

TARGET_USERS_RAW = os.environ.get("TARGET_USERS", "").strip()
TARGET_USERS = [u.strip().lstrip("@") for u in TARGET_USERS_RAW.split(",") if u.strip()]


def log(msg: str) -> None:
    print(msg, flush=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                log(f"WARNING: {STATE_FILE} did not contain a JSON object. Starting fresh.")
                return {}
            return data
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARNING: could not read {STATE_FILE} ({e}). Starting fresh in-memory state. "
            f"The on-disk file is left untouched unless we successfully write new state.")
        return {}


def save_state_atomic(state: dict) -> None:
    """Write state.json atomically so a crash mid-write can't corrupt it."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(STATE_FILE.parent) if str(STATE_FILE.parent) else ".",
        prefix=".state_tmp_",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_fxtwitter_url(username: str, tweet_id: str) -> str:
    return f"https://fxtwitter.com/{username}/status/{tweet_id}"


def send_discord_notification(username: str, tweet_id: str, text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        log("ERROR: DISCORD_WEBHOOK_URL is not set; cannot send notification.")
        return False

    url = build_fxtwitter_url(username, tweet_id)
    role_prefix = f"<@&{DISCORD_ROLE_ID}> " if DISCORD_ROLE_ID else ""

    snippet = text.strip()
    if len(snippet) > 300:
        snippet = snippet[:297] + "..."

    content = f"{role_prefix}**New post from @{username}**\n{snippet}\n{url}"

    payload = {"content": content}

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            log(f"ERROR: Discord webhook returned {resp.status_code}: {resp.text[:500]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"ERROR: failed to POST to Discord webhook: {e}")
        return False


def is_eligible_tweet(tweet) -> bool:
    """
    Decide whether a tweet counts as a 'new original post' worth notifying on.
    Skips pure retweets always. Skips replies unless INCLUDE_REPLIES is true.
    Quote tweets are treated as original posts (eligible).
    """
    # twikit tweet objects expose these attributes; retweeted tweets typically
    # surface as a distinct object with `retweeted_tweet` populated, or the
    # text starts with "RT @". We defensively check both.
    text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""

    is_retweet = bool(getattr(tweet, "retweeted_tweet", None)) or text.startswith("RT @")
    if is_retweet:
        return False

    in_reply_to = getattr(tweet, "in_reply_to", None) or getattr(tweet, "in_reply_to_status_id", None)
    if in_reply_to and not INCLUDE_REPLIES:
        return False

    return True


async def fetch_latest_eligible_tweet(client: Client, username: str):
    """
    Returns the most recent eligible (non-RT, non-reply-unless-configured) tweet
    for a user, or None if none found / on failure. Raises nothing; caller should
    still wrap this in try/except for network-layer surprises during the call itself.
    """
    user = await client.get_user_by_screen_name(username)
    if user is None:
        log(f"WARNING: could not resolve user @{username} (account may be suspended/renamed).")
        return None

    tweets = await client.get_user_tweets(user.id, "Tweets", count=10)
    if not tweets:
        return None

    for tweet in tweets:
        if is_eligible_tweet(tweet):
            return tweet

    return None


async def process_user(client: Client, username: str, state: dict) -> dict:
    """
    Checks one user, sends a Discord notification if there's a new eligible tweet,
    and returns an updated copy of this user's state entry. Never raises;
    all errors are caught and logged so one bad account doesn't halt the run.
    """
    user_state = state.get(username, {})
    last_id = user_state.get("last_id")

    try:
        tweet = await fetch_latest_eligible_tweet(client, username)
    except Exception as e:
        log(f"ERROR: failed to fetch tweets for @{username}: {e}")
        traceback.print_exc()
        return user_state  # unchanged; don't touch state on failure

    if tweet is None:
        log(f"INFO: no eligible tweets found for @{username} this run.")
        return user_state

    tweet_id = str(tweet.id)
    tweet_text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""

    if last_id is None:
        # First run for this user: baseline silently, no Discord ping.
        log(f"INFO: first run for @{username}; baselining at tweet {tweet_id} (no notification sent).")
        return {"last_id": tweet_id}

    if tweet_id == last_id:
        log(f"INFO: no new tweet for @{username} (still {tweet_id}).")
        return user_state

    # Defensive: X snowflake IDs are roughly chronological, so a simple
    # integer comparison is a reasonable "is this actually newer" guard
    # against weirdness like deleted/re-fetched tweets.
    try:
        is_newer = int(tweet_id) > int(last_id)
    except ValueError:
        is_newer = True  # if IDs aren't numeric for some reason, trust the API ordering

    if not is_newer:
        log(f"INFO: fetched tweet {tweet_id} for @{username} is not newer than stored {last_id}; skipping.")
        return user_state

    log(f"INFO: new tweet detected for @{username}: {tweet_id}")
    sent = send_discord_notification(username, tweet_id, tweet_text)
    if sent:
        return {"last_id": tweet_id}
    else:
        # Don't advance state if the notification failed to send, so we retry next run.
        log(f"WARNING: notification failed for @{username}; will retry next run without updating state.")
        return user_state


async def main_async() -> int:
    if not X_AUTH_TOKEN or not X_CT0:
        log("FATAL: X_AUTH_TOKEN and X_CT0 must both be set.")
        return 1

    if not TARGET_USERS:
        log("FATAL: TARGET_USERS is empty. Set it to a comma-separated list of usernames.")
        return 1

    state = load_state()
    original_state = json.dumps(state, sort_keys=True)

    client = Client(language="en-US")
    try:
        client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0})
    except Exception as e:
        log(f"FATAL: failed to set session cookies on client: {e}")
        return 1

    for username in TARGET_USERS:
        try:
            updated_entry = await process_user(client, username, state)
            state[username] = updated_entry
        except Exception as e:
            # Absolute last-resort guard: a bug in process_user itself should
            # still not kill the whole run or touch state for this user.
            log(f"ERROR: unexpected failure processing @{username}: {e}")
            traceback.print_exc()
            continue

    new_state = json.dumps(state, sort_keys=True)
    if new_state != original_state:
        try:
            save_state_atomic(state)
            log("INFO: state.json updated.")
        except Exception as e:
            log(f"ERROR: failed to write {STATE_FILE}: {e}")
            traceback.print_exc()
            return 1
    else:
        log("INFO: no state changes this run.")

    return 0


def main() -> None:
    exit_code = asyncio.run(main_async())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
