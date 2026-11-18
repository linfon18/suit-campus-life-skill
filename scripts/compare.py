"""Compare named dishes across the current anonymous school-canteen catalog."""

import argparse
from collections import defaultdict
import datetime
import json
from pathlib import Path
import runpy
import unicodedata
import uuid


DINING = runpy.run_path(str(Path(__file__).with_name("dining.py")),
                        run_name="campus_comparison_dining")


def text_key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def nonempty(text):
    if not text.strip():
        raise argparse.ArgumentTypeError("Dish must not be blank")
    return text.strip()


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--dish", required=True, type=nonempty)
    root.add_argument("--canteen", choices=("A", "B", "C", "紫荆", "石榴"))
    root.add_argument("--option", action="append", default=[])
    root.add_argument("--budget", type=DINING["budget_value"])
    root.add_argument("--price-scope", choices=("dish", "with-box"), default="with-box")
    root.add_argument("--now", action="store_true")
    root.add_argument("--exclude-shop", action="append", default=[])
    root.add_argument("--max-pages", type=DINING["integer_range"](1, 10000))
    root.add_argument("--max-menus", type=DINING["integer_range"](1, 100000))
    root.add_argument("--workers", type=DINING["integer_range"](1, 8), default=4)
    root.add_argument("--summary", action="store_true",
                      help="Print concise evidence without raw shops or duplicate request bodies")
    root.add_argument("--limit", type=DINING["integer_range"](1, 300), default=100)
    root.add_argument("--output-dir")
    return root


def status_band(row):
    state = row["availability"]
    if state["current_order_conditions_supported"]:
        return "current_conditions_supported"
    return {
        "Open": "open_other_conditions_unconfirmed",
        "Prebook": "prebook",
        "Pause": "pause",
    }.get(state["platform_status"], "unknown")


def compare_rows(rows, args):
    ranked, unranked, groups = [], [], defaultdict(list)
    metric = "dish_fen" if args.price_scope == "dish" else "with_box_estimate_fen"
    for row in rows:
        name = text_key(row["item"].get("itemName"))
        if text_key(args.dish) not in name:
            continue
        row["match_kind"] = "exact_name" if name == text_key(args.dish) else "name_contains"
        row["status_band"] = status_band(row)
        row["comparison_fen"] = row["pricing"][metric]
        row["comparison_yuan"] = DINING["yuan"](row["comparison_fen"])
        reasons = list(row["constraint_failures"])
        if row["comparison_fen"] is None:
            reasons.append("comparison_price_unknown")
        if args.now and not row["availability"]["current_order_conditions_supported"]:
            reasons.append("current_order_conditions_not_confirmed")
        row["comparison_exclusions"] = sorted(set(reasons))
        if reasons:
            unranked.append(row)
            continue
        ranked.append(row)
        signature = tuple(sorted(
            (text_key(option["group"]), text_key(option["label"]))
            for option in row["pricing"]["selected_options"]
        ))
        groups[(name, signature, row["status_band"])].append(row)
    ranked.sort(key=lambda row: (row["comparison_fen"], row["shop_id"],
                                 str(row["item"]["id"])))
    comparisons = []
    for (_, _, band), members in groups.items():
        low = min(row["comparison_fen"] for row in members)
        high = max(row["comparison_fen"] for row in members)
        shops = {row["shop_id"] for row in members}
        comparisons.append({
            "item_name": members[0]["item"]["itemName"],
            "selected_options": members[0]["pricing"]["selected_options"],
            "status_band": band,
            "candidate_count": len(members),
            "distinct_shop_count": len(shops),
            "lowest_known_fen": low,
            "lowest_known_yuan": DINING["yuan"](low),
            "highest_known_fen": high,
            "spread_fen": high - low if len(shops) > 1 else None,
            "spread_yuan": DINING["yuan"](high - low) if len(shops) > 1 else None,
            "lowest_candidates": [{
                "shop_id": row["shop_id"], "shop_name": row["shop_name"],
                "canteen": row["canteen"], "item_id": str(row["item"]["id"]),
            } for row in members if row["comparison_fen"] == low],
            "same_name_does_not_verify_equal_portions": True,
        })
    comparisons.sort(key=lambda group: (group["lowest_known_fen"], group["item_name"]))
    return ranked, unranked, comparisons


def execute(args, api):
    raw = DINING["parser"]().parse_args(["menu"])
    raw.canteen, raw.option, raw.budget = args.canteen, args.option, args.budget
    raw.budget_scope, raw.exclude_shop = args.price_scope, args.exclude_shop
    raw.max_pages, raw.max_menus, raw.limit = args.max_pages, args.max_menus, None
    raw.dish, raw.workers, raw.scan_all_menus = args.dish, args.workers, True
    # Keep all states and match dish names locally, not ingredient descriptions.
    result = DINING["execute"](raw, api)
    ranked, unranked, groups = compare_rows(result["items"], args)
    result["command"] = "compare"
    result["filters"].update(dish=args.dish, now=args.now)
    result["comparison"] = {
        "title": "食堂比价",
        "price_scope": args.price_scope,
        "ranking_count": len(ranked),
        "unranked_count": len(unranked),
        "comparison_group_count": len(groups),
        "ranking_is_checkout_quote": False,
        "delivery_included": False,
        "global_school_cheapest_verified": False,
        "same_name_does_not_verify_equal_portions": True,
    }
    result["items"] = ranked[:args.limit]
    result["unranked_items"] = unranked[:args.limit]
    result["comparison_groups"] = groups[:args.limit]
    coverage = result["coverage"]
    coverage.update(
        name_matches=len(ranked) + len(unranked),
        items_matching=len(ranked) + len(unranked),
        items_returned=len(result["items"]),
        items_truncated=len(ranked) > args.limit,
        unranked_items_truncated=len(unranked) > args.limit,
        comparison_groups_truncated=len(groups) > args.limit,
    )
    coverage["platform_scope_scan_complete"] = (
        coverage["shop_scope_source"] == "live_school_groups"
        and coverage["listing_exhausted"]
        and not coverage.get("group_shop_ids_not_returned")
        and coverage["menus_failed"] == 0
        and coverage["menus_unscanned"] == 0
        and not result["errors"]
    )
    canteens = {}
    for check in result["menu_checks"]:
        label = check["canteen"] or "unknown"
        summary = canteens.setdefault(label, {
            "menus_success": 0, "menus_failed": 0, "menus_empty": 0,
            "ranked_matches": 0, "unranked_matches": 0,
        })
        summary["menus_success" if check["status"] == "success" else "menus_failed"] += 1
        if check["status"] == "success" and check["item_count"] == 0:
            summary["menus_empty"] += 1
    for key, rows in (("ranked_matches", ranked), ("unranked_matches", unranked)):
        for row in rows:
            canteens[row["canteen"] or "unknown"][key] += 1
    coverage["by_canteen"] = canteens
    if hasattr(api, "audit"):
        result["outgoing_requests"] = api.audit.requests
    return result


def summary_result(result):
    summary = DINING["pick"](result, (
        "status", "source", "queried_at_beijing", "school_id", "command", "filters",
        "comparison", "coverage", "errors", "warnings", "comparison_groups",
        "data_available", "error_type", "artifact",
    ))
    for key in ("items", "unranked_items"):
        summary[key] = []
        for row in result.get(key, []):
            selected = DINING["pick"](row, (
                "shop_id", "shop_name", "canteen", "pricing", "availability",
                "comparison_fen", "comparison_yuan", "comparison_exclusions", "status_band",
            ))
            selected["item"] = DINING["pick"](row["item"], (
                "id", "itemName", "category", "originPrice", "discountPrice", "boxFee",
            ))
            summary[key].append(selected)
    requests = result.get("requests", [])
    origin = "https://m.kplocker.com"
    summary["retrieved_urls"] = list(dict.fromkeys(
        origin + event["path"] for event in requests
        if event.get("status") == "pass" and event.get("http_status") == 200
    ))
    summary["failed_urls"] = list(dict.fromkeys(
        origin + event["path"] for event in requests
        if not event.get("will_retry") and (
            event.get("status") == "fail" or event.get("data_usable") is False
        )
    ))
    summary["retry_summary"] = {
        "request_attempts": len(requests),
        "retry_attempts": sum(event.get("attempt", 1) > 1 for event in requests),
        "failed_attempts": sum(event.get("status") == "fail"
                               or event.get("data_usable") is False for event in requests),
    }
    return summary


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args, DINING["PublicApi"]())
    except Exception as error:
        result = {
            "status": "failed", "command": "compare", "source": "live_query_failed",
            "queried_at_beijing": datetime.datetime.now(DINING["BEIJING"]).isoformat(),
            "error_type": type(error).__name__, "data_available": False,
        }
    if args.output_dir:
        filename = "campus-price-compare-" + result.get("run_id", uuid.uuid4().hex) + ".json"
        result["artifact"] = {"status": "pass", "filename": filename}
        try:
            with (Path(args.output_dir) / filename).open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n")
        except Exception as error:
            result["artifact"] = {"status": "fail", "error_type": type(error).__name__}
    output = summary_result(result) if args.summary else result
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return result


if __name__ == "__main__":
    main()
