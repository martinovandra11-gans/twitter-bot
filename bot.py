#!/usr/bin/env python3
"""
X (Twitter) reply-thread bot (free-friendly)
- Membaca daftar replies dari file (default: replies.txt)
- Membalas secara beruntun (thread) ke 1 post (tweet) tertentu
- Anti-spam & anti-limit:
  * Maksimum n tweet per hari (konfigurable)
  * Jeda minimum antartweet (konfigurable)
  * Batas maksimum per-thread (mis. stop setelah 6–8 blok)
  * Random delay 15–45 detik antar-chunk (tweet yang terbelah karena >280)
  * Hanya post 1 blok reply per eksekusi (disarankan via cron/scheduler)
  * Exponential backoff saat error/limit
- Menyimpan state di folder .state (akan di-commit & push oleh workflow agar persist)

ENV yang penting:
- X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET (wajib)
- PARENT_TWEET_ID: ID tweet awal yang ingin dibalas (opsional jika PARENT_TEXT dipakai)
- PARENT_TEXT: jika diisi & belum ada state parent, bot akan membuat tweet awal lalu gunakan sebagai parent
- REPLIES_FILE: default "replies.txt"
- MAX_TWEETS_PER_DAY: default 12 (disarankan < limit free tier)
- MIN_GAP_MINUTES: default 20 (jangan terlalu rapat)
- MIN_GAP_SECONDS: default 0 (jika >0, dipakai alih-alih menit)
- GAP_JITTER_SECONDS: default 0 (toleransi acak 0..N detik)
- POST_PER_RUN: default 1 (sangat disarankan 1 untuk aman)
- HOURS_ACTIVE: contoh "08-21" (UTC); bot hanya aktif di jam tersebut
- MAX_REPLIES_PER_THREAD: default 8 (hentikan setelah 8 blok di replies.txt)
- CHUNK_DELAY_MIN_SECONDS: default 15
- CHUNK_DELAY_MAX_SECONDS: default 45
- DRY_RUN: "1" untuk simulasi tanpa posting
"""
import os, json, time, pathlib, datetime, re, sys, random
from typing import List
import tweepy

# ---------- Helpers for ENV ----------
def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def env_str(name: str, default: str) -> str:
    return os.getenv(name, default)

# ---------- Config ----------
API_KEY = env_str("X_API_KEY", "C78ZGAu2tvgZ6U5qlZRhyEv1n")
API_SECRET = env_str("X_API_SECRET", "W6MrXLBXWt0UfpWHzzojfCyQw6dBn570Gbow4bsWm7xRmXtlvW")
ACCESS_TOKEN = env_str("X_ACCESS_TOKEN", "1784182425192681472-ICFBv3Yk8uq0bp4tubrZ2KLoOdq5ln")
ACCESS_SECRET = env_str("X_ACCESS_SECRET", "GjdBzevoGgYi7U2lMy0rSKCKsyVae5oNRtLVI649A0clm")

REPLIES_FILE = env_str("REPLIES_FILE", "replies.txt")
MAX_TWEETS_PER_DAY = env_int("MAX_TWEETS_PER_DAY", 12)
MIN_GAP_MINUTES = env_int("MIN_GAP_MINUTES", 20)
MIN_GAP_SECONDS = env_int("MIN_GAP_SECONDS", 0)
GAP_JITTER_SECONDS = env_int("GAP_JITTER_SECONDS", 0)  # toleransi acak 0..N detik
POST_PER_RUN = env_int("POST_PER_RUN", 1)
HOURS_ACTIVE = env_str("HOURS_ACTIVE", "")  # e.g., "08-21" in UTC
MAX_REPLIES_PER_THREAD = env_int("MAX_REPLIES_PER_THREAD", 8)
CHUNK_DELAY_MIN_SECONDS = env_int("CHUNK_DELAY_MIN_SECONDS", 15)
CHUNK_DELAY_MAX_SECONDS = env_int("CHUNK_DELAY_MAX_SECONDS", 45)
DRY_RUN = env_str("DRY_RUN", "0") == "1"

PARENT_TWEET_ID = env_str("PARENT_TWEET_ID", "").strip()
PARENT_TEXT = env_str("PARENT_TEXT", "").strip()

STATE_DIR = pathlib.Path(".state")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"

# ---------- Client ----------
def get_client() -> tweepy.Client:
    if not (API_KEY and API_SECRET and ACCESS_TOKEN and ACCESS_SECRET):
        print("ERROR: Missing API creds. Set X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_SECRET.", file=sys.stderr)
        sys.exit(2)
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_SECRET,
        wait_on_rate_limit=True
    )

# ---------- State ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "parent_tweet_id": None,
        "next_index": 0,
        "last_post_ts": 0.0,
        "today": "",
        "count_today": 0,
        "last_error_cooldown_sec": 0
    }

def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2))

def reset_daily_if_needed(st):
    today = datetime.date.today().isoformat()
    if st.get("today") != today:
        st["today"] = today
        st["count_today"] = 0

# ---------- Parsing replies file ----------
SEP_PATTERNS = [
    r"^\s*$",                         # blank line
    r"^\s*lanjut.*$",                 # lines that start with 'lanjut'
    r"^\s*---+\s*$"                   # '---' separators
]
SEP_RE = re.compile("|".join(SEP_PATTERNS), re.IGNORECASE)

def parse_reply_blocks(text: str) -> List[str]:
    blocks = []
    cur = []
    for ln in text.splitlines():
        if SEP_RE.match(ln):
            if cur and any(s.strip() for s in cur):
                blocks.append("\n".join(cur).strip())
                cur = []
            continue
        cur.append(ln.rstrip())
    if cur and any(s.strip() for s in cur):
        blocks.append("\n".join(cur).strip())
    return blocks

def load_replies(path: str) -> List[str]:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"Replies file not found: {p.resolve()}", file=sys.stderr)
        return []
    text = p.read_text(encoding="utf-8")
    blocks = parse_reply_blocks(text)
    return blocks

# ---------- Utilities ----------
def in_active_hours() -> bool:
    if not HOURS_ACTIVE:
        return True
    try:
        s, e = HOURS_ACTIVE.split("-")
        start_h = int(s); end_h = int(e)
        now_h = datetime.datetime.utcnow().hour
        if start_h <= end_h:
            return start_h <= now_h < end_h
        # overnight window like "21-06"
        return now_h >= start_h or now_h < end_h
    except Exception:
        return True

def can_post_now(st) -> bool:
    # Hitung gap minimal dalam detik (seconds overrules minutes jika >0)
    base_gap_sec = max(0, MIN_GAP_SECONDS) if 'MIN_GAP_SECONDS' in globals() else 0
    if base_gap_sec <= 0:
        base_gap_sec = max(0, MIN_GAP_MINUTES * 60)
    jitter = 0
    try:
        jitter = random.uniform(0, max(0, GAP_JITTER_SECONDS))
    except Exception:
        jitter = 0
    required_gap_sec = base_gap_sec + jitter
    # Daily cap
    if st["count_today"] >= MAX_TWEETS_PER_DAY:
        print("Daily cap reached. Skip.", file=sys.stderr)
        return False
    # Gap check
    now = time.time()
    if st["last_post_ts"]:
        gap_sec = (now - st["last_post_ts"])
        if gap_sec < required_gap_sec:
            # Visualize both minutes and seconds for clarity
            print(f"Respecting MIN_GAP: {gap_sec:.1f}s < {required_gap_sec:.1f}s. Skip.", file=sys.stderr)
            return False
    # Error cooldown backoff
    if st.get("last_error_cooldown_sec", 0) > 0:
        if now < st["last_error_cooldown_sec"]:
            wait_left = int(st["last_error_cooldown_sec"] - now)
            print(f"In cooldown ({wait_left}s left). Skip.", file=sys.stderr)
            return False
        else:
            st["last_error_cooldown_sec"] = 0
    return True

def exponential_backoff_seconds(attempt: int) -> int:
    # 30s, 60s, 120s ... + jitter
    base = 30 * (2 ** max(0, attempt-1))
    return int(base + random.uniform(0, 10))

def chunk_text(t: str, max_len: int = 270) -> List[str]:
    # Split long text into chunks under 280 chars (safe margin)
    if len(t) <= max_len:
        return [t]
    parts = []
    while t:
        if len(t) <= max_len:
            parts.append(t)
            break
        # try split at last whitespace before limit
        cut = t.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(t[:cut].strip())
        t = t[cut:].lstrip()
    return parts

def random_chunk_delay() -> float:
    lo = max(0, CHUNK_DELAY_MIN_SECONDS)
    hi = max(lo, CHUNK_DELAY_MAX_SECONDS)
    return random.uniform(lo, hi)

# ---------- Main ----------
def main():
    st = load_state()
    reset_daily_if_needed(st)
    if not in_active_hours():
        print("Outside active hours. Skip.")
        save_state(st); return

    client = get_client()

    # Determine parent
    parent_id = st.get("parent_tweet_id")
    env_parent = PARENT_TWEET_ID or None

    if not parent_id and env_parent:
        parent_id = env_parent
        st["parent_tweet_id"] = parent_id
        st["next_index"] = 0  # reset thread progress when new parent is set
        save_state(st)

    if not parent_id and PARENT_TEXT:
        # create the parent tweet once
        if DRY_RUN:
            print(f"[DRY_RUN] Would create parent tweet: {PARENT_TEXT!r}")
            parent_id = "DRY_PARENT_ID"
        else:
            resp = client.create_tweet(text=PARENT_TEXT)
            parent_id = str(resp.data["id"])
            print(f"Created parent tweet: {parent_id}")
        st["parent_tweet_id"] = parent_id
        st["next_index"] = 0
        save_state(st)

    if not parent_id:
        print("No parent tweet specified. Set PARENT_TWEET_ID or PARENT_TEXT.", file=sys.stderr)
        return

    replies = load_replies(REPLIES_FILE)
    if not replies:
        print("No replies to post. Exit.")
        return

    next_i = int(st.get("next_index", 0))

    # Enforce per-thread cap
    if next_i >= MAX_REPLIES_PER_THREAD:
        print(f"Per-thread cap reached (MAX_REPLIES_PER_THREAD={MAX_REPLIES_PER_THREAD}). Nothing to do.")
        return

    # Also don't exceed number of blocks available
    if next_i >= len(replies):
        print("All replies in file posted. Nothing to do.")
        return

    if not can_post_now(st):
        save_state(st); return

    # Post up to POST_PER_RUN reply blocks, but not beyond per-thread cap
    posted = 0
    attempt = 0
    while posted < POST_PER_RUN and next_i < len(replies) and next_i < MAX_REPLIES_PER_THREAD:
        text_block = replies[next_i].strip()
        chunks = chunk_text(text_block)

        try:
            last_id = parent_id
            for idx, ch in enumerate(chunks):
                if DRY_RUN:
                    print(f"[DRY_RUN] Would reply (part {idx+1}/{len(chunks)}) to {last_id}: {ch!r}")
                else:
                    resp = client.create_tweet(text=ch, in_reply_to_tweet_id=last_id)
                    last_id = str(resp.data["id"])
                # random delay antar-chunk (kecuali setelah chunk terakhir)
                if idx < len(chunks) - 1:
                    d = random_chunk_delay()
                    if DRY_RUN:
                        print(f"[DRY_RUN] Sleeping ~{d:.0f}s between chunks")
                    else:
                        time.sleep(d)

            posted += 1
            next_i += 1
            st["next_index"] = next_i
            st["last_post_ts"] = time.time()
            st["count_today"] = int(st.get("count_today", 0)) + len(chunks)
            save_state(st)
            print(f"Posted reply block #{next_i} (chunks={len(chunks)}).")
        except tweepy.TweepyException as e:
            attempt += 1
            wait = exponential_backoff_seconds(attempt)
            st["last_error_cooldown_sec"] = time.time() + wait
            save_state(st)
            print(f"Error posting: {e}. Backing off for {wait}s.", file=sys.stderr)
            break

if __name__ == "__main__":
    main()