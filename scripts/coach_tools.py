"""Study-date helpers and optional external historical-source lookup."""

import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
BANK = ROOT / "references" / "exam-bank"
BEIJING = dt.timezone(dt.timedelta(hours=8))
ANSWER = "\u3010\u7b54\u6848\u3011"
EXPLANATION = "\u3010\u89e3\u6790\u3011"


def bounded(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("Expected an integer")
        if not low <= number <= high:
            raise argparse.ArgumentTypeError("Integer outside allowed range")
        return number
    return parse


def date_value(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a valid YYYY-MM-DD date")


def intervals_value(value):
    try:
        result = sorted({int(part.strip()) for part in value.split(",")})
        if not result or any(number < 1 or number > 366 for number in result):
            raise ValueError()
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("Expected comma-separated offsets from 1 to 366 days")


def sections(path):
    lines = path.read_text("utf-8-sig").splitlines()
    starts = [(index, line.lstrip("#").strip()) for index, line in enumerate(lines)
              if re.match(r"^#{2,3}\s+", line)]
    for position, (begin, heading) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        text = "\n".join(lines[begin:end]).strip()
        if end > begin + 1:
            yield {
                "id": heading, "line_start": begin + 1, "line_end": end,
                "text": text,
            }


def source_warnings(path, question_id):
    warnings = ["User-provided historical source; factual accuracy is not independently verified."]
    if path.parent.name == "cet4" and not path.stem.endswith("-answers"):
        warnings.append("No matching answer file for this date and set is bundled.")
    if path.parent.name == "cet6":
        warnings.append("No reference translations are bundled for this collection.")
    if path.parent.name == "ncre1" and question_id == "Q1":
        warnings.append("Explanation omits the word 'directly' used in the question; review wording.")
    if path.parent.name == "ncre1" and question_id in ("Q7", "Q8"):
        warnings.append("Capacity-unit conversion convention is not explicit; do not treat as universally unambiguous.")
    return warnings


def missing_bank(command, location):
    return {
        "status": "not_bundled", "command": command, "matches": [], "files": [],
        "bank_directory": str(location), "retrieved_urls": [], "network_used": False,
        "reason": "No readable historical-source Markdown files were supplied for this lookup.",
        "next_step": (
            "Use references/exam-bank/INDEX.md and scripts/github_fetch.py for selected "
            "CET cloud sources. Other exams need environment search or the user's question. "
            "Only use --bank-dir when the user explicitly supplies an external source directory."
        ),
        "coverage_note": "Source absence is not a completed search with zero matches.",
    }


def selected_bank(args):
    return Path(args.bank_dir).resolve() if args.bank_dir else BANK


def local_source_name(path, bank):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return (bank.name + "/" + path.relative_to(bank).as_posix())


def bank_search(args):
    bank = selected_bank(args)
    paths = sorted((bank / args.exam).glob("*.md"))
    if not paths:
        return dict(missing_bank("bank-search", bank), exam=args.exam)
    terms = args.query.casefold().split() if args.query else []
    matches = []
    for path in paths:
        if path.stem.endswith("-answers") and not args.include_answer:
            continue
        for section in sections(path):
            if args.id and section["id"].casefold() != args.id.casefold():
                continue
            text = section["text"]
            if not args.include_answer:
                text = text.split(ANSWER, 1)[0].rstrip()
            score = sum(term in text.casefold() for term in terms)
            if terms and not score:
                continue
            matches.append({
                "source_file": local_source_name(path, bank),
                "id": section["id"], "line_start": section["line_start"],
                "line_end": section["line_end"],
                "matched_terms": [term for term in terms if term in text.casefold()],
                "excerpt": text[:args.max_chars], "truncated": len(text) > args.max_chars,
                "answer_requested": args.include_answer,
                "source_status": "external_unverified" if args.bank_dir else "bundled_unverified",
                "warnings": source_warnings(path, section["id"]), "_score": score,
            })
    matches.sort(key=lambda value: (-value["_score"], value["source_file"], value["line_start"]))
    total = len(matches)
    selected = matches[:args.limit]
    for value in selected:
        del value["_score"]
    return {
        "status": "pass", "command": "bank-search", "exam": args.exam,
        "matches": selected, "matching_sections": total, "results_truncated": total > args.limit,
        "retrieved_urls": [], "network_used": False,
        "note": "Literal source lookup only; generated practice and diagnosis remain the model's task.",
    }


def bank_audit(args):
    bank = selected_bank(args)
    paths = sorted(bank.glob("*/*.md"))
    if not paths:
        return missing_bank("bank-audit", bank)
    records = []
    for path in paths:
        text = path.read_text("utf-8-sig")
        blocks = list(sections(path))
        record = {
            "file": local_source_name(path, bank), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "section_count": len(blocks),
            "source_status": "external_unverified" if args.bank_dir else "bundled_unverified",
        }
        if path.parent.name == "ncre1":
            questions = [block for block in blocks if re.fullmatch(r"Q\d+", block["id"])]
            ids = [int(block["id"][1:]) for block in questions]
            invalid, missing_explanations = [], []
            for block in questions:
                labels = re.findall(r"(?m)^([A-D])[).、]", block["text"])
                keys = re.findall(re.escape(ANSWER) + r"\s*([A-D]+)\s*(?:\n|$)", block["text"])
                if Counter(labels) != Counter("ABCD") or len(keys) != 1 or len(keys[0]) != 1:
                    invalid.append(block["id"])
                if EXPLANATION not in block["text"]:
                    missing_explanations.append(block["id"])
            record.update(
                question_count=len(questions), duplicate_ids=[number for number, count
                    in Counter(ids).items() if count > 1],
                missing_ids=sorted(set(range(1, max(ids, default=0) + 1)) - set(ids)),
                invalid_choice_or_answer_format=invalid,
                missing_explanation_count=len(missing_explanations),
                missing_explanation_ids=missing_explanations,
                known_review_flags=["Q1", "Q7", "Q8"],
            )
        record["answer_markers"] = text.count(ANSWER)
        records.append(record)
    return {
        "status": "pass", "command": "bank-audit", "files": records,
        "retrieved_urls": [], "network_used": False,
        "limitation": "Format checks do not verify correctness, exam provenance, or answer pairing.",
    }


def plan(args):
    days = args.days if args.days is not None else (args.exam_date - args.start).days
    if days < 0:
        raise ValueError("Exam date is before plan start; confirm the intended exam.")
    if days > 366:
        raise ValueError("Limit a single plan to 366 days.")
    schedule = []
    for offset in range(days):
        day = args.start + dt.timedelta(days=offset)
        minutes = args.weekend_minutes if day.weekday() >= 5 and args.weekend_minutes is not None \
            else args.daily_minutes
        if minutes == 0:
            blocks = []
        elif minutes < 15:
            blocks = [{"task": "one_focused_review", "minutes": minutes}]
        else:
            recall = max(2, minutes // 5)
            practice = minutes * 3 // 5
            blocks = [
                {"task": "recall_previous_error", "minutes": recall},
                {"task": "independent_variant", "minutes": practice},
                {"task": "explain_and_record", "minutes": minutes - recall - practice},
            ]
        schedule.append({
            "date": day.isoformat(), "weekday": day.isoweekday(),
            "available_minutes": minutes, "blocks": blocks, "rest_day": minutes == 0,
        })
    return {
        "status": "pass", "command": "plan", "start": args.start.isoformat(),
        "exam_date": args.exam_date.isoformat() if args.exam_date else None,
        "exam_date_verified": False, "exam_date_source": "user_argument" if args.exam_date else "unknown",
        "exam_day_excluded": True, "days": schedule,
        "total_minutes": sum(day["available_minutes"] for day in schedule),
        "note": "Calendar allocation only; the model fills tasks from actual subject and error evidence.",
        "retrieved_urls": [], "network_used": False,
    }


def review_dates(args):
    if args.until and args.until < args.learned:
        raise ValueError("Review cutoff is before the learning date.")
    values = [{"offset_days": offset, "date": (args.learned + dt.timedelta(days=offset)).isoformat()}
              for offset in args.intervals]
    return {
        "status": "pass", "command": "review-dates", "learned": args.learned.isoformat(),
        "reviews": [row for row in values if not args.until or date_value(row["date"]) <= args.until],
        "after_cutoff": [row for row in values if args.until and date_value(row["date"]) > args.until],
        "automatic_reminders_created": False,
        "note": "Adjustable practice offsets, not an individualized forgetting prediction.",
        "retrieved_urls": [], "network_used": False,
    }


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    search = commands.add_parser("bank-search")
    search.add_argument("--exam", choices=("cet4", "cet6", "ncre1"), required=True)
    target = search.add_mutually_exclusive_group(required=True)
    target.add_argument("--query")
    target.add_argument("--id")
    search.add_argument("--limit", type=bounded(1, 10), default=3)
    search.add_argument("--max-chars", type=bounded(100, 5000), default=1400)
    search.add_argument("--include-answer", action="store_true")
    search.add_argument("--bank-dir", help="Explicitly supplied external historical-source directory.")
    audit = commands.add_parser("bank-audit")
    audit.add_argument("--bank-dir", help="Explicitly supplied external historical-source directory.")
    calendar = commands.add_parser("plan")
    calendar.add_argument("--start", type=date_value, default=dt.datetime.now(BEIJING).date())
    duration = calendar.add_mutually_exclusive_group(required=True)
    duration.add_argument("--days", type=bounded(1, 366))
    duration.add_argument("--exam-date", type=date_value)
    calendar.add_argument("--daily-minutes", type=bounded(0, 360), default=30)
    calendar.add_argument("--weekend-minutes", type=bounded(0, 360))
    review = commands.add_parser("review-dates")
    review.add_argument("--learned", type=date_value, required=True)
    review.add_argument("--intervals", type=intervals_value, default=[1, 3, 7, 14])
    review.add_argument("--until", type=date_value)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "bank-search":
            if args.query is not None and not args.query.strip():
                raise ValueError("Search query must not be blank.")
            result = bank_search(args)
        elif args.command == "bank-audit":
            result = bank_audit(args)
        elif args.command == "plan":
            result = plan(args)
        else:
            result = review_dates(args)
    except (ValueError, OSError) as error:
        result = {"status": "error", "command": args.command,
                  "error": str(error), "retrieved_urls": []}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return result


if __name__ == "__main__":
    main()
