import time
import traceback
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from email.utils import format_datetime

# 1) Pick ONE source that you can fetch without getting blocked.
# If RSS works, prefer RSS. If not, use a "latest" HTML page.
SOURCE_RSS = "https://www.foreignaffairs.com/rss.xml"         # try this first
SOURCE_HTML_FALLBACK = "https://www.foreignaffairs.com/"      # fallback if RSS fails

OUTPUT_FILE = "index.xml"
DEBUG_FILE = "debug.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PAYWALL_MARKERS_ANY = [
    "This article is part of our premium archives",
    "premium archives",
    "To continue reading and get full access to our entire archive, you must subscribe",
    "Subscribe",
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

def fetch_url(url: str, timeout=30):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def fetch_source_feed():
    r = fetch_url(SOURCE_RSS, timeout=30)
    feed = feedparser.parse(r.content)
    return feed, r.status_code, r.headers.get("content-type", "")

def parse_latest_from_html():
    """
    Fallback: scrape links from the homepage (or a latest page).
    This is a heuristic and may need adjustment if their HTML changes.
    """
    r = fetch_url(SOURCE_HTML_FALLBACK, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")

    links = []
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.foreignaffairs.com" + href
        if not href.startswith("https://www.foreignaffairs.com/"):
            continue
        # crude: keep only article-like URLs, drop obvious non-articles
        if any(x in href for x in ["/podcasts/", "/videos/", "/newsletters/", "/events/"]):
            continue
        links.append(href)

    # de-dup while preserving order
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)

    return out[:40], r.status_code, r.headers.get("content-type", "")

import re
import unicodedata

def _norm(s: str) -> str:
    if not s:
        return ""
    # Normalize unicode (e.g., non-breaking spaces), lowercase, collapse whitespace
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_paywalled(url: str) -> bool:
    r = requests.get(url, headers=HEADERS, timeout=25)
    # If they ever return paywall-ish HTTP codes, treat as paywalled.
    if r.status_code in (401, 402, 403):
        return True
    r.raise_for_status()

    html = _norm(r.text)

    # Strong, specific marker from your screenshot
    if "this article is part of our premium archives" in html:
        return True

    # Other strong phrases commonly present in that overlay
    strong = [
        "premium archives",
        "to continue reading and get full access to our entire archive, you must subscribe",
        "already a subscriber? log in",
    ]
    if any(m in html for m in strong):
        return True

    # Backup heuristic: paywall overlay tends to contain BOTH “subscribe” and “log in”
    # (avoid using "subscribe" alone because many free pages show it in the header)
    if ("subscribe" in html) and ("log in" in html) and ("entire archive" in html or "premium" in html):
        return True

    return False

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

    items_xml = []
    kept = dropped_paywalled = dropped_no_link = check_errors = 0

    # Try RSS first
    entries = []
    try:
        feed, status, ctype = fetch_source_feed()
        debug_lines.append(f"source_mode=rss")
        debug_lines.append(f"source_http_status={status}")
        debug_lines.append(f"source_content_type={ctype}")
        debug_lines.append(f"source_entries_count={len(feed.entries)}")
        entries = feed.entries[:40]
    except Exception as e:
        debug_lines.append("WARN_fetch_source_rss_failed")
        debug_lines.append(repr(e))
        debug_lines.append(traceback.format_exc())

    # Fallback to HTML if RSS failed
    if not entries:
        try:
            links, status, ctype = parse_latest_from_html()
            debug_lines.append(f"source_mode=html_fallback")
            debug_lines.append(f"source_http_status={status}")
            debug_lines.append(f"source_content_type={ctype}")
            debug_lines.append(f"source_links_count={len(links)}")

            # Turn links into pseudo-entries
            entries = [{"link": u, "title": u, "summary": ""} for u in links]
        except Exception as e:
            debug_lines.append("ERROR_fetch_source_html_failed")
            debug_lines.append(repr(e))
            debug_lines.append(traceback.format_exc())
            write_outputs([], debug_lines)
            return

    for e in entries:
        link = e.get("link")
        if not link:
            dropped_no_link += 1
            continue

        try:
            if is_paywalled(link):
                dropped_paywalled += 1
                if dropped_paywalled <= 15:
                    debug_lines.append(f"dropped_paywalled_url={link}")
                continue

        except Exception:
            # fail-open so the feed doesn't go empty if checks are blocked intermittently
            check_errors += 1

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
        time.sleep(1.0)

    debug_lines.append(f"kept_items={kept}")
    debug_lines.append(f"dropped_paywalled={dropped_paywalled}")
    debug_lines.append(f"dropped_no_link={dropped_no_link}")
    debug_lines.append(f"paywall_check_errors={check_errors}")

    write_outputs(items_xml, debug_lines)

if __name__ == "__main__":
    main()
