"""
Economic News Bot (AI-powered, DeepSeek backend)
-------------------------------------------------
Fetches latest economic news from major RSS feeds and uses DeepSeek AI
to select 10 important stories.

Each story includes:
  - English commentary (2-4 sentences) explaining why it matters
  - A CEFR C1/C2 vocabulary list with Japanese translations
    (great for English study at advanced level)

Split: 3 macro stories + 7 corporate/innovation stories (configurable).

DeepSeek pricing (V3.2 / V4-flash, May 2026):
  - Input:  $0.28 / 1M tokens (cache miss), $0.028 / 1M tokens (cache hit)
  - Output: $0.42 / 1M tokens
"""

from __future__ import annotations

import json
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

import feedparser
from dateutil import parser as date_parser
from dotenv import load_dotenv
from openai import OpenAI

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

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "14"))
MACRO_COUNT = int(os.getenv("MACRO_COUNT", "3"))
CORPORATE_INNOVATION_COUNT = int(os.getenv("CORPORATE_INNOVATION_COUNT", "7"))
MAX_ITEMS_TO_AI = int(os.getenv("MAX_ITEMS_TO_AI", "150"))

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class VocabEntry:
    word: str           # English word/phrase from the article
    japanese: str       # Japanese translation
    cefr: str = "C1"    # "C1" or "C2"


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str
    source: str
    source_weight: float
    published: datetime
    rank: int = 0
    category: str = ""              # "macro" or "corporate_innovation"
    commentary: str = ""            # English commentary
    vocabulary: list[VocabEntry] = field(default_factory=list)
    also_reported_by: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# RSS fetching
# ---------------------------------------------------------------------------

def parse_published(entry) -> datetime | None:
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
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _strip_html(text: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_feed(name: str, url: str, weight: float, cutoff: datetime) -> list[NewsItem]:
    log.info("Fetching %s", name)
    items: list[NewsItem] = []
    try:
        feed = feedparser.parse(
            url,
            request_headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; econ-news-bot/2.0; +https://example.com/bot)"
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
        summary = _strip_html(summary)[:800]
        link = entry.get("link") or ""
        items.append(NewsItem(
            title=title, link=link, summary=summary,
            source=name, source_weight=weight, published=pub,
        ))
    log.info("  %s: %d recent items", name, len(items))
    return items


def fetch_all_news() -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_items: list[NewsItem] = []
    for name, url, weight in RSS_SOURCES:
        all_items.extend(fetch_feed(name, url, weight, cutoff))
    log.info("Fetched %d total items across %d sources",
             len(all_items), len(RSS_SOURCES))
    return all_items


def trim_for_ai(items: list[NewsItem]) -> list[NewsItem]:
    if len(items) <= MAX_ITEMS_TO_AI:
        return items
    items_sorted = sorted(
        items,
        key=lambda it: (it.source_weight, it.published),
        reverse=True,
    )
    return items_sorted[:MAX_ITEMS_TO_AI]


# ---------------------------------------------------------------------------
# AI selection and commentary (DeepSeek backend)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a senior economics editor curating a twice-daily news digest for a sophisticated reader who is also studying English at CEFR C1-C2 level.

Your task: from a list of recent headlines pulled from major outlets (Reuters, Bloomberg, FT, WSJ, CNBC, BBC, Economist, MarketWatch, Yahoo Finance), pick the 10 most important and impactful stories of the day.

Selection criteria:
- Real economic / market / geopolitical significance, not clickbait
- Stories reported by multiple major outlets (broader coverage = higher consensus on importance)
- Surprising / breaking / unexpected developments that move markets
- A balanced mix across topics: don't pick 8 stories about the same Fed decision

Split the 10 picks into two categories:
- "macro" ({MACRO_COUNT} stories): global economy & US economy -- central banks, inflation, GDP, jobs, fiscal policy, sovereign debt, broad markets, commodities, currency, geopolitics with macroeconomic impact (trade wars, tariffs, sanctions, supply chain)
- "corporate_innovation" ({CORPORATE_INNOVATION_COUNT} stories): companies, innovation/tech, M&A, big tech, AI/semiconductors, EVs, biotech, startups

For each pick, provide TWO things:

1. **commentary** (2-4 sentences in ENGLISH explaining):
   - Why this story matters (impact, who is affected, why now)
   - Brief background context if useful for understanding

2. **vocabulary** (a list of 3-6 CEFR C1-C2 level English words or phrases from the article title/summary/commentary, each with a Japanese translation):
   - Focus on advanced vocabulary the reader would benefit from learning
   - Skip A1-B2 level words (common words like "company", "market", "rise")
   - Include each word's CEFR level ("C1" or "C2")
   - Translation should be concise and natural Japanese

Also note which outlets reported the same story (using the input list -- match by content similarity, not exact title).

Return your answer as STRICT JSON in this exact structure. Do NOT wrap in markdown fences. Do NOT add prose:
{{
  "macro": [
    {{
      "id": <int, id from the input list>,
      "commentary": "<2-4 sentences in English>",
      "vocabulary": [
        {{"word": "<English word/phrase>", "japanese": "<Japanese translation>", "cefr": "C1"}}
      ],
      "also_reported_by_ids": [<ids of other items covering the same story>]
    }}
  ],
  "corporate_innovation": [
    {{"id": <int>, "commentary": "<...>", "vocabulary": [...], "also_reported_by_ids": [<int>]}}
  ]
}}

Important rules:
- Output ONLY valid JSON, nothing else
- Exactly {MACRO_COUNT} items in "macro" and {CORPORATE_INNOVATION_COUNT} in "corporate_innovation" ({MACRO_COUNT + CORPORATE_INNOVATION_COUNT} total)
- commentary MUST be in English
- Japanese translations MUST be in Japanese (日本語), not Chinese or romaji
- Each vocabulary entry's cefr field must be "C1" or "C2"
- also_reported_by_ids may be empty list if no other outlet covered it"""


def select_with_ai(items: list[NewsItem]) -> list[NewsItem]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY environment variable is required. "
            "Get one at https://platform.deepseek.com"
        )
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    catalog_lines = []
    for i, it in enumerate(items):
        time_str = it.published.strftime("%Y-%m-%d %H:%M UTC")
        snippet = it.summary[:400] if it.summary else ""
        catalog_lines.append(
            f"[id={i}] [{it.source}] [{time_str}]\n"
            f"  Title: {it.title}\n"
            f"  Summary: {snippet}"
        )
    catalog = "\n\n".join(catalog_lines)

    user_message = (
        f"Here are {len(items)} recent economic news items from major outlets. "
        f"Select the {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} most important and write commentary "
        f"plus a CEFR C1-C2 vocabulary list with Japanese translations as instructed.\n\n"
        f"--- ITEMS ---\n{catalog}"
    )

    log.info("Calling DeepSeek (%s) with %d candidate items...",
             DEEPSEEK_MODEL, len(items))

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_tokens=8000,
            temperature=0.3,
        )
    except Exception as e:
        msg = str(e)
        if "402" in msg or "Insufficient Balance" in msg:
            log.error(
                "DeepSeek API balance is 0. Top up at https://platform.deepseek.com "
                "(usually $2-5 lasts for months)."
            )
        elif "401" in msg or "Authentication" in msg:
            log.error("DeepSeek API key is invalid. Check the DEEPSEEK_API_KEY secret.")
        else:
            log.error("DeepSeek API call failed: %s", msg)
        raise

    usage = response.usage
    in_tokens = usage.prompt_tokens
    out_tokens = usage.completion_tokens
    cached = 0
    try:
        cached = getattr(usage, "prompt_tokens_details", None).cached_tokens or 0
    except AttributeError:
        cached = 0
    fresh = in_tokens - cached
    in_cost = (fresh * 0.28 + cached * 0.028) / 1_000_000
    out_cost = out_tokens * 0.42 / 1_000_000
    total_usd = in_cost + out_cost
    log.info(
        "AI usage: %d in (%d cached) + %d out tokens (~$%.4f, ~¥%.1f)",
        in_tokens, cached, out_tokens, total_usd, total_usd * 150,
    )

    raw_text = response.choices[0].message.content.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        log.error("AI response was not valid JSON: %s", e)
        log.error("Raw response: %s", raw_text[:1000])
        raise

    selected: list[NewsItem] = []
    for category in ("macro", "corporate_innovation"):
        picks = parsed.get(category, [])
        for rank, pick in enumerate(picks, start=1):
            try:
                item_id = int(pick["id"])
                item = items[item_id]
            except (KeyError, ValueError, IndexError) as e:
                log.warning("Skipping invalid pick %s: %s", pick, e)
                continue
            item.category = category
            item.rank = rank
            item.commentary = pick.get("commentary", "").strip()

            # Parse vocabulary list
            vocab_list = []
            for v in pick.get("vocabulary", []):
                try:
                    word = str(v.get("word", "")).strip()
                    jp = str(v.get("japanese", "")).strip()
                    cefr = str(v.get("cefr", "C1")).strip().upper()
                    if word and jp:
                        vocab_list.append(VocabEntry(word=word, japanese=jp, cefr=cefr))
                except (AttributeError, TypeError):
                    continue
            item.vocabulary = vocab_list

            also_ids = pick.get("also_reported_by_ids", [])
            also_outlets = []
            for aid in also_ids:
                try:
                    also_outlets.append(items[int(aid)].source)
                except (ValueError, IndexError):
                    continue
            item.also_reported_by = sorted(set(also_outlets) - {item.source})
            selected.append(item)

    log.info("AI selected %d items (%d macro + %d corp/innov)",
             len(selected),
             sum(1 for x in selected if x.category == "macro"),
             sum(1 for x in selected if x.category == "corporate_innovation"))
    return selected


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _collect_all_vocab(items: list[NewsItem]) -> list[tuple[VocabEntry, int]]:
    """Collect all vocabulary across items, paired with their article index."""
    result = []
    for idx, it in enumerate(items, start=1):
        for v in it.vocabulary:
            result.append((v, idx))
    return result


def build_email_html(items: list[NewsItem], edition_en: str) -> str:
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    macro = [it for it in items if it.category == "macro"]
    corp = [it for it in items if it.category == "corporate_innovation"]

    # ---------- Vocabulary index at the top ----------
    # Layout: one word per row, full width. Three columns:
    #   [article# + CEFR badge]   [English word]   [Japanese translation]
    # Uses a simple table -- renders identically on Gmail, Apple Mail, Outlook,
    # and mobile clients. Each row stretches to the full available width.
    all_vocab = _collect_all_vocab(items)
    vocab_section = ""
    if all_vocab:
        vocab_rows = []
        for i, (v, article_idx) in enumerate(all_vocab):
            cefr_color = "#7a3fd8" if v.cefr == "C2" else "#1a4d8c"
            cefr_bg = "#f3ebff" if v.cefr == "C2" else "#eaf2fb"
            # Subtle zebra striping for readability
            row_bg = "#ffffff" if i % 2 == 0 else "#faf8ff"
            vocab_rows.append(
                f'<tr style="background:{row_bg};">'
                # Article number + CEFR badge (compact, left-aligned)
                f'<td style="padding:8px 10px;vertical-align:middle;white-space:nowrap;'
                f'font-size:11px;color:{cefr_color};font-weight:700;">'
                f'<span style="display:inline-block;background:{cefr_bg};'
                f'padding:2px 7px;border-radius:8px;">'
                f'#{article_idx} &middot; {escape(v.cefr)}</span>'
                f'</td>'
                # English word
                f'<td style="padding:8px 10px;vertical-align:middle;'
                f'font-size:14px;font-weight:600;color:#111;">'
                f'{escape(v.word)}</td>'
                # Japanese translation (takes remaining width)
                f'<td style="padding:8px 10px;vertical-align:middle;'
                f'font-size:14px;color:#555;width:100%;">'
                f'{escape(v.japanese)}</td>'
                f'</tr>'
            )
        vocab_section = f"""
          <tr><td style="padding:18px 0 6px 0;">
            <div style="font-size:13px;font-weight:700;color:#7a3fd8;text-transform:uppercase;
                        letter-spacing:0.5px;border-bottom:2px solid #7a3fd8;padding-bottom:6px;">
              📚 Vocabulary (CEFR C1-C2)
            </div>
            <div style="font-size:11px;color:#888;margin-top:3px;">
              Advanced English vocabulary from today's articles, with Japanese translations.
              <span style="color:#aaa;">#N = article number</span>
            </div>
          </td></tr>
          <tr><td style="padding:10px 0 14px 0;">
            <table style="width:100%;border-collapse:collapse;border-radius:8px;
                          overflow:hidden;border:1px solid #ece7f5;">
              {''.join(vocab_rows)}
            </table>
          </td></tr>
        """

    # ---------- Article sections ----------
    def _section(title: str, subtitle: str, section_items: list[NewsItem], start_idx: int) -> str:
        if not section_items:
            return ""
        rows = []
        for i, it in enumerate(section_items, start=start_idx):
            published_local = it.published.astimezone().strftime("%H:%M %Z")
            n_outlets = 1 + len(it.also_reported_by)
            if n_outlets >= 2:
                badge = (
                    f'<span style="display:inline-block;background:#d1f5e0;color:#0a6b2c;'
                    f'font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;'
                    f'margin-left:6px;">📢 {n_outlets} outlets</span>'
                )
                outlets_line = (
                    f'<div style="color:#888;font-size:11px;margin-top:3px;">'
                    f'Also reported by: {escape(", ".join(it.also_reported_by[:6]))}'
                    f'{"…" if len(it.also_reported_by) > 6 else ""}'
                    f'</div>'
                )
            else:
                badge = ""
                outlets_line = ""

            commentary_html = ""
            if it.commentary:
                commentary_html = (
                    f'<div style="background:#f8f9fb;border-left:3px solid #1a4d8c;'
                    f'padding:10px 14px;margin-top:10px;color:#222;font-size:13px;'
                    f'line-height:1.6;border-radius:0 4px 4px 0;">'
                    f'{escape(it.commentary)}</div>'
                )

            # Per-article vocabulary -- same one-row-per-word layout as the index.
            vocab_inline = ""
            if it.vocabulary:
                vocab_rows_local = []
                for v in it.vocabulary:
                    cefr_color = "#7a3fd8" if v.cefr == "C2" else "#1a4d8c"
                    cefr_bg = "#f3ebff" if v.cefr == "C2" else "#eaf2fb"
                    vocab_rows_local.append(
                        f'<tr>'
                        f'<td style="padding:4px 8px 4px 0;vertical-align:middle;'
                        f'white-space:nowrap;font-size:10px;color:{cefr_color};'
                        f'font-weight:700;">'
                        f'<span style="display:inline-block;background:{cefr_bg};'
                        f'padding:1px 6px;border-radius:6px;">{escape(v.cefr)}</span>'
                        f'</td>'
                        f'<td style="padding:4px 10px 4px 0;vertical-align:middle;'
                        f'font-size:13px;font-weight:600;color:#111;">{escape(v.word)}</td>'
                        f'<td style="padding:4px 0;vertical-align:middle;'
                        f'font-size:13px;color:#555;width:100%;">{escape(v.japanese)}</td>'
                        f'</tr>'
                    )
                vocab_inline = (
                    f'<table style="width:100%;border-collapse:collapse;margin-top:10px;">'
                    f'{"".join(vocab_rows_local)}'
                    f'</table>'
                )

            rows.append(f"""
                <tr>
                  <td style="padding:16px 8px;vertical-align:top;font-weight:bold;color:#888;width:30px;">{i}.</td>
                  <td style="padding:16px 8px;vertical-align:top;">
                    <a href="{escape(it.link)}" style="color:#1a4d8c;text-decoration:none;font-weight:600;font-size:15px;">
                      {escape(it.title)}
                    </a>{badge}
                    <div style="color:#666;font-size:12px;margin-top:4px;">
                      {escape(it.source)} &middot; {published_local}
                    </div>
                    {outlets_line}
                    {commentary_html}
                    {vocab_inline}
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
        "🌍 Macro",
        "World &amp; US economy: central banks, inflation, jobs, fiscal policy, trade",
        macro, 1,
    )
    corp_section = _section(
        "🏢 Corporate &amp; Innovation",
        "Big tech, M&amp;A, AI/semiconductors, biotech, startups",
        corp, len(macro) + 1,
    )

    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:-apple-system,'Hiragino Sans','Yu Gothic',Segoe UI,Helvetica,Arial,sans-serif;background:#f4f4f4;margin:0;padding:12px;">
  <table style="max-width:680px;width:100%;margin:0 auto;background:#fff;border-radius:6px;padding:16px;box-sizing:border-box;">
    <tr><td>
      <h1 style="margin:0 0 4px 0;font-size:22px;color:#1a4d8c;">
        Today's Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Economic Headlines
      </h1>
      <div style="color:#888;font-size:13px;margin-bottom:20px;">
        {edition_en} &middot; {today_str}
      </div>
      <table style="width:100%;border-collapse:collapse;">
        {vocab_section}
        {macro_section}
        {corp_section}
      </table>
      <div style="color:#aaa;font-size:11px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
        Curated by AI (DeepSeek) from {len(RSS_SOURCES)} sources. Commentary in English with CEFR C1-C2 vocabulary translated to Japanese.
      </div>
    </td></tr>
  </table>
</body></html>"""


def build_email_text(items: list[NewsItem], edition_en: str) -> str:
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    macro = [it for it in items if it.category == "macro"]
    corp = [it for it in items if it.category == "corporate_innovation"]
    lines = [
        f"Today's Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Economic Headlines -- {edition_en}",
        today_str,
        "=" * 60,
        "",
    ]

    # Vocabulary index
    all_vocab = _collect_all_vocab(items)
    if all_vocab:
        lines.append("## 📚 VOCABULARY (CEFR C1-C2)")
        lines.append("-" * 60)
        for v, article_idx in all_vocab:
            lines.append(f"  #{article_idx} [{v.cefr}] {v.word} = {v.japanese}")
        lines.append("")

    def _add(header: str, section: list[NewsItem], start_idx: int) -> None:
        if not section:
            return
        lines.append(f"## {header}")
        lines.append("-" * 60)
        for i, it in enumerate(section, start=start_idx):
            time = it.published.astimezone().strftime("%H:%M %Z")
            n_outlets = 1 + len(it.also_reported_by)
            badge = f" [📢 {n_outlets} outlets]" if n_outlets >= 2 else ""
            lines.append(f"{i}. {it.title}{badge}")
            lines.append(f"   {it.source} | {time}")
            if it.also_reported_by:
                lines.append(f"   Also reported by: {', '.join(it.also_reported_by[:6])}"
                             f"{'…' if len(it.also_reported_by) > 6 else ''}")
            if it.commentary:
                lines.append(f"   💬 {it.commentary}")
            if it.vocabulary:
                vocab_str = ", ".join(f"{v.word}={v.japanese}({v.cefr})" for v in it.vocabulary)
                lines.append(f"   📚 {vocab_str}")
            lines.append(f"   {it.link}")
            lines.append("")

    _add("MACRO -- World & US economy", macro, 1)
    _add("CORPORATE & INNOVATION", corp, len(macro) + 1)
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
    edition_en = "Morning Edition" if hour < 12 else "Evening Edition"

    msg = EmailMessage()
    msg["Subject"] = (
        f"📊 Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Economic Headlines -- "
        f"{edition_en} ({datetime.now().strftime('%b %d')})"
    )
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(build_email_text(items, edition_en))
    msg.add_alternative(build_email_html(items, edition_en), subtype="html")

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
    log.info("=== econ_news_bot (AI/DeepSeek) run start ===")
    items = fetch_all_news()
    if not items:
        log.warning("No items fetched -- all feeds may be down. Skipping email.")
        return 1

    items = trim_for_ai(items)
    log.info("Sending %d items to AI for selection", len(items))

    selected = select_with_ai(items)
    if not selected:
        log.warning("AI returned no items. Skipping email.")
        return 1

    selected.sort(key=lambda it: (
        0 if it.category == "macro" else 1, it.rank,
    ))

    log.info("Top %d picks:", len(selected))
    for it in selected:
        n_outlets = 1 + len(it.also_reported_by)
        log.info("  %s #%d [%d outlets, %d vocab] %s -- %s",
                 it.category, it.rank, n_outlets, len(it.vocabulary),
                 it.source, it.title[:60])

    send_email(selected)
    log.info("=== econ_news_bot run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
