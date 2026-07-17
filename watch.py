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
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "watch.log"
HEARTBEAT_PATH = ROOT / "HEARTBEAT.md"

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

# ---------- Willingdon auto-book planning ----------
def _minutes_since_midnight(value):
    """Return minutes since midnight for an HH:MM or HH:MM:SS string."""
    h, m = value.split(":")[:2]
    return int(h) * 60 + int(m)


def _slot_price(slot):
    try:
        return float(slot.get("price", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _booking_segments(slot_inventory, policy):
    """Normalize bookable API slots into deterministic, auto-bookable segments.

    The regular watcher intentionally reports each court independently. The
    auto-book planner needs a single inventory so that it can safely build a
    continuous session which moves from one court to another at an exact slot
    boundary.
    """
    earliest = _minutes_since_midnight(policy["earliest_start"])
    latest_end = policy.get("latest_end")
    latest_end_min = _minutes_since_midnight(latest_end) if latest_end else None
    allowed_days = set(policy["days"])
    seen = set()
    out = []

    for item in slot_inventory:
        raw = item["slot"]
        if not raw.get("is_available", False) or raw.get("available_count", 0) <= 0:
            continue
        try:
            start = datetime.strptime(raw["start_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            end = datetime.strptime(raw["end_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except (KeyError, TypeError, ValueError):
            continue
        slot_id = raw.get("id")
        unique_id = slot_id or (item["facility_id"], start.isoformat(), end.isoformat())
        if unique_id in seen or end <= start:
            continue
        seen.add(unique_id)

        if start.weekday() not in allowed_days:
            continue
        if _minutes_since_midnight(start.strftime("%H:%M")) < earliest:
            continue
        # A session must stay inside the booking policy's evening boundary. This
        # preserves Willingdon's existing 22:00 weekday preference.
        if latest_end_min is not None and _minutes_since_midnight(end.strftime("%H:%M")) > latest_end_min:
            continue

        out.append({
            "slot_id": slot_id,
            "facility_id": item["facility_id"],
            "facility_name": item["facility_name"],
            "start": start,
            "end": end,
            "price": _slot_price(raw),
        })

    return sorted(out, key=lambda s: (s["start"], s["end"], s["facility_name"], s["facility_id"], s["slot_id"] or ""))


def _compact_booking_segments(segments):
    """Merge adjacent API grid slots on the same court into one checkout line."""
    compact = []
    for segment in segments:
        if compact and compact[-1]["facility_id"] == segment["facility_id"] and compact[-1]["end"] == segment["start"]:
            compact[-1]["end"] = segment["end"]
            compact[-1]["slot_ids"].append(segment["slot_id"])
            compact[-1]["price_total"] += segment["price"]
        else:
            compact.append({
                "facility_id": segment["facility_id"],
                "facility_name": segment["facility_name"],
                "start": segment["start"],
                "end": segment["end"],
                "slot_ids": [segment["slot_id"]],
                "price_total": segment["price"],
            })
    return compact


def _booking_plan_rank(segments):
    """Prefer fewer court changes, then fewer checkout lines, then lower cost."""
    compact = _compact_booking_segments(segments)
    return (
        len(compact) - 1,
        len(compact),
        sum(line["price_total"] for line in compact),
        tuple(line["facility_id"] for line in compact),
    )


def build_auto_booking_plans(slot_inventory, policy):
    """Find qualifying continuous sessions, allowing court switches at boundaries.

    Each returned plan covers exactly ``duration_minutes``. A plan contains one
    checkout line per uninterrupted court segment, so a 60-minute Court 1 +
    60-minute Court 2 session has two lines and one court switch.
    """
    required = int(policy["duration_minutes"])
    max_switches = int(policy.get("max_court_switches", 0))
    segments = _booking_segments(slot_inventory, policy)
    by_start = defaultdict(list)
    for segment in segments:
        by_start[segment["start"]].append(segment)
    for candidates in by_start.values():
        candidates.sort(key=lambda s: (s["end"], s["facility_name"], s["facility_id"], s["slot_id"] or ""))

    plans = []
    for start in sorted(by_start):
        target = start + timedelta(minutes=required)
        memo = {}

        def best_path(cursor):
            if cursor == target:
                return []
            if cursor in memo:
                return memo[cursor]
            best = None
            for segment in by_start.get(cursor, []):
                if segment["end"] > target:
                    continue
                tail = best_path(segment["end"])
                if tail is None:
                    continue
                candidate = [segment] + tail
                if best is None or _booking_plan_rank(candidate) < _booking_plan_rank(best):
                    best = candidate
            memo[cursor] = best
            return best

        path = best_path(start)
        if not path:
            continue
        booking_lines = _compact_booking_segments(path)
        if len(booking_lines) - 1 > max_switches:
            continue
        plans.append({
            "venue": policy["venue"],
            "date_iso": start.strftime("%Y-%m-%d"),
            "start": start.strftime("%H:%M"),
            "end": target.strftime("%H:%M"),
            "duration_minutes": required,
            "court_switches": len(booking_lines) - 1,
            "booking_lines": [
                {
                    **line,
                    "start": line["start"].strftime("%H:%M"),
                    "end": line["end"].strftime("%H:%M"),
                }
                for line in booking_lines
            ],
        })

    # This is deliberately global, rather than simply earliest-slot-first: a
    # single-court session is preferable to an earlier session that requires a
    # court move. Within the same shape, take the earlier and cheaper option.
    def rank_plan(plan):
        return (
            plan["court_switches"],
            len(plan["booking_lines"]),
            plan["date_iso"],
            plan["start"],
            sum(line["price_total"] for line in plan["booking_lines"]),
            tuple(line["facility_id"] for line in plan["booking_lines"]),
        )

    return sorted(plans, key=rank_plan)

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
    near_hrs = int(cfg.get("near_reminder_hours", 24))
    auto_booking = cfg.get("auto_booking", {})

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

    # Load prior state up front so we can inherit per-slot reminder flags.
    prior = {}
    if STATE_PATH.exists():
        try:
            prior = json.loads(STATE_PATH.read_text())
        except Exception:
            prior = {}

    # collect all (venue, facility, run) tuples — current state
    current = {}  # key -> dict
    auth_failed = False
    failed_venues = set()  # transient failures → inherit prior state for these venues
    auto_booking_inventory = []
    for venue in cfg["venues"]:
        venue_had_failure = False
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
                venue_had_failure = True
                continue
            slot_data = r["data"].get("slot_data", [])
            all_slots = []
            for day in slot_data:
                all_slots.extend(day.get("slots", []))
            if auto_booking.get("enabled") and venue["name"] == auto_booking.get("venue"):
                auto_booking_inventory.extend({
                    "facility_id": fac["id"],
                    "facility_name": fac["name"],
                    "slot": slot,
                } for slot in all_slots)
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
                        # Inherit reminder flag if this exact slot was tracked before —
                        # ensures we send the "still open" near-reminder at most once.
                        "reminded_near": prior.get(key, {}).get("reminded_near", False),
                        **run,
                    }
        if venue_had_failure:
            failed_venues.add(venue["name"])

    # Build an independent Willingdon auto-book plan. This has no effect on the
    # established watcher alerts above: it merely evaluates stricter booking
    # rules and supports changing courts at consecutive slot boundaries.
    if auto_booking.get("enabled"):
        auto_plans = build_auto_booking_plans(auto_booking_inventory, auto_booking)
        if auto_plans:
            plan = auto_plans[0]
            court_names = " → ".join(line["facility_name"] for line in plan["booking_lines"])
            log(
                "auto-book candidate: "
                f"{plan['venue']} {plan['date_iso']} {plan['start']}–{plan['end']} "
                f"({plan['court_switches']} switch(es): {court_names})"
            )
            if auto_booking.get("dry_run", True):
                log("auto-book dry run: checkout deliberately disabled")
            else:
                # This guard remains until the exact Hudle checkout/hold API is
                # mapped. It prevents a partially booked multi-court session.
                log("auto-book blocked: checkout adapter has not been configured")

    if auth_failed:
        telegram_send(cfg, "🔐 <b>Hudle watcher: auth token expired.</b>\nRe-capture a fresh cURL from hudle.in DevTools and update <code>~/.claude/hudle-watcher/config.json</code> token field.")
        log("auth failed; alerted user; exiting")
        sys.exit(1)

    # diff vs prior state — prior was loaded at top of main()
    # If any venue had a transient failure this run, inherit its prior entries.
    # Without this, a one-run network blip would clear those entries from state and
    # the next successful run would re-alert on slots that were already known.
    if failed_venues and prior:
        inherited = 0
        for k, r in prior.items():
            if r.get("venue") in failed_venues and k not in current:
                current[k] = r
                inherited += 1
        if inherited:
            log(f"  inherited {inherited} prior entries from {failed_venues} (transient failure)")

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
                # Bold times for at-a-glance scanning. Court numbers omitted by design —
                # user can pick the court inside the booking flow.
                parts = [f"<b>{short_t(r['start'], r['end'])}</b>" for r in runs]
                times = ", ".join(parts)
                v0 = runs[0]
                suffix = ""
                if v0.get("venue_coupon_note") and "FREE" in v0["venue_coupon_note"].upper():
                    suffix = " <i>(FREE w/ HSBCPHOENIX)</i>"
                lines.append(f"<b>{venue_name}</b> · {times}{suffix}")

        # Single Hudle app link — all venue deep-links open the same app, no point
        # showing three. Pick any venue's deep link as the entry point.
        any_link = next((r.get("venue_app_link") for r in current.values() if r.get("venue_app_link")), None)
        if any_link:
            lines.append(f"\n📲 <a href=\"{any_link}\">Open in Hudle app</a>")

        telegram_send(cfg, "\n".join(lines))

    # ---- Near-date "still open" reminder ----
    # For any slot still in current state, check if play time is within the reminder
    # threshold (default 24h). If so AND we haven't reminded for this slot yet,
    # send a single ⏰ reminder. This catches the case where the user got the
    # initial alert days ago and forgot.
    #
    # IMPORTANT: if a slot is BRAND NEW this run and its play time is already
    # inside the reminder window, the new-slot alert above already serves as the
    # reminder — don't double-fire. We mark those as reminded_near=True too.
    now_ist = datetime.now(IST)
    new_keys_set = set(new_keys)
    near_keys = []
    for key, r in current.items():
        if r.get("reminded_near"):
            continue
        if key in new_keys_set:
            # Just announced in the new-slot alert. If play is within window,
            # mark reminder done so we don't fire a redundant ⏰ message now or later.
            try:
                play_dt = datetime.strptime(
                    f"{r['date_iso']} {r['start']}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=IST)
                if 0 < (play_dt - now_ist).total_seconds() / 3600 <= near_hrs:
                    r["reminded_near"] = True
            except Exception:
                pass
            continue
        try:
            play_dt = datetime.strptime(
                f"{r['date_iso']} {r['start']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=IST)
        except Exception:
            continue
        hours_to_play = (play_dt - now_ist).total_seconds() / 3600
        if 0 < hours_to_play <= near_hrs:
            near_keys.append(key)
            # Mark right away so a Telegram failure doesn't cause a re-send next run.
            r["reminded_near"] = True

    if near_keys:
        # Build same date-grouped layout as the new-slot alert.
        def fmt_time2(t_str):
            parts = t_str.split(":"); h, m = int(parts[0]), int(parts[1])
            suf = "am" if h < 12 else "pm"
            h12 = h % 12
            if h12 == 0: h12 = 12
            return (f"{h12}:{m:02d}" if m else f"{h12}", suf)
        def short_t2(start, end):
            s, ss = fmt_time2(start); e, es = fmt_time2(end)
            return f"{s}–{e}{es}" if ss == es else f"{s}{ss}–{e}{es}"

        rlines = ["⏰ <b>Still open — playing soon?</b>"]
        # Group by date → venue
        by_date_r = {}
        for k in near_keys:
            r = current[k]
            by_date_r.setdefault(r["date_iso"], {}).setdefault(r["venue"], []).append(r)
        for date_iso in sorted(by_date_r):
            day = by_date_r[date_iso]
            sample = next(iter(day.values()))[0]
            rlines.append(f"\n<b>📅 {sample['weekday']} {sample['date_dm']}</b>")
            for venue_name in sorted(day):
                runs = sorted(day[venue_name], key=lambda r: r["start"])
                parts = [f"<b>{short_t2(r['start'], r['end'])}</b>" for r in runs]
                v0 = runs[0]
                suf = " <i>(FREE w/ HSBCPHOENIX)</i>" if v0.get("venue_coupon_note") and "FREE" in v0["venue_coupon_note"].upper() else ""
                rlines.append(f"<b>{venue_name}</b> · {', '.join(parts)}{suf}")

        any_link = next((r.get("venue_app_link") for r in current.values() if r.get("venue_app_link")), None)
        if any_link:
            rlines.append(f"\n📲 <a href=\"{any_link}\">Open in Hudle app</a>")

        log(f"sending {len(near_keys)} near-date reminders")
        telegram_send(cfg, "\n".join(rlines))

    # persist
    STATE_PATH.write_text(json.dumps(current, indent=2))

    # Weekly heartbeat (Sunday): touches HEARTBEAT.md and Telegram-pings to confirm
    # the watcher is alive. The committed file edit also keeps the repo "active" so
    # GitHub's 60-day inactivity rule doesn't auto-disable our scheduled workflow.
    now = datetime.now(IST)
    if now.weekday() == 6:  # Sunday
        last_hb = None
        if HEARTBEAT_PATH.exists():
            try:
                last_hb = datetime.fromtimestamp(HEARTBEAT_PATH.stat().st_mtime, IST)
            except Exception:
                last_hb = None
        # Once per Sunday — only update if last update was 5+ days ago.
        if last_hb is None or (now - last_hb).days >= 5:
            venues_with_slots = sorted({r["venue"] for r in current.values()})
            HEARTBEAT_PATH.write_text(
                "# Hudle watcher heartbeat\n\n"
                f"Last healthy run: {now.strftime('%Y-%m-%d %H:%M IST')}\n"
                f"Slots currently tracked: {len(current)}\n"
                f"Venues with bookable slots: {', '.join(venues_with_slots) or '(none)'}\n"
            )
            log(f"heartbeat written; pinging Telegram")
            telegram_send(cfg, (
                "💚 <b>Hudle watcher healthy</b>\n\n"
                f"Tracking <b>{len(current)}</b> bookable slot windows across "
                f"{len(venues_with_slots)} venue(s):\n"
                + ("\n".join(f"• {v}" for v in venues_with_slots) if venues_with_slots else "• (none right now)")
                + "\n\nNext heartbeat in ~7 days."
            ))

if __name__ == "__main__":
    main()
