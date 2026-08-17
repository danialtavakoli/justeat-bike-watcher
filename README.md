# Just Eat bike-slot → Telegram watcher (Genoa)

Just Eat only shows a vehicle on Step 4 of the courier form if they're actively
recruiting for it in that city. Genoa currently shows **Own Scooter** only.
This pings your Telegram the moment a bike option appears.

**Right now (17 Aug 2026)** the only Italian cities recruiting bikes are
Ciampino, Pavia, and the Riccione/Cattolica coastal area. Genoa: scooter only.

---

## How it works

The form page embeds its whole recruitment config in an inline
`window.language = {…}` blob. Inside it, every city has a Vehicle-step question
whose `options` map is exactly what Step 4 renders — for Genoa:

```json
{"Driver Bike": false, "Company Bike": false, "Driver E-Bike": false,
 "Company E-Bike": false, "Driver Scooter": true, "Company Scooter": false,
 "Driver E-Roller": false, "Driver Car / Kombi": false, ...}
```

So **one ordinary GET request** gives the truth for all 53 cities at once. No
headless browser, no clicking through the form, no personal data submitted
anywhere. It's a ~1-second check.

It has to run on your machine: the cloud sandbox I built this in has no
outbound internet — it can reach neither `justeat.it` nor `api.telegram.org` —
so a hosted/scheduled version isn't possible.

## Setup

### 1. Telegram bot (2 minutes)

1. Telegram → search **@BotFather** → `/newbot`.
2. Name it (`Just Eat Watcher`), username must end in `bot`.
3. Copy the token it gives you (`8123456789:AAH…`). Keep it private.
4. Search your new bot and press **Start** — it can't message you until you do.
5. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find
   `"chat":{"id":123456789` — that number is your `chat_id`.

### 2. Install and check it works

```bash
pip install requests
python watch.py --list
```

`--list` prints every city and its vehicles, with 🚲 next to any that has a
bike. If that looks right, the scraper works.

### 3. Configure

```bash
python watch.py --once      # creates config.json on first run
```

Edit `config.json`:

```json
{
  "telegram_token": "8123456789:AAH...",
  "chat_id": "123456789",
  "cities": ["Genoa"],
  "alert_on_any_city": false,
  "interval_minutes": 30,
  "notify_on_any_change": true,
  "heartbeat_hours": 24
}
```

- `cities` — add more names exactly as `--list` prints them (`["Genoa", "Milan"]`).
- `alert_on_any_city` — set `true` to also hear about bike openings anywhere in
  Italy. Useful if you'd relocate; noisy if you wouldn't.

```bash
python watch.py --test-telegram
```

You should get a message. If not, you skipped pressing **Start** on the bot.

### 4. Run it

Cron, so it survives reboots — `crontab -e`:

```cron
*/30 * * * * cd /full/path/to/justeat-bot && /usr/bin/python3 watch.py --once >> cron.log 2>&1
```

Or just `python watch.py` to run it in a terminal.

Every 30 minutes is plenty — these openings last days, not minutes.

## What you'll receive

| | |
|---|---|
| 🚲 **BIKE IS OPEN IN GENOA!** | the one you're waiting for — sent once, with the apply link |
| ℹ️ options changed | any other change to Genoa's vehicle list |
| ✅ watcher alive | once a day, plus where bike is open nationally, so silence stays meaningful |
| ⚠️ failure warning | after 5 failed checks in a row — means Just Eat changed the page |

## Files

| file | what it is |
|---|---|
| `watch.py` | the whole bot |
| `config.json` | token, chat id, cities, interval |
| `state.json` | last-seen vehicles per city, so you're told once per change |
| `watch.log` | every check, timestamped |
| `selftest.py` | offline test — fakes Just Eat and Telegram, verifies the parser and all alert logic |

## What's verified, and what isn't

Verified against the live page from your browser: the `window.language`
extraction works on the real HTML, and the "which question is the vehicle
question" heuristic agrees with a hardcoded lookup on **all 53 cities** —
correctly reporting Genoa as scooter-only, matching what you saw on Step 4.

`selftest.py` covers the rest offline: bike opens → one alert; bike stays →
no duplicate; bike closes; unrelated vehicle added; changes in unwatched
cities stay silent.

Not verified: that a plain `requests.get` (no browser, no cookies) gets the
same HTML — some sites gate that behind bot protection. `python watch.py --list`
is the one-command check, and it's the first thing to run.
