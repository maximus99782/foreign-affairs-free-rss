import time
import traceback
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from email.utils import format_datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SOURCE_RSS = "https://www.foreignaffairs.com/rss.xml"
OUTPUT_FILE = "index.xml"
DEBUG_FILE = "debug.txt"

MAX_ENTRIES = 40
SLEEP_SECONDS = 0.3  # Playwright is heavy; keep this small

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# The exact overlay text you showed (plus a few robust helpers)
PAYWALL_MUST_HAVE_ANY = [
    "This article is part of our premium archives",
]
PAYWALL_HELPERS = [
    "premium archives",
    "To continue reading and get full access to our entire archive, you must subscribe",
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

def is_paywalled_playwright(context, url: str):
    """
    Returns (paywalled: bool, reason: str)
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        # Give JS a moment to render overlays
        page.wait_for_timeout(1500)

        body_text = page.inner_text("body")

        # Strong signal: exact sentence
        for s in PAYWALL_MUST_HAVE_ANY:
            if s in body_text:
                return True, "overlay_exact"

        # Backup: combination check (reduces false positives)
        hits = sum(1 for s in PAYWALL_HELPERS if s in body_text)
        if hits >= 2:
            return True, "overlay_combo"

        return False, "no_overlay_text"
    except PlaywrightTimeoutError:
        # Fail-closed: if we cannot render/verify, drop it (free-only goal)
        return True, "playwright_timeout_drop"
    except Exception as ex:
        return True, f"playwright_error_drop_{type(ex).__name__}"
    finally:
        page.close()

def write_outputs(items_xml, debug_lines):
    now = format_datetime(datetime.now(timezone.utc))
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Foreign Affairs (free-only-ish)</title>
    <link>https://www.foreignaffairs.com/</link>
    <description>Filtered RSS feed; see debug.txt for run details</description>
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
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )

        try:
            for idx, e in enumerate(feed.entries[:MAX_ENTRIES]):
                link = e.get("link")
                if not link:
                    dropped_no_link += 1
                    continue

                try:
                    pw, reason = is_paywalled_playwright(context, link)
                    if pw:
                        dropped_paywalled += 1
                        if dropped_paywalled <= 25:
                            debug_lines.append(f"dropped_paywalled_url={link} reason={reason}")
                        continue
                    else:
                        if idx < 5:
                            debug_lines.append(f"kept_probe_url={link} reason={reason}")

                except Exception as ex:
                    check_errors += 1
                    debug_lines.append(f"paywall_check_error_url={link} err={type(ex).__name__}")
                    # fail-closed
                    dropped_paywalled += 1
                    continue

                title = xml_escape(e.get("title", ""))
                desc = xml_escape(e.get("summary", ""))
                pub = xml_escape(e.get("published", ""))

                items_xml.append(f"""
    <item>
      <title>{title}</title>
      <link>{xml_escape(link)}</link>
      <pubDate>{pub}</pubDate>
      <description>{desc}</description>
    </item>
                """)
                kept += 1
                time.sleep(SLEEP_SECONDS)

        finally:
            context.close()
            browser.close()

    debug_lines.append(f"kept_items={kept}")
    debug_lines.append(f"dropped_paywalled={dropped_paywalled}")
    debug_lines.append(f"dropped_no_link={dropped_no_link}")
    debug_lines.append(f"paywall_check_errors={check_errors}")

    write_outputs(items_xml, debug_lines)

if __name__ == "__main__":
    main()
