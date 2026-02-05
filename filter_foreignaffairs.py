# filter_foreignaffairs.py
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

MAX_ENTRIES = 40
SLEEP_SECONDS = 0.8  # sleep on every URL to reduce rate/variant issues

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# You said: drop anything that requires email or subscription (free-open only).
PAYWALL_VISIBLE_TEXT = [
    # premium archives / subscriber prompts
    "This article is part of our premium archives",
    "To continue reading and get full access",
    "Subscribe",

    # email unlock gate (your screenshot)
    "Finish reading this article for free",
    "Enter your email",
    "Get it Now",
    "Already a subscriber? Log In",
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
    """
    Prefer published_parsed (struct_time) -> RFC822.
    Fallback to now if missing.
    """
    tt = entry.get("published_parsed") or entry.get("updated_parsed")
    if tt:
        ts = calendar.timegm(tt)  # treat as UTC
        return format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc))
    return format_datetime(datetime.now(timezone.utc))

def _visible_text_hit(page, text: str) -> bool:
    loc = page.get_by_text(text, exact=False)
    if loc.count() == 0:
        return False
    try:
        return loc.first.is_visible()
    except Exception:
        return False

def detect_gate_visible(page) -> tuple[bool, str]:
    """
    Visible-only gate detection (avoids hidden DOM false positives).
    Returns (paywalled, reason).
    """
    # Strong text triggers, but only if visible
    for s in PAYWALL_VISIBLE_TEXT:
        if _visible_text_hit(page, s):
            return True, f"visible_text:{s[:50]}"

    # Visible email gate structure: email input visible, usually in a dialog/modal
    email_visible = page.locator('input[type="email"]:visible').count() > 0
    if email_visible:
        dialog_visible = page.locator('[role="dialog"]:visible').count() > 0
        get_it_now = _visible_text_hit(page, "Get it Now")
        finish_free = _visible_text_hit(page, "Finish reading this article for free")
        if dialog_visible or get_it_now or finish_free:
            return True, "email_gate_visible"

    return False, "no_visible_gate"

def is_paywalled_playwright(browser, url: str):
    """
    Returns (paywalled: bool, reason: str)
    Uses a fresh context per URL to avoid cookie/meter contamination.
    """
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale="en-US",
    )
    page = context.new_page()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        page.wait_for_timeout(2000)

        pw, reason = detect_gate_visible(page)
        if pw:
            return True, reason

        # Some gates appear after scroll (or after a second of interaction)
        page.evaluate("window.scrollTo(0, 900)")
        page.wait_for_timeout(800)

        pw, reason = detect_gate_visible(page)
        if pw:
            return True, f"{reason}_after_scroll"

        return False, "no_gate_detected"

    except PlaywrightTimeoutError:
        return True, "playwright_timeout_drop"
    except Exception as ex:
        return True, f"playwright_error_drop_{type(ex).__name__}"
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

def write_outputs(items_xml, debug_lines):
    now = format_datetime(datetime.now(timezone.utc))
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Foreign Affairs (free-open only)</title>
    <link>https://www.foreignaffairs.com/</link>
    <description>Filtered RSS feed (drops email-gated + subscriber-gated items). See debug.txt.</description>
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
    debug_lines = [f"run_utc={datetime.now(timezone.utc).isoformat()}"]

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
        return

    items_xml = []
    kept = 0
    dropped_paywalled = 0
    dropped_no_link = 0
    check_errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        try:
            for idx, e in enumerate(feed.entries[:MAX_ENTRIES]):
                link = e.get("link")
                if not link:
                    dropped_no_link += 1
                    continue

                try:
                    pw, reason = is_paywalled_playwright(browser, link)
                    if pw:
                        dropped_paywalled += 1
                        if dropped_paywalled <= 25:
                            debug_lines.append(f"dropped_paywalled_url={link} reason={reason}")
                        time.sleep(SLEEP_SECONDS)
                        continue
                    else:
                        if kept < 5:
                            debug_lines.append(f"kept_probe_url={link} reason={reason}")

                except Exception as ex:
                    check_errors += 1
                    debug_lines.append(f"paywall_check_error_url={link} err={type(ex).__name__}")
                    dropped_paywalled += 1
                    time.sleep(SLEEP_SECONDS)
                    continue

                title = xml_escape(e.get("title", ""))
                desc = xml_escape(e.get("summary", ""))
                pub = xml_escape(to_rfc822_pubdate(e))

                guid = xml_escape(link)

                items_xml.append(f"""
    <item>
      <title>{title}</title>
      <link>{xml_escape(link)}</link>
      <guid isPermaLink="true">{guid}</guid>
      <pubDate>{pub}</pubDate>
      <description>{desc}</description>
    </item>
                """)
                kept += 1
                time.sleep(SLEEP_SECONDS)

        finally:
            try:
                browser.close()
            except Exception:
                pass

    debug_lines.append(f"kept_items={kept}")
    debug_lines.append(f"dropped_paywalled={dropped_paywalled}")
    debug_lines.append(f"dropped_no_link={dropped_no_link}")
    debug_lines.append(f"paywall_check_errors={check_errors}")

    write_outputs(items_xml, debug_lines)

if __name__ == "__main__":
    main()
