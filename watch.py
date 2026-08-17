#!/usr/bin/env python3
"""
Just Eat IT courier — bike-slot watcher with Telegram alerts.

How it works
------------
https://www.justeat.it/en/courier/form embeds the whole recruitment config in
an inline `window.language = {...}` blob. Inside it, every city carries a
`form_questions` entry for the Vehicle step whose `options` map says which
vehicles are actually being recruited, e.g. for Genoa:

    {"Driver Bike": false, "Company Bike": false, "Driver E-Bike": false,
     "Company E-Bike": false, "Driver Scooter": true, ...}

That map is exactly what Step 4 renders. So one ordinary GET — no browser, no
form filling, no personal data — tells us the truth for all 53 cities at once.

Usage
-----
    python watch.py --list            # print every city's current vehicles
    python watch.py --test-telegram   # verify token/chat id
    python watch.py --once            # single check (use from cron)
    python watch.py                   # loop forever
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
# scheduled task does), so printing 🚲 or an accented city name would raise
# UnicodeEncodeError and kill the run. Force UTF-8 and never die on a glyph.
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

# A vehicle question is identified by its option keys looking like vehicles,
# rather than by one hardcoded key, so a renamed option doesn't break us.
VEHICLE_HINT = re.compile(r"bike|scooter|roller|kombi|car|vehicle|walker", re.I)
BIKE_RE = re.compile(r"\bbike\b|\be-?bike\b|bicycle|bici", re.I)


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
        "alert_on_any_city": False,
        "interval_minutes": 30,
        "notify_on_any_change": True,
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
    return cfg


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
    for q in city.get("form_questions") or []:
        opts = q.get("options")
        if not isinstance(opts, dict) or len(opts) < 3:
            continue
        if sum(1 for k in opts if VEHICLE_HINT.search(k)) >= 3:
            return q
    return None


def city_vehicles(html: str) -> dict[str, dict]:
    """-> {city_name: {"slug": str, "available": [str, ...]}}"""
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
        if q is None:
            continue
        available = sorted(k for k, v in q["options"].items() if v)
        out[name] = {"slug": c.get("slug") or "", "available": available}
    if not out:
        raise ValueError("found cities but no vehicle question in any of them")
    return out


def has_bike(available: list[str]) -> bool:
    return any(BIKE_RE.search(v) for v in available)


def fetch_html() -> str:
    r = requests.get(
        FORM_URL,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


# ------------------------------------------------------------------- telegram


def send_telegram(cfg: dict, text: str) -> bool:
    token = str(cfg.get("telegram_token", ""))
    chat_id = str(cfg.get("chat_id", ""))
    if not token or token.startswith("PASTE") or not chat_id or chat_id.startswith("PASTE"):
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


# ---------------------------------------------------------------------- check


def check_once(cfg: dict) -> int:
    state = load_json(STATE_PATH, {}) or {}
    now = datetime.now(timezone.utc)

    try:
        data = city_vehicles(fetch_html())
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

    for name, info in sorted(data.items()):
        is_watched = name.lower() in watched
        bike_now = has_bike(info["available"])
        before = prev.get(name) or {}
        bike_before = bool(before.get("bike"))
        prev_avail = before.get("available")
        link = APPLY_URL.format(slug=info["slug"])

        if is_watched and bike_now and not bike_before:
            messages.append(
                f"🚲 <b>BIKE IS OPEN IN {name.upper()}!</b>\n\n"
                f"Vehicles now recruiting: {', '.join(info['available'])}\n\n"
                f'<a href="{link}">Apply now</a>'
            )
        elif is_watched and bike_before and not bike_now:
            messages.append(f"🚲 Bike closed again in {name}. Still watching.")
        elif (
            is_watched
            and cfg.get("notify_on_any_change")
            and prev_avail is not None
            and prev_avail != info["available"]
        ):
            messages.append(
                f"ℹ️ <b>{name}</b> vehicle options changed\n"
                f"Before: {', '.join(prev_avail) or '—'}\n"
                f"Now: {', '.join(info['available']) or '—'}"
            )
        elif (
            not is_watched
            and cfg.get("alert_on_any_city")
            and bike_now
            and not bike_before
            and prev_avail is not None
        ):
            messages.append(
                f"🚲 Bike opened in <b>{name}</b> (not a city you watch)\n"
                f'<a href="{link}">Apply</a>'
            )

    for msg in messages:
        send_telegram(cfg, msg)
    if messages:
        log(f"{len(messages)} alert(s) sent")

    watched_summary = {
        n: v["available"] for n, v in data.items() if n.lower() in watched
    }
    log(
        "check ok — "
        + "; ".join(f"{n}: {', '.join(v) or 'none'}" for n, v in watched_summary.items())
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
            lines = [f"• {n}: {', '.join(v) or 'nothing'}" for n, v in watched_summary.items()]
            bike_cities = sorted(n for n, v in data.items() if has_bike(v["available"]))
            send_telegram(
                cfg,
                "✅ Watcher alive.\n" + "\n".join(lines)
                + f"\n\nBike open anywhere in Italy: {', '.join(bike_cities) or 'nowhere'}",
            )
            state["last_heartbeat"] = now.isoformat(timespec="seconds")

    state["cities"] = {
        n: {"available": v["available"], "bike": has_bike(v["available"]), "slug": v["slug"]}
        for n, v in data.items()
    }
    state["last_check"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def cmd_list() -> int:
    data = city_vehicles(fetch_html())
    width = max(len(n) for n in data)
    for name, info in sorted(data.items()):
        mark = "🚲" if has_bike(info["available"]) else "  "
        print(f"{mark} {name.ljust(width)}  {', '.join(info['available']) or '—'}")
    print(f"\n{len(data)} cities.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Just Eat bike-slot Telegram watcher")
    ap.add_argument("--list", action="store_true", help="print all cities and exit")
    ap.add_argument("--once", action="store_true", help="check once and exit (for cron)")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message")
    args = ap.parse_args()

    if args.list:
        return cmd_list()

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(default_config(), indent=2), encoding="utf-8")
        log("created config.json — fill in telegram_token and chat_id, then re-run.")
        return 1

    cfg = load_json(CONFIG_PATH)
    if not cfg:
        return 1
    cfg = apply_env_overrides(cfg)

    if args.test_telegram:
        ok = send_telegram(cfg, "🤖 Just Eat watcher connected. I'll ping you when bike opens.")
        log("test message sent" if ok else "test message FAILED")
        return 0 if ok else 1

    if args.once:
        return check_once(cfg)

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
