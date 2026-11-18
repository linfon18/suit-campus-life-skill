"""Live, anonymous school-menu lookup and explicit-constraint filtering."""

import argparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
from itertools import count
from pathlib import Path
import runpy
import threading
import time
import uuid


SCHOOL_ID = 185
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
LIST_PATH = "/mm/wx/api/shop/gets"
MENU_PATH = "/mm/wx/api/shop/all/gets"
QUEUE_PATH = "/mm/wx/api/shop/order/accepted/blocked"
GROUP_PATH = "/mm/wx/api/shop/categories"
SHOP_FIELDS = (
    "id", "shopName", "placeName", "address", "status", "openStatus",
    "bizZoneId", "bizZoneName", "saleTimeRange", "mealTime",
    "shipFee", "userShipFee", "minToShip", "allowPrebook",
    "takeAway", "takeOut", "eatIn", "eatInTake", "mustCategory",
)
ITEM_FIELDS = (
    "id", "shopId", "itemName", "originPrice", "discountPrice", "boxFee",
    "catId", "stockAmount", "saleOnTime", "saleOnBook", "saleTimeRange",
    "shipDays", "singlePurchaseForbidden", "isFood", "noBuy",
    "minOrderQuantity", "maxOrderQuantity",
)


def pick(value, fields):
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in fields if name in value}


def valid_id(value):
    return (type(value) is int and value > 0) or (
        isinstance(value, str) and value.isascii() and value.isdecimal()
        and int(value) > 0
    )


def fen(value):
    return value if type(value) is int and value >= 0 else None


def yuan(value):
    return None if value is None else "{:.2f}".format(Decimal(value) / 100)


def budget_value(text):
    try:
        amount = Decimal(text)
        scaled = amount * 100
        if not amount.is_finite() or amount < 0 or scaled != scaled.to_integral_value():
            raise ValueError()
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise argparse.ArgumentTypeError("Budget must be nonnegative yuan, at most 2 decimals")


def integer_range(minimum, maximum):
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError("Expected an integer")
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError("Value outside allowed range")
        return value
    return parse


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def description(value):
    if not isinstance(value, str):
        return ""
    parser = TextOnly()
    parser.feed(value[:6000])
    return " ".join(" ".join(parser.parts).split())[:500]


def infer_canteen(shop):
    text = " ".join(str(shop.get(key) or "") for key in ("shopName", "placeName", "address"))
    for label in ("A", "B", "C"):
        if label + "食堂" in text:
            return label
    for label in ("紫荆", "石榴"):
        if label in text:
            return label
    return None


def within_hours(value, now):
    if not isinstance(value, str) or not value.strip():
        return None
    minute_now = now.hour * 60 + now.minute
    matches = []
    try:
        for segment in value.split(";"):
            start, end = segment.strip().split("-")
            endpoints = []
            for index, text in enumerate((start, end)):
                hours, minutes = (int(part) for part in text.strip().split(":"))
                if not (0 <= minutes <= 59 and 0 <= hours <= 24):
                    return None
                if hours == 24 and (minutes != 0 or index == 0):
                    return None
                endpoints.append(hours * 60 + minutes)
            begin, finish = endpoints
            if begin == finish:
                return None
            matches.append(
                begin <= minute_now < finish if begin < finish
                else minute_now >= begin or minute_now < finish
            )
    except (ValueError, TypeError):
        return None
    return any(matches)


def select_options(item, requested):
    groups = item.get("itemAttrs", [])
    if not isinstance(groups, list):
        return [], None, ["malformed_option_groups"]
    selected, errors, used = [], [], set()
    total = 0
    for group in groups:
        if not isinstance(group, dict):
            errors.append("malformed_option_group")
            continue
        options = group.get("specs")
        if not isinstance(options, list) or not options or not all(
            isinstance(option, dict) and isinstance(option.get("label"), str)
            for option in options
        ):
            errors.append("malformed_option_values")
            continue
        matches = [option for option in options if option["label"] in requested]
        if len(matches) > 1:
            errors.append("conflicting_options:" + str(group.get("name", "")))
            continue
        if matches:
            chosen, source = matches[0], "user"
            used.add(chosen["label"])
        else:
            defaults = [option for option in options if option.get("default") is True]
            if defaults:
                chosen, source = defaults[-1], "platform_default"
            else:
                priced = [option for option in options if fen(option.get("money", 0)) is not None]
                if not priced:
                    errors.append("unknown_option_price")
                    continue
                chosen = min(priced, key=lambda option: option.get("money", 0))
                source = "lowest_price_proposal"
        money = fen(chosen.get("money", 0))
        selected.append({
            "group": group.get("name"),
            "label": chosen["label"],
            "money_fen": money,
            "selection_source": source,
        })
        if money is None:
            errors.append("unknown_option_price")
        else:
            total += money
    errors.extend("required_option_missing:" + value for value in sorted(set(requested) - used))
    return selected, None if errors else total, errors


def price_item(item, requested):
    selected, extra, errors = select_options(item, requested)
    base = fen(item.get("discountPrice"))
    basis = "discountPrice"
    if base is None:
        base, basis = fen(item.get("originPrice")), "originPrice"
    if base is None:
        errors.append("unknown_base_price")
    if base == 0 and not item.get("itemAttrs"):
        errors.append("zero_base_without_verified_options")
    dish = base + extra if base is not None and extra is not None and not errors else None
    box = fen(item.get("boxFee"))
    return {
        "base_field": basis,
        "base_fen": base,
        "selected_options": selected,
        "dish_fen": dish,
        "dish_yuan": yuan(dish),
        "box_fen": box,
        "box_yuan": yuan(box),
        "with_box_estimate_fen": dish + box if dish is not None and box is not None else None,
        "with_box_estimate_yuan": yuan(dish + box) if dish is not None and box is not None else None,
        "errors": errors,
        "not_a_checkout_quote": True,
        "delivery_included": False,
    }


def availability(shop, item, now):
    shop_hours = within_hours(shop.get("saleTimeRange"), now)
    item_hours = within_hours(item.get("saleTimeRange"), now)
    unknown, blocked = [], []
    state = shop.get("openStatus")
    if state in ("Pause", "Prebook"):
        blocked.append("shop_" + state.lower())
    elif state != "Open":
        unknown.append("current_shop_state_unknown")
    for name, value in (("shop_hours", shop_hours), ("item_hours", item_hours)):
        if value is False:
            blocked.append(name + "_closed")
        elif value is None:
            unknown.append(name + "_unknown")
    if item.get("saleOnTime") is False:
        blocked.append("item_not_for_current_day")
    elif item.get("saleOnTime") is not True:
        unknown.append("current_day_sale_unknown")
    if item.get("shipDays") != "0123456":
        unknown.append("weekday_rule_unverified")
    if item.get("noBuy") is True or fen(item.get("stockAmount")) == 0:
        blocked.append("explicit_unavailable_or_zero_stock")
    if item.get("singlePurchaseForbidden") is True:
        blocked.append("single_purchase_forbidden")
    return {
        "platform_status": state,
        "shop_hours_now": shop_hours,
        "item_hours_now": item_hours,
        "current_order_conditions_supported": not blocked and not unknown,
        "blocking_reasons": blocked,
        "unknowns": unknown,
        "instant_pickup_guaranteed": False,
    }


def normalize_options(item):
    groups = item.get("itemAttrs")
    if not isinstance(groups, list):
        return []
    return [{
        "name": group.get("name"),
        "spec": group.get("spec"),
        "options": [pick(option, ("label", "money", "originPrice", "default"))
                    for option in group.get("specs", []) if isinstance(option, dict)],
    } for group in groups if isinstance(group, dict) and isinstance(group.get("specs"), list)]


class PublicApi:
    def __init__(self):
        # Load the bundled anonymous transport without adding any import search path.
        library = runpy.run_path(str(Path(__file__).resolve().with_name("probe.py")),
                                 run_name="campus_life_transport")
        self.audit = library["AuditedHTTPS"]()
        self.library = library
        self.local = threading.local()
        self.request_once = library["request_once"]
        self.requests = []

    def thread_opener(self):
        if not hasattr(self.local, "opener"):
            library = self.library
            audit = library["AuditedHTTPS"]()
            audit.requests = self.audit.requests
            self.local.opener = library["urllib"].request.build_opener(
                library["urllib"].request.ProxyHandler({}), library["NoRedirects"](), audit
            )
        return self.local.opener

    def query(self, path, body):
        for attempt in range(1, 4):
            event, data = self.request_once(self.thread_opener(), "POST", path, body)
            usable = event.get("status") == "pass"
            if usable and path in (GROUP_PATH, LIST_PATH):
                usable = isinstance(data, list)
            elif usable and path == MENU_PATH:
                usable = isinstance(data, dict) and isinstance(data.get("shop"), dict) \
                    and isinstance(data.get("items"), list)
            code = event.get("http_status")
            retryable = (code is None or code in (408, 425, 429, 500, 502, 503, 504)
                         or code == 200 and not usable)
            retry = not usable and retryable and attempt < 3
            event.update(request_scope=pick(body, ("bizZoneId", "startRow", "id")),
                         attempt=attempt, data_usable=usable, will_retry=retry)
            self.requests.append(event)
            if not retry:
                return event, data if usable else None
            time.sleep(attempt)


def read_groups(api, canteen, include_unlisted=False):
    if include_unlisted:
        return {"source": "unlisted_requested", "shop_ids": None,
                "canteen_by_id": {}, "groups": []}, []
    _, groups = api.query(GROUP_PATH, {
        "bizZoneId": str(SCHOOL_ID),
        "couponUseChannel": "synjonesApp", "cateringChannel": "SynjonesApp",
    })
    valid, errors = [], []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or str(group.get("bizZoneId")) != str(SCHOOL_ID):
                continue
            if not isinstance(group.get("shops"), str) or not isinstance(group.get("name"), str):
                errors.append({"stage": "groups", "reason": "Malformed school group"})
                continue
            parts = [part.strip() for part in group["shops"].split(",") if part.strip()]
            if not all(valid_id(part) for part in parts):
                errors.append({"stage": "groups", "reason": "Invalid group shop IDs"})
                continue
            valid.append({
                "name": group["name"], "canteen": infer_canteen({"placeName": group["name"]}),
                "bizZoneName": group.get("bizZoneName"), "shop_ids": parts,
            })
    if not valid:
        return {"source": "group_catalog_unavailable", "shop_ids": None,
                "canteen_by_id": {}, "groups": []}, [{
                    "stage": "groups", "reason": "Group catalog unavailable; listing is unfiltered"
                }]
    selected = [group for group in valid if not canteen or group["canteen"] == canteen]
    ids, locations = [], {}
    for group in selected:
        for shop_id in group["shop_ids"]:
            if shop_id not in locations:
                ids.append(shop_id)
                locations[shop_id] = group["canteen"]
    return {"source": "live_school_groups", "shop_ids": ids,
            "canteen_by_id": locations, "groups": valid}, errors


def read_shops(api, max_pages, scope=None):
    found, errors, seen_pages, seen_ids = {}, [], set(), set()
    pages_fetched = 0
    complete, stop_reason = False, "page_limit"
    scope = scope or {"source": "unfiltered", "shop_ids": None, "canteen_by_id": {}}
    allowed = set(scope["shop_ids"]) if scope["shop_ids"] is not None else None
    if allowed == set():
        return [], {"listing_exhausted": True, "listing_stop_reason": "no_matching_group",
                    "unique_shops_returned": 0, "school_wide_completeness_guaranteed": False}, []
    for page in count():
        if max_pages is not None and page >= max_pages:
            break
        body = {
            "statuses": ["Open", "Pause"],
            "startRow": page * 9, "limitRows": 9,
            "bizZoneId": SCHOOL_ID, "sortField": "sort desc", "diningTypes": [],
            "couponUseChannel": "synjonesApp", "cateringChannel": "SynjonesApp",
        }
        if allowed is not None:
            body["shops"] = ",".join(scope["shop_ids"])
        event, rows = api.query(LIST_PATH, body)
        pages_fetched += 1
        if not isinstance(rows, list):
            errors.append({"stage": "shop_list", "reason": "No array data",
                           "http_status": event.get("http_status")})
            stop_reason = "list_request_failed"
            break
        page_ids = tuple(str(row.get("id")) for row in rows if isinstance(row, dict))
        if rows and (page_ids in seen_pages or not set(page_ids) - seen_ids):
            stop_reason = "repeated_page"
            errors.append({"stage": "shop_list", "reason": "Repeated page"})
            break
        seen_pages.add(page_ids)
        seen_ids.update(page_ids)
        for shop in rows:
            if not isinstance(shop, dict) or not valid_id(shop.get("id")):
                errors.append({"stage": "shop_list", "reason": "Invalid shop record"})
                continue
            if shop.get("bizZoneId") not in (None, SCHOOL_ID):
                errors.append({"stage": "shop_list", "reason": "Different school"})
                continue
            identity = str(shop["id"])
            if allowed is not None and identity not in allowed:
                errors.append({"stage": "shop_list", "reason": "Shop outside live group scope"})
                continue
            found[identity] = dict(
                pick(shop, SHOP_FIELDS),
                canteen=scope["canteen_by_id"].get(identity) or infer_canteen(shop),
            )
        if len(rows) < 9:
            complete, stop_reason = True, "short_page"
            break
        if allowed is not None and allowed <= set(found):
            complete, stop_reason = True, "all_group_shop_ids_returned"
            break
    return list(found.values()), {
        "listing_exhausted": complete,
        "listing_stop_reason": stop_reason,
        "listing_pages_fetched": pages_fetched,
        "unique_shops_returned": len(found),
        "base_status_filter": ["Open", "Pause"],
        "group_shop_ids_not_returned": sorted(allowed - set(found)) if allowed is not None else None,
        "school_wide_completeness_guaranteed": False,
    }, errors


def choose_shops(shops, args):
    filtered = [shop for shop in shops if
                (not args.canteen or shop["canteen"] == args.canteen)
                and ((args.shop == str(shop["id"]))
                     if args.shop.isascii() and args.shop.isdecimal()
                     else (not args.shop or args.shop.casefold() in str(shop.get("shopName", "")).casefold()))
                and str(shop["id"]) not in args.exclude_shop]
    if args.command == "recommend":
        allowed_states = ("Open",) if args.now else ("Open", "Prebook")
        filtered = [shop for shop in filtered if shop.get("openStatus") in allowed_states]
    order = {"Open": 0, "Prebook": 1, "Pause": 2}
    interleaved = []
    for priority in range(4):
        buckets = defaultdict(deque)
        for shop in filtered:
            if order.get(shop.get("openStatus"), 3) == priority:
                buckets[shop["canteen"] or "unknown"].append(shop)
        while any(buckets.values()):
            for bucket in buckets.values():
                if bucket:
                    interleaved.append(bucket.popleft())
    return interleaved


def addon_flag(item, category):
    name = str(item.get("itemName") or "").strip()
    return name in (
        "米饭", "白米饭", "餐盒", "打包盒", "餐具", "加饭", "加蛋", "加面",
        "蟹柳棒", "西红柿", "青菜", "卤蛋",
    ) or any(
        term in category for term in ("加料", "配菜", "餐具", "小料", "加菜")
    )


def item_row(shop, item, categories, args, now):
    category = item.get("category")
    if not isinstance(category, str):
        category = categories.get(str(item.get("catId")), "")
    public = pick(item, ITEM_FIELDS)
    public.update(
        itemDesc=description(item.get("itemDesc")),
        category=category,
        option_groups=normalize_options(item),
    )
    pricing = price_item(item, args.option)
    state = availability(shop, item, now)
    reasons = list(pricing["errors"])
    if item.get("singlePurchaseForbidden") is True:
        reasons.append("single_purchase_forbidden")
    if shop.get("openStatus") == "Prebook" and item.get("saleOnBook") is not True:
        reasons.append("prebook_item_not_confirmed")
    if item.get("isFood") is False:
        reasons.append("explicit_non_food")
    if item.get("noBuy") is True or fen(item.get("stockAmount")) == 0:
        reasons.append("explicit_unavailable_or_zero_stock")
    if fen(item.get("minOrderQuantity")) is not None and item["minOrderQuantity"] > 1:
        reasons.append("minimum_quantity_exceeds_one")
    if addon_flag(item, category):
        reasons.append("possible_addon_not_a_meal")
    if args.now and not state["current_order_conditions_supported"]:
        reasons.append("current_order_conditions_not_confirmed")
    value = pricing["dish_fen"] if args.budget_scope == "dish" else pricing["with_box_estimate_fen"]
    if args.budget is not None and (value is None or value > args.budget):
        reasons.append("budget_unknown_or_exceeded")
    return {
        "shop_id": str(shop["id"]), "shop_name": shop.get("shopName"),
        "canteen": shop.get("canteen"), "item": public,
        "pricing": pricing, "availability": state,
        "mechanical_constraints_pass": not reasons,
        "constraint_failures": sorted(set(reasons)),
        "meal_suitability_requires_review": True,
    }


def load_menus(api, shops, args, result, now):
    selected = shops[:args.max_menus]
    workers = min(getattr(args, "workers", 1), max(1, len(selected)))

    def fetch(listing):
        shop_id = str(listing["id"])
        try:
            return api.query(MENU_PATH, {
                "status": "Open", "id": shop_id,
                "couponUseChannel": "synjonesApp", "cateringChannel": "SynjonesApp",
            })
        except Exception as error:
            return {"http_status": None, "error_type": type(error).__name__}, None

    def collect(responses):
        for listing, (event, data) in zip(selected, responses):
            yield listing, event, data

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    responses = pool.map(fetch, selected) if pool else map(fetch, selected)
    try:
        consume_menus(collect(responses), args, result, now, len(shops))
    finally:
        if pool:
            pool.shutdown(wait=True)
    result["coverage"]["menu_workers"] = workers


def consume_menus(responses, args, result, now, shop_count):
    rows, matched_count, seen_items = [], 0, set()
    success, attempted, item_count, consecutive_failures = 0, 0, 0, 0
    menu_checks = []
    failures = defaultdict(int)
    for listing, event, data in responses:
        attempted += 1
        shop_id = str(listing["id"])
        detail = data.get("shop") if isinstance(data, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        scope_ok = isinstance(detail, dict) and type(detail.get("bizZoneId")) is int \
            and detail["bizZoneId"] == SCHOOL_ID
        if not scope_ok or not isinstance(items, list):
            consecutive_failures += 1
            menu_checks.append({"shop_id": shop_id, "shop_name": listing.get("shopName"),
                                "canteen": listing.get("canteen"), "status": "failed",
                                "item_count": None})
            result["errors"].append({
                "stage": "menu", "shop_id": shop_id,
                "reason": "School unconfirmed" if not scope_ok else "No item array",
                "http_status": event.get("http_status"),
            })
            if consecutive_failures >= 3 and not getattr(args, "scan_all_menus", False):
                break
            continue
        success += 1
        consecutive_failures = 0
        menu_checks.append({"shop_id": shop_id, "shop_name": listing.get("shopName"),
                            "canteen": listing.get("canteen"), "status": "success",
                            "item_count": len(items)})
        shop = dict(listing, **pick(detail, tuple(key for key in SHOP_FIELDS
                                                 if key not in ("id", "openStatus"))))
        shop["canteen"] = listing.get("canteen") or infer_canteen(shop)
        category_rows = data.get("categories") if isinstance(data.get("categories"), list) else []
        categories = {str(cat.get("id")): cat.get("categoryName", "")
                      for cat in category_rows if isinstance(cat, dict)
                      and isinstance(cat.get("categoryName"), str)}
        for item in items:
            if not isinstance(item, dict) or not valid_id(item.get("id")):
                result["errors"].append({"stage": "item", "shop_id": shop_id,
                                         "reason": "Invalid item record"})
                continue
            if "shopId" in item and str(item["shopId"]) != shop_id:
                result["errors"].append({"stage": "item", "reason": "Different shop",
                                         "shop_id": shop_id})
                continue
            identity = (shop_id, str(item["id"]))
            if identity in seen_items:
                continue
            seen_items.add(identity)
            item_count += 1
            searchable = str(item.get("itemName") or "") + " " + description(item.get("itemDesc"))
            if args.dish and args.dish.casefold() not in searchable.casefold():
                continue
            row = item_row(shop, item, categories, args, now)
            if args.command == "recommend" and not row["mechanical_constraints_pass"]:
                for reason in row["constraint_failures"]:
                    failures[reason] += 1
                continue
            matched_count += 1
            rows.append(row)
    if args.command == "recommend":
        metric = "dish_fen" if args.budget_scope == "dish" else "with_box_estimate_fen"
        rows.sort(key=lambda row: (
            {"Open": 0, "Prebook": 1, "Pause": 2}.get(
                row["availability"]["platform_status"], 3),
            row["pricing"][metric] if row["pricing"][metric] is not None else 10**12,
        ))
    result["items"] = rows[:args.limit]
    result["menu_checks"] = menu_checks
    result["coverage"].update(
        menus_attempted=attempted, menus_success=success,
        menus_empty=sum(check["status"] == "success" and check["item_count"] == 0
                        for check in menu_checks),
        menus_unscanned=max(0, shop_count - attempted),
        menus_failed=attempted - success,
        items_seen=item_count, items_matching=matched_count,
        items_returned=len(result["items"]),
        items_truncated=args.limit is not None and matched_count > args.limit,
        excluded_reason_counts=dict(failures),
    )


def execute(args, api):
    now = datetime.datetime.now(BEIJING)
    result = {
        "schema_version": 1, "run_id": uuid.uuid4().hex,
        "source": "live_anonymous_api", "queried_at_beijing": now.isoformat(),
        "school_id": SCHOOL_ID, "command": args.command,
        "filters": {
            "canteen": args.canteen, "shop": args.shop, "dish": args.dish,
            "budget_fen": args.budget, "budget_scope": args.budget_scope,
            "options": args.option, "now": args.now, "exclude_shop": args.exclude_shop,
        },
        "coverage": {}, "shops": [], "items": [], "queues": [], "errors": [],
        "requests": api.requests,
        "warnings": [
            "Budget checks cover only the named price scope, not checkout or delivery.",
            "Menu presence does not guarantee stock, immediate pickup, or order success.",
            "Allergen, complete ingredient and nutrition data are not verified.",
        ],
    }
    scope, scope_errors = read_groups(api, args.canteen, args.include_unlisted)
    result["coverage"].update(
        shop_scope_source=scope["source"],
        scope_shop_count=len(scope["shop_ids"]) if scope["shop_ids"] is not None else None,
        school_groups=[{"name": group["name"], "shop_count": len(group["shop_ids"])}
                       for group in scope["groups"]],
    )
    result["errors"].extend(scope_errors)
    shops, coverage, errors = read_shops(api, args.max_pages, scope)
    result["coverage"].update(coverage)
    result["errors"].extend(errors)
    chosen = choose_shops(shops, args)
    result["coverage"].update(matching_shops=len(chosen),
                              shop_status_counts={
                                  state: sum(shop.get("status") == state for shop in shops)
                                  for state in ("Open", "Prebook", "Pause")
                              } | {"unknown": sum(shop.get("status") not in (
                                  "Open", "Prebook", "Pause") for shop in shops)},
                              shop_open_status_counts={
                                  state: sum(shop.get("openStatus") == state for shop in shops)
                                  for state in ("Open", "Prebook", "Pause")
                              } | {"unknown": sum(shop.get("openStatus") not in (
                                  "Open", "Prebook", "Pause") for shop in shops)},
                              status_counts_scope="all_returned_shops_before_user_filters",
                              shops_returned=len(chosen) if args.limit is None else min(len(chosen), args.limit),
                              shops_truncated=args.limit is not None and len(chosen) > args.limit)
    result["shops"] = chosen[:args.limit]
    result["shop_selection"] = {
        "query": args.shop, "match_mode": "exact_id_or_name_contains",
        "matched_count": len(chosen), "selected_ids": [str(shop["id"]) for shop in chosen],
        "state": "matched" if chosen else "no_matching_shop",
    }
    if args.shop and not chosen:
        result["warnings"].append(
            "No returned shop matched this ID/name and the other filters; no arbitrary ID was queried.")
    if args.command in ("menu", "recommend"):
        load_menus(api, chosen, args, result, now)
    elif args.command == "queue":
        for shop in chosen[:2]:
            event, data = api.query(QUEUE_PATH, {
                "id": str(shop["id"]), "couponUseChannel": "synjonesApp",
                "cateringChannel": "SynjonesApp",
            })
            values = pick(data, ("blockedOrders", "waitTime"))
            known = {key: type(values.get(key)) in (int, float)
                     for key in ("blockedOrders", "waitTime")}
            result["queues"].append({
                "shop_id": str(shop["id"]), "shop_name": shop.get("shopName"),
                "status": "known" if all(known.values()) else "unavailable",
                "field_has_number": known, "values": values,
                "http_status": event.get("http_status"),
                "business_code": event.get("business_code"),
                "data_present": event.get("data_present"),
                "scope": "Other ordering channels are unverified.",
            })
        result["coverage"]["queues_attempted"] = len(result["queues"])
    result["status"] = ("partial" if result["errors"] else
                        "no_matching_shop" if not chosen else
                        "no_recommendations" if args.command == "recommend" and not result["items"]
                        else "pass")
    return result


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("command", choices=("shops", "menu", "recommend", "queue"))
    root.add_argument("--canteen", choices=("A", "B", "C", "紫荆", "石榴"))
    root.add_argument("--shop", default="", type=str.strip,
                      help="Exact ID from the returned shop list, or a shop-name keyword.")
    root.add_argument("--dish", default="")
    root.add_argument("--budget", type=budget_value)
    root.add_argument("--budget-scope", choices=("dish", "with-box"), default="with-box")
    root.add_argument("--option", action="append", default=[])
    root.add_argument("--now", action="store_true")
    root.add_argument("--exclude-shop", action="append", default=[])
    root.add_argument("--max-pages", type=integer_range(1, 12), default=6)
    root.add_argument("--include-unlisted", action="store_true")
    root.add_argument("--max-menus", type=integer_range(1, 48), default=6)
    root.add_argument("--limit", type=integer_range(1, 300), default=20)
    root.add_argument("--output-dir")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.now and args.command != "recommend":
        parser().error("--now is supported only with recommend")
    try:
        result = execute(args, PublicApi())
    except Exception as error:
        result = {
            "source": "live_query_failed",
            "queried_at_beijing": datetime.datetime.now(BEIJING).isoformat(),
            "error_type": type(error).__name__,
            "data_available": False,
        }
    if args.output_dir:
        filename = "campus-dining-" + result.get("run_id", uuid.uuid4().hex) + ".json"
        result["artifact"] = {"status": "pass", "filename": filename}
        try:
            text = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False)
            with (Path(args.output_dir) / filename).open("x", encoding="utf-8") as stream:
                stream.write(text + "\n")
        except Exception as error:
            result["artifact"] = {"status": "fail", "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return result


if __name__ == "__main__":
    main()
