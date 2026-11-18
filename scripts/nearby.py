"""Public search around the fixed school location, without API keys or login."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import sys
import urllib.parse


SPEC = importlib.util.spec_from_file_location(
    "nearby_public_fetch", Path(__file__).with_name("official_fetch.py"))
FETCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FETCH)
SCHOOL = "深圳信息职业技术大学"
ADDRESS = "深圳市龙岗区龙翔大道2188号"
ALIASES = (SCHOOL, "深圳信息职业技术学院")
MAP_HOSTS = {"www.amap.com", "ditu.amap.com", "map.baidu.com"}
HOSTS = MAP_HOSTS


def public_link(url):
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 443)):
            return False
        host = parts.hostname.lower().rstrip(".")
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return not any(ord(c) < 32 for c in url)
        return False
    except (ValueError, TypeError):
        return False


def in_scope(url):
    if not public_link(url):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.fragment:
        return False
    if parts.hostname == "map.baidu.com" and parts.path == "/":
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if set(query) == {"qt", "uid"}:
            return (query["qt"] == ["inf"] and len(query["uid"]) == 1
                    and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", query["uid"][0]) is not None)
        return (set(query) == {"qt", "c", "wd", "rn"}
                and all(len(v) == 1 for v in query.values())
                and query["qt"] == ["s"] and query["c"] == ["340"]
                and query["rn"] == ["10"] and bool(query["wd"][0]))
    return (parts.hostname in MAP_HOSTS and parts.path.startswith("/place/")
            and not parts.query)


def detail_url(url):
    if not in_scope(url):
        return False
    parts = urllib.parse.urlsplit(url)
    return (parts.path.startswith("/place/") or
            (parts.hostname == "map.baidu.com" and parts.path == "/"
             and urllib.parse.parse_qs(parts.query).get("qt") == ["inf"]))


def text(value, limit=300):
    return " ".join(value.split())[:limit] if isinstance(value, str) else None


def count(value):
    return value if type(value) is int and value >= 0 else None


def listing(row, source):
    """Only copy business fields; never serialize service auth/debug/widget payloads."""
    if not isinstance(row, dict):
        return None
    name, address = text(row.get("name")), text(row.get("addr"), 500)
    if not name or not address:
        return None
    uid = row.get("uid")
    uid = uid if isinstance(uid, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", uid) else None
    admin = row.get("admin_info")
    admin = admin if isinstance(admin, dict) else {}
    business = row.get("business_time")
    business = business if isinstance(business, dict) else {}
    times = business.get("data")
    hours = []
    for entry in times[:3] if isinstance(times, list) else []:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("business_time_text")
        if isinstance(fields, dict):
            for key in ("common", "festival"):
                value = text(fields.get(key), 120)
                if value:
                    hours.append({"kind": key, "text": value})
    return {
        "name": name, "address": address, "uid": uid,
        "area": text(row.get("area_name")), "city": text(admin.get("city_name")),
        "district": text(admin.get("area_name")),
        "listed_hours": hours, "hours_updated_unix": count(business.get("update_time")),
        "open_now_verified": False, "price": None, "rating": None,
        "proximity_status": "requires_address_review",
        "evidence_level": "public_map_listing", "source_urls": [source],
        "map_link": ("https://map.baidu.com/?" + urllib.parse.urlencode({"qt": "inf", "uid": uid}))
                    if uid else None,
        "map_link_fetched": False,
    }


def query_text(value):
    value = value.strip()
    if not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise ValueError("Use a nonempty query of at most 160 characters without controls.")
    return value


def search(client, query):
    url = "https://map.baidu.com/?" + urllib.parse.urlencode(
        {"qt": "s", "c": 340, "wd": query, "rn": 10})
    event, body, headers = client.get(url)
    if body is None:
        return {"query": query, "url": url, "status": "failed", "items": []}
    try:
        data = json.loads(FETCH.decode(body, headers))
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ValueError("Unexpected public map response.")
        metadata = data["result"]
        if metadata.get("error", 0) not in (0, "0", None):
            raise ValueError("Public map service reported an error.")
        content = data.get("content")
        total = count(metadata.get("total"))
        if content is None and total == 0:
            content = []
        if not isinstance(content, list):
            raise ValueError("No readable public map listing.")
        items = [item for row in content[:10] if (item := listing(row, url))]
        if content and not items:
            raise ValueError("Response contained no usable name/address pairs.")
        if not content and total != 0:
            raise ValueError("Nonempty total without readable listings.")
    except (ValueError, UnicodeError):
        return {"query": query, "url": url, "status": "parse_failed", "items": [],
                "error": "Unreadable or unsupported response; no raw server payload retained."}
    return {"query": query, "url": url, "status": "parsed" if items else "empty",
            "items": items, "reported_total": total, "rows_received": len(content),
            "rows_omitted": max(0, len(content) - len(items)),
            "coverage": "first_page_only", "complete_area_scan": False}


def read_detail(client, url):
    if urllib.parse.urlsplit(url).path.startswith("/place/"):
        return FETCH.fetch_page(client, url, 5000)
    event, body, headers = client.get(url)
    if body is None:
        return {"url": url, "status": "fail", "http_status": event.get("http_status")}
    try:
        data = json.loads(FETCH.decode(body, headers))
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise ValueError("Unsupported map detail response.")
        if data["result"].get("error", 0) not in (0, "0", None):
            raise ValueError("Map detail service reported an error.")
        content = data.get("content")
        rows = [content] if isinstance(content, dict) else content
        if not isinstance(rows, list):
            raise ValueError("No readable business detail.")
        expected_uid = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["uid"][0]
        items = [item for row in rows if isinstance(row, dict)
                 and row.get("uid") == expected_uid and (item := listing(row, url))]
        if len(items) != 1:
            raise ValueError("Expected exactly one matching business UID and name/address.")
        items[0]["map_link_fetched"] = True
        return {"url": url, "status": "pass", "items": items,
                "evidence_level": "public_map_listing", "open_now_verified": False}
    except (ValueError, UnicodeError):
        return {"url": url, "status": "parse_failed",
                "error": "Unreadable or mismatched map detail; no raw payload retained."}


def run(args, client=None):
    keyword = query_text(args.query)
    client = client or FETCH.PublicClient(HOSTS, url_guard=in_scope, timeout=8, max_attempts=3)
    if args.read_url:
        urls = list(dict.fromkeys(args.read_url))
        if len(urls) > 4 or any(not detail_url(u) for u in urls):
            raise ValueError("Read at most four discovered HTTPS /place/ or Baidu qt=inf&uid details.")
        searches, results = [], []
    else:
        queries = [f"{SCHOOL} 附近 {keyword}", f"{ALIASES[1]} {keyword}"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            searches = list(pool.map(lambda q: search(client, q), queries))
        indexed = {}
        for response in searches:
            for item in response["items"]:
                key = (item["uid"], item["name"], item["address"]) if item["uid"] else (
                    None, item["name"], item["address"])
                if key not in indexed:
                    indexed[key] = dict(item, source_urls=[])
                indexed[key]["source_urls"].append(response["url"])
        results = list(indexed.values())
        urls = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(lambda u: read_detail(client, u), urls))
    requests = list(client.requests)
    retrieved = list(dict.fromkeys(r["url"] for r in requests if r["status"] == "pass"))
    failed = list(dict.fromkeys(
        [r["url"] for r in requests if r["status"] != "pass" and not r["will_retry"]]
        + [s["url"] for s in searches if s["status"] == "parse_failed"]
        + [p["url"] for p in pages if p["status"] != "pass"]))
    status = ("evidence_for_review" if results or any(p["status"] == "pass" for p in pages)
              else "no_results" if searches and all(s["status"] == "empty" for s in searches)
              else "search_unavailable_or_unreadable")
    return {
        "status": status, "partial": bool(failed),
        "school": SCHOOL, "address": ADDRESS, "location_basis": "fixed_school_name_and_address",
        "query": keyword,
        "searches": [{k: v for k, v in s.items() if k != "items"}
                     | {"usable_items": len(s["items"])} for s in searches],
        "results": results, "detail_pages": pages,
        "map_search_link": "https://uri.amap.com/search?" + urllib.parse.urlencode(
            {"keyword": f"{SCHOOL} {keyword}", "city": "深圳", "src": "campus-life", "callnative": 0}),
        "map_search_link_fetched": False,
        "notice": "Map listings provide published names/addresses, not verified proximity, access or current "
                  "opening status. LLM must filter branches and distinguish campus/other campuses/off-campus. "
                  "Only first pages were read; prices, suitability and routes need separate evidence. "
                  "Generated map links were not fetched.",
        "retrieved_at_beijing": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
        "retrieved_urls": retrieved, "failed_urls": failed,
        "retry_count": sum(r["attempt"] > 1 for r in requests),
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--query", required=True, help="Category and explicit constraints; school location is fixed.")
    result.add_argument("--read-url", action="append",
                        help="Discovered /place/ URL or returned Baidu qt=inf&uid link; at most four.")
    return result


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] in ("evidence_for_review", "no_results") else 2
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error),
                          "retrieved_urls": [], "failed_urls": []}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
