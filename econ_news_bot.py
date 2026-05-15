"""
Economic News Bot
-----------------
Fetches today's top economic news from major RSS feeds (Reuters, Bloomberg, FT, WSJ,
CNBC, etc.), ranks the 10 most impactful headlines, and emails a digest.

Designed to be triggered by cron / Task Scheduler twice a day (07:00 and 21:00).

Configure via the .env file -- see .env.example.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import Iterable

import feedparser
from dateutil import parser as date_parser
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("econ_news_bot")


# RSS sources. Each entry is (display_name, url, source_weight 0-1).
# Source weight reflects editorial authority; tweak to taste.
RSS_SOURCES: list[tuple[str, str, float]] = [
    ("Reuters Business",   "https://feeds.reuters.com/reuters/businessNews",          1.0),
    ("Reuters Markets",    "https://feeds.reuters.com/reuters/USMarketsNews",         1.0),
    ("FT Home",            "https://www.ft.com/?format=rss",                          1.0),
    ("FT World",           "https://www.ft.com/world?format=rss",                     0.9),
    ("Bloomberg Markets",  "https://feeds.bloomberg.com/markets/news.rss",            1.0),
    ("Bloomberg Economics","https://feeds.bloomberg.com/economics/news.rss",          1.0),
    ("WSJ Markets",        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",           0.95),
    ("WSJ World",          "https://feeds.a.dj.com/rss/RSSWorldNews.xml",             0.85),
    ("CNBC Economy",       "https://www.cnbc.com/id/20910258/device/rss/rss.html",    0.85),
    ("CNBC Finance",       "https://www.cnbc.com/id/10000664/device/rss/rss.html",    0.85),
    ("BBC Business",       "https://feeds.bbci.co.uk/news/business/rss.xml",          0.8),
    ("The Economist Finance","https://www.economist.com/finance-and-economics/rss.xml",0.9),
    ("Yahoo Finance",      "https://finance.yahoo.com/news/rssindex",                 0.7),
    ("MarketWatch Top",    "https://feeds.marketwatch.com/marketwatch/topstories/",   0.8),
]


# ============================================================
# Category A: Global economy & US economy (macro / policy)
# Used to pick the FIRST 5 headlines.
# ============================================================
MACRO_KEYWORDS: dict[str, float] = {
    # Monetary policy / central banks
    "federal reserve": 3.0, "fed ": 2.5, "fomc": 3.0, "powell": 2.5,
    "ecb": 2.5, "bank of england": 2.5, "boj": 2.5, "bank of japan": 2.5,
    "pboc": 2.5, "rate hike": 3.0, "rate cut": 3.0, "interest rate": 2.5,
    "monetary policy": 2.5, "quantitative": 2.0,
    # Inflation / prices
    "inflation": 3.0, "cpi": 3.0, "ppi": 2.5, "deflation": 2.5,
    "core inflation": 3.0, "consumer price": 2.5,
    # Employment
    "jobs report": 3.0, "nonfarm payroll": 3.0, "unemployment": 2.5,
    "jobless claims": 2.0, "labor market": 2.0, "wages": 1.5,
    # Growth
    "gdp": 3.0, "recession": 3.0, "economic growth": 2.0, "pmi": 2.0,
    "manufacturing": 1.5, "retail sales": 2.0, "consumer spending": 2.0,
    # Markets (broad indices, yields, currency -- macro signals)
    "stock market": 1.5, "s&p 500": 2.0, "nasdaq": 1.5, "dow jones": 1.5,
    "bond yield": 2.5, "treasury yield": 2.5, "yield curve": 2.5,
    "credit spread": 2.0, "dollar": 1.5, "currency": 1.5, "forex": 1.5,
    "euro": 1.0, "yuan": 1.5, "yen": 1.5,
    # Commodities (macro-relevant)
    "oil price": 2.0, "crude oil": 2.0, "gold price": 1.5, "opec": 2.0,
    # Banking / sovereign / fiscal
    "banking crisis": 3.5, "bank failure": 3.5, "default": 2.5,
    "debt ceiling": 3.0, "sovereign debt": 2.5, "credit rating": 2.0,
    "fiscal policy": 2.0, "budget deficit": 2.0, "treasury": 2.0,
    # US-specific
    "white house": 1.5, "us economy": 2.0, "american economy": 2.0,
    "biden": 1.0, "trump": 1.0, "congress": 1.0,
}

# ============================================================
# Category B: Corporate, innovation & geopolitics
# Used to pick the OTHER 5 headlines.
# ============================================================
CORPORATE_GEO_KEYWORDS: dict[str, float] = {
    # Corporate / deals
    "earnings": 2.0, "merger": 2.5, "acquisition": 2.5, "ipo": 2.0,
    "bankruptcy": 2.5, "layoff": 2.0, "ceo": 1.5, "buyback": 1.5,
    "revenue": 1.0, "profit": 1.0, "guidance": 1.5,
    # Big tech / specific giant companies
    "apple": 2.0, "microsoft": 2.0, "google": 2.0, "alphabet": 2.0,
    "amazon": 2.0, "meta": 2.0, "nvidia": 2.5, "tesla": 2.0,
    "openai": 2.5, "anthropic": 2.0, "tsmc": 2.5, "samsung": 2.0,
    # Innovation / tech
    "artificial intelligence": 2.5, "ai chip": 2.5, "generative ai": 2.5,
    "semiconductor": 2.5, "chip ": 2.0, "quantum": 2.0,
    "robotics": 1.5, "automation": 1.5, "biotech": 2.0, "gene therapy": 1.5,
    "ev ": 2.0, "electric vehicle": 2.0, "battery": 1.5,
    "renewable": 1.5, "clean energy": 1.5, "fusion": 1.5,
    "cybersecurity": 1.5, "data breach": 1.5, "cloud computing": 1.5,
    "startup": 1.0, "venture capital": 1.5, "funding round": 1.5,
    # Geopolitics with economic / market impact
    "tariff": 2.5, "trade war": 3.0, "sanctions": 2.5, "trade deal": 2.5,
    "supply chain": 2.0, "china": 1.5, "russia": 1.5, "ukraine": 1.5,
    "middle east": 1.5, "israel": 1.5, "iran": 1.5, "taiwan": 2.0,
    "north korea": 1.5, "european union": 1.0, "g7": 1.5, "g20": 1.5,
    "export control": 2.0, "national security": 1.5,
}

# Combined dictionary for the relevance filter (any keyword in either category
# means the article is potentially relevant)
ALL_KEYWORDS: dict[str, float] = {**MACRO_KEYWORDS, **CORPORATE_GEO_KEYWORDS}

# How many of each category to include in the digest
MACRO_COUNT = int(os.getenv("MACRO_COUNT", "5"))
CORPORATE_GEO_COUNT = int(os.getenv("CORPORATE_GEO_COUNT", "5"))


# Window of "today's" news. We look back this many hours from now.
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "14"))

# How many headlines to include in the email
TOP_N = int(os.getenv("TOP_N", "10"))

# Max characters of article summary to include in the email body.
# RSS feeds vary -- Reuters/Bloomberg give 1-2 sentences, FT gives a paragraph.
# 600 is roughly 3-4 sentences (a meaningful blurb without bloating the email).
SUMMARY_CHAR_LIMIT = int(os.getenv("SUMMARY_CHAR_LIMIT", "600"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    link: str
    summary: str
    source: str
    source_weight: float
    published: datetime
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    # Which bucket the article belongs to: "macro" or "corporate_geo"
    category: str = ""
    macro_score: float = 0.0
    corp_geo_score: float = 0.0


# ---------------------------------------------------------------------------
# Fetch + score
# ---------------------------------------------------------------------------

def parse_published(entry) -> datetime | None:
    """Best-effort parse of an RSS entry's publish date into UTC."""
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if not val:
            continue
        try:
            dt = date_parser.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    # Fall back to feedparser's struct_time if available
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_feed(name: str, url: str, weight: float, cutoff: datetime) -> list[NewsItem]:
    """Fetch one RSS feed and return items newer than cutoff."""
    log.info("Fetching %s", name)
    items: list[NewsItem] = []
    try:
        feed = feedparser.parse(
            url,
            request_headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; econ-news-bot/1.0; "
                    "+https://example.com/bot)"
                ),
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
    except Exception as e:
        log.warning("  %s: fetch error: %s", name, e)
        return items

    if feed.bozo and not feed.entries:
        log.warning("  %s: feed parse error, no entries", name)
        return items

    for entry in feed.entries:
        pub = parse_published(entry)
        if pub is None or pub < cutoff:
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        summary = (entry.get("summary") or entry.get("description") or "").strip()
        # feedparser leaves HTML in summary -- crude strip
        # Keep up to ~1200 chars so we can show longer blurbs in the email
        summary = _strip_html(summary)[:1200]
        link = entry.get("link") or ""
        items.append(
            NewsItem(
                title=title,
                link=link,
                summary=summary,
                source=name,
                source_weight=weight,
                published=pub,
            )
        )
    log.info("  %s: %d recent items", name, len(items))
    return items


def _strip_html(text: str) -> str:
    """Quick-and-dirty HTML stripper for RSS summaries."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def score_item(item: NewsItem) -> None:
    """Score a NewsItem in place. Computes scores for BOTH categories,
    then assigns the item to whichever category it scores higher in."""
    haystack = (item.title + " " + item.summary).lower()
    title_lower = item.title.lower()

    def _score_against(kw_dict: dict[str, float]) -> tuple[float, list[str]]:
        total = 0.0
        matched: list[str] = []
        for kw, weight in kw_dict.items():
            if kw in haystack:
                multiplier = 2.0 if kw in title_lower else 1.0
                total += weight * multiplier
                matched.append(kw)
        return total, matched

    macro_raw, macro_matched = _score_against(MACRO_KEYWORDS)
    corp_raw, corp_matched = _score_against(CORPORATE_GEO_KEYWORDS)

    # Recency bonus: linearly decays over the lookback window
    now = datetime.now(timezone.utc)
    age_hours = (now - item.published).total_seconds() / 3600
    recency = max(0.0, 1.0 - age_hours / LOOKBACK_HOURS)

    # Apply source authority + recency multipliers to both scores
    multiplier = (0.6 + 0.4 * item.source_weight) * (0.7 + 0.3 * recency)
    item.macro_score = macro_raw * multiplier
    item.corp_geo_score = corp_raw * multiplier

    # Assign to whichever category scored higher
    if item.macro_score >= item.corp_geo_score:
        item.category = "macro"
        item.score = item.macro_score
    else:
        item.category = "corporate_geo"
        item.score = item.corp_geo_score

    item.matched_keywords = list(set(macro_matched + corp_matched))


def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Drop near-duplicates by normalized title prefix."""
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in items:
        key = "".join(c for c in it.title.lower() if c.isalnum())[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def collect_top_news() -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_items: list[NewsItem] = []
    for name, url, weight in RSS_SOURCES:
        all_items.extend(fetch_feed(name, url, weight, cutoff))

    log.info("Fetched %d total items across %d sources", len(all_items), len(RSS_SOURCES))

    for item in all_items:
        score_item(item)

    # Keep only items that matched at least one impact keyword
    relevant = [it for it in all_items if it.matched_keywords]
    log.info("After relevance filter: %d items", len(relevant))

    relevant = dedupe(relevant)
    log.info("After dedup: %d items", len(relevant))

    # Split into two buckets and take the top N from each
    macro_items = sorted(
        [it for it in relevant if it.category == "macro"],
        key=lambda x: x.macro_score, reverse=True,
    )[:MACRO_COUNT]
    corp_geo_items = sorted(
        [it for it in relevant if it.category == "corporate_geo"],
        key=lambda x: x.corp_geo_score, reverse=True,
    )[:CORPORATE_GEO_COUNT]

    log.info("Selected %d macro + %d corporate/geo items",
             len(macro_items), len(corp_geo_items))

    # Top up if one bucket is short (e.g. quiet news day on one side)
    target = MACRO_COUNT + CORPORATE_GEO_COUNT
    chosen_links = {it.link for it in macro_items + corp_geo_items}
    if len(macro_items) + len(corp_geo_items) < target:
        leftovers = [it for it in relevant if it.link not in chosen_links]
        leftovers.sort(key=lambda x: x.score, reverse=True)
        needed = target - len(macro_items) - len(corp_geo_items)
        # Add to whichever bucket the leftover belongs to
        for it in leftovers[:needed]:
            if it.category == "macro":
                macro_items.append(it)
            else:
                corp_geo_items.append(it)

    return macro_items + corp_geo_items


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email_html(items: list[NewsItem], edition: str) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")

    macro_items = [it for it in items if it.category == "macro"]
    corp_geo_items = [it for it in items if it.category == "corporate_geo"]

    def _section(title: str, subtitle: str, section_items: list[NewsItem], start_idx: int) -> str:
        if not section_items:
            return ""
        rows = []
        for i, it in enumerate(section_items, start=start_idx):
            published_local = it.published.astimezone().strftime("%H:%M %Z")
            summary_text = it.summary[:SUMMARY_CHAR_LIMIT]
            ellipsis = "&hellip;" if len(it.summary) > SUMMARY_CHAR_LIMIT else ""
            rows.append(f"""
                <tr>
                  <td style="padding:14px 8px;vertical-align:top;font-weight:bold;color:#888;width:30px;">{i}.</td>
                  <td style="padding:14px 8px;vertical-align:top;">
                    <a href="{escape(it.link)}" style="color:#1a4d8c;text-decoration:none;font-weight:600;font-size:15px;">
                      {escape(it.title)}
                    </a>
                    <div style="color:#666;font-size:12px;margin-top:4px;">
                      {escape(it.source)} &middot; {published_local}
                    </div>
                    <div style="color:#333;font-size:13px;margin-top:8px;line-height:1.55;">
                      {escape(summary_text)}{ellipsis}
                    </div>
                  </td>
                </tr>
            """)
        return f"""
          <tr><td style="padding:18px 0 6px 0;">
            <div style="font-size:13px;font-weight:700;color:#1a4d8c;text-transform:uppercase;letter-spacing:0.5px;border-bottom:2px solid #1a4d8c;padding-bottom:6px;">
              {title}
            </div>
            <div style="font-size:11px;color:#888;margin-top:3px;">{subtitle}</div>
          </td></tr>
          {''.join(rows)}
        """

    macro_section = _section(
        "🌍 Global &amp; US Economy",
        "Central banks, inflation, jobs, growth, markets, fiscal policy",
        macro_items, 1,
    )
    corp_section = _section(
        "🏢 Corporate, Innovation &amp; Geopolitics",
        "Big tech, M&amp;A, AI/semiconductors, trade, international affairs",
        corp_geo_items, len(macro_items) + 1,
    )

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
  <table style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;padding:24px;">
    <tr><td>
      <h1 style="margin:0 0 4px 0;font-size:22px;color:#1a4d8c;">Top 10 Economic Headlines</h1>
      <div style="color:#888;font-size:13px;margin-bottom:20px;">{edition} edition &middot; {today}</div>
      <table style="width:100%;border-collapse:collapse;">
        {macro_section}
        {corp_section}
      </table>
      <div style="color:#aaa;font-size:11px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
        Curated from {len(RSS_SOURCES)} sources. {len(macro_items)} macro + {len(corp_geo_items)} corporate/geo. Ranked by topical importance, source authority, and recency.
      </div>
    </td></tr>
  </table>
</body></html>"""


def build_email_text(items: list[NewsItem], edition: str) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    macro_items = [it for it in items if it.category == "macro"]
    corp_geo_items = [it for it in items if it.category == "corporate_geo"]

    lines = [
        f"TOP 10 ECONOMIC HEADLINES -- {edition} edition",
        today,
        "=" * 60,
        "",
    ]

    def _add_section(header: str, section_items: list[NewsItem], start_idx: int) -> None:
        if not section_items:
            return
        lines.append(f"## {header}")
        lines.append("-" * 60)
        for i, it in enumerate(section_items, start=start_idx):
            published_local = it.published.astimezone().strftime("%H:%M %Z")
            lines.append(f"{i}. {it.title}")
            lines.append(f"   {it.source} | {published_local}")
            if it.summary:
                lines.append(f"   {it.summary[:SUMMARY_CHAR_LIMIT]}")
            lines.append(f"   {it.link}")
            lines.append("")

    _add_section("GLOBAL & US ECONOMY", macro_items, 1)
    _add_section("CORPORATE, INNOVATION & GEOPOLITICS",
                 corp_geo_items, len(macro_items) + 1)

    return "\n".join(lines)


def send_email(items: list[NewsItem]) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    from_addr = os.getenv("FROM_ADDR", smtp_user)
    from_name = os.getenv("FROM_NAME", "Economic News Bot")
    to_addrs = [a.strip() for a in os.environ["TO_ADDRS"].split(",") if a.strip()]

    hour = datetime.now().hour
    edition = "Morning" if hour < 12 else "Evening"

    msg = EmailMessage()
    msg["Subject"] = f"📊 Top 10 Economic Headlines -- {edition} ({datetime.now().strftime('%b %d')})"
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(build_email_text(items, edition))
    msg.add_alternative(build_email_html(items, edition), subtype="html")

    log.info("Sending email to %s via %s:%d", to_addrs, smtp_host, smtp_port)
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    log.info("Email sent.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("=== econ_news_bot run start ===")
    items = collect_top_news()
    if not items:
        log.warning("No relevant news items found. Skipping email.")
        return 1
    log.info("Top %d items:", len(items))
    for i, it in enumerate(items, 1):
        log.info("  %2d. [%s, %.2f] %s -- %s",
                 i, it.category, it.score, it.source, it.title[:70])
    send_email(items)
    log.info("=== econ_news_bot run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())