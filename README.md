# Economic News Bot 📊

Automated digest of the **10 most important economic news stories** of the day, emailed twice daily at **7 AM** and **9 PM**.

## How it works

1. **Fetches** RSS feeds from 14 major outlets: Reuters, Bloomberg, FT, WSJ, CNBC, BBC, The Economist, MarketWatch, Yahoo Finance.
2. **Filters** to articles from the last ~14 hours.
3. **Scores** each story by three signals:
   - **Keyword impact** — heavy weight for Fed/ECB decisions, CPI, GDP, jobs reports, recessions, debt ceilings, etc. Title hits count double.
   - **Source authority** — Reuters/Bloomberg/FT weighted higher than aggregators.
   - **Recency** — newer stories score higher.
4. **Deduplicates** stories covering the same event from multiple outlets.
5. **Emails** the top 10 as a clean HTML digest with links, snippets, and timestamps.

## Setup

### 1. Install dependencies

```bash
cd econ_news_bot
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure email credentials

```bash
cp .env.example .env
```

Then edit `.env` and fill in your SMTP details. **Gmail is the easiest option:**

1. Enable 2-factor authentication on your Google account.
2. Generate an App Password: https://myaccount.google.com/apppasswords
3. Use that 16-character password as `SMTP_PASS` (NOT your regular Gmail password).

Other providers work too — Outlook (`smtp-mail.outlook.com:587`), Fastmail, SendGrid, AWS SES, etc.

### 3. Test it

```bash
python econ_news_bot.py
```

You should see log output and receive an email within ~20 seconds.

## Scheduling

### Linux / macOS — `cron`

```bash
crontab -e
```

Add these two lines (adjust the path to where you cloned the project):

```cron
0 7  * * * cd /path/to/econ_news_bot && /path/to/econ_news_bot/venv/bin/python econ_news_bot.py >> /tmp/econ_news.log 2>&1
0 21 * * * cd /path/to/econ_news_bot && /path/to/econ_news_bot/venv/bin/python econ_news_bot.py >> /tmp/econ_news.log 2>&1
```

Cron uses **server local time**. Run `date` to check what timezone your machine is on, and `timedatectl set-timezone <zone>` (Linux) if you need to change it.

### macOS — `launchd` (more reliable than cron when the laptop sleeps)

Create `~/Library/LaunchAgents/com.user.econnews.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.econnews</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/econ_news_bot/venv/bin/python</string>
    <string>/path/to/econ_news_bot/econ_news_bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/econ_news_bot</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/tmp/econ_news.log</string>
  <key>StandardErrorPath</key><string>/tmp/econ_news.err</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.user.econnews.plist
```

### Windows — Task Scheduler

1. Open **Task Scheduler** → **Create Task**.
2. **General** tab: name it "Econ News Bot", check "Run whether user is logged on or not".
3. **Triggers** tab: add two daily triggers, one at 07:00 and one at 21:00.
4. **Actions** tab: add an action:
   - Program: `C:\path\to\econ_news_bot\venv\Scripts\python.exe`
   - Arguments: `econ_news_bot.py`
   - Start in: `C:\path\to\econ_news_bot`

### Cloud (always-on, no laptop needed)

For a "set and forget" deployment that runs even when your computer is off:

- **GitHub Actions** (free for public repos): create a `.github/workflows/news.yml` with a `cron` schedule. Store SMTP credentials as repository secrets.
- **AWS Lambda + EventBridge**: package as a zip, schedule with two cron rules.
- **Railway / Render / Fly.io**: deploy and add a cron job.
- **Raspberry Pi**: same `cron` setup as Linux.

Ask if you'd like me to write the GitHub Actions workflow file — that's usually the simplest free option.

## Customization

- **Tune what counts as "impactful":** edit the `IMPACT_KEYWORDS` dictionary in `econ_news_bot.py`. Add your favorite topics (e.g. "ai chip", "ev market") with a weight 1.0–3.0.
- **Add or remove sources:** edit the `RSS_SOURCES` list. The third value (0.0–1.0) is the source authority weight.
- **Change the look-back window:** `LOOKBACK_HOURS` in `.env`. For two-a-day runs, 14 hours is a good default; for a single morning run, use 24.
- **Send more or fewer items:** change `TOP_N` in `.env`.

## Troubleshooting

- **"SMTPAuthenticationError"** with Gmail: you're using your account password instead of an App Password. Generate one at https://myaccount.google.com/apppasswords.
- **"No relevant news items found"**: either you ran it at 4 AM when feeds are quiet, or many feeds are temporarily down. Try widening `LOOKBACK_HOURS` to 24.
- **Some feeds 403**: outlets occasionally block bot user-agents. The bot already sends a custom UA; if a specific feed stays broken, remove it from `RSS_SOURCES`.
