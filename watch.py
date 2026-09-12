#!/usr/bin/env python3
"""
Just Eat IT courier — e-bike slot watcher with Telegram alerts.

Two independent signals, both read from one GET
-----------------------------------------------
https://www.justeat.it/en/courier/form embeds its whole recruitment config in
an inline `window.language = {...}` blob. For every city that blob carries
*two* separate statements about what is being recruited, and they can disagree:

1. `form_questions` -> the "Vehicle Selection" question (`data_key`
   `vehicle_type`). Its `options` map is what the Step-4 dropdown renders:

       {"Driver Bike": false, "Driver E-Bike": false, "Driver Scooter": true,
        "Driver Car / Kombi": true, ...}

2. `job_postings` -> concrete adverts for a specific (shift, vehicle) pair:

       {"option_1": "Friday and weekend evenings", "option_2": "Driver E-Bike"}

Signal 2 can advertise a vehicle that signal 1 still marks `false` — that is
exactly the state Genoa is in as of 12 Sep 2026, and it is why the first
version of this script (which read only signal 1) stayed silent through a real
e-bike opening. We now watch the union of both and say which one fired.

Usage
-----
    python watch.py --list              # every city, both signals
    python watch.py --diagnose Genoa    # full detail for one city
    python watch.py --test-telegram     # verify token/chat id
    python watch.py --once              # single check (use from cron/Actions)
    python watch.py --once --poll 22    # keep checking for 22 minutes, then exit
    python watch.py                     # loop forever
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Windows: the console is cp1252 when stdout is redirected to a file (as the
# scheduled task does), so printing a bike glyph or an accented city name would
# raise UnicodeEncodeError and kill the run. Force UTF-8, never die on a glyph.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "watch.log"

# Overridable so selftest.py can point at a local fixture.
FORM_URL = os.environ.get("JE_FORM_URL", "https://www.justeat.it/en/courier/form")
APPLY_URL = "https://www.justeat.it/en/courier/form?city={slug}&page=city"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# The vehicle question is identified by its data_key first; the option-key
# heuristic is the fallback for when Just Eat renames that.
VEHICLE_DATA_KEY = "vehicle_type"
VEHICLE_HINT = re.compile(r"bike|scooter|roller|kombi|car|vehicle|walker", re.I)

# What counts as a hit. Default: e-bike only — a pedal bike is a different job.
# Override with "vehicle_pattern" in config.json.
DEFAULT_VEHICLE_PATTERN = r"e-?\s?bike"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------- config


def default_config() -> dict:
    return {
        "telegram_token": "PASTE_BOT_TOKEN_HERE",
        "chat_id": "PASTE_CHAT_ID_HERE",
        "cities": ["Genoa"],
        "vehicle_pattern": DEFAULT_VEHICLE_PATTERN,
        "vehicle_label": "e-bike",
        "alert_on_any_city": False,
        "interval_minutes": 30,
        "notify_on_any_change": False,
        "heartbeat_hours": 24,
    }


def apply_env_overrides(cfg: dict) -> dict:
    """Env vars beat config.json, so secrets never live in a committed file.

    Used by the GitHub Actions runner, which reads them from repo secrets.
    """
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg["telegram_token"] = os.environ["TELEGRAM_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("WATCH_CITIES"):
        cfg["cities"] = [c.strip() for c in os.environ["WATCH_CITIES"].split(",") if c.strip()]
    if os.environ.get("WATCH_VEHICLE_PATTERN"):
        cfg["vehicle_pattern"] = os.environ["WATCH_VEHICLE_PATTERN"]
    return cfg


def vehicle_matcher(cfg: dict) -> re.Pattern:
    pat = str(cfg.get("vehicle_pattern") or DEFAULT_VEHICLE_PATTERN)
    try:
        return re.compile(pat, re.I)
    except re.error as exc:
        log(f"bad vehicle_pattern {pat!r} ({exc}) — falling back to default")
        return re.compile(DEFAULT_VEHICLE_PATTERN, re.I)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"could not read {path.name}: {exc}")
        return default


# -------------------------------------------------------------------- parsing


def extract_language_blob(html: str) -> dict:
    """Pull the `window.language = {...}` JSON out of the page HTML."""
    m = re.search(r"window\.language\s*=\s*", html)
    if not m:
        raise ValueError("window.language not found — page layout changed")
    obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    if not isinstance(obj, dict):
        raise ValueError("window.language was not an object")
    return obj


def vehicle_question(city: dict) -> dict | None:
    """The Step-4 vehicle dropdown, by data_key first and by shape second."""
    questions = city.get("form_questions") or []

    for q in questions:
        fq = q.get("form_question")
        if isinstance(fq, dict) and fq.get("data_key") == VEHICLE_DATA_KEY:
            if isinstance(q.get("options"), dict):
                return q

    for q in questions:
        opts = q.get("options")
        if not isinstance(opts, dict) or len(opts) < 3:
            continue
        if sum(1 for k in opts if VEHICLE_HINT.search(k)) >= 3:
            return q
    return None


def city_postings(city: dict) -> list[dict]:
    """Concrete adverts: [{"vehicle": "Driver E-Bike", "shift": "..."}, ...].

    Shape is job_postings[].attributes.postings[].attributes, where option_2 is
    the vehicle and option_1 the shift. Anything unexpected is skipped rather
    than raising — one malformed advert must not blind the other signal.
    """
    out: list[dict] = []
    for block in city.get("job_postings") or []:
        if not isinstance(block, dict):
            continue
        inner = (block.get("attributes") or {}).get("postings")
        for posting in inner or []:
            if not isinstance(posting, dict):
                continue
            attrs = posting.get("attributes") or {}
            if not isinstance(attrs, dict):
                continue
            vehicle = attrs.get("option_2") or attrs.get("vehicle")
            shift = attrs.get("option_1") or attrs.get("shift")
            if not vehicle:
                continue
            entry = {"vehicle": str(vehicle), "shift": str(shift) if shift else ""}
            if entry not in out:
                out.append(entry)
    return out


def city_signals(html: str) -> dict[str, dict]:
    """-> {key: {name, names, slug, dropdown, postings, offered}}

    Keyed by city_option_id where present, so the duplicate "Genoa"/"Genova"
    entries Just Eat ships for the same city (both city_option_id 202) collapse
    into one watched city instead of alerting twice or splitting the state.
    """
    lang = extract_language_blob(html)
    cities = lang.get("city_options") or []
    if not cities:
        raise ValueError("no city_options in window.language")

    out: dict[str, dict] = {}
    for c in cities:
        name = c.get("name")
        if not name:
            continue
        q = vehicle_question(c)
        postings = city_postings(c)
        if q is None and not postings:
            continue

        dropdown = sorted(k for k, v in (q or {}).get("options", {}).items() if v)
        coid = c.get("city_option_id")
        key = f"coid:{coid}" if coid is not None else (c.get("slug") or name)

        entry = out.get(key)
        if entry is None:
            out[key] = {
                "name": name,
                "names": [name],
                "slug": c.get("slug") or "",
                "city_option_id": coid,
                "dropdown": dropdown,
                "postings": list(postings),
            }
        else:
            # Same city under a second display name: union both signals.
            if name not in entry["names"]:
                entry["names"].append(name)
            entry["dropdown"] = sorted(set(entry["dropdown"]) | set(dropdown))
            for p in postings:
                if p not in entry["postings"]:
                    entry["postings"].append(p)
            if not entry["slug"]:
                entry["slug"] = c.get("slug") or ""

    if not out:
        raise ValueError("found cities but no vehicle question or job posting in any of them")

    for entry in out.values():
        entry["offered"] = sorted(
            set(entry["dropdown"]) | {p["vehicle"] for p in entry["postings"]}
        )
    return out


def matches(pattern: re.Pattern, vehicles) -> list[str]:
    return [v for v in vehicles if pattern.search(v)]


def hits(pattern: re.Pattern, info: dict) -> dict:
    """Which of the two signals currently advertise the wanted vehicle."""
    from_dropdown = matches(pattern, info["dropdown"])
    from_postings = [p for p in info["postings"] if pattern.search(p["vehicle"])]
    return {
        "dropdown": from_dropdown,
        "postings": from_postings,
        "open": bool(from_dropdown or from_postings),
    }


def is_watched(info: dict, watched: list[str]) -> bool:
    candidates = {n.lower() for n in info.get("names") or [info["name"]]}
    if info.get("slug"):
        candidates.add(info["slug"].lower())
    return bool(candidates & set(watched))


def fetch_html() -> str:
    r = requests.get(
        FORM_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB,en;q=0.9",
            # The page is served `max-age=300, private`; ask for a fresh copy so
            # no proxy hands us a five-minute-old view of a short opening.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.text


# ------------------------------------------------------------------- telegram


PLACEHOLDER_PREFIXES = ("PASTE", "SET_VIA")


def _unset(value: str) -> bool:
    return not value or value.startswith(PLACEHOLDER_PREFIXES)


def send_telegram(cfg: dict, text: str) -> bool:
    token = str(cfg.get("telegram_token", ""))
    chat_id = str(cfg.get("chat_id", ""))
    if _unset(token) or _unset(chat_id):
        log("Telegram not configured — message not sent:\n" + text)
        return False

    base = cfg.get("telegram_api_base", "https://api.telegram.org")
    try:
        r = requests.post(
            f"{base}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:300]}")
            return False
        return True
    except requests.RequestException as exc:
        log(f"Telegram request failed: {exc}")
        return False


# --------------------------------------------------------------------- alerts


def describe_postings(postings: list[dict]) -> str:
    bits = []
    for p in postings:
        bits.append(f"{p['vehicle']} — {p['shift']}" if p["shift"] else p["vehicle"])
    return "; ".join(bits)


def open_message(info: dict, hit: dict, label: str) -> str:
    link = APPLY_URL.format(slug=info["slug"])
    lines = [f"🚲 <b>{label.upper()} IS OPEN IN {info['name'].upper()}!</b>", ""]

    if hit["postings"]:
        lines.append(f"📋 Job advert: <b>{describe_postings(hit['postings'])}</b>")
    if hit["dropdown"]:
        lines.append(f"📝 Application form offers: <b>{', '.join(hit['dropdown'])}</b>")
    if hit["postings"] and not hit["dropdown"]:
        lines.append(
            "\n<i>This is advertised as a job posting while the form's vehicle "
            "dropdown still hides it. Open the form and check Step 4 — if it "
            "isn't listed there, apply through the posting on the city page.</i>"
        )

    lines += [
        "",
        f"All vehicles currently offered: {', '.join(info['offered']) or '—'}",
        "",
        f'<a href="{link}">Apply now</a>',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- check


def check_once(cfg: dict) -> int:
    state = load_json(STATE_PATH, {}) or {}
    now = datetime.now(timezone.utc)
    pattern = vehicle_matcher(cfg)
    label = str(cfg.get("vehicle_label") or "e-bike")

    try:
        data = city_signals(fetch_html())
    except Exception as exc:
        fails = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = fails
        log(f"check failed ({fails} in a row): {exc}")
        if fails in (5, 20, 100):
            send_telegram(
                cfg,
                f"⚠️ Just Eat watcher has failed {fails} times in a row.\n"
                f"<code>{str(exc)[:250]}</code>\n"
                f"Just Eat probably changed the page — the parser needs updating.",
            )
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return 1

    state["consecutive_failures"] = 0
    watched = [c.strip().lower() for c in cfg.get("cities") or ["Genoa"]]
    prev = state.get("cities") or {}
    messages: list[str] = []

    for key, info in sorted(data.items(), key=lambda kv: kv[1]["name"]):
        watching = is_watched(info, watched)
        hit = hits(pattern, info)

        before = prev.get(key)
        if before is None:  # schema 1 keyed state by display name
            before = prev.get(info["name"]) or {}
        # Schema 1 had a "bike" flag and no "open"; read it as the previous
        # answer so upgrading doesn't replay an alert the user already got.
        open_before = bool(before.get("open", before.get("bike", False)))
        known_before = bool(before)
        offered_before = before.get("offered", before.get("available"))

        if watching and hit["open"] and not open_before:
            messages.append(open_message(info, hit, label))
        elif watching and open_before and not hit["open"]:
            messages.append(
                f"🚲 {label.title()} closed again in {info['name']}. Still watching."
            )
        elif (
            watching
            and cfg.get("notify_on_any_change")
            and offered_before is not None
            and offered_before != info["offered"]
        ):
            messages.append(
                f"ℹ️ <b>{info['name']}</b> vehicle options changed\n"
                f"Before: {', '.join(offered_before) or '—'}\n"
                f"Now: {', '.join(info['offered']) or '—'}"
            )
        elif (
            not watching
            and cfg.get("alert_on_any_city")
            and hit["open"]
            and not open_before
            and known_before
        ):
            link = APPLY_URL.format(slug=info["slug"])
            messages.append(
                f"🚲 {label.title()} opened in <b>{info['name']}</b> "
                f'(not a city you watch)\n<a href="{link}">Apply</a>'
            )

    for msg in messages:
        send_telegram(cfg, msg)
    if messages:
        log(f"{len(messages)} alert(s) sent")

    watched_now = {k: v for k, v in data.items() if is_watched(v, watched)}
    if not watched_now:
        log(f"WARNING: none of {cfg.get('cities')} matched any city on the page")

    def summarise(info: dict) -> str:
        hit = hits(pattern, info)
        post = f" | advert: {describe_postings(info['postings'])}" if info["postings"] else ""
        return (f"{info['name']}: {'MATCH' if hit['open'] else 'no'} "
                f"[{', '.join(info['dropdown']) or 'none'}]{post}")

    log(
        "check ok — "
        + "; ".join(summarise(v) for v in watched_now.values())
        + f" ({len(data)} cities scanned)"
    )

    # Proof of life, so silence stays meaningful.
    hb_hours = float(cfg.get("heartbeat_hours", 0) or 0)
    if hb_hours > 0:
        last = state.get("last_heartbeat")
        due = True
        if last:
            try:
                due = (now - datetime.fromisoformat(last)).total_seconds() >= hb_hours * 3600
            except ValueError:
                due = True
        if due:
            lines = []
            for info in watched_now.values():
                hit = hits(pattern, info)
                lines.append(
                    f"• {info['name']}: {'🚲 OPEN' if hit['open'] else 'no ' + label}"
                    f" — form offers {', '.join(info['dropdown']) or 'nothing'}"
                    + (f"; advert: {describe_postings(info['postings'])}"
                       if info["postings"] else "")
                )
            elsewhere = sorted(v["name"] for v in data.values() if hits(pattern, v)["open"])
            send_telegram(
                cfg,
                "✅ Watcher alive.\n" + "\n".join(lines)
                + f"\n\n{label.title()} open anywhere in Italy: "
                + (", ".join(elsewhere) or "nowhere"),
            )
            state["last_heartbeat"] = now.isoformat(timespec="seconds")

    state["cities"] = {
        k: {
            "name": v["name"],
            "slug": v["slug"],
            "dropdown": v["dropdown"],
            "postings": v["postings"],
            "offered": v["offered"],
            "open": hits(pattern, v)["open"],
        }
        for k, v in data.items()
    }
    state["last_check"] = now.isoformat(timespec="seconds")
    state["schema"] = 2
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


# -------------------------------------------------------------------- reports


def cmd_list(cfg: dict) -> int:
    pattern = vehicle_matcher(cfg)
    data = city_signals(fetch_html())
    width = max(len(v["name"]) for v in data.values())
    for info in sorted(data.values(), key=lambda v: v["name"]):
        mark = "*" if hits(pattern, info)["open"] else " "
        post = f"   [advert: {describe_postings(info['postings'])}]" if info["postings"] else ""
        print(f"{mark} {info['name'].ljust(width)}  {', '.join(info['dropdown']) or '-'}{post}")
    print(f"\n{len(data)} cities.")
    return 0


def cmd_diagnose(cfg: dict, wanted: str) -> int:
    pattern = vehicle_matcher(cfg)
    data = city_signals(fetch_html())
    found = [v for v in data.values()
             if wanted.lower() in {n.lower() for n in v["names"]} | {v["slug"].lower()}]
    if not found:
        print(f"No city matching {wanted!r}. Names: "
              + ", ".join(sorted(v["name"] for v in data.values())))
        return 1
    for info in found:
        hit = hits(pattern, info)
        print(f"City:            {info['name']}  (slug {info['slug']}, "
              f"city_option_id {info['city_option_id']}, aliases {info['names']})")
        print(f"Form dropdown:   {', '.join(info['dropdown']) or '-'}")
        print(f"Job postings:    {describe_postings(info['postings']) or '-'}")
        print(f"Union offered:   {', '.join(info['offered']) or '-'}")
        print(f"Pattern:         {pattern.pattern}")
        print(f"Match dropdown:  {hit['dropdown'] or '-'}")
        print(f"Match postings:  {describe_postings(hit['postings']) or '-'}")
        print(f"=> OPEN:         {hit['open']}")
    return 0


def poll_waits(poll_minutes: float, interval_minutes: float) -> list[float]:
    """Sleeps for a --poll window, after the first check has already happened.

    The final wait is shortened rather than dropped, so the window always ends
    with a check right on the deadline instead of one gap short of it.
    """
    total = max(0.0, poll_minutes) * 60
    gap = max(30.0, interval_minutes * 60)
    waits: list[float] = []
    elapsed = 0.0
    while total - elapsed >= 1:
        wait = min(gap, total - elapsed)
        waits.append(wait)
        elapsed += wait
    return waits


def main() -> int:
    ap = argparse.ArgumentParser(description="Just Eat e-bike slot Telegram watcher")
    ap.add_argument("--list", action="store_true", help="print all cities and exit")
    ap.add_argument("--diagnose", metavar="CITY", help="show both signals for one city")
    ap.add_argument("--once", action="store_true", help="check once and exit (for cron)")
    ap.add_argument("--poll", type=float, metavar="MINUTES", default=0.0,
                    help="with --once: keep re-checking for this many minutes before "
                         "exiting. GitHub delivers scheduled runs hours late, so one "
                         "run that covers a window beats one instant sample.")
    ap.add_argument("--poll-interval", type=float, metavar="MINUTES", default=4.0,
                    help="gap between checks while --poll runs (default 4)")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(default_config(), indent=2), encoding="utf-8")
        log("created config.json — fill in telegram_token and chat_id, then re-run.")
        return 1

    cfg = load_json(CONFIG_PATH)
    if not cfg:
        return 1
    cfg = apply_env_overrides(cfg)

    if args.list:
        return cmd_list(cfg)

    if args.diagnose:
        return cmd_diagnose(cfg, args.diagnose)

    if args.test_telegram:
        ok = send_telegram(cfg, "🤖 Just Eat watcher connected. "
                                "I'll ping you when an e-bike slot opens in Genoa.")
        log("test message sent" if ok else "test message FAILED")
        return 0 if ok else 1

    if args.once:
        rc = check_once(cfg)
        for wait in poll_waits(args.poll, args.poll_interval):
            time.sleep(wait)
            rc = check_once(cfg)
        return rc

    interval = max(60, int(float(cfg.get("interval_minutes", 30)) * 60))
    log(f"watching every {interval // 60} min — Ctrl+C to stop")
    while True:
        try:
            check_once(cfg)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception as exc:
            log(f"unexpected error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
