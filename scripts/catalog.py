"""Collect the whole returned menu catalog once, without semantic filtering."""

import argparse
from collections import defaultdict
import datetime
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time
import uuid


DINING = runpy.run_path(str(Path(__file__).with_name("dining.py")),
                       run_name="campus_catalog_dining")
ITEM_COLUMNS = [
    "id", "name", "category", "description", "origin_fen", "discount_fen",
    "box_fen", "option_groups", "selected_options", "dish_fen", "with_box_fen",
    "stock", "sale_on_time", "sale_on_book", "sale_time_range", "ship_days",
    "single_purchase_forbidden", "is_food", "no_buy", "min_order_quantity",
    "max_order_quantity", "current_conditions_supported", "blocking_reasons",
    "unknowns", "constraint_failures",
]
SNAPSHOT_REUSE_SECONDS = 300


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--canteen", choices=("A", "B", "C", "紫荆", "石榴"))
    root.add_argument("--workers", type=DINING["integer_range"](1, 8), default=4)
    root.add_argument("--option", action="append", default=[])
    root.add_argument("--budget", type=DINING["budget_value"])
    root.add_argument("--price-scope", choices=("dish", "with-box"), default="with-box")
    root.add_argument("--now", action="store_true")
    mode = root.add_mutually_exclusive_group()
    mode.add_argument("--state-dir", help="Existing task directory with permitted read/write access.")
    mode.add_argument("--read-snapshot", help="Read the next page locally; never query menus.")
    mode.add_argument("--rank-snapshot", help="Rank the model-selected item references locally.")
    mode.add_argument("--check-snapshot", help="Check freshness before reusing a previous query.")
    mode.add_argument("--full", action="store_true", help="Explicit diagnostic full stdout; may be large.")
    root.add_argument("--snapshot-id", help="ID returned by the original scan.")
    root.add_argument("--cursor", type=DINING["integer_range"](0, 10000000), default=0)
    root.add_argument("--max-output-bytes", type=DINING["integer_range"](8192, 48000), default=32000)
    root.add_argument("--select", help="Model-selected references: 1,3,7-12; use 'none' for no matches.")
    root.add_argument("--reviewed-through", type=DINING["integer_range"](0, 10000000))
    root.add_argument("--order", choices=("asc", "desc"), default="asc")
    root.add_argument("--intent", choices=("meal", "items"), default="meal",
                      help="Ranking only: meal excludes flagged components; items allows explicit snacks/add-ons.")
    root.add_argument("--limit", type=DINING["integer_range"](1, 20), default=8)
    return root


def pack(row):
    item, price, state = row["item"], row["pricing"], row["availability"]
    return [
        item["id"], item.get("itemName"), item.get("category"),
        item.get("itemDesc"), item.get("originPrice"), item.get("discountPrice"),
        item.get("boxFee"), item.get("option_groups"), price["selected_options"],
        price["dish_fen"], price["with_box_estimate_fen"], item.get("stockAmount"),
        item.get("saleOnTime"), item.get("saleOnBook"), item.get("saleTimeRange"),
        item.get("shipDays"), item.get("singlePurchaseForbidden"), item.get("isFood"),
        item.get("noBuy"), item.get("minOrderQuantity"), item.get("maxOrderQuantity"),
        state["current_order_conditions_supported"], state["blocking_reasons"],
        state["unknowns"], row["constraint_failures"],
    ]


def compact_shop(shop):
    rows = shop["items"]
    constant = {
        index: rows[0][index] for index in range(len(ITEM_COLUMNS))
        if rows and all(row[index] == rows[0][index] for row in rows[1:])
    }
    varying = [index for index in range(len(ITEM_COLUMNS)) if index not in constant]
    return {
        **shop,
        "item_defaults": {ITEM_COLUMNS[index]: value for index, value in constant.items()},
        "row_columns": [ITEM_COLUMNS[index] for index in varying],
        "items": [[row[index] for index in varying] for row in rows],
    }


def execute(args, api):
    query = DINING["parser"]().parse_args(["menu"])
    query.canteen, query.workers = args.canteen, args.workers
    query.option, query.budget = args.option, args.budget
    query.budget_scope, query.now = args.price_scope, args.now
    query.dish = ""
    query.max_pages = query.max_menus = query.limit = None
    query.scan_all_menus = True
    raw = DINING["execute"](query, api)
    rows_by_shop = defaultdict(list)
    for row in raw["items"]:
        rows_by_shop[row["shop_id"]].append(pack(row))
    checks = {check["shop_id"]: check for check in raw["menu_checks"]}
    shops = []
    for shop in raw["shops"]:
        shop_id = str(shop["id"])
        check = checks[shop_id]
        shops.append(compact_shop({
            "id": shop_id, "name": shop.get("shopName"),
            "canteen": shop.get("canteen"), "platform_status": shop.get("openStatus"),
            "menu_status": check["status"], "returned_item_count": check["item_count"],
            "items": rows_by_shop[shop_id],
        }))
    coverage = dict(raw["coverage"])
    coverage["platform_scope_scan_complete"] = (
        coverage["shop_scope_source"] == "live_school_groups"
        and coverage["listing_exhausted"]
        and not coverage.get("group_shop_ids_not_returned")
        and coverage["menus_failed"] == 0
        and coverage["menus_unscanned"] == 0
        and not raw["errors"]
    )
    requests = raw["requests"]
    return {
        "schema_version": 1, "command": "catalog", "source": raw["source"],
        "queried_at_beijing": raw["queried_at_beijing"], "school_id": DINING["SCHOOL_ID"],
        "filters": raw["filters"], "semantic_filter_applied": False,
        "price_scope": args.price_scope, "money_unit": "fen",
        "item_columns": ITEM_COLUMNS, "shops": shops, "coverage": coverage,
        "errors": raw["errors"], "warnings": raw["warnings"],
        "description_limit_chars": 500,
        "model_task": "Classify all returned items, then rank eligible items. Keep this scan for the active query. A later follow-up requires a freshness check; new queries fetch again.",
        "retrieved_urls": list(dict.fromkeys(
            "https://m.kplocker.com" + event["path"] for event in requests
            if event.get("http_status") == 200 and event.get("data_present")
        )),
        "failed_urls": list(dict.fromkeys(
            "https://m.kplocker.com" + event["path"] for event in requests
            if not event.get("will_retry") and (
                event.get("status") == "fail" or event.get("data_usable") is False
                or event.get("http_status") != 200
            )
        )),
        "retry_summary": {
            "request_attempts": len(requests),
            "retry_attempts": sum(event.get("attempt", 1) > 1 for event in requests),
            "failed_attempts": sum(event.get("status") == "fail"
                                   or event.get("data_usable") is False
                                   or event.get("http_status") != 200 for event in requests),
        },
    }


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def output_bytes(value):
    return len((encoded(value) + "\n").encode("utf-8"))


def snapshot_freshness(catalog, now=None):
    now = now if now is not None else datetime.datetime.now(DINING["BEIJING"])
    state = {
        "checked_at_beijing": now.isoformat(),
        "reuse_max_age_seconds": SNAPSHOT_REUSE_SECONDS,
        "age_seconds": None, "expires_at": None, "reuse_allowed": False,
        "state": "unknown_timestamp",
    }
    try:
        collected = datetime.datetime.fromisoformat(catalog["queried_at_beijing"])
        if collected.utcoffset() is None:
            return state
        age = (now - collected).total_seconds()
        expires = (collected + datetime.timedelta(seconds=SNAPSHOT_REUSE_SECONDS)).isoformat()
        crossed_date = collected.astimezone(DINING["BEIJING"]).date() != now.astimezone(DINING["BEIJING"]).date()
    except (KeyError, TypeError, ValueError, OverflowError):
        return state
    state.update(
        age_seconds=round(age, 3),
        expires_at=expires,
        reuse_allowed=0 <= age < SNAPSHOT_REUSE_SECONDS and not crossed_date,
        state=("clock_skew" if age < 0 else "crossed_date" if crossed_date
               else "fresh" if age < SNAPSHOT_REUSE_SECONDS else "expired"),
    )
    return state


def check_snapshot(catalog, snapshot_id):
    freshness = snapshot_freshness(catalog)
    return {
        "status": "pass" if freshness["reuse_allowed"] else "refresh_required",
        "command": "catalog-freshness", "snapshot_id": snapshot_id,
        "queried_at_beijing": catalog.get("queried_at_beijing"),
        "freshness": freshness, "network_used": False,
        "note": "Only continuous follow-ups may reuse a fresh snapshot. New queries and current-state checks fetch again. Paging an active query remains pinned to its original scan.",
    }


def catalog_records(catalog):
    yield ["catalog", {key: value for key, value in catalog.items() if key != "shops"}]
    reference = 0
    for shop_index, shop in enumerate(catalog["shops"]):
        yield ["shop", shop_index, {key: value for key, value in shop.items() if key != "items"}]
        for row in shop["items"]:
            reference += 1
            yield ["item", reference, shop_index, row]


def delivery_units(catalog):
    # Oversized records are split without deleting fields or cutting Unicode characters.
    units = []
    for record_index, record in enumerate(catalog_records(catalog)):
        text = encoded(record)
        if len(text.encode("utf-8")) <= 6000:
            units.append(record)
        else:
            fragments = [text[start:start + 256] for start in range(0, len(text), 256)]
            units.extend(["fragment", record_index, index, len(fragments), fragment]
                         for index, fragment in enumerate(fragments))
    return units


def snapshot_page(catalog, snapshot_id, path, cursor, maximum):
    units = delivery_units(catalog)
    if not 0 <= cursor <= len(units):
        raise ValueError("Cursor outside this snapshot.")
    page = {
        "status": "pass", "command": "catalog-page", "snapshot_id": snapshot_id,
        "snapshot_file": str(path), "cursor": cursor, "next_cursor": None,
        "unit_count": len(units), "item_count": sum(len(shop["items"]) for shop in catalog["shops"]),
        "queried_at_beijing": catalog["queried_at_beijing"],
        "freshness": snapshot_freshness(catalog),
        "network_used": False, "semantic_filter_applied": False,
        "records": [],
    }
    end = cursor
    for unit in units[cursor:]:
        candidate = {**page, "records": [*page["records"], unit],
                     "next_cursor": end + 1 if end + 1 < len(units) else None}
        if output_bytes(candidate) > maximum:
            break
        page = candidate
        end += 1
    if end == cursor and cursor < len(units):
        raise ValueError("Output budget cannot hold one delivery unit.")
    page["next_cursor"] = end if end < len(units) else None
    page["end_cursor"] = end
    # Reserve end_cursor in the fitting check too.
    while output_bytes(page) > maximum and page["records"]:
        page["records"].pop()
        end -= 1
        page.update(end_cursor=end, next_cursor=end)
    if end == cursor and cursor < len(units):
        raise ValueError("Output budget cannot hold one delivery unit.")
    return page


def scan_snapshot(args):
    try:
        directory = Path(args.state_dir).resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("State directory does not exist. Create the permitted task directory "
                         "first, then pass it with --state-dir.") from error
    if not directory.is_dir():
        raise ValueError("State directory must be an existing permitted task directory.")
    snapshot_id = uuid.uuid4().hex
    path = directory / ("campus-catalog-" + snapshot_id + ".json")
    assert path.resolve().parent == directory
    started = time.perf_counter()
    # Check the chosen directory before making network requests; do not try other directories.
    with path.open("x", encoding="utf-8", newline="\n") as target:
        catalog = execute(args, DINING["PublicApi"]())
        catalog["collection_elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        content = encoded(catalog)
        target.write(encoded({
            "snapshot_schema": 1, "snapshot_id": snapshot_id,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "catalog": catalog,
        }))
    return snapshot_page(catalog, snapshot_id, path, 0, args.max_output_bytes)


def read_snapshot(path, expected_id):
    if not expected_id:
        raise ValueError("Use the snapshot ID returned by this scan.")
    source = Path(path).resolve(strict=True)
    if source.stat().st_size > 64000000:
        raise ValueError("Snapshot exceeds the supported size.")
    saved = json.loads(source.read_text(encoding="utf-8-sig"))
    if (saved.get("snapshot_schema") != 1 or saved.get("snapshot_id") != expected_id
            or source.name != "campus-catalog-" + expected_id + ".json"):
        raise ValueError("Snapshot identity mismatch.")
    catalog = saved["catalog"]
    actual = hashlib.sha256(encoded(catalog).encode("utf-8")).hexdigest()
    if actual != saved.get("sha256"):
        raise ValueError("Snapshot content changed; do not mix pages.")
    return catalog, source


def select_references(value, total):
    if value == "none":
        return set()
    if not value:
        raise ValueError("Supply the model-selected item references; no automatic category filter.")
    selected = set()
    for part in value.split(","):
        bounds = part.strip().split("-")
        if len(bounds) not in (1, 2) or not all(bound.isdigit() for bound in bounds):
            raise ValueError("Expected item references such as 1,3,7-12.")
        first, last = int(bounds[0]), int(bounds[-1])
        if not 1 <= first <= last <= total:
            raise ValueError("Item reference outside this snapshot.")
        selected.update(range(first, last + 1))
    return selected


def rank_snapshot(catalog, args):
    total_units = len(delivery_units(catalog))
    if args.reviewed_through != total_units:
        raise ValueError("Finish reviewing every page before ranking model-selected items.")
    entries = []
    for record in catalog_records(catalog):
        if record[0] != "item":
            continue
        _, reference, shop_index, row = record
        shop = catalog["shops"][shop_index]
        item = {**shop["item_defaults"], **dict(zip(shop["row_columns"], row))}
        entries.append((reference, shop, item))
    selected = select_references(args.select, len(entries))
    key = "dish_fen" if args.price_scope == "dish" else "with_box_fen"
    ranked, excluded = [], defaultdict(int)
    for reference, shop, item in entries:
        if reference not in selected:
            continue
        value = item.get(key)
        ignored = {"budget_unknown_or_exceeded"}
        if args.intent == "items":
            ignored.add("possible_addon_not_a_meal")
        reasons = [reason for reason in item.get("constraint_failures", []) if reason not in ignored]
        if type(value) is not int or value < 0:
            reasons.append("unknown_price")
        budget = catalog.get("filters", {}).get("budget_fen")
        if budget is not None and (type(value) is not int or value > budget):
            reasons.append("budget_unknown_or_exceeded")
        if (args.now or catalog.get("filters", {}).get("now")) \
                and item.get("current_conditions_supported") is not True:
            reasons.append("current_order_conditions_not_confirmed")
        if reasons:
            for reason in set(reasons):
                excluded[reason] += 1
            continue
        ranked.append({
            "ref": reference, "shop": shop["name"], "canteen": shop["canteen"],
            "platform_status": shop["platform_status"],
            "meal_component_flagged": "possible_addon_not_a_meal" in item.get("constraint_failures", []),
            **{field: item.get(field) for field in (
                "name", "dish_fen", "with_box_fen", "box_fen", "selected_options",
                "current_conditions_supported", "blocking_reasons", "unknowns",
            )},
        })
    ranked.sort(key=lambda row: ((-row[key] if args.order == "desc" else row[key]), row["ref"]))
    best = ranked[0][key] if ranked else None
    winners = [row["ref"] for row in ranked if row[key] == best]
    # Compact tied references while preserving every winner, even beyond the display limit.
    ranges = []
    for reference in sorted(winners):
        if ranges and reference == ranges[-1][1] + 1:
            ranges[-1][1] = reference
        else:
            ranges.append([reference, reference])
    return {
        "status": "pass", "command": "catalog-rank", "snapshot_id": args.snapshot_id,
        "queried_at_beijing": catalog["queried_at_beijing"], "price_scope": args.price_scope,
        "freshness": snapshot_freshness(catalog),
        "order": args.order, "network_used": False,
        "intent": args.intent, "meal_suitability_requires_model_review": True,
        "model_declared_reviewed_through": args.reviewed_through,
        "semantic_selection_source": "model_supplied_references_not_independently_verified",
        "selected_count": len(selected), "ranked_count": len(ranked),
        "excluded_count": len(selected) - len(ranked), "excluded_reason_counts": dict(excluded),
        "best_price_fen": best, "winner_count": len(winners), "winner_ref_ranges": ranges,
        "results": ranked[:args.limit], "results_truncated": len(ranked) > args.limit,
        "coverage": catalog["coverage"], "retrieved_urls": catalog["retrieved_urls"],
        "failed_urls": catalog["failed_urls"], "retry_summary": catalog["retry_summary"],
        "availability_note": "Platform state at collection time; no checkout or ordering was verified.",
    }


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.read_snapshot or args.rank_snapshot or args.check_snapshot:
            catalog, path = read_snapshot(
                args.read_snapshot or args.rank_snapshot or args.check_snapshot, args.snapshot_id,
            )
            result = (check_snapshot(catalog, args.snapshot_id) if args.check_snapshot else
                      rank_snapshot(catalog, args) if args.rank_snapshot else
                      snapshot_page(catalog, args.snapshot_id, path,
                                    args.cursor, args.max_output_bytes))
        elif args.state_dir:
            result = scan_snapshot(args)
            result["network_used"] = True
        elif args.full:
            result = execute(args, DINING["PublicApi"]())
        else:
            result = {
                "status": "action_required", "command": "catalog", "network_used": False,
                "reason": "Choose an already permitted task directory with --state-dir before scanning.",
                "note": "Full stdout is diagnostic only (--full). Do not read tool-output to recover a normal query.",
            }
    except Exception as error:
        result = {
            "status": "failed", "command": "catalog", "error_type": type(error).__name__,
            "error": str(error)[:200] if isinstance(error, (OSError, ValueError)) else "Catalog operation failed.",
            "note": "Do not retry successful menus or switch directories after a permission denial.",
        }
    if not args.full and output_bytes(result) > args.max_output_bytes:
        result = {
            "status": "output_too_large", "command": "catalog", "snapshot_id": args.snapshot_id,
            "reason": "Use smaller pages or a lower --limit; the complete snapshot was not deleted.",
        }
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    print(encoded(result))
    return result


if __name__ == "__main__":
    main()
