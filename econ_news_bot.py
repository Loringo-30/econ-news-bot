"""
Economic News Bot (Twitter/X source, AI-powered via DeepSeek)
--------------------------------------------------------------
Fetches recent tweets from a curated list of Twitter/X accounts,
sends them to DeepSeek AI to select the most important 10, and
emails a digest with English commentary and CEFR C1-C2 vocabulary
(with Japanese translations).

Twitter data source: TwitterAPI.io  ($0.15 per 1,000 tweets)
AI:                  DeepSeek        ($0.28 / 1M input, $0.42 / 1M output)

The list of accounts to follow is stored in twitter_accounts.txt.
Edit that file to change who the bot follows.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path

import requests
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

# ---- Twitter (TwitterAPI.io) ----
TWITTERAPI_BASE_URL = os.getenv("TWITTERAPI_BASE_URL", "https://api.twitterapi.io")
TWITTERAPI_ENDPOINT = "/twitter/user/last_tweets"

# Path to file listing accounts to follow. Read at startup.
ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", "twitter_accounts.txt")

# How many recent tweets to consider from each account. TwitterAPI.io returns
# up to 20 per call by default; if you need more, add pagination logic.
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", "20"))

# Whether to keep retweets. Defaults to false (usually noisy).
INCLUDE_RETWEETS = os.getenv("INCLUDE_RETWEETS", "false").lower() == "true"

# Whether to keep replies. Defaults to false (they need context to make sense).
INCLUDE_REPLIES = os.getenv("INCLUDE_REPLIES", "false").lower() == "true"

# How far back to keep tweets (in hours). Older tweets are dropped.
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "14"))

# Cap on tweets sent to AI (keeps prompt size and cost bounded).
MAX_TWEETS_TO_AI = int(os.getenv("MAX_TWEETS_TO_AI", "200"))

# ---- AI (DeepSeek) ----
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# ---- Digest layout ----
MACRO_COUNT = int(os.getenv("MACRO_COUNT", "3"))
CORPORATE_INNOVATION_COUNT = int(os.getenv("CORPORATE_INNOVATION_COUNT", "7"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class VocabEntry:
    word: str
    japanese: str
    cefr: str = "C1"


@dataclass
class Tweet:
    """One tweet from one account."""
    id: str
    text: str              # Full tweet text
    author_username: str   # Handle without "@"
    author_display: str    # Display name (e.g. "Elon Musk")
    author_verified: bool
    author_followers: int  # Follower count (used as an authority hint)
    created_at: datetime   # UTC
    url: str               # https://twitter.com/user/status/id
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    is_reply: bool = False
    is_retweet: bool = False

    # Filled in by the AI selection step:
    rank: int = 0
    category: str = ""              # "macro" or "corporate_innovation"
    commentary: str = ""            # English commentary
    vocabulary: list[VocabEntry] = field(default_factory=list)
    also_from: list[str] = field(default_factory=list)  # other accounts on same topic


# ---------------------------------------------------------------------------
# Load account list
# ---------------------------------------------------------------------------

def load_accounts() -> list[str]:
    """Read the accounts file, one username per line, ignoring comments and
    blank lines. Returns cleaned lowercase list without '@'."""
    path = Path(ACCOUNTS_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"Accounts file not found: {path.absolute()}. "
            "Create twitter_accounts.txt with one username per line."
        )
    accounts: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading @ if present
        if line.startswith("@"):
            line = line[1:]
        line = line.split()[0]  # in case of trailing comments after the handle
        if line:
            accounts.append(line.lower())
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for a in accounts:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    log.info("Loaded %d Twitter accounts from %s", len(unique), path.name)
    if not unique:
        raise ValueError(
            "No accounts found in accounts file. Add usernames (one per line) "
            "to twitter_accounts.txt."
        )
    return unique


# ---------------------------------------------------------------------------
# TwitterAPI.io fetching
# ---------------------------------------------------------------------------

def _parse_iso_datetime(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = date_parser.parse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _extract_tweet(raw: dict, fallback_username: str) -> Tweet | None:
    """Convert TwitterAPI.io's raw tweet JSON into our Tweet dataclass.
    TwitterAPI.io mirrors X's own field names closely."""
    tid = str(raw.get("id") or "").strip()
    text = (raw.get("text") or "").strip()
    if not tid or not text:
        return None

    # Author info: TwitterAPI.io usually nests author under "author"
    author = raw.get("author") or {}
    username = (author.get("userName") or fallback_username or "").lstrip("@").lower()
    display = author.get("name") or username
    verified = bool(author.get("isVerified") or author.get("isBlueVerified"))
    followers = int(author.get("followers") or 0)

    created_at = _parse_iso_datetime(raw.get("createdAt") or "")
    if created_at is None:
        return None

    # Determine reply/retweet status. Field names vary a bit; try both.
    is_reply = bool(
        raw.get("isReply") or raw.get("inReplyToUsername")
        or raw.get("inReplyToId")
    )
    is_retweet = bool(
        raw.get("isRetweet") or raw.get("retweeted_tweet")
        or text.startswith("RT @")
    )

    url = raw.get("url") or f"https://twitter.com/{username}/status/{tid}"

    return Tweet(
        id=tid,
        text=text,
        author_username=username,
        author_display=display,
        author_verified=verified,
        author_followers=followers,
        created_at=created_at,
        url=url,
        like_count=int(raw.get("likeCount") or 0),
        retweet_count=int(raw.get("retweetCount") or 0),
        reply_count=int(raw.get("replyCount") or 0),
        is_reply=is_reply,
        is_retweet=is_retweet,
    )


def fetch_user_tweets(api_key: str, username: str, cutoff: datetime) -> list[Tweet]:
    """Fetch the latest tweets from one account, filtered to cutoff time."""
    url = TWITTERAPI_BASE_URL + TWITTERAPI_ENDPOINT
    headers = {"X-API-Key": api_key}
    params = {"userName": username}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    except requests.RequestException as e:
        log.warning("  @%s: request failed: %s", username, e)
        return []

    if resp.status_code == 401:
        raise RuntimeError(
            "TwitterAPI.io rejected the API key (401). "
            "Check the TWITTERAPI_KEY secret."
        )
    if resp.status_code == 402 or "insufficient" in resp.text.lower():
        raise RuntimeError(
            "TwitterAPI.io balance is exhausted. "
            "Top up at https://twitterapi.io."
        )
    if resp.status_code != 200:
        log.warning("  @%s: HTTP %d: %s", username, resp.status_code, resp.text[:200])
        return []

    try:
        payload = resp.json()
    except ValueError:
        log.warning("  @%s: response was not JSON", username)
        return []

    # TwitterAPI.io returns tweets under a "tweets" key (sometimes under "data")
    raw_tweets = (
        payload.get("tweets")
        or payload.get("data")
        or (payload.get("data") or {}).get("tweets")
        or []
    )
    if not raw_tweets:
        log.info("  @%s: no tweets returned", username)
        return []

    parsed = []
    for raw in raw_tweets[:TWEETS_PER_ACCOUNT]:
        t = _extract_tweet(raw, username)
        if t and t.created_at >= cutoff:
            parsed.append(t)

    log.info("  @%s: %d recent tweets (of %d fetched)",
             username, len(parsed), len(raw_tweets))
    return parsed


def fetch_all_tweets() -> list[Tweet]:
    """Fetch tweets from every account in the list."""
    api_key = os.environ.get("TWITTERAPI_KEY")
    if not api_key:
        raise RuntimeError(
            "TWITTERAPI_KEY environment variable is required. "
            "Get one at https://twitterapi.io"
        )

    accounts = load_accounts()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info("Fetching tweets since %s from %d accounts...",
             cutoff.strftime("%Y-%m-%d %H:%M UTC"), len(accounts))

    all_tweets: list[Tweet] = []
    for i, username in enumerate(accounts, 1):
        log.info("[%d/%d] Fetching @%s", i, len(accounts), username)
        tweets = fetch_user_tweets(api_key, username, cutoff)
        all_tweets.extend(tweets)
        # Small delay to be polite to the API (also avoids rate-limit hiccups)
        time.sleep(0.15)

    log.info("Fetched %d total tweets from %d accounts",
             len(all_tweets), len(accounts))
    return all_tweets


def filter_tweets(tweets: list[Tweet]) -> list[Tweet]:
    """Drop replies/retweets according to config, and near-duplicates."""
    kept: list[Tweet] = []
    seen_prefixes: set[str] = set()
    for t in tweets:
        if t.is_reply and not INCLUDE_REPLIES:
            continue
        if t.is_retweet and not INCLUDE_RETWEETS:
            continue
        if len(t.text.strip()) < 20:
            continue  # skip trivially short tweets
        # Dedup based on the first ~80 alphanumeric chars of the text
        key = "".join(c for c in t.text.lower() if c.isalnum())[:80]
        if key in seen_prefixes:
            continue
        seen_prefixes.add(key)
        kept.append(t)
    log.info("After filtering (replies=%s, retweets=%s): %d tweets",
             INCLUDE_REPLIES, INCLUDE_RETWEETS, len(kept))
    return kept


def trim_for_ai(tweets: list[Tweet]) -> list[Tweet]:
    """If too many tweets, keep the ones from the most-followed accounts
    plus the most-engaged tweets, so AI cost stays bounded."""
    if len(tweets) <= MAX_TWEETS_TO_AI:
        return tweets
    # Rank by (engagement * follower authority) so a small account with a viral
    # tweet still competes with a big account's normal tweet.
    def _score(t: Tweet) -> float:
        engagement = t.like_count + 2 * t.retweet_count + t.reply_count
        # log-scale follower count so mega-accounts don't dominate entirely
        import math
        authority = math.log10(max(t.author_followers, 10))
        return engagement * (1 + authority * 0.5)
    tweets_sorted = sorted(tweets, key=_score, reverse=True)
    kept = tweets_sorted[:MAX_TWEETS_TO_AI]
    log.info("Trimmed to top %d tweets by engagement × authority", len(kept))
    return kept


# ---------------------------------------------------------------------------
# AI selection and commentary (DeepSeek backend)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a senior economics editor curating a twice-daily digest for a sophisticated reader who is also studying English at CEFR C1-C2 level.

Your task: from a stream of recent tweets by hand-picked economists, journalists, policymakers, and industry leaders, pick the {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} most important and impactful stories/insights of the day.

Selection criteria:
- Real economic / market / geopolitical / technology significance
- Prefer tweets that break news, announce material developments, or provide sharp original analysis over reposts of known facts
- If several accounts tweet about the same story, treat that as a strong signal that it matters (and note them together)
- A balanced mix across topics: don't pick 8 stories about the same event

Split the {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} picks into two categories:
- "macro" ({MACRO_COUNT} stories): global economy & US economy -- central banks, inflation, GDP, jobs, fiscal policy, sovereign debt, broad markets, commodities, currency, geopolitics with macroeconomic impact
- "corporate_innovation" ({CORPORATE_INNOVATION_COUNT} stories): companies, innovation/tech, M&A, big tech, AI/semiconductors, EVs, biotech, startups

For each pick, provide:

1. **commentary** (2-4 sentences in ENGLISH):
   - What the tweet says and why it matters (impact, who's affected, why now)
   - Brief background context if useful

2. **vocabulary** (3-6 CEFR C1-C2 English words/phrases from the tweet or commentary, each with a Japanese translation):
   - Focus on advanced vocabulary useful for an intermediate/advanced English learner
   - Skip A1-B2 level words
   - Include each word's CEFR level ("C1" or "C2")
   - Translation should be concise natural Japanese

3. **also_from_ids**: IDs of OTHER tweets in the input that discuss the same story/topic

Return your answer as STRICT JSON in this exact structure. Do NOT wrap in markdown fences. Do NOT add prose:
{{
  "macro": [
    {{
      "id": <int, id from the input list>,
      "commentary": "<2-4 sentences in English>",
      "vocabulary": [
        {{"word": "<English word/phrase>", "japanese": "<Japanese translation>", "cefr": "C1"}}
      ],
      "also_from_ids": [<ids of other tweets covering the same story>]
    }}
  ],
  "corporate_innovation": [
    {{"id": <int>, "commentary": "<...>", "vocabulary": [...], "also_from_ids": [<int>]}}
  ]
}}

Rules:
- Output ONLY valid JSON, nothing else
- Exactly {MACRO_COUNT} items in "macro" and {CORPORATE_INNOVATION_COUNT} in "corporate_innovation"
- commentary MUST be in English
- Japanese translations MUST be in Japanese (日本語), not Chinese or romaji
- Each vocabulary entry's cefr field must be "C1" or "C2"
- also_from_ids may be an empty list"""


def select_with_ai(tweets: list[Tweet]) -> list[Tweet]:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY environment variable is required. "
            "Get one at https://platform.deepseek.com"
        )
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    catalog_lines = []
    for i, t in enumerate(tweets):
        time_str = t.created_at.strftime("%Y-%m-%d %H:%M UTC")
        verified_mark = " ✓" if t.author_verified else ""
        # Truncate very long tweets to keep prompt size sane
        text_short = t.text if len(t.text) <= 500 else t.text[:500] + "…"
        catalog_lines.append(
            f"[id={i}] @{t.author_username}{verified_mark} "
            f"({t.author_followers:,} followers) [{time_str}] "
            f"[❤️ {t.like_count:,} 🔁 {t.retweet_count:,} 💬 {t.reply_count:,}]\n"
            f"  {text_short}"
        )
    catalog = "\n\n".join(catalog_lines)

    user_message = (
        f"Here are {len(tweets)} recent tweets from hand-picked economists, "
        f"journalists, and industry leaders. Select the "
        f"{MACRO_COUNT + CORPORATE_INNOVATION_COUNT} most important stories/insights "
        f"and write commentary + CEFR C1-C2 vocabulary as instructed.\n\n"
        f"--- TWEETS ---\n{catalog}"
    )

    log.info("Calling DeepSeek (%s) with %d candidate tweets...",
             DEEPSEEK_MODEL, len(tweets))

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
            log.error("DeepSeek balance is 0. Top up at https://platform.deepseek.com")
        elif "401" in msg or "Authentication" in msg:
            log.error("DeepSeek API key is invalid. Check DEEPSEEK_API_KEY secret.")
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

    selected: list[Tweet] = []
    for category in ("macro", "corporate_innovation"):
        picks = parsed.get(category, [])
        for rank, pick in enumerate(picks, start=1):
            try:
                tid = int(pick["id"])
                t = tweets[tid]
            except (KeyError, ValueError, IndexError) as e:
                log.warning("Skipping invalid pick %s: %s", pick, e)
                continue
            t.category = category
            t.rank = rank
            t.commentary = pick.get("commentary", "").strip()

            vocab = []
            for v in pick.get("vocabulary", []):
                try:
                    word = str(v.get("word", "")).strip()
                    jp = str(v.get("japanese", "")).strip()
                    cefr = str(v.get("cefr", "C1")).strip().upper()
                    if word and jp:
                        vocab.append(VocabEntry(word=word, japanese=jp, cefr=cefr))
                except (AttributeError, TypeError):
                    continue
            t.vocabulary = vocab

            also = []
            for aid in pick.get("also_from_ids", []):
                try:
                    also.append("@" + tweets[int(aid)].author_username)
                except (ValueError, IndexError):
                    continue
            t.also_from = sorted(set(also) - {"@" + t.author_username})
            selected.append(t)

    log.info("AI selected %d tweets (%d macro + %d corp/innov)",
             len(selected),
             sum(1 for x in selected if x.category == "macro"),
             sum(1 for x in selected if x.category == "corporate_innovation"))
    return selected


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _collect_all_vocab(tweets: list[Tweet]) -> list[tuple[VocabEntry, int]]:
    result = []
    for idx, t in enumerate(tweets, start=1):
        for v in t.vocabulary:
            result.append((v, idx))
    return result


def build_email_html(tweets: list[Tweet], edition_en: str) -> str:
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    macro = [t for t in tweets if t.category == "macro"]
    corp = [t for t in tweets if t.category == "corporate_innovation"]

    # ---------- Vocabulary index ----------
    all_vocab = _collect_all_vocab(tweets)
    vocab_section = ""
    if all_vocab:
        vocab_rows = []
        for i, (v, idx) in enumerate(all_vocab):
            cefr_color = "#7a3fd8" if v.cefr == "C2" else "#1a4d8c"
            cefr_bg = "#f3ebff" if v.cefr == "C2" else "#eaf2fb"
            row_bg = "#ffffff" if i % 2 == 0 else "#faf8ff"
            vocab_rows.append(
                f'<tr style="background:{row_bg};">'
                f'<td style="padding:8px 10px;vertical-align:middle;white-space:nowrap;'
                f'font-size:11px;color:{cefr_color};font-weight:700;">'
                f'<span style="display:inline-block;background:{cefr_bg};'
                f'padding:2px 7px;border-radius:8px;">'
                f'#{idx} &middot; {escape(v.cefr)}</span>'
                f'</td>'
                f'<td style="padding:8px 10px;vertical-align:middle;'
                f'font-size:14px;font-weight:600;color:#111;">{escape(v.word)}</td>'
                f'<td style="padding:8px 10px;vertical-align:middle;'
                f'font-size:14px;color:#555;width:100%;">{escape(v.japanese)}</td>'
                f'</tr>'
            )
        vocab_section = f"""
          <tr><td style="padding:18px 0 6px 0;">
            <div style="font-size:13px;font-weight:700;color:#7a3fd8;text-transform:uppercase;
                        letter-spacing:0.5px;border-bottom:2px solid #7a3fd8;padding-bottom:6px;">
              📚 Vocabulary (CEFR C1-C2)
            </div>
            <div style="font-size:11px;color:#888;margin-top:3px;">
              Advanced English vocabulary from today's tweets, with Japanese translations.
              <span style="color:#aaa;">#N = tweet number</span>
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
    def _section(title: str, subtitle: str, section_tweets: list[Tweet], start_idx: int) -> str:
        if not section_tweets:
            return ""
        rows = []
        for i, t in enumerate(section_tweets, start=start_idx):
            local_time = t.created_at.astimezone().strftime("%H:%M %Z")
            n_from = 1 + len(t.also_from)
            if n_from >= 2:
                badge = (
                    f'<span style="display:inline-block;background:#d1f5e0;color:#0a6b2c;'
                    f'font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;'
                    f'margin-left:6px;">📢 {n_from} accounts</span>'
                )
                also_line = (
                    f'<div style="color:#888;font-size:11px;margin-top:3px;">'
                    f'Also tweeting: {escape(", ".join(t.also_from[:6]))}'
                    f'{"…" if len(t.also_from) > 6 else ""}'
                    f'</div>'
                )
            else:
                badge = ""
                also_line = ""

            verified_mark = ' <span style="color:#1d9bf0;">✓</span>' if t.author_verified else ""
            engagement = (
                f'<span style="color:#888;font-size:11px;">'
                f'❤️ {t.like_count:,} &middot; 🔁 {t.retweet_count:,} &middot; 💬 {t.reply_count:,}'
                f'</span>'
            )

            # The tweet text itself (this replaces the RSS headline)
            tweet_text_html = (
                f'<div style="color:#111;font-size:14px;line-height:1.5;'
                f'margin-top:6px;white-space:pre-wrap;">{escape(t.text)}</div>'
            )

            commentary_html = ""
            if t.commentary:
                commentary_html = (
                    f'<div style="background:#f8f9fb;border-left:3px solid #1a4d8c;'
                    f'padding:10px 14px;margin-top:10px;color:#222;font-size:13px;'
                    f'line-height:1.6;border-radius:0 4px 4px 0;">'
                    f'{escape(t.commentary)}</div>'
                )

            vocab_inline = ""
            if t.vocabulary:
                vocab_rows_local = []
                for v in t.vocabulary:
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
                    <div>
                      <a href="{escape(t.url)}" style="color:#1a4d8c;text-decoration:none;font-weight:600;font-size:14px;">
                        {escape(t.author_display)}{verified_mark}
                        <span style="color:#888;font-weight:normal;">@{escape(t.author_username)}</span>
                      </a>{badge}
                    </div>
                    <div style="color:#666;font-size:11px;margin-top:2px;">
                      {local_time} &middot; {engagement}
                    </div>
                    {also_line}
                    {tweet_text_html}
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
        Today's Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Tweets
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
        Curated by AI (DeepSeek) from tweets by hand-picked economists, journalists, and industry leaders.
      </div>
    </td></tr>
  </table>
</body></html>"""


def build_email_text(tweets: list[Tweet], edition_en: str) -> str:
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    macro = [t for t in tweets if t.category == "macro"]
    corp = [t for t in tweets if t.category == "corporate_innovation"]
    lines = [
        f"Today's Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Tweets -- {edition_en}",
        today_str,
        "=" * 60,
        "",
    ]

    all_vocab = _collect_all_vocab(tweets)
    if all_vocab:
        lines.append("## 📚 VOCABULARY (CEFR C1-C2)")
        lines.append("-" * 60)
        for v, idx in all_vocab:
            lines.append(f"  #{idx} [{v.cefr}] {v.word} = {v.japanese}")
        lines.append("")

    def _add(header: str, section: list[Tweet], start_idx: int) -> None:
        if not section:
            return
        lines.append(f"## {header}")
        lines.append("-" * 60)
        for i, t in enumerate(section, start=start_idx):
            local_time = t.created_at.astimezone().strftime("%H:%M %Z")
            n_from = 1 + len(t.also_from)
            badge = f" [📢 {n_from} accounts]" if n_from >= 2 else ""
            verified = " ✓" if t.author_verified else ""
            lines.append(f"{i}. @{t.author_username}{verified} ({t.author_display}){badge}")
            lines.append(f"   {local_time} | ❤️ {t.like_count:,} 🔁 {t.retweet_count:,}")
            if t.also_from:
                lines.append(f"   Also tweeting: {', '.join(t.also_from[:6])}"
                             f"{'…' if len(t.also_from) > 6 else ''}")
            lines.append(f"   \"{t.text[:500]}{'…' if len(t.text) > 500 else ''}\"")
            if t.commentary:
                lines.append(f"   💬 {t.commentary}")
            if t.vocabulary:
                vs = ", ".join(f"{v.word}={v.japanese}({v.cefr})" for v in t.vocabulary)
                lines.append(f"   📚 {vs}")
            lines.append(f"   {t.url}")
            lines.append("")

    _add("MACRO -- World & US economy", macro, 1)
    _add("CORPORATE & INNOVATION", corp, len(macro) + 1)
    return "\n".join(lines)


def send_email(tweets: list[Tweet]) -> None:
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
        f"🐦 Top {MACRO_COUNT + CORPORATE_INNOVATION_COUNT} Tweets -- "
        f"{edition_en} ({datetime.now().strftime('%b %d')})"
    )
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(build_email_text(tweets, edition_en))
    msg.add_alternative(build_email_html(tweets, edition_en), subtype="html")

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
    log.info("=== econ_news_bot (Twitter + AI) run start ===")
    tweets = fetch_all_tweets()
    if not tweets:
        log.warning("No tweets fetched. Skipping email.")
        return 1

    tweets = filter_tweets(tweets)
    if not tweets:
        log.warning("No tweets left after filtering. Skipping email.")
        return 1

    tweets = trim_for_ai(tweets)
    log.info("Sending %d tweets to AI for selection", len(tweets))

    selected = select_with_ai(tweets)
    if not selected:
        log.warning("AI returned no tweets. Skipping email.")
        return 1

    selected.sort(key=lambda t: (
        0 if t.category == "macro" else 1, t.rank,
    ))

    log.info("Top %d picks:", len(selected))
    for t in selected:
        log.info("  %s #%d [@%s, %d vocab] %s",
                 t.category, t.rank, t.author_username,
                 len(t.vocabulary), t.text[:60])

    send_email(selected)
    log.info("=== econ_news_bot run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
