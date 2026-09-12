# Just Eat e-bike slot → Telegram watcher (Genoa)

Pings your Telegram the moment Just Eat's courier form actually lets you pick
an **e-bike** in Genoa.

---

## What counts as "open"

The form page embeds its recruitment config in an inline `window.language = {…}`
blob, and that blob says two different things about each city:

| Signal | Where | Genoa, 12 Sep 2026 |
|---|---|---|
| **Step-4 vehicle dropdown** | `city.form_questions[]` where `data_key == "vehicle_type"` → `options` | `"Driver E-Bike": false` |
| Job advert | `city.job_postings[].attributes.postings[].attributes` | `{"option_1": "Friday and weekend evenings", "option_2": "Driver E-Bike"}` |

**Only the dropdown decides whether you can apply.** Step 4 states this itself:

> Please note that if your vehicle does not appear as an option, it means we
> are not currently searching for it.

The advert is marketing copy and goes stale. Genoa proved it on 12 Sep 2026:
the advert named `Driver E-Bike` while Step 4 offered only Own Scooter and Own
Car. A watcher that treats an advert as an opening cries wolf.

So alerts fire on the dropdown. Adverts are still parsed and recorded — they
show up in `--diagnose`, `--list` and the heartbeat — and you can promote them
to alerts with `alert_on_job_posting: true` if you want an early warning and
accept the false alarms. Those alerts are worded so you can tell them apart.

## Commands

```bash
python watch.py --list              # every city: Step 4 options + any adverts
python watch.py --diagnose Genoa    # both signals for one city, and the verdict
python watch.py --history Genoa     # every recorded open/close, with durations
python watch.py --test-telegram     # verify token / chat id
python watch.py --once              # single check
python watch.py --once --poll 50    # keep checking for 50 min, then exit
python watch.py                     # loop forever, interval_minutes apart
python selftest.py                  # offline test suite, no network needed
```

`--diagnose` is the one to reach for when you suspect a miss or a false alarm.
This is Genoa as of 12 Sep 2026 — advert up, slot closed:

```
Form dropdown:   Driver Car / Kombi, Driver Scooter
Job postings:    Driver E-Bike — Friday and weekend evenings
Match dropdown:  -
Match postings:  Driver E-Bike — Friday and weekend evenings
=> OPEN:         False
```

## History

`history.jsonl` is the permanent record: one JSON line per transition, appended
on every open and close, for **every** city — not just the watched ones, since
transitions are rare and it answers "was it open anywhere?" later.

```
$ python watch.py --history Genoa

Genoa
  start  2026-08-17T03:43:16Z  watching, currently closed
  OPEN   2026-08-18T09:38:18Z  Driver E-Bike  [dropdown]  (opened within the previous 0.93h)
  CLOSE  2026-08-18T16:36:44Z  — open for at least 7.0h
```

`gap_hours` on each line is how long since the previous check, so an "opened"
after a five-hour gap means *somewhere in those five hours*, not at that
timestamp. Don't read the timestamps as exact.

The file was seeded by `backfill_history.py`, which replays the `state: …`
commits back to 17 Aug 2026 and recovers the 94 transitions they imply — so the
record covers the whole monitored period, not just the part since the feature
was added. Backfilled lines are tagged `"origin": "backfill"`, live ones
`"live"`. Re-running it is a no-op.

Nothing before **17 Aug 2026** exists, anywhere. Just Eat's CDN returns 403 to
archive crawlers — even for `robots.txt` — so the Wayback Machine, Common Crawl
and archive.today all have zero captures of this page, and the page itself only
ever ships current state. That history is not hard to get; it is gone.

## Configuration

`config.json` (env vars win, so secrets never live in a committed file):

| Key | Meaning |
|---|---|
| `cities` | Matched against each city's display name **and** slug. Genoa ships under both `Genoa` and `Genova`; both are listed, and they collapse into one city by `city_option_id` so you can't get double alerts. |
| `vehicle_pattern` | Regex for what counts as a hit. Default `e-?\s?bike` — matches `Driver E-Bike` and `Company E-Bike`, deliberately **not** plain `Driver Bike` (a different job) or `Driver E-Roller`. Set it to `bike` to go back to any bike. |
| `vehicle_label` | What to call it in messages. |
| `alert_on_job_posting` | Off. On, a job advert alone will alert — early warning, with false alarms. |
| `notify_on_any_change` | Off. Genoa's `Driver Car / Kombi` flag flipped four times in September; that is not news. |
| `alert_on_any_city` | Off. Turn on to hear about e-bikes in cities you don't watch. |
| `heartbeat_hours` | A daily "still alive" message, so silence stays meaningful. |

Env overrides: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `WATCH_CITIES`,
`WATCH_VEHICLE_PATTERN`.

## How often it actually runs

The workflow asks for `*/30 * * * *`. **GitHub does not honour that.** Measured
delivery has been **6–8 runs a day, 1–5 hours apart** — `gh run list` shows it.

That gap is the thing most likely to make you miss a slot: on 18 Aug the Genoa
e-bike window lasted about seven hours, and a shorter one would fall straight
through. So each run now holds its runner and polls for ~50 minutes
(`--poll 50 --poll-interval 5`, 11 checks), and the concurrency queue starts
the next run as soon as one finishes — which in practice keeps a checker alive
most of the day at 5-minute resolution.

This only works because **the repo is public**: Actions minutes are unmetered
on public repos. On a private repo the same setting would burn the 2000-minute
monthly quota in days and stop the watcher completely. If you ever make it
private again, drop back to a plain `--once`.

Running `python watch.py` on your own machine is still the most reliable
option — `interval_minutes` is then honoured exactly and costs nothing.

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

`state.json` is "what it looks like right now" — the memory that stops repeat
alerts. `history.jsonl` is the permanent log. Both are committed back by the
workflow after every check.

`state.json` is schema 2: keyed by `coid:<city_option_id>`, storing `dropdown`,
`postings`, `offered` and the `open` verdict per city.

The previous verdict is **re-derived from the stored signals** on every run
rather than read back from the stored boolean. That means changing what counts
as open — flipping `alert_on_job_posting`, editing `vehicle_pattern` — can
never fake an "opened!" or "closed again" message on the next run. Schema-1
files (keyed by display name, with a `bike` flag) are read and upgraded in
place the same way.
