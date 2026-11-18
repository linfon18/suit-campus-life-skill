"""Analyze model-reviewed timetables or export RFC 5545 calendars, offline."""

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
import uuid


LOCAL = dt.timezone(dt.timedelta(hours=8))
NAMESPACE = uuid.UUID("719ea3a9-6796-49ad-8996-e89b6e662db5")
MAX_EVENTS = 20000
# Transcribed from the user's school bell-schedule image on 2026-11-14.
SUIT_PERIOD_TIME = {
    "1": ["08:30", "09:10"], "2": ["09:15", "09:55"],
    "3": ["10:10", "10:50"], "4": ["10:55", "11:35"],
    "5": ["14:00", "14:40"], "6": ["14:45", "15:25"],
    "7-8": ["15:40", "17:00"], "9-10": ["18:20", "19:40"],
    "11-12": ["19:50", "21:10"],
}


class NeedsInput(ValueError):
    pass


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in [{low}, {high}].")
    return value


def text(value, label, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label} must be a nonempty string.")
    if len(value) > 10000 or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise ValueError(f"{label} contains unsupported controls or is too long.")
    return value


def date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD.")
    return dt.date.fromisoformat(value)


def parse_weeks(spec):
    value = unicodedata.normalize("NFKC", text(spec, "weeks_spec"))
    value = re.sub(r"\s+", "", value).replace("，", ",").replace("、", ",")
    value = value.replace("至", "-").replace("~", "-").replace("～", "-")
    value = value.replace("—", "-").replace("–", "-").replace("－", "-")
    result = set()
    for part in value.split(","):
        match = re.fullmatch(r"第?(\d+)(?:-(\d+))?周?(?:\((单|双)周?\))?", part)
        if not match:
            raise ValueError(f"Unrecognized weeks_spec segment: {part!r}")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= 60:
            raise ValueError("Week ranges must increase within 1..60.")
        result.update(w for w in range(first, last + 1)
                      if not match[3] or w % 2 == (1 if match[3] == "单" else 0))
    if not result:
        raise ValueError("weeks_spec resolves to no weeks.")
    return sorted(result)


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("Period times must use HH:MM.")
    return dt.time.fromisoformat(value)


def periods(raw):
    if not isinstance(raw, dict) or not raw:
        raise NeedsInput("Supply period_time from an applicable timetable or bell schedule.")
    rows, used = [], set()
    for key, times in raw.items():
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", key)
        if not match or not isinstance(times, list) or len(times) != 2:
            raise ValueError("period_time maps '1' or '7-8' to ['HH:MM','HH:MM'].")
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= 30:
            raise ValueError("Period indices must increase within 1..30.")
        slots = set(range(first, last + 1))
        if slots & used:
            raise ValueError("period_time entries must not overlap in period indices.")
        used.update(slots)
        start, end = map(clock, times)
        if start >= end:
            raise ValueError("Each period must end after it starts on the same day.")
        rows.append((first, last, start, end))
    rows.sort()
    if any(a[3] > b[2] for a, b in zip(rows, rows[1:])):
        raise ValueError("Period times must be ordered and nonoverlapping.")
    return rows


def course_times(course, rows):
    first = integer(course.get("start_p"), "start_p", 1, 30)
    last = integer(course.get("end_p"), "end_p", first, 30)
    covered = {p for a, b, _, _ in rows for p in range(a, b + 1)}
    starts = [s for a, _, s, _ in rows if a == first]
    ends = [e for _, b, _, e in rows if b == last]
    if not starts or not ends or not set(range(first, last + 1)) <= covered:
        raise NeedsInput(f"Missing exact time boundaries for periods {first}-{last}; "
                         "do not subdivide grouped bell times.")
    if starts[0] >= ends[0]:
        raise ValueError("Course end must follow its start.")
    return starts[0], ends[0]


def bell_times(cfg):
    value = cfg.get("period_time")
    return periods(SUIT_PERIOD_TIME if value is None else value)


def bell_basis(cfg):
    return "suit_user_image_2026_11_14" if cfg.get("period_time") is None else "provided_period_time"


def esc(value):
    normalized = re.sub(r"\r\n?", "\n", str(value))
    escaped = {"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n"}
    return "".join(escaped.get(char, char) for char in normalized)


def fold(line):
    """RFC 5545 limits physical lines to 75 octets, including continuation SP."""
    output, chunk, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            output.append(chunk)
            chunk, size = " ", 1
        chunk += char
        size += width
    return output + [chunk]


def uid(calendar_id, identity):
    return str(uuid.uuid5(NAMESPACE, calendar_id + "|" + identity)) + "@campus-life"


def object_list(cfg, key, limit=300):
    value = cfg.get(key, [])
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(x, dict) for x in value):
        raise ValueError(f"{key} must be a list of at most {limit} objects.")
    return value


def array(cfg, key):
    value = cfg.get(key, [])
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError(f"{key} must be an array of at most 1000 values.")
    return value


def build(cfg, now=None):
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1:
        raise ValueError("Supply a schema_version: 1 object.")
    if not cfg.get("week1_monday"):
        raise NeedsInput("Supply the applicable first teaching week's Monday (week1_monday).")
    monday = date(cfg["week1_monday"])
    if monday.weekday() != 0:
        raise ValueError("week1_monday must be a Monday.")
    if cfg.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("This module currently supports Asia/Shanghai only.")
    times = bell_times(cfg)
    calendar_id = text(cfg.get("calendar_id"), "calendar_id")
    name = text(cfg.get("calendar_name"), "calendar_name")
    alarm = integer(cfg.get("alarms_minutes_before", 15), "alarms_minutes_before", 0, 10080)
    adjustment_status = cfg.get("calendar_adjustments_status")
    if adjustment_status not in ("confirmed", "unverified"):
        raise NeedsInput("Set calendar_adjustments_status to confirmed or unverified explicitly.")
    warnings = []
    if cfg.get("period_time") is None:
        warnings.append("Using the school bell preset transcribed from the user's 2026-11-14 image; "
                        "not independently verified as a current official schedule.")
    if adjustment_status == "unverified":
        warnings.append("School holidays and makeup arrangements are unverified; "
                        "only the supplied exceptions are applied.")
    sources = object_list(cfg, "sources", 100)
    for item in sources:
        text(item.get("kind"), "source kind")
        text(item.get("source"), "source")
    holidays = {date(x) for x in array(cfg, "holidays")}
    courses = object_list(cfg, "courses")
    original, identities, signatures = {}, set(), set()
    for course in courses:
        key = text(course.get("id"), "course id")
        if key in identities:
            raise ValueError(f"Duplicate course id: {key}")
        identities.add(key)
        text(course.get("name"), "course name")
        text(course.get("location"), "location", False)
        text(course.get("teacher"), "teacher", False)
        day = integer(course.get("weekday"), "weekday", 0, 6)
        weeks = parse_weeks(course.get("weeks_spec"))
        start, end = course_times(course, times)
        signature = (course["name"], day, start, end, tuple(weeks), course.get("location", ""))
        if signature in signatures:
            raise ValueError("Duplicate course occurrence definition; review repeated PDF cells.")
        signatures.add(signature)
        for week in weeks:
            on = monday + dt.timedelta(weeks=week - 1, days=day)
            original[key, on] = {
                "course_id": key, "name": course["name"], "date": on,
                "start": dt.datetime.combine(on, start, LOCAL),
                "end": dt.datetime.combine(on, end, LOCAL),
                "original_date": on, "week": week, "course": course,
                "uid": uid(calendar_id, "course|" + key + "|" + on.isoformat()),
            }
    active = {key: event for key, event in original.items() if key[1] not in holidays}
    cancelled = set()
    for item in object_list(cfg, "cancelled_classes", 1000):
        on = date(item.get("date"))
        selected = array(item, "course_ids")
        if any(key not in identities for key in selected):
            raise ValueError("cancelled_classes contains unknown course_ids.")
        matched = [key for key in original if key[1] == on and (not selected or key[0] in selected)]
        if not matched:
            raise ValueError("Cancellation does not match any scheduled course.")
        if selected and set(selected) != {key[0] for key in matched}:
            raise ValueError("Some selected cancellation courses have no occurrence on that date.")
        cancelled.update(matched)
        for key in matched:
            active.pop(key, None)
    moved, replacements, moved_keys = [], set(), set()
    for item in object_list(cfg, "makeup_classes", 1000):
        source, target = date(item.get("source_date")), date(item.get("date"))
        if source == target:
            raise ValueError("A makeup source and target must differ.")
        if type(item.get("replace_target_day")) is not bool:
            raise ValueError("Each makeup requires an explicit replace_target_day boolean.")
        selected = array(item, "course_ids")
        if any(key not in identities for key in selected):
            raise ValueError("makeup_classes contains unknown course_ids.")
        matches = [(key, event) for key, event in original.items()
                   if key[1] == source and (not selected or key[0] in selected)]
        if not matches:
            raise ValueError("Makeup source_date does not match scheduled classes.")
        matched_ids = {key[0] for key, _ in matches}
        if selected and set(selected) != matched_ids:
            raise ValueError("Some selected makeup courses have no occurrence on source_date.")
        if item["replace_target_day"]:
            replacements.add(target)
        for key, event in matches:
            if key in moved_keys or key in cancelled:
                raise ValueError("An occurrence cannot be moved twice or both cancelled and moved.")
            moved_keys.add(key)
            active.pop(key, None)
            changed = dict(event, date=target, makeup=True)
            changed["start"] = dt.datetime.combine(target, event["start"].timetz())
            changed["end"] = dt.datetime.combine(target, event["end"].timetz())
            moved.append(changed)
    active = {key: event for key, event in active.items() if key[1] not in replacements}
    events = sorted([*active.values(), *moved], key=lambda e: (e["start"], e["course_id"]))
    conflict_count, conflicts = 0, []
    for i, event in enumerate(events):
        for other in events[i + 1:]:
            if other["start"] >= event["end"]:
                break
            conflict_count += 1
            if len(conflicts) < 50:
                conflicts.append({"date": event["date"].isoformat(),
                                  "course_ids": [event["course_id"], other["course_id"]]})
    if conflict_count:
        warnings.append(f"{conflict_count} time overlaps preserved; review the source timetable.")
    unscheduled = object_list(cfg, "unscheduled")
    for item in unscheduled:
        text(item.get("name"), "unscheduled name")
    if unscheduled:
        warnings.append(f"{len(unscheduled)} unscheduled entries excluded; no invented times or all-day events.")
    all_day = object_list(cfg, "allday_events")
    day_ids = set()
    for item in all_day:
        key = text(item.get("id"), "all-day id")
        text(item.get("name"), "all-day name")
        text(item.get("source_ref"), "all-day source_ref")
        start, end = date(item.get("start")), date(item.get("end_exclusive"))
        if end <= start or (end - start).days > 420 or key in day_ids:
            raise ValueError("Invalid or duplicate all-day event; end_exclusive must follow start.")
        day_ids.add(key)
    if not events and not all_day:
        raise NeedsInput("No dated events to export.")
    if len(events) + len(all_day) > MAX_EVENTS:
        raise ValueError("Calendar exceeds the event budget.")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("DTSTAMP must use an aware datetime.")
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Campus Life//Timetable 4.9//CN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME;VALUE=TEXT:" + esc(name),
        "X-WR-TIMEZONE:Asia/Shanghai",
        "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD",
        "DTSTART:19700101T000000", "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800",
        "TZNAME:CST", "END:STANDARD", "END:VTIMEZONE",
    ]
    if warnings:
        lines.append("X-WR-CALDESC;VALUE=TEXT:" + esc("\n".join(warnings)))
    for event in events:
        course = event["course"]
        description = [
            f"第{event['week']}周；第{course['start_p']}-{course['end_p']}节",
            "周次原文：" + course["weeks_spec"],
        ]
        if course.get("teacher"):
            description.append("教师：" + course["teacher"])
        if course.get("source_ref"):
            description.append("课表依据：" + text(course["source_ref"], "source_ref"))
        if event.get("makeup"):
            description.append("调课：原日期 " + event["original_date"].isoformat())
        lines.extend([
            "BEGIN:VEVENT", "UID:" + event["uid"], "DTSTAMP:" + stamp,
            "DTSTART;TZID=Asia/Shanghai:" + event["start"].strftime("%Y%m%dT%H%M%S"),
            "DTEND;TZID=Asia/Shanghai:" + event["end"].strftime("%Y%m%dT%H%M%S"),
            "SUMMARY:" + esc(event["name"]),
            "LOCATION:" + esc(course.get("location") or ""),
            "DESCRIPTION:" + esc("\n".join(description)),
        ])
        if alarm:
            lines.extend([
                "BEGIN:VALARM", f"TRIGGER:-PT{alarm}M", "ACTION:DISPLAY",
                "DESCRIPTION:" + esc(event["name"] + f"将在{alarm}分钟后开始"), "END:VALARM",
            ])
        lines.append("END:VEVENT")
    for item in all_day:
        lines.extend([
            "BEGIN:VEVENT", "UID:" + uid(calendar_id, "allday|" + item["id"]),
            "DTSTAMP:" + stamp, "DTSTART;VALUE=DATE:" + date(item["start"]).strftime("%Y%m%d"),
            "DTEND;VALUE=DATE:" + date(item["end_exclusive"]).strftime("%Y%m%d"),
            "SUMMARY:" + esc(item["name"]),
            "DESCRIPTION:" + esc(text(item.get("description"), "description", False)
                                  + "\n依据：" + item["source_ref"]), "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    data = ("\r\n".join(part for line in lines for part in fold(line)) + "\r\n").encode("utf-8")
    ids = [event["uid"] for event in events] + [uid(calendar_id, "allday|" + e["id"]) for e in all_day]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate event UIDs.")
    counts = Counter(e["course_id"] for e in events)
    report = {
        "status": "pass_with_warnings" if warnings else "pass",
        "events": len(ids), "timed_events": len(events), "allday_events": len(all_day),
        "makeup_events": len(moved), "course_definitions": len(courses),
        "timezone": "Asia/Shanghai", "alarms_minutes_before": alarm,
        "calendar_adjustments_status": adjustment_status,
        "period_time_basis": bell_basis(cfg),
        "first_timed_date": events[0]["date"].isoformat() if events else None,
        "last_timed_date": events[-1]["date"].isoformat() if events else None,
        "courses": [{"id": c["id"], "name": c["name"], "occurrences": counts[c["id"]]}
                    for c in courses],
        "excluded_unscheduled": unscheduled, "conflict_count": conflict_count,
        "conflicts": conflicts, "warnings": warnings, "sources": sources,
        "retrieved_urls": [], "failed_urls": [],
    }
    return data, report


def analyze(cfg, week=None):
    """Describe original teaching-week patterns, without inventing calendar dates."""
    if not isinstance(cfg, dict) or cfg.get("schema_version") != 1:
        raise ValueError("Supply a schema_version: 1 object.")
    if cfg.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("This module currently supports Asia/Shanghai only.")
    if week is not None:
        integer(week, "week", 1, 60)
    rows = bell_times(cfg)
    courses, ids, signatures, missing_times = [], set(), set(), []
    for raw in object_list(cfg, "courses"):
        key = text(raw.get("id"), "course id")
        name = text(raw.get("name"), "course name")
        day = integer(raw.get("weekday"), "weekday", 0, 6)
        first = integer(raw.get("start_p"), "start_p", 1, 30)
        last = integer(raw.get("end_p"), "end_p", first, 30)
        weeks = parse_weeks(raw.get("weeks_spec"))
        location = text(raw.get("location"), "location", False)
        signature = (name, day, first, last, tuple(weeks), location)
        if key in ids or signature in signatures:
            raise ValueError("Duplicate course id or occurrence definition; review the source.")
        ids.add(key)
        signatures.add(signature)
        try:
            start, end = course_times(raw, rows)
            start, end = start.strftime("%H:%M"), end.strftime("%H:%M")
        except NeedsInput:
            start = end = None
            missing_times.append(key)
        courses.append({"id": key, "name": name, "weekday": day, "start_p": first,
                        "end_p": last, "weeks": weeks, "weeks_spec": raw["weeks_spec"],
                        "location": location, "source_ref": raw.get("source_ref"),
                        "start": start, "end": end})
    if not courses:
        raise NeedsInput("No scheduled courses to analyze; preserve unscheduled entries separately.")
    unscheduled = object_list(cfg, "unscheduled")
    for item in unscheduled:
        text(item.get("name"), "unscheduled name")
    sources = object_list(cfg, "sources", 100)
    for item in sources:
        text(item.get("kind"), "source kind")
        text(item.get("source"), "source")
    dated = bool(array(cfg, "holidays"))
    for key in ("cancelled_classes", "makeup_classes", "allday_events"):
        dated = bool(object_list(cfg, key, 1000)) or dated
    warnings = [
        "Original timetable patterns only; no calendar dates or actual current teaching week inferred.",
        "Gaps are unscheduled timetable windows, not confirmed personal availability.",
        "Periods are numbered slots; daily span and clock gaps include breaks, not net teaching minutes.",
        "The last listed teaching week is not proof of the semester end or examination dates.",
    ]
    if cfg.get("calendar_adjustments_status") != "confirmed":
        warnings.append("School holidays and makeup arrangements remain unverified.")
    if dated:
        warnings.append("Supplied dated exceptions are NOT applied in this relative analysis; "
                        "reconcile them before allocating tasks on actual dates.")
    if missing_times:
        warnings.append("Some exact bell boundaries are unknown; no grouped times were subdivided.")
    if unscheduled:
        warnings.append("Unscheduled entries remain unallocated and are not counted as free time.")

    def minute(value):
        parsed = clock(value)
        return parsed.hour * 60 + parsed.minute

    def day_report(day, active):
        ordered = sorted(active, key=lambda c: (c["start_p"], c["end_p"], c["id"]))
        occupied = sorted({p for c in ordered for p in range(c["start_p"], c["end_p"] + 1)})
        pairs = [(a["id"], b["id"]) for i, a in enumerate(ordered) for b in ordered[i + 1:]
                 if max(a["start_p"], b["start_p"]) <= min(a["end_p"], b["end_p"])]
        period_gaps = [{"start_p": a + 1, "end_p": b - 1}
                       for a, b in zip(occupied, occupied[1:]) if b > a + 1]
        complete = bool(ordered) and all(c["start"] is not None for c in ordered)
        gaps, end, first_time, last_time = None, None, None, None
        if complete:
            gaps = []
            first_time = min(c["start"] for c in ordered)
            last_time = max(c["end"] for c in ordered)
            for course in ordered:
                if end is not None and course["start"] > end:
                    gaps.append({"start": end, "end": course["start"],
                                 "elapsed_minutes": minute(course["start"]) - minute(end)})
                end = max(end or course["end"], course["end"])
        return {
            "weekday": day, "course_ids": [c["id"] for c in ordered],
            "sessions": len(ordered),
            "scheduled_periods": sum(c["end_p"] - c["start_p"] + 1 for c in ordered),
            "occupied_periods": len(occupied), "period_gaps": period_gaps,
            "first_start": first_time, "last_end": last_time,
            "daily_span_minutes": minute(last_time) - minute(first_time) if complete else None,
            "clock_gaps": gaps, "conflict_count": len(pairs),
            "conflicts": [list(pair) for pair in pairs[:50]],
        }

    selected = [week] if week is not None else list(range(1, max(max(c["weeks"]) for c in courses) + 1))
    weekly, patterns, known_patterns = [], [], {}
    for number in selected:
        days = [day_report(day, [c for c in courses if c["weekday"] == day and number in c["weeks"]])
                for day in range(7)]
        signature = tuple(tuple(day["course_ids"]) for day in days)
        if signature not in known_patterns:
            known_patterns[signature] = len(patterns) + 1
            patterns.append({"pattern_id": len(patterns) + 1, "weeks": [], "days": days})
        pattern = known_patterns[signature]
        patterns[pattern - 1]["weeks"].append(number)
        weekly.append({
            "week": number, "pattern_id": pattern,
            "sessions": sum(d["sessions"] for d in days),
            "scheduled_periods": sum(d["scheduled_periods"] for d in days),
            "occupied_periods": sum(d["occupied_periods"] for d in days),
            "conflict_count": sum(d["conflict_count"] for d in days),
        })
    conflicts = sum(w["conflict_count"] for w in weekly)
    if conflicts:
        warnings.append(f"{conflicts} overlapping course pairs across selected weeks; review before planning.")
    return {
        "status": "pass_with_warnings", "mode": "original_timetable_analysis",
        "date_basis": "relative_teaching_week", "dated_exceptions_applied": False,
        "period_time_basis": bell_basis(cfg),
        "dated_exceptions_present": dated, "analyzed_weeks": selected,
        "course_definitions": len(courses), "distinct_course_names": len({c["name"] for c in courses}),
        "scheduled_occurrences": sum(w["sessions"] for w in weekly),
        "scheduled_periods": sum(w["scheduled_periods"] for w in weekly),
        "conflict_count": conflicts, "weekly": weekly, "patterns": patterns, "courses": courses,
        "missing_exact_times": missing_times, "unscheduled": unscheduled,
        "warnings": warnings, "sources": sources, "retrieved_urls": [], "failed_urls": [],
    }


def extract_pdf(path, page=1, pages=2, max_chars=16000):
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"status": "reader_required", "note": "Use an available PDF reader/OCR; "
                "pypdf is optional and is not installed automatically.", "retrieved_urls": []}
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 32000000:
        raise ValueError("PDF must be an existing file no larger than 32 MB.")
    reader = PdfReader(source)
    if reader.is_encrypted:
        return {"status": "reader_required", "note": "Encrypted PDF; no password guessing.",
                "retrieved_urls": []}
    integer(page, "page", 1, len(reader.pages))
    integer(pages, "pages", 1, 3)
    integer(max_chars, "max_chars", 1000, 30000)
    result, remaining = [], max_chars
    next_page = None
    for index in range(page - 1, min(len(reader.pages), page - 1 + pages)):
        body = reader.pages[index].extract_text(extraction_mode="layout") or ""
        clipped = len(body) > remaining
        result.append({"page": index + 1, "text": body[:remaining],
                       "text_truncated": clipped, "ocr_needed": not body.strip()})
        remaining -= len(body[:remaining])
        if clipped or remaining == 0:
            next_page = index + 1 if clipped else (index + 2 if index + 1 < len(reader.pages) else None)
            break
        next_page = index + 2 if index + 1 < len(reader.pages) else None
    return {"status": "reader_required" if any(r["text_truncated"] or r["ocr_needed"] for r in result) else "pass",
            "total_pages": len(reader.pages), "pages": result, "next_page": next_page,
            "note": "Text extraction is not verification of weekday columns or merged cells.",
            "retrieved_urls": [], "failed_urls": []}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", help="Model-reviewed JSON; no school calendar defaults.")
    mode.add_argument("--extract-pdf", help="Optional local pypdf text extraction; omit -S.")
    result.add_argument("--output", help="New .ics path in an already permitted existing directory.")
    result.add_argument("--validate-only", action="store_true", help="Validate without writing a file.")
    result.add_argument("--analyze", action="store_true", help="Analyze original teaching-week patterns; no dates needed.")
    result.add_argument("--week", type=int, help="Select a teaching week for --analyze, from 1 to 60.")
    result.add_argument("--page", type=int, default=1)
    result.add_argument("--pages", type=int, default=2)
    result.add_argument("--max-chars", type=int, default=16000)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.week is not None and not args.analyze:
            raise ValueError("--week requires --analyze.")
        if args.analyze and (args.output or args.validate_only or args.extract_pdf):
            raise ValueError("--analyze requires --config and does not export or validate ICS.")
        if args.extract_pdf:
            if args.output or args.validate_only:
                raise ValueError("PDF extraction does not accept --output or --validate-only.")
            report = extract_pdf(args.extract_pdf, args.page, args.pages, args.max_chars)
        else:
            path = Path(args.config)
            if path.stat().st_size > 2000000:
                raise ValueError("Config exceeds 2 MB.")
            cfg = json.loads(path.read_text("utf-8-sig"))
            if args.analyze:
                report = analyze(cfg, args.week)
                print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
                return 0
            data, report = build(cfg)
            if args.validate_only:
                if args.output:
                    raise ValueError("Use either --validate-only or --output.")
            else:
                if not args.output:
                    raise ValueError("Supply --output or --validate-only.")
                target = Path(args.output)
                if target.suffix.lower() != ".ics" or not target.parent.is_dir():
                    raise ValueError("Output must be an .ics file in an existing permitted directory.")
                with target.open("xb") as stream:
                    stream.write(data)
                report.update(output_file=str(target.resolve()), bytes=len(data),
                              sha256=hashlib.sha256(data).hexdigest())
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if report["status"] in ("pass", "pass_with_warnings") else 2
    except Exception as error:
        print(json.dumps({"status": "needs_input" if isinstance(error, NeedsInput) else "error",
                          "error": str(error), "retrieved_urls": [], "failed_urls": []},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
