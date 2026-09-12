#!/usr/bin/env python3
"""
Just Eat IT courier — e-bike slot watcher with Telegram alerts.

What counts as "open", and what doesn't
---------------------------------------
https://www.justeat.it/en/courier/form embeds its whole recruitment config in
an inline `window.language = {...}` blob. For every city that blob carries
*two* statements about what is being recruited, and they can disagree:

1. `form_questions` -> the "Vehicle Selection" question (`data_key`
   `vehicle_type`). Its `options` map is exactly what Step 4 renders:

       {"Driver Bike": false, "Driver E-Bike": false, "Driver Scooter": true,
        "Driver Car / Kombi": true, ...}

2. `job_postings` -> adverts naming a (shift, vehicle) pair:

       {"option_1": "Friday and weekend evenings", "option_2": "Driver E-Bike"}

**Signal 1 is the one that decides whether you can apply.** Step 4 says so in
as many words: "if your vehicle does not appear as an option, it means we are
not currently searching for it."

Signal 2 is *not* proof of an applyable slot. On 12 Sep 2026 Genoa advertised
"Driver E-Bike — Friday and weekend evenings" while Step 4 offered only Own
Scooter and Own Car, verified by loading the form. Adverts go stale.

So: alerts fire on signal 1. Signal 2 is recorded and shown in --diagnose and
--list, and can be promoted to an alert with `alert_on_job_posting` if you want
early warnings and accept the false alarms that come with them.

Usage
-----
    python watch.py --list              # every city, both signals
    python watch.py --diagnose Genoa    # full detail for one city
    python watch.py --history Genoa     # every recorded open/close episode
    python watch.py --test-telegram     # verify token/chat id
    python watch.py --once              # single check (use from cron/Actions)
    python watch.py --once --poll 50    # keep checking for 50 minutes, then exit
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
# Append-only record of every open/close, so the answer to "when was it open?"
# is one file rather than a script that replays hundreds of state commits.
HISTORY_PATH = HERE / "history.jsonl"

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
        "alert_on_job_posting": False,
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


def hits(pattern: re.Pattern, info: dict, posting_counts: bool = False) -> dict:
    """Which signals advertise the wanted vehicle, and whether that means open.

    `open` follows the Step-4 dropdown alone unless `posting_counts` is set: an
    advert is not a slot you can select. See the module docstring.
    """
    from_dropdown = matches(pattern, info.get("dropdown") or [])
    from_postings = [p for p in info.get("postings") or []
                     if pattern.search(p.get("vehicle", ""))]
    return {
        "dropdown": from_dropdown,
        "postings": from_postings,
        "open": bool(from_dropdown) or bool(posting_counts and from_postings),
    }


def previous_hits(pattern: re.Pattern, before: dict, posting_counts: bool) -> dict:
    """Re-derive the last verdict from the signals we stored, not from the
    stored boolean — so changing what counts as "open" can never emit a
    phantom opened/closed alert on the first run after the change."""
    return hits(pattern, {
        # schema 1 called it "available"
        "dropdown": before.get("dropdown", before.get("available")) or [],
        "postings": before.get("postings") or [],
    }, posting_counts)


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


# -------------------------------------------------------------------- history


def history_event(kind: str, info: dict, key: str, hit: dict, label: str,
                  watching: bool, gap_hours: float | None, when: datetime,
                  origin: str = "live") -> dict:
    """One line of history.jsonl. Field order is the reading order."""
    source = "dropdown" if hit["dropdown"] else ("advert" if hit["postings"] else None)
    return {
        "ts": when.isoformat(timespec="seconds"),
        "event": kind,                       # first_seen | opened | closed
        "city": info["name"],
        "match": label,
        "open": hit["open"],
        "source": source,                    # which signal carried it
        "matched": hit["dropdown"] or [p["vehicle"] for p in hit["postings"]],
        "dropdown": info["dropdown"],
        "postings": info["postings"],
        "watched": watching,
        # How long since the previous check: an "opened" seen after a 5h gap
        # means it opened somewhere in those 5 hours, not at this timestamp.
        "gap_hours": None if gap_hours is None else round(gap_hours, 2),
        "slug": info["slug"],
        "key": key,
        "origin": origin,                    # live | backfill
    }


def append_history(events: list[dict]) -> None:
    """Never let bookkeeping break a check."""
    if not events:
        return
    try:
        with HISTORY_PATH.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError as exc:
        log(f"could not append to {HISTORY_PATH.name}: {exc}")


def read_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    out = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # a torn last line must not lose the whole file
    return out


# --------------------------------------------------------------------- alerts


def describe_postings(postings: list[dict]) -> str:
    bits = []
    for p in postings:
        bits.append(f"{p['vehicle']} — {p['shift']}" if p["shift"] else p["vehicle"])
    return "; ".join(bits)


def open_message(info: dict, hit: dict, label: str) -> str:
    link = APPLY_URL.format(slug=info["slug"])

    if hit["dropdown"]:
        lines = [
            f"🚲 <b>{label.upper()} IS SELECTABLE IN {info['name'].upper()}!</b>", "",
            f"📝 Step 4 now offers: <b>{', '.join(hit['dropdown'])}</b>",
        ]
        if hit["postings"]:
            lines.append(f"📋 Advert: {describe_postings(hit['postings'])}")
        lines.append("\n<i>Go now — this can close within the hour.</i>")
    else:
        # Only reachable with alert_on_job_posting on.
        lines = [
            f"📋 <b>{label.upper()} ADVERTISED IN {info['name'].upper()}</b>", "",
            f"Advert: <b>{describe_postings(hit['postings'])}</b>",
            "",
            "<i>Early warning only — Step 4 does not offer it yet, so you "
            "probably cannot select it. Adverts can be stale.</i>",
        ]

    lines += [
        "",
        f"Step 4 currently offers: {', '.join(info['dropdown']) or 'nothing'}",
        "",
        f'<a href="{link}">Open the form</a>',
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
    posting_counts = bool(cfg.get("alert_on_job_posting"))
    prev = state.get("cities") or {}
    messages: list[str] = []
    events: list[dict] = []

    gap_hours = None
    if state.get("last_check"):
        try:
            gap_hours = (now - datetime.fromisoformat(state["last_check"])).total_seconds() / 3600
        except ValueError:
            pass

    for key, info in sorted(data.items(), key=lambda kv: kv[1]["name"]):
        watching = is_watched(info, watched)
        hit = hits(pattern, info, posting_counts)

        before = prev.get(key)
        if before is None:  # schema 1 keyed state by display name
            before = prev.get(info["name"]) or {}
        known_before = bool(before)
        open_before = known_before and previous_hits(pattern, before, posting_counts)["open"]
        offered_before = before.get("offered", before.get("available"))

        # History covers every city, not just the watched ones — transitions are
        # rare, so the file stays small and answers "when was it open anywhere?"
        if not known_before:
            if watching or hit["open"]:
                events.append(history_event("first_seen", info, key, hit, label,
                                            watching, gap_hours, now))
        elif hit["open"] != open_before:
            events.append(history_event("opened" if hit["open"] else "closed",
                                        info, key, hit, label, watching, gap_hours, now))

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

    append_history(events)
    for e in events:
        log(f"history: {e['city']} {e['event']}"
            + (f" via {e['source']} ({', '.join(e['matched'])})" if e["source"] else ""))

    for msg in messages:
        send_telegram(cfg, msg)
    if messages:
        log(f"{len(messages)} alert(s) sent")

    watched_now = {k: v for k, v in data.items() if is_watched(v, watched)}
    if not watched_now:
        log(f"WARNING: none of {cfg.get('cities')} matched any city on the page")

    def summarise(info: dict) -> str:
        hit = hits(pattern, info, posting_counts)
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
                hit = hits(pattern, info, posting_counts)
                lines.append(
                    f"• {info['name']}: {'🚲 OPEN' if hit['open'] else 'no ' + label}"
                    f" — form offers {', '.join(info['dropdown']) or 'nothing'}"
                    + (f"; advert: {describe_postings(info['postings'])}"
                       if info["postings"] else "")
                )
            elsewhere = sorted(v["name"] for v in data.values()
                               if hits(pattern, v, posting_counts)["open"])
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
            "open": hits(pattern, v, posting_counts)["open"],
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
    posting_counts = bool(cfg.get("alert_on_job_posting"))
    data = city_signals(fetch_html())
    width = max(len(v["name"]) for v in data.values())
    for info in sorted(data.values(), key=lambda v: v["name"]):
        mark = "*" if hits(pattern, info, posting_counts)["open"] else " "
        post = f"   [advert: {describe_postings(info['postings'])}]" if info["postings"] else ""
        print(f"{mark} {info['name'].ljust(width)}  {', '.join(info['dropdown']) or '-'}{post}")
    print(f"\n{len(data)} cities.")
    return 0


def cmd_history(cfg: dict, wanted: str | None) -> int:
    """Readable view of history.jsonl, paired into episodes with durations."""
    rows = read_history()
    if wanted:
        w = wanted.lower()
        rows = [r for r in rows if w in (r.get("city", "").lower(), r.get("slug", "").lower())]
    if not rows:
        print("No history recorded yet."
              + (f" (nothing for {wanted!r})" if wanted else ""))
        return 1

    by_city: dict[str, list[dict]] = {}
    for r in rows:
        by_city.setdefault(r.get("city", "?"), []).append(r)

    for city, entries in sorted(by_city.items()):
        entries.sort(key=lambda r: r["ts"])
        print(f"\n{city}")
        opened_at = None
        for r in entries:
            when = r["ts"].replace("+00:00", "Z")
            gap = r.get("gap_hours")
            fuzz = f"  (opened within the previous {gap}h)" if gap else ""
            if r["event"] in ("opened", "first_seen") and r.get("open"):
                print(f"  OPEN   {when}  {', '.join(r['matched']) or '?'}"
                      f"  [{r.get('source') or '?'}]{fuzz}")
                opened_at = r["ts"]
            elif r["event"] == "closed":
                dur = ""
                if opened_at:
                    try:
                        hours = (datetime.fromisoformat(r["ts"])
                                 - datetime.fromisoformat(opened_at)).total_seconds() / 3600
                        dur = f"  — open for at least {hours:.1f}h"
                    except ValueError:
                        pass
                print(f"  CLOSE  {when}{dur}")
                opened_at = None
            elif r["event"] == "first_seen":
                print(f"  start  {when}  watching, currently closed")
        if opened_at:
            print("  ...still open as of the last recorded check")
    print(f"\n{len(rows)} events in {HISTORY_PATH.name}.")
    return 0


def cmd_diagnose(cfg: dict, wanted: str) -> int:
    pattern = vehicle_matcher(cfg)
    posting_counts = bool(cfg.get("alert_on_job_posting"))
    data = city_signals(fetch_html())
    found = [v for v in data.values()
             if wanted.lower() in {n.lower() for n in v["names"]} | {v["slug"].lower()}]
    if not found:
        print(f"No city matching {wanted!r}. Names: "
              + ", ".join(sorted(v["name"] for v in data.values())))
        return 1
    for info in found:
        hit = hits(pattern, info, posting_counts)
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
    ap.add_argument("--history", nargs="?", const="", metavar="CITY",
                    help="print recorded open/close episodes (optionally one city)")
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

    if args.history is not None:
        return cmd_history(cfg, args.history or None)

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
