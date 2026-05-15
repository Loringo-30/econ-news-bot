"""
Economic News Bot (AI-powered, DeepSeek backend)
-------------------------------------------------
Fetches latest economic news from major RSS feeds and uses DeepSeek AI
to select the 10 most important stories and write Japanese commentary.

DeepSeek pricing (V3.2 / V4-flash, May 2026):
  - Input:  $0.28 / 1M tokens (cache miss), $0.028 / 1M tokens (cache hit)
  - Output: $0.42 / 1M tokens
  - About 3.5x cheaper input, 12x cheaper output than Claude Haiku 4.5.

Designed to be triggered by cron (or GitHub Actions) twice a day.
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
MACRO_COUNT = int(os.getenv("MACRO_COUNT", "5"))
CORPORATE_GEO_COUNT = int(os.getenv("CORPORATE_GEO_COUNT", "5"))
MAX_ITEMS_TO_AI = int(os.getenv("MAX_ITEMS_TO_AI", "150"))

# DeepSeek model. "deepseek-chat" is the legacy alias for non-thinking mode
# (currently maps to V4-Flash). It will be deprecated 2026/07/24 in favor
# of "deepseek-v4-flash" — change here when the time comes.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")


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
    rank: int = 0
    category: str = ""
    commentary_jp: str = ""
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

SYSTEM_PROMPT = """You are a senior economics editor curating a twice-daily news digest for a sophisticated reader.

Your task: from a list of recent headlines pulled from major outlets (Reuters, Bloomberg, FT, WSJ, CNBC, BBC, Economist, MarketWatch, Yahoo Finance), pick the 10 most important and impactful stories of the day.

Selection criteria:
- Real economic / market / geopolitical significance, not clickbait
- Stories reported by multiple major outlets (broader coverage = higher consensus on importance)
- Surprising / breaking / unexpected developments that move markets
- A balanced mix across topics: don't pick 8 stories about the same Fed decision

Split the 10 picks into two categories:
- "macro" (5 stories): global economy & US economy -- central banks, inflation, GDP, jobs, fiscal policy, sovereign debt, broad markets, commodities, currency
- "corporate_geo" (5 stories): companies, innovation/tech, M&A, big tech, AI/semiconductors, EVs, and geopolitics with economic impact (trade wars, tariffs, sanctions, supply chain)

For each pick, write a 2-4 sentence commentary IN JAPANESE explaining:
- Why this story matters (impact, who is affected, why now)
- Brief background context if useful for understanding

Also note which outlets reported the same story (using the input list -- match by content similarity, not exact title).

Return your answer as STRICT JSON in this exact structure. Do NOT wrap in markdown fences. Do NOT add prose:
{
  "macro": [
    {
      "id": <int, the id of the chosen item from the input list>,
      "commentary_jp": "<2-4 sentences in Japanese>",
      "also_reported_by_ids": [<ids of other items that cover the same story>]
    }
  ],
  "corporate_geo": [
    {"id": <int>, "commentary_jp": "<...>", "also_reported_by_ids": [<int>]}
  ]
}

Important rules:
- Output ONLY valid JSON, nothing else
- Exactly 5 items in each category (10 total)
- Use the integer id from the input list
- commentary_jp MUST be Japanese (日本語), not English or Chinese
- also_reported_by_ids may be an empty list if only one outlet covered the story"""


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
        f"Select the 10 most important and write Japanese commentary as instructed.\n\n"
        f"--- ITEMS ---\n{catalog}"
    )

    log.info("Calling DeepSeek (%s) with %d candidate items...",
             DEEPSEEK_MODEL, len(items))

    # Use response_format json_object for stricter JSON output.
    # DeepSeek supports this OpenAI-compatible parameter.
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

    # Cost estimation. DeepSeek bills input cache hits vs misses separately.
    usage = response.usage
    in_tokens = usage.prompt_tokens
    out_tokens = usage.completion_tokens
    # OpenAI-compatible usage may include cached_tokens via prompt_tokens_details
    cached = 0
    try:
        cached = getattr(usage, "prompt_tokens_details", None).cached_tokens or 0
    except AttributeError:
        cached = 0
    fresh = in_tokens - cached
    # Per million tokens (May 2026 DeepSeek V3.2 / V4-flash rates)
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
    for category in ("macro", "corporate_geo"):
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
            item.commentary_jp = pick.get("commentary_jp", "").strip()
            also_ids = pick.get("also_reported_by_ids", [])
            also_outlets = []
            for aid in also_ids:
                try:
                    also_outlets.append(items[int(aid)].source)
                except (ValueError, IndexError):
                    continue
            item.also_reported_by = sorted(set(also_outlets) - {item.source})
            selected.append(item)

    log.info("AI selected %d items (%d macro + %d corp/geo)",
             len(selected),
             sum(1 for x in selected if x.category == "macro"),
             sum(1 for x in selected if x.category == "corporate_geo"))
    return selected


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def build_email_html(items: list[NewsItem], edition_jp: str) -> str:
    today_jp = datetime.now().strftime("%Y年%m月%d日 (%A)")
    macro = [it for it in items if it.category == "macro"]
    corp = [it for it in items if it.category == "corporate_geo"]

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
                    f'margin-left:6px;">📢 {n_outlets}社が報道</span>'
                )
                outlets_line = (
                    f'<div style="color:#888;font-size:11px;margin-top:3px;">'
                    f'他の報道: {escape(", ".join(it.also_reported_by[:6]))}'
                    f'{"…" if len(it.also_reported_by) > 6 else ""}'
                    f'</div>'
                )
            else:
                badge = ""
                outlets_line = ""

            commentary_html = ""
            if it.commentary_jp:
                commentary_html = (
                    f'<div style="background:#f8f9fb;border-left:3px solid #1a4d8c;'
                    f'padding:10px 14px;margin-top:10px;color:#222;font-size:13px;'
                    f'line-height:1.7;border-radius:0 4px 4px 0;">'
                    f'{escape(it.commentary_jp)}</div>'
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
        "🌍 世界経済 &amp; アメリカ経済",
        "中央銀行・インフレ・雇用・成長・市場・財政",
        macro, 1,
    )
    corp_section = _section(
        "🏢 企業・イノベーション &amp; 地政学",
        "大手企業・M&amp;A・AI/半導体・貿易・国際情勢",
        corp, len(macro) + 1,
    )

    return f"""<!doctype html>
<html><body style="font-family:-apple-system,'Hiragino Sans','Yu Gothic',Segoe UI,Helvetica,Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px;">
  <table style="max-width:680px;margin:0 auto;background:#fff;border-radius:6px;padding:24px;">
    <tr><td>
      <h1 style="margin:0 0 4px 0;font-size:22px;color:#1a4d8c;">本日の重要経済ニュース 10選</h1>
      <div style="color:#888;font-size:13px;margin-bottom:20px;">{edition_jp} &middot; {today_jp}</div>
      <table style="width:100%;border-collapse:collapse;">
        {macro_section}
        {corp_section}
      </table>
      <div style="color:#aaa;font-size:11px;margin-top:24px;border-top:1px solid #eee;padding-top:12px;">
        AI（DeepSeek）が{len(RSS_SOURCES)}媒体のニュースから選別し、解説を生成しました。
      </div>
    </td></tr>
  </table>
</body></html>"""


def build_email_text(items: list[NewsItem], edition_jp: str) -> str:
    today_jp = datetime.now().strftime("%Y年%m月%d日 (%A)")
    macro = [it for it in items if it.category == "macro"]
    corp = [it for it in items if it.category == "corporate_geo"]
    lines = [
        f"本日の重要経済ニュース 10選 -- {edition_jp}",
        today_jp,
        "=" * 60,
        "",
    ]

    def _add(header: str, section: list[NewsItem], start_idx: int) -> None:
        if not section:
            return
        lines.append(f"## {header}")
        lines.append("-" * 60)
        for i, it in enumerate(section, start=start_idx):
            time = it.published.astimezone().strftime("%H:%M %Z")
            n_outlets = 1 + len(it.also_reported_by)
            badge = f" [📢 {n_outlets}社が報道]" if n_outlets >= 2 else ""
            lines.append(f"{i}. {it.title}{badge}")
            lines.append(f"   {it.source} | {time}")
            if it.also_reported_by:
                lines.append(f"   他の報道: {', '.join(it.also_reported_by[:6])}"
                             f"{'…' if len(it.also_reported_by) > 6 else ''}")
            if it.commentary_jp:
                lines.append(f"   💬 {it.commentary_jp}")
            lines.append(f"   {it.link}")
            lines.append("")

    _add("世界経済 & アメリカ経済", macro, 1)
    _add("企業・イノベーション & 地政学", corp, len(macro) + 1)
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
    edition_jp = "朝刊" if hour < 12 else "夕刊"

    msg = EmailMessage()
    msg["Subject"] = (
        f"📊 本日の重要経済ニュース 10選 -- "
        f"{edition_jp} ({datetime.now().strftime('%m月%d日')})"
    )
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(build_email_text(items, edition_jp))
    msg.add_alternative(build_email_html(items, edition_jp), subtype="html")

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
        log.info("  %s #%d [%d outlets] %s -- %s",
                 it.category, it.rank, n_outlets, it.source, it.title[:60])

    send_email(selected)
    log.info("=== econ_news_bot run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
