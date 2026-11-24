"""Standalone daily ActivityWatch recap.

Talks to an already-running aw-server over its local REST API. No aw-* imports,
no dependency on the frozen ActivityWatch bundle, no third-party packages.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE_URL = "http://localhost:5600/api/0"
RETRY_TIMEOUT_SECONDS = 60
RETRY_INTERVAL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 10
TOP_N = 5

LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
STATE_DIR = (
    Path(LOCALAPPDATA) / "aw-daily-summary"
    if LOCALAPPDATA
    else Path.home() / ".aw-daily-summary"
)
LOG_PATH = STATE_DIR / "daily_summary.log"
SENTINEL_PATH = STATE_DIR / "last_shown.txt"
SETTINGS_PATH = (
    Path(LOCALAPPDATA)
    / "activitywatch"
    / "activitywatch"
    / "aw-server"
    / "settings.json"
    if LOCALAPPDATA
    else None
)


def _log(message):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def api_request(path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = Request(BASE_URL + path, data=data, headers=headers)
    with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_server():
    deadline = datetime.now() + timedelta(seconds=RETRY_TIMEOUT_SECONDS)
    attempt = 0
    while True:
        attempt += 1
        try:
            api_request("/info")
            _log(f"server reachable (attempt {attempt})")
            return True
        except (HTTPError, URLError, OSError, ValueError) as exc:
            _log(f"server not ready (attempt {attempt}): {exc}")
            if datetime.now() >= deadline:
                return False
            time.sleep(RETRY_INTERVAL_SECONDS)


def discover_buckets():
    buckets = api_request("/buckets/")
    found = {"window": [], "afk": [], "web": []}
    for bucket_id, metadata in buckets.items():
        client = metadata.get("client") or ""
        if bucket_id.startswith("aw-watcher-window_") or client == "aw-watcher-window":
            found["window"].append(bucket_id)
        elif bucket_id.startswith("aw-watcher-afk_") or client == "aw-watcher-afk":
            found["afk"].append(bucket_id)
        elif bucket_id.startswith("aw-watcher-web") or client == "aw-watcher-web":
            found["web"].append(bucket_id)
    for kind in found:
        found[kind].sort()
    _log(
        "buckets: window=%s afk=%s web=%s"
        % (found["window"], found["afk"], found["web"])
    )
    return found


def load_settings():
    start_of_day = "00:00"
    raw_classes = []
    if SETTINGS_PATH and SETTINGS_PATH.is_file():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            value = settings.get("startOfDay") or "00:00"
            parts = str(value).split(":")
            if len(parts) >= 2 and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59:
                start_of_day = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            raw_classes = settings.get("classes") or []
            if not raw_classes:
                active_ids = set(settings.get("active_set_ids") or [])
                for category_set in settings.get("category_sets") or []:
                    if category_set.get("id") in active_ids:
                        raw_classes = category_set.get("categories") or []
                        break
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            _log(f"could not read AW settings: {exc}")
    else:
        _log("AW settings.json not found; using 00:00 and no categories")
    return start_of_day, normalize_classes(raw_classes)


def normalize_classes(raw_classes):
    classes = []
    for entry in raw_classes:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or []
        if isinstance(name, str):
            name = [name]
        rule = entry.get("rule") or {}
        if rule.get("type") == "none":
            continue
        classes.append((list(name), rule))
    return classes


def compile_rules(classes):
    """Mirror aw_transform.classify._plan: highest rank, then latest rule wins."""
    entries = []
    for index, (name, rule) in enumerate(classes):
        regex = rule.get("regex")
        pattern = None
        if regex:
            try:
                pattern = re.compile(
                    regex, (re.IGNORECASE if rule.get("ignore_case") else 0) | re.UNICODE
                )
            except re.error as exc:
                _log(f"invalid category regex {regex!r}: {exc}")
        if pattern is None:
            continue
        priority = rule.get("priority", rule.get("weight"))
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = None
        rank = priority if priority is not None else len(name) * 10
        keys = rule.get("select_keys") or None
        entries.append((rank, index, name, pattern, keys))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(name, pattern, keys) for _, _, name, pattern, keys in entries]


def categorize(data, compiled):
    for name, pattern, keys in compiled:
        values = [data.get(key) for key in keys] if keys else list(data.values())
        for value in values:
            if isinstance(value, str) and pattern.search(value):
                return " > ".join(name) if name else "Uncategorized"
    return "Uncategorized"


def day_bounds(start_of_day, now):
    """[start, end) for the day before the current AW day."""
    hour, minute = (int(part) for part in start_of_day.split(":"))
    today_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    current_day_start = today_start if now >= today_start else today_start - timedelta(days=1)
    return current_day_start - timedelta(days=1), current_day_start


def query_events(bucket_id, afk_bucket_id, start, end):
    statements = [f'we = flood(query_bucket("{bucket_id}"));']
    if afk_bucket_id:
        statements.extend(
            [
                f'af = flood(query_bucket("{afk_bucket_id}"));',
                'na = filter_keyvals(af, "status", ["not-afk"]);',
                "act = filter_period_intersect(we, na);",
                "RETURN = act;",
            ]
        )
    else:
        statements.extend(["RETURN = we;"])
    payload = {
        "timeperiods": [f"{start.isoformat()}/{end.isoformat()}"],
        "query": statements,
    }
    result = api_request("/query/", payload)
    if not isinstance(result, list) or not result or not isinstance(result[0], list):
        _log(f"unexpected query response: {result!r}")
        return []
    return result[0]


def human_duration(seconds):
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def ranked_lines(counts):
    lines = []
    for rank, (label, seconds) in enumerate(
        sorted(counts.items(), key=lambda item: item[1], reverse=True)[:TOP_N], 1
    ):
        lines.append(f"  {rank}. {label} - {human_duration(seconds)}")
    return lines or ["  (none)"]


def build_report(events, web_events, compiled, label, start_of_day):
    app_totals = defaultdict(float)
    category_totals = defaultdict(float)
    domain_totals = defaultdict(float)
    total_active = 0.0

    for event in events:
        duration = float(event.get("duration") or 0)
        total_active += duration
        data = event.get("data") or {}
        app_totals[data.get("app") or "<unknown>"] += duration
        category_totals[categorize(data, compiled)] += duration

    for event in web_events:
        duration = float(event.get("duration") or 0)
        data = event.get("data") or {}
        domain = data.get("$domain") or urlparse(data.get("url") or "").hostname
        if domain:
            domain_totals[domain] += duration

    lines = [
        f"ActivityWatch recap - {label} ({start_of_day} day boundary)",
        "",
        f"Active time: {human_duration(total_active)}",
        "",
        "Top apps:",
    ]
    lines.extend(ranked_lines(app_totals))
    lines.extend(["", "Top websites / domains:"])
    if domain_totals:
        lines.extend(ranked_lines(domain_totals))
    else:
        lines.append("  (no aw-watcher-web data for this period)")
    lines.extend(["", "Categories:"])
    lines.extend(ranked_lines(category_totals))
    return "\n".join(lines)


def show_message(title, text, is_warning=False):
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        if is_warning:
            messagebox.showwarning(title, text)
        else:
            messagebox.showinfo(title, text)
        root.update()
        root.destroy()
    except Exception as exc:
        _log(f"could not display dialog: {exc}")
        print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Daily ActivityWatch recap pop-up")
    parser.add_argument("--force", action="store_true", help="ignore the once-per-day marker")
    parser.add_argument(
        "--print", dest="print_only", action="store_true", help="print the report instead of popping a dialog"
    )
    args = parser.parse_args(argv)

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    start_of_day, classes = load_settings()
    now = datetime.now().astimezone()
    start, end = day_bounds(start_of_day, now)
    marker = start.isoformat()
    if not args.force and not args.print_only and SENTINEL_PATH.is_file():
        try:
            if SENTINEL_PATH.read_text(encoding="utf-8").strip() == marker:
                _log(f"already shown for window starting {marker}; skipping")
                return 0
        except OSError:
            pass

    if not wait_for_server():
        message = "ActivityWatch recap unavailable: aw-server did not respond within 60 seconds."
        _log(message)
        if not args.print_only:
            show_message("ActivityWatch recap", message, is_warning=True)
        return 1

    try:
        buckets = discover_buckets()
        label = start.strftime("%a, %b %d")

        events = []
        if buckets["window"]:
            events = query_events(
                buckets["window"][0], buckets["afk"][0] if buckets["afk"] else None, start, end
            )
        else:
            _log("no window bucket found")

        if not events and now >= end:
            _log("no events for yesterday; falling back to today so far")
            events = query_events(
                buckets["window"][0], buckets["afk"][0] if buckets["afk"] else None, end, now
            ) if buckets["window"] else []
            label = "today so far"

        web_events = []
        if buckets["web"]:
            web_events = query_events(buckets["web"][0], None, start, end)

        report = build_report(events, web_events, compile_rules(classes), label, start_of_day)
        _log(f"report generated for {label}: {len(events)} window events")
    except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as exc:
        message = f"ActivityWatch recap failed: {exc}"
        _log(message)
        if not args.print_only:
            show_message("ActivityWatch recap", message, is_warning=True)
        return 1

    if args.print_only:
        print(report)
        return 0

    show_message("ActivityWatch daily recap", report)
    try:
        SENTINEL_PATH.write_text(marker, encoding="utf-8")
    except OSError as exc:
        _log(f"could not write sentinel: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
