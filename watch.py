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
import re
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
def find_runs(slots, schedule, min_minutes, max_display_minutes):
    """
    slots: list of slot dicts (from API), sorted by start_time.
    Returns list of run dicts: {date_iso, weekday, start, end, duration_min, ...}.
    A "run" is a maximal contiguous block of available slots all within an eligible
    schedule window. Runs shorter than min_minutes are dropped. Runs longer than
    max_display_minutes get reported as a max_display_minutes window starting at the
    run's first slot (so a 4h opening reports as a 2h window — the user can see the
    longer span on Hudle if they want).
    """
    parsed = []
    for s in slots:
        # A slot is *actually* bookable only when BOTH conditions hold:
        #   is_available=True       → the slot is in the active booking pool for its tier
        #                              (e.g. excludes HSBC-tier-greyed slots, or past-closing slots)
        #   available_count > 0     → there's at least one free court at that time
        if not s.get("is_available", False):
            continue
        if s.get("available_count", 0) <= 0:
            continue
        try:
            st = datetime.strptime(s["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            et = datetime.strptime(s["end_time"],   "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except Exception:
            continue
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
        actual_min = (parsed[j]["end"] - parsed[i]["start"]).total_seconds() / 60
        if actual_min >= min_minutes:
            display_min = min(actual_min, max_display_minutes)
            display_end = parsed[i]["start"] + timedelta(minutes=display_min)
            runs.append({
                "date_iso": parsed[i]["start"].strftime("%Y-%m-%d"),
                "date_dm": parsed[i]["start"].strftime("%d/%m"),
                "weekday": parsed[i]["start"].strftime("%a"),
                "start": parsed[i]["start"].strftime("%H:%M"),
                "end": display_end.strftime("%H:%M"),
                "duration_min": int(display_min),
                "actual_duration_min": int(actual_min),
                "slot_ids": [p["raw"]["id"] for p in parsed[i:j + 1]],
                "price_total": sum(float(p["raw"].get("price", 0)) for p in parsed[i:j + 1]),
            })
        i = j + 1
    return runs

# ---------- main ----------
def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    horizon = int(cfg.get("horizon_days", 7))
    min_min = int(cfg.get("min_duration_minutes", 60))
    max_disp = int(cfg.get("max_display_minutes", 120))

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
            runs = find_runs(all_slots, venue["schedule"], min_min, max_disp)
            for run in runs:
                # Dedup key intentionally OMITS facility — same venue/date/window across
                # multiple courts is one alert, with court names merged for display.
                # User can only physically be at one court, so multiple options at the
                # same time = redundant noise.
                key = f"{venue['name']}|{run['date_iso']}|{run['start']}|{run['end']}"
                if key in current:
                    # Append this facility to the existing entry.
                    existing = current[key]
                    if fac["name"] not in existing["facilities"]:
                        existing["facilities"].append(fac["name"])
                else:
                    current[key] = {
                        "venue": venue["name"],
                        "venue_app_link": venue.get("app_link"),
                        "venue_coupon_note": venue.get("coupon_note"),
                        "facilities": [fac["name"]],
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
        # Compact format: group by DATE (the way you plan), one line per venue per day,
        # times comma-separated. Court names dropped — visible in the booking flow.
        # Times use am/pm 12-hour format ("8–10pm" not "20–22") — way more readable.
        def fmt_time(t_str):
            """ "20:30:00" -> ("8:30", "pm");  "10:00" -> ("10", "am") """
            parts = t_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            suf = "am" if h < 12 else "pm"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            return (f"{h12}:{m:02d}" if m else f"{h12}", suf)

        def short_t(start, end):
            """Format a time range. If both ends share am/pm, only suffix the end:
                  9-11am, 8:30-10pm, 4-5pm. If they cross periods, suffix both:
                  11am-12pm, 11am-1pm. """
            s, ssuf = fmt_time(start)
            e, esuf = fmt_time(end)
            if ssuf == esuf:
                return f"{s}–{e}{esuf}"
            return f"{s}{ssuf}–{e}{esuf}"

        # Build {date_iso: {venue_name: [run, run, ...]}}, then drop runs that are
        # strictly contained inside another run at the same venue/day. Reason: if
        # 09–11 is open you could already play 10–11; listing both is redundant.
        # Only "maximal" time ranges survive.
        def to_min(s):
            h, m = s.split(":")[:2]
            return int(h) * 60 + int(m)

        def dominates(a, b):
            # a contains b strictly: a covers b's range and is at least as long.
            return (
                to_min(a["start"]) <= to_min(b["start"])
                and to_min(a["end"]) >= to_min(b["end"])
                and (to_min(a["end"]) - to_min(a["start"])) > (to_min(b["end"]) - to_min(b["start"]))
            )

        by_date = {}
        # Group all runs per (venue, date) first
        by_vd = {}
        for k in new_keys:
            r = current[k]
            by_vd.setdefault((r["venue"], r["date_iso"]), []).append(r)
        # Filter dominated runs within each group
        for (venue_name, date_iso), runs in by_vd.items():
            kept = [r for r in runs if not any(dominates(o, r) for o in runs if o is not r)]
            by_date.setdefault(date_iso, {})[venue_name] = kept

        lines = ["🎾 <b>Padel — slots open</b>"]
        for date_iso in sorted(by_date.keys()):
            day = by_date[date_iso]
            # Use any run for the weekday/date_dm format
            sample = next(iter(day.values()))[0]
            lines.append(f"\n<b>📅 {sample['weekday']} {sample['date_dm']}</b>")
            for venue_name in sorted(day.keys()):
                runs = sorted(day[venue_name], key=lambda r: r["start"])
                # Extract court numbers from facility names: "Padel Court 1" -> 1.
                # Phoenix's "HSBC Padel Court" has no number, so falls back to no suffix.
                def courts_str(facs):
                    nums = sorted({int(m.group(1)) for f in facs if (m := re.search(r"Court\s+(\d+)", f))})
                    if not nums: return ""
                    if len(nums) == 1: return f" (Court {nums[0]})"
                    return f" (Courts {', '.join(str(n) for n in nums)})"

                parts = [f"{short_t(r['start'], r['end'])}{courts_str(r.get('facilities', []))}" for r in runs]
                times = ", ".join(parts)
                v0 = runs[0]
                suffix = ""
                if v0.get("venue_coupon_note") and "FREE" in v0["venue_coupon_note"].upper():
                    suffix = " <i>(FREE w/ HSBCPHOENIX)</i>"
                lines.append(f"{venue_name} · {times}{suffix}")

        # Compact app links footer.
        venue_links = []
        seen = set()
        for r in current.values():
            v = r["venue"]
            if v in seen or not r.get("venue_app_link"): continue
            seen.add(v)
            venue_links.append(f"<a href=\"{r['venue_app_link']}\">{v}</a>")
        if venue_links:
            lines.append("\n📲 " + "  ·  ".join(venue_links))

        telegram_send(cfg, "\n".join(lines))

    # persist
    STATE_PATH.write_text(json.dumps(current, indent=2))

if __name__ == "__main__":
    main()
