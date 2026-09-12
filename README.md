# Just Eat e-bike slot → Telegram watcher (Genoa)

Pings your Telegram the moment Just Eat starts recruiting **e-bike** couriers
in Genoa.

**Status as of 12 Sep 2026: Genoa e-bike IS open** — advertised as
`Driver E-Bike — Friday and weekend evenings`.

---

## The bug this repo had, and why it mattered

The first version watched one signal and missed a real opening.

Just Eat's courier form embeds its whole recruitment config in an inline
`window.language = {…}` blob, and that blob states **twice** what a city is
recruiting — in two places that do not agree with each other:

| Signal | Where | Genoa, 12 Sep 2026 |
|---|---|---|
| Step-4 vehicle dropdown | `city.form_questions[]` where `data_key == "vehicle_type"` → `options` | `"Driver E-Bike": false` |
| Job advert | `city.job_postings[].attributes.postings[].attributes` | `{"option_1": "Friday and weekend evenings", "option_2": "Driver E-Bike"}` |

The original `watch.py` read only the first one, so it recorded
`Genoa → bike: false` and stayed silent while the second one was openly
advertising an e-bike job. That is the gap someone else applied through.

Genoa is not a fluke of one field being stale — the two signals are maintained
independently. Busto Arsizio currently advertises `Driver Scooter` in a posting
while its dropdown offers only `Driver Car / Kombi`.

**The watcher now takes the union of both signals**, and the alert says which
one fired so you know whether to use the form's Step 4 or the posting on the
city page.

## How it works

One ordinary GET of `https://www.justeat.it/en/courier/form` returns the truth
for all 53 cities at once. No headless browser, no clicking through the form,
no personal data submitted anywhere. A check takes about a second.

## Commands

```bash
python watch.py --list              # every city: dropdown + any job adverts
python watch.py --diagnose Genoa    # both signals for one city, and the verdict
python watch.py --test-telegram     # verify token / chat id
python watch.py --once              # single check (what Actions runs)
python watch.py --once --poll 10    # keep checking for 10 min, then exit
python watch.py                     # loop forever, interval_minutes apart
python selftest.py                  # offline test suite, no network needed
```

`--diagnose` is the one to reach for when you suspect a miss:

```
City:            Genoa  (slug genoa, city_option_id 202, aliases ['Genoa'])
Form dropdown:   Driver Car / Kombi, Driver Scooter
Job postings:    Driver E-Bike — Friday and weekend evenings
Pattern:         e-?\s?bike
Match dropdown:  -
Match postings:  Driver E-Bike — Friday and weekend evenings
=> OPEN:         True
```

## Configuration

`config.json` (env vars win, so secrets never live in a committed file):

| Key | Meaning |
|---|---|
| `cities` | Matched against each city's display name **and** slug. Genoa ships under both `Genoa` and `Genova`; both are listed, and they collapse into one city by `city_option_id` so you can't get double alerts. |
| `vehicle_pattern` | Regex for what counts as a hit. Default `e-?\s?bike` — matches `Driver E-Bike` and `Company E-Bike`, deliberately **not** plain `Driver Bike` (a different job) or `Driver E-Roller`. Set it to `bike` to go back to any bike. |
| `vehicle_label` | What to call it in messages. |
| `notify_on_any_change` | Off. Genoa's `Driver Car / Kombi` flag flipped four times in September; that is not news. |
| `alert_on_any_city` | Off. Turn on to hear about e-bikes in cities you don't watch. |
| `heartbeat_hours` | A daily "still alive" message, so silence stays meaningful. |

Env overrides: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `WATCH_CITIES`,
`WATCH_VEHICLE_PATTERN`.

## How often it actually runs

The workflow asks for `*/30 * * * *`. **GitHub does not honour that.** On a
private repo, scheduled runs are deprioritised: the real delivery over the last
month has been **6–8 runs a day, 1–5 hours apart**. `gh run list` shows it.

`--poll` exists to widen each run into a window, and the workflow deliberately
**does not use it**, because the arithmetic doesn't work on a private repo:
2000 free Actions minutes a month, every started minute billed, ~7 runs a day.

| Per-run poll | Extra coverage of a 3-hour gap | Minutes/month |
|---|---|---|
| none (current) | — | ~210 |
| 4 min | 2% | ~1050 |
| 25 min | 14% | ~5500 — **quota dies mid-month** |

Buying 2% for 5× the quota is a bad trade, and exhausting the quota stops the
watcher completely. Cheap and alive beats frequent and dead.

If you want genuinely tighter coverage, pick one:

- **Make this repo public.** Actions minutes stop being metered, and
  `--poll 25 --poll-interval 5` in the workflow becomes the right call. The
  repo holds no secrets — they live in GitHub Secrets — only `state.json`.
- **Run it on your own PC.** `run_watcher.bat` via Task Scheduler, or just
  leave `python watch.py` running; then `interval_minutes` is honoured exactly
  and costs nothing.

Both are belt-and-braces: what actually went wrong was the missing signal, not
the cadence. The 18 Aug opening lasted about seven hours and *was* caught.

## Setup

### 1. Telegram bot

1. Telegram → **@BotFather** → `/newbot`, name it, username must end in `bot`.
2. Copy the token.
3. Message your new bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.

### 2. GitHub Actions (how it runs today)

Repo → Settings → Secrets and variables → Actions:

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

The workflow commits `state.json` back to the repo after every check — that's
the memory that stops it alerting twice for the same opening.

### 3. Or run it locally

```bash
pip install requests
python watch.py --test-telegram
python watch.py
```

On Windows, point Task Scheduler at `run_watcher.bat`.

## State

`state.json` is schema 2: keyed by `coid:<city_option_id>`, storing `dropdown`,
`postings`, `offered` and the `open` verdict per city. Schema-1 files (keyed by
display name, with a `bike` flag) are read transparently and upgraded in place
without replaying alerts you already received.
