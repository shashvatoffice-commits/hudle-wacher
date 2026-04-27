# Hudle Padel Watcher

Polls Hudle every 5 min for newly-available padel slots at preferred Mumbai venues
and pings via Telegram.

**Venues monitored:**
- Willingdon Outdoor Sports Arena (Maali Khata)
- The Cricket Club of India
- Phoenix HSBC Racquet Club (Fri-Sun only, HSBC Padel Court)

**Filters:**
- Mon-Fri: 18:00-22:00 IST
- Sat-Sun: 08:00-11:00 + 17:00-22:00 IST (Willingdon, CCI)
- Phoenix: Fri-Sun all day
- Min continuous duration: 1.5 hours

## Setup (one-time)

1. Create a **private** GitHub repo (e.g. `hudle-watcher`).
2. Push this folder to it:
   ```bash
   cd ~/.claude/hudle-watcher
   git init -b main
   git add .
   git commit -m "initial"
   git remote add origin git@github.com:YOUR_USERNAME/hudle-watcher.git
   git push -u origin main
   ```
3. In the repo on github.com, go to **Settings → Secrets and variables → Actions → New repository secret** and add these five:

   | Name | Value |
   |---|---|
   | `HUDLE_TOKEN` | the long Bearer JWT from your captured cURL |
   | `HUDLE_API_SECRET` | `hudle-api1798@prod` |
   | `HUDLE_APP_ID` | `2501015753736145000537361080192024` |
   | `TELEGRAM_BOT_TOKEN` | `8714847794:AAEKMWF-RMXWACoW80D_Td8UTJF0wIDcfF8` |
   | `TELEGRAM_CHAT_ID` | `8486587853` |

4. Go to the **Actions** tab → "Hudle Padel Watcher" → "Run workflow" to trigger
   it manually the first time and confirm it works. After that the 5-min cron
   takes over automatically.

## When the auth token expires (~May 2027)

You'll get a Telegram message: "🔐 Hudle watcher: auth token expired."
Re-capture a fresh cURL from `hudle.in` DevTools (Network tab, any
`api.hudle.in` call), copy the new `Bearer ...` token, and update the
`HUDLE_TOKEN` secret in GitHub. No code change needed.

## Local testing

You can test the script locally before pushing:

```bash
export HUDLE_TOKEN="..."
export HUDLE_API_SECRET="hudle-api1798@prod"
export HUDLE_APP_ID="2501015753736145000537361080192024"
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python3 watch.py
```

State persists in `state.json` (gitignored). First run alerts on every currently
available slot; subsequent runs only alert on new ones.

## Tweaking

- Add/remove venues in `config.json`.
- Change time windows or min duration in `config.json`.
- Change cadence in `.github/workflows/watch.yml` (note: GitHub's minimum
  scheduled cron is 5 min; cron lower than that is silently rounded up).
