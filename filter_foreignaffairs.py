# filter_foreignaffairs.py
# Goal: publish ONLY “free-open” Foreign Affairs items (no email gate, no subscription gate),
# with a conservative rule: an item must be detected free-open in N consecutive runs
# before it is emitted into index.xml.

import json
import time
import traceback
import calendar
import requests
import feedparser

from datetime import datetime, timezone
from email.utils import format_datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


SOURCE_RSS = "https://www.foreignaffairs.com/rss.xml"
OUTPUT_FILE = "index.xml"
DEBUG_FILE = "debug.txt"
STATE_FILE = "state.json"

MAX_ENTRIES = 40

# Sleep every URL check (kept or dropped) to reduce “botty” behavior
SLEEP_SECONDS = 0.8

# Conservative: require this many consecutive runs “FREE” before publishing
CONFIRM_FREE_RUNS = 2

# Positive proof heuristic: if visible article body is below this, treat as gated/preview
MIN_VISIBLE_WORDS = 700

# Prune state for URLs not seen in last X runs (hourly schedule: 168 = 7 days)
PRUNE_AFTER_RUNS = 168

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Avoid “Subscribe” alone (it can appear in nav/footer). Use stronger phrases.
PAYWALL_PHRASES = [
    "Finish reading this article for free",
    "Enter your email",
    "Get it Now",
    "Already a subscriber? Log In",
    "This article is part of our premium archives",
    "To continue reading and get full access",
    "Get unlimited access to all Foreign Affairs",
]


def xml_escape(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def fetch_source_feed():
    r = requests.get(SOURCE_RSS, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return feedparser.parse(r.content), r.status_code, r.headers.get("content-type", "")


def to_rfc822_pubdate(entry) -> str:
    tt = entry.get("published_parsed") or entry.get("updated_parsed")
    if tt:
        ts = calendar.timegm(tt)
        return format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc))
    return format_datetime(datetime.now(timezone.utc))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"__meta__": {"run_seq": 0}}
            if "__meta__" not in data:
                data["__meta__"] = {"run_seq": 0}
            if "run_seq" not in data["__meta__"]:
                data["__meta__"]["run_seq"] = 0
            return data
    except FileNotFoundError:
        return {"__meta__": {"run_seq": 0}}
    except Exception:
        # If state is corrupted, start fresh rather than fail.
        return {"__meta__": {"run_seq": 0}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def visible_wordcount(page) -> int:
    """
    Count visible words in the article body. If this is small, you are probably seeing a preview.
    We try a few selectors; take the max.
    """
    selectors = [
        "article p:visible",
        "main article p:visible",
        "main p:visible",
    ]

    best = 0
    for sel in selectors:
        loc = page.locator(sel)
        n = loc.count()
        if n == 0:
            continue
        total = 0
        # cap work
        for i in range(min(n, 120)):
            try:
                t = loc.nth(i).inner_text(timeout=2000).strip()
            except Exception:
                continue
            if t:
                total += len(t.split())
        if total > best:
            best = total

    return best


def has_visible_phrase(page, phrase: str) -> bool:
    loc = page.get_by_text(phrase, exact=False)
    if loc.count() == 0:
        return False
    try:
        return loc.first.is_visible()
    except Exception:
        return False


def is_gated_free_open_only(page) -> tuple[bool, str]:
    """
    Returns (gated, reason). Gated includes email-gated and subscriber-gated.
    """
    # 1) Visible phrase checks (only if visible to avoid hidden DOM false positives)
    for p in PAYWALL_PHRASES:
        if has_visible_phrase(page, p):
            return True, f"visible_phrase:{p[:60]}"

    # 2) Visible email input in a dialog-ish context
    email_visible = page.locator('input[type="email"]:visible').count() > 0
    if email_visible:
        dialog_visible = page.locator('[role="dialog"]:visible').count() > 0
        if dialog_visible:
            return True, "email_gate_visible_dialog"
        # still treat as gate if it pairs with the common CTA text
        if has_visible_phrase(page, "Get it Now") or has_visible_phrase(page, "Finish reading this article for free"):
            return True, "email_gate_visible_cta"

    # 3) Positive proof: must have enough visible body words
    words = visible_wordcount(page)
    if words < MIN_VISIBLE_WORDS:
        return True, f"content_too_short_words={words}"

    return False, f"free_open_words={words}"


def check_url_free_open(browser, url: str) -> tuple[bool, str]:
    """
    Returns (is_free_open, reason). Uses a fresh context to reduce cookie/meter contamination.
    """
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="en-US",
    )
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        page.wait_for_timeout(2000)

        gated, reason = is_gated_free_open_only(page)
        if gated:
            return False, reason

        # Some gates appear after scroll
        page.evaluate("window.scrollTo(0, 1000)")
        page.wait_for_timeout(900)

        gated, reason = is_gated_free_open_only(page)
        if gated:
            return False, f"{reason}_after_scroll"

        return True, reason

    except PlaywrightTimeoutError:
        return False, "playwright_timeout_drop"
    except Exception as ex:
        return False, f"playwright_error_drop_{type(ex).__name__}"
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass


def prune_state(state: dict, run_seq: int):
    keys = [k for k in state.keys() if k != "__meta__"]
    cutoff = run_seq - PRUNE_AFTER_RUNS
    for k in keys:
        last_seen = state.get(k, {}).get("last_seen_seq", -10**9)
        if last_seen < cutoff:
            state.pop(k, None)


def write_outputs(items_xml, debug_lines):
    now = format_datetime(datetime.now(timezone.utc))
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Foreign Affairs (free-open only)</title>
    <link>https://www.foreignaffairs.com/</link>
    <description>Filtered RSS: only items that are free-open (no email gate, no subscription), confirmed across runs.</description>
    <lastBuildDate>{now}</lastBuildDate>
    {''.join(items_xml)}
  </channel>
</rss>
"""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss)

    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(debug_lines) + "\n")


def main():
    now_utc = datetime.now(timezone.utc).isoformat()
    debug_lines = [f"run_utc={now_utc}"]

    state = load_state()
    run_seq = int(state.get("__meta__", {}).get("run_seq", 0)) + 1
    state["__meta__"] = {"run_seq": run_seq, "last_run_utc": now_utc}
    debug_lines.append(f"run_seq={run_seq}")

    try:
        feed, status, ctype = fetch_source_feed()
        debug_lines.append("source_mode=rss")
        debug_lines.append(f"source_http_status={status}")
        debug_lines.append(f"source_content_type={ctype}")
        debug_lines.append(f"source_entries_count={len(feed.entries)}")
    except Exception as e:
        debug_lines.append("ERROR_fetch_source_feed")
        debug_lines.append(repr(e))
        debug_lines.append(traceback.format_exc())
        write_outputs([], debug_lines)
        save_state(state)
        return

    checked = 0
    free_now = 0
    gated_now = 0
    published = 0
    errors = 0

    # Track items eligible for output (must be in current RSS + confirmed free across runs)
    items_xml = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            for e in feed.entries[:MAX_ENTRIES]:
                url = e.get("link")
                if not url:
                    continue

                checked += 1
                title = e.get("title", "")
                pub_rfc822 = to_rfc822_pubdate(e)

                is_free, reason = check_url_free_open(browser, url)

                rec = state.get(url, {})
                prev_status = rec.get("last_status")
                prev_seen_seq = rec.get("last_seen_seq", -1)
                prev_streak = int(rec.get("free_streak", 0))

                if is_free:
                    free_now += 1
                    # consecutive means seen in immediately previous run and was FREE
                    if prev_status == "FREE" and prev_seen_seq == (run_seq - 1):
                        streak = prev_streak + 1
                    else:
                        streak = 1

                    state[url] = {
                        "title": title,
                        "last_status": "FREE",
                        "last_reason": reason,
                        "free_streak": streak,
                        "last_seen_seq": run_seq,
                        "last_seen_utc": now_utc,
                    }

                    if streak >= CONFIRM_FREE_RUNS:
                        # emit into RSS
                        items_xml.append(f"""
    <item>
      <title>{xml_escape(title)}</title>
      <link>{xml_escape(url)}</link>
      <guid isPermaLink="true">{xml_escape(url)}</guid>
      <pubDate>{xml_escape(pub_rfc822)}</pubDate>
      <description>{xml_escape(e.get("summary", ""))}</description>
    </item>
                        """)
                        published += 1

                    if free_now <= 10:
                        debug_lines.append(f"free_url={url} streak={state[url]['free_streak']} reason={reason}")

                else:
                    gated_now += 1
                    state[url] = {
                        "title": title,
                        "last_status": "GATED",
                        "last_reason": reason,
                        "free_streak": 0,
                        "last_seen_seq": run_seq,
                        "last_seen_utc": now_utc,
                    }
                    if gated_now <= 25:
                        debug_lines.append(f"dropped_gated_url={url} reason={reason}")

                time.sleep(SLEEP_SECONDS)

        except Exception as ex:
            errors += 1
            debug_lines.append(f"ERROR_main_loop err={type(ex).__name__}")
            debug_lines.append(traceback.format_exc())
        finally:
            try:
                browser.close()
            except Exception:
                pass

    prune_state(state, run_seq)

    debug_lines.append(f"checked_urls={checked}")
    debug_lines.append(f"free_now={free_now}")
    debug_lines.append(f"gated_now={gated_now}")
    debug_lines.append(f"published_items={published}")
    debug_lines.append(f"errors={errors}")

    write_outputs(items_xml, debug_lines)
    save_state(state)


if __name__ == "__main__":
    main()
