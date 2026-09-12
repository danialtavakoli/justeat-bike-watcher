#!/usr/bin/env python3
"""
Seed history.jsonl from the state.json commits already in this repo.

The watcher only started appending to history.jsonl in September 2026, but every
check since 17 Aug 2026 is preserved as a `state: ...` commit. This replays those
commits and writes the open/close transitions they imply, so the history file
covers the whole recorded period rather than starting mid-stream.

One-off, and safe to re-run: it refuses to touch a history.jsonl that already
holds backfilled events.

    python backfill_history.py            # write it
    python backfill_history.py --dry-run  # just show what it would write
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import watch  # noqa: E402

HERE = Path(__file__).parent


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def state_commits() -> list[tuple[str, str]]:
    out = git("log", "--reverse", "--format=%H %cI", "--", "state.json").strip()
    return [tuple(line.split()) for line in out.splitlines() if line.strip()]


def city_entries(state: dict) -> dict[str, dict]:
    """Normalise schema 1 and 2 into {key: {name, slug, dropdown, postings}}."""
    out = {}
    for key, v in (state.get("cities") or {}).items():
        if not isinstance(v, dict):
            continue
        out[key if key.startswith("coid:") else v.get("name", key)] = {
            "name": v.get("name", key),
            "slug": v.get("slug", ""),
            "dropdown": v.get("dropdown", v.get("available")) or [],
            "postings": v.get("postings") or [],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    existing = watch.read_history()
    if any(e.get("origin") == "backfill" for e in existing):
        print("history.jsonl already contains backfilled events — nothing to do.")
        return 0

    cfg = watch.load_json(watch.CONFIG_PATH) or watch.default_config()
    pattern = watch.vehicle_matcher(cfg)
    label = str(cfg.get("vehicle_label") or "e-bike")
    watched = [c.strip().lower() for c in cfg.get("cities") or ["Genoa"]]
    posting_counts = bool(cfg.get("alert_on_job_posting"))

    commits = state_commits()
    if not commits:
        print("No state.json commits found — is this a git checkout of the repo?")
        return 1
    print(f"replaying {len(commits)} state commits "
          f"({commits[0][1]} -> {commits[-1][1]})")

    # Schema 1 keyed by display name, schema 2 by coid: map old keys to new ones
    # via the city name so a city's history doesn't split at the schema change.
    prev_open: dict[str, bool] = {}
    prev_ts: str | None = None
    events: list[dict] = []

    for sha, ts in commits:
        blob = git("show", f"{sha}:state.json")
        try:
            state = json.loads(blob)
        except json.JSONDecodeError:
            continue

        gap = None
        if prev_ts:
            gap = (datetime.fromisoformat(ts)
                   - datetime.fromisoformat(prev_ts)).total_seconds() / 3600

        for _, info in sorted(city_entries(state).items(), key=lambda kv: kv[1]["name"]):
            name = info["name"]
            key = f"name:{name}"        # stable across the schema change
            hit = watch.hits(pattern, info, posting_counts)
            watching = name.lower() in watched or info["slug"].lower() in watched
            known = key in prev_open

            kind = None
            if not known and (watching or hit["open"]):
                kind = "first_seen"
            elif known and hit["open"] != prev_open[key]:
                kind = "opened" if hit["open"] else "closed"

            if kind:
                events.append(watch.history_event(
                    kind, info, key, hit, label, watching, gap,
                    datetime.fromisoformat(ts), origin="backfill"))
            prev_open[key] = hit["open"]
        prev_ts = ts

    print(f"{len(events)} transitions recovered:")
    for e in events:
        print(f"  {e['ts']}  {e['event']:10s} {e['city']:12s} "
              f"{', '.join(e['matched']) or '-'}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # Backfill belongs before anything the live watcher already appended.
    watch.HISTORY_PATH.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events + existing),
        encoding="utf-8")
    print(f"\nwrote {len(events)} backfilled + {len(existing)} existing events "
          f"to {watch.HISTORY_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
