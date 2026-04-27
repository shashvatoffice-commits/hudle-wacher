#!/usr/bin/env python3
"""
Hudle Padel Slot Watcher.
Runs on a cron/launchd schedule. Each invocation:
  1. Reads config.json — venues, facilities, schedules, auth.
  2. For each venue's currently-eligible time windows, fetches slot grid for today..today+horizon_days.
  3. Filters slots to user's eligible day/time windows + minimum continuous duration.
  4. Diffs against state.json — alerts to Telegram on newly-available slot runs.
  5. Persists current snapshot.

No LLM calls. Pure stdlib. Logs to ~/.claude/hudle-watcher/watch.log.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "watch.log"

IST = timezone(timedelta(hours=5, minutes=30))

# ---------- logging (append to file, also echo to stderr) ----------
def log(msg):
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------- HTTP ----------
def _secret(name, cfg_path=None):
    """Read a secret from env first, then optionally fall back to config.json local-dev path."""
    v = os.environ.get(name)
    if v:
        return v
    if cfg_path:
        cur = json.loads(CONFIG_PATH.read_text())
        for k in cfg_path:
            cur = cur.get(k, {}) if isinstance(cur, dict) else {}
        if isinstance(cur, str) and cur:
            return cur
    raise SystemExit(f"missing secret: env {name} not set (and no local fallback in config.json)")

def hudle_call(path, cfg, method="GET"):
    url = f"https://api.hudle.in{path}"
    headers = {
        "authorization": f"Bearer {_secret('HUDLE_TOKEN', ['hudle','token'])}",
        "api-secret":     _secret("HUDLE_API_SECRET", ["hudle","api_secret"]),
        "x-app-id":       _secret("HUDLE_APP_ID",    ["hudle","x_app_id"]),
        "x-device-source": "3",
        "origin": "https://hudle.in",
        "referer": "https://hudle.in/",
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    if method == "POST":
        headers["content-length"] = "0"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            return {"_auth_expired": True, "code": 401, "body": body}
        log(f"HTTP {e.code} on {path}: {body}")
        return None
    except Exception as e:
        log(f"net err on {path}: {e}")
        return None

def telegram_send(cfg, text):
    url = f"https://api.telegram.org/bot{_secret('TELEGRAM_BOT_TOKEN', ['telegram','bot_token'])}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": _secret("TELEGRAM_CHAT_ID", ["telegram","chat_id"]),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        log(f"telegram send failed: {e}")
        return False

# ---------- venue/facility discovery ----------
def discover_padel_facilities(venue_id, cfg):
    """Return list of {name,id} for active padel facilities at a venue."""
    r = hudle_call(f"/api/v1/venues/{venue_id}/facilities", cfg)
    if not r or not isinstance(r.get("data"), list):
        return []
    out = []
    for f in r["data"]:
        if not f.get("enabled"):
            continue
        sports = [s.get("name") for s in f.get("sports", [])]
        if "Padel" not in sports:
            continue
        out.append({"name": f["name"], "id": f["id"], "slot_length": f.get("slot_length", 30)})
    return out

# ---------- schedule logic ----------
def time_in_window(t_str, start_str, end_str):
    """t_str / start_str / end_str are 'HH:MM' or 'HH:MM:SS'."""
    def to_min(s):
        h, m = s.split(":")[:2]
        return int(h) * 60 + int(m)
    return to_min(start_str) <= to_min(t_str) < to_min(end_str)

def is_slot_in_schedule(slot_dt, schedule):
    """slot_dt is a datetime (IST). schedule is list of {days, start, end}."""
    dow = slot_dt.weekday()  # Mon=0..Sun=6
    t = slot_dt.strftime("%H:%M")
    return any(dow in win["days"] and time_in_window(t, win["start"], win["end"]) for win in schedule)

# ---------- core: find runs ----------
def find_runs(slots, schedule, min_minutes):
    """
    slots: list of slot dicts (from API), sorted by start_time.
    Returns list of run dicts: {date, start, end, duration_min, slots:[ids], price_total}
    A run is N consecutive available slots, all within an eligible time window,
    contiguous (each slot.end == next slot.start), totaling ≥ min_minutes.
    """
    # parse start/end for each slot
    parsed = []
    for s in slots:
        if s.get("available_count", 0) <= 0:
            continue
        try:
            st = datetime.strptime(s["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            et = datetime.strptime(s["end_time"],   "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            continue
        # Filter to schedule window
        if not is_slot_in_schedule(st, schedule):
            continue
        parsed.append({"start": st, "end": et, "raw": s})
    parsed.sort(key=lambda x: x["start"])

    runs = []
    i = 0
    while i < len(parsed):
        j = i
        while j + 1 < len(parsed) and parsed[j + 1]["start"] == parsed[j]["end"] \
                and is_slot_in_schedule(parsed[j + 1]["start"], schedule):
            j += 1
        # parsed[i..j] is a maximal contiguous run; emit all sub-runs ≥ min_minutes that are also contained
        # We emit the LONGEST contiguous run only — more digestible than every sub-window.
        run_min = (parsed[j]["end"] - parsed[i]["start"]).total_seconds() / 60
        if run_min >= min_minutes:
            runs.append({
                "date": parsed[i]["start"].strftime("%Y-%m-%d"),
                "weekday": parsed[i]["start"].strftime("%a"),
                "start": parsed[i]["start"].strftime("%H:%M"),
                "end": parsed[j]["end"].strftime("%H:%M"),
                "duration_min": int(run_min),
                "slot_ids": [p["raw"]["id"] for p in parsed[i:j + 1]],
                "price_total": sum(float(p["raw"].get("price", 0)) for p in parsed[i:j + 1]),
            })
        i = j + 1
    return runs

# ---------- main ----------
def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    horizon = int(cfg.get("horizon_days", 7))
    min_min = int(cfg.get("min_duration_minutes", 90))

    today = datetime.now(IST).date()
    end_date = today + timedelta(days=horizon)

    # auto-discover padel facilities for venues that opt in (always overrides config list).
    for venue in cfg["venues"]:
        if venue.get("auto_discover_padel"):
            log(f"discovering padel facilities for {venue['name']}…")
            discovered = discover_padel_facilities(venue["venue_id"], cfg)
            if discovered:
                venue["facilities"] = discovered
            log(f"  using {len(venue['facilities'])}: {[f['name'] for f in venue['facilities']]}")

    # collect all (venue, facility, run) tuples — current state
    current = {}  # key -> dict
    auth_failed = False
    for venue in cfg["venues"]:
        for fac in venue["facilities"]:
            path = (
                f"/api/v1/venues/{venue['venue_id']}/facilities/{fac['id']}/slots"
                f"?start_date={today}&end_date={end_date}&grid=1"
            )
            r = hudle_call(path, cfg)
            if r and r.get("_auth_expired"):
                auth_failed = True
                continue
            if not r or not r.get("success"):
                log(f"  {venue['name']} / {fac['name']}: skip ({r.get('message') if r else 'no resp'})")
                continue
            slot_data = r["data"].get("slot_data", [])
            all_slots = []
            for day in slot_data:
                all_slots.extend(day.get("slots", []))
            runs = find_runs(all_slots, venue["schedule"], min_min)
            for run in runs:
                key = f"{venue['name']}|{fac['name']}|{run['date']}|{run['start']}|{run['end']}"
                current[key] = {
                    "venue": venue["name"],
                    "venue_slug": venue.get("slug"),
                    "facility": fac["name"],
                    **run,
                }

    if auth_failed:
        telegram_send(cfg, "🔐 <b>Hudle watcher: auth token expired.</b>\nRe-capture a fresh cURL from hudle.in DevTools and update <code>~/.claude/hudle-watcher/config.json</code> token field.")
        log("auth failed; alerted user; exiting")
        sys.exit(1)

    # diff vs prior state
    prior = {}
    if STATE_PATH.exists():
        try:
            prior = json.loads(STATE_PATH.read_text())
        except Exception:
            prior = {}

    new_keys = sorted(set(current) - set(prior))
    log(f"current_runs={len(current)}  prior_runs={len(prior)}  new={len(new_keys)}")

    if new_keys:
        # group new runs by venue + date for readability
        lines = ["🎾 <b>New padel slots available</b>"]
        by_venue = {}
        for k in new_keys:
            r = current[k]
            by_venue.setdefault(r["venue"], []).append(r)
        for venue_name, runs in by_venue.items():
            lines.append(f"\n🏟 <b>{venue_name}</b>")
            runs.sort(key=lambda r: (r["date"], r["start"], r["facility"]))
            for r in runs:
                hrs = r["duration_min"] / 60
                price = f"₹{int(r['price_total'])}" if r["price_total"] else ""
                lines.append(
                    f"  • {r['weekday']} {r['date']} {r['start']}–{r['end']} "
                    f"({hrs:.1f}h) — <i>{r['facility']}</i> {price}".rstrip()
                )
        # link
        lines.append('\n🔗 Book at <a href="https://hudle.in/">hudle.in</a> or in the app.')
        telegram_send(cfg, "\n".join(lines))

    # persist
    STATE_PATH.write_text(json.dumps(current, indent=2))

if __name__ == "__main__":
    main()
