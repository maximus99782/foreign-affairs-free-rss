# filter_foreignaffairs.py
import time
import traceback
import requests
import feedparser
import re
import unicodedata
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from email.utils import format_datetime

SOURCE_RSS = "https://www.foreignaffairs.com/rss.xml"
OUTPUT_FILE = "index.xml"
DEBUG_FILE = "debug.txt"

MAX_ENTRIES = 40
SLEEP_SECONDS = 1.0

# If True: if we cannot verify an article (request fails), we DROP it.
# This is stricter and matches your goal "free-only".
FAIL_CLOSED = True

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Foreign Affairs paywall text (based on your screenshot)
MUST_HAVE_PAYWALL_PHRASES = [
    "this article is part of our premium archives",
    "to continue reading and get full access to our entire archive you must subscribe",
    "already a subscriber log in",
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

def _norm_for_matching(s: str) -> str:
    """
    Normalize aggressively so phrase matching survives odd whitespace/punctuation.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"\s+", " ", s)
    # remove punctuation to make "Already a subscriber? Log In →" match reliably
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _html_and_text(url: str):
    """
    Fetch URL and return (status_code, final_url, html_raw, text_norm, html_norm).
    """
    r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    status = r.status_code
    final_url = r.url or url
    html_raw = r.text or ""

    soup = BeautifulSoup(html_raw, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    text_norm = _norm_for_matching(page_text)
    html_norm = _norm_for_matching(html_raw)

    return status, final_url, html_raw, text_norm, html_norm

def is_paywalled(url: str):
    """
    Returns: (bool paywalled, str reason)
    """
    status, final_url, html_raw, text_norm, html_norm = _html_and_text(url)

    # Hard blocks or paywall HTTP codes -> treat as paywalled
    if status in (401, 402, 403):
        return True, f"http_{status}"

    # Redirects to subscribe/login paths -> paywalled
    fu = final_url.lower()
    if any(x in fu for x in ["/subscribe", "/subscription", "/login"]):
        return True, "redirect_subscribe_login"

    # Strong structured signal sometimes present in HTML/JSON-LD:
    # isAccessibleForFree:false
    if re.search(r'"isAccessibleForFree"\s*:\s*false', html_raw):
        return True, "schema_isAccessibleForFree_false"

    # Cicero-style phrase signature (AND)
    must_have = [_norm_for_matching(p) for p in MUST_HAVE_PAYWALL_PHRASES]
    if all(p in text_norm for p in must_have):
        return True, "overlay_must_have_text"

    # Broader variants (sometimes the exact sentence changes)
    if "premium archives" in text_norm and "subscribe" in text_norm and "log in" in text_norm:
        return True, "overlay_combo_text"

    # Sometimes the modal text is present in HTML but not in extracted visible text
    if "premium archives" in html_norm and "subscribe" in html_norm and "log in" in html_norm:
        return True, "overlay_combo_html"

    return False, "no_paywall_signal"

def main():
    debug_lines = []
    debug_lines.append(f"run_utc={datetime.now(timezone.utc).isoformat()}")

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

    for idx, e in enumerate(feed.entries[:MAX_ENTRIES]):
        link = e.get("link")
        if not link:
            dropped_no_link += 1
            continue

        try:
            pw, reason = is_paywalled(link)
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

            if FAIL_CLOSED:
                dropped_paywalled += 1
                continue
            # FAIL_OPEN behavior:
            # keep the article if checks fail (not recommended for "free-only")

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

    debug_lines.append(f"kept_items={kept}")
    debug_lines.append(f"dropped_paywalled={dropped_paywalled}")
    debug_lines.append(f"dropped_no_link={dropped_no_link}")
    debug_lines.append(f"paywall_check_errors={check_errors}")
    debug_lines.append(f"fail_closed={FAIL_CLOSED}")

    write_outputs(items_xml, debug_lines)

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

if __name__ == "__main__":
    main()
