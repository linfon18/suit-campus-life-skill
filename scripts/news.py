"""Read dated notices and news from the public university website."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ORIGIN = "https://www.suit-sz.edu.cn"
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
SOURCES = {
    "notices": {"label": "通知公告", "path": "/index/tzgg.htm",
                "next_prefix": "/index/tzgg/"},
    "news": {"label": "深信要闻", "path": "/index/sxyw.htm",
             "next_prefix": "/index/sxyw/"},
    "updates": {"label": "深信大资讯", "path": "/sxdzx.htm",
                "next_prefix": "/sxdzx/"},
}
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK_TAGS = set("p div li tr h1 h2 h3 h4 h5 h6 blockquote br".split())
MAX_BYTES = 2 * 1024 * 1024


def compact(value):
    return " ".join(value.split())


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def find_all(self, predicate):
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    yield child
                yield from child.find_all(predicate)

    def first(self, predicate):
        return next(self.find_all(predicate), None)

    def text(self, blocks=False):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text(blocks))
                if blocks and child.tag in BLOCK_TAGS:
                    parts.append("\n")
                elif blocks and child.tag in {"td", "th"}:
                    parts.append(" | ")
        return "".join(parts)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parsed_date(value):
    match = re.search(r"(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?", value)
    if match:
        try:
            return datetime.date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    return None


def public_link(base, href):
    url = urllib.parse.urljoin(base, href or "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def allowed_url(url):
    parts = urllib.parse.urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.suit-sz.edu.cn"
        and not parts.query and not parts.fragment
        and bool(re.fullmatch(
            r"/(?:index/(?:tzgg|sxyw)(?:/\d+)?|sxdzx(?:/\d+)?|info/(?:1036|1038)/\d+)\.htm",
            parts.path,
        ))
    )


def parse_listing(html, page_url, category):
    root = Document(html).root
    container = root.first(lambda n: n.tag == "ul" and (
        n.has_class("list-text") or n.has_class("list-pics")
    ))
    if container is None:
        raise ValueError("listing_template_not_recognized")
    records, issues = [], []
    for item in container.find_all(lambda n: n.tag == "li"):
        anchors = list(item.find_all(lambda n: n.tag == "a" and n.attrs.get("href")))
        anchor = next((n for n in anchors if n.has_class("tit") or n.attrs.get("title")), None)
        if anchor is None:
            anchor = next((n for n in anchors if "/info/" in (
                public_link(page_url, n.attrs["href"]) or ""
            )), None)
        if anchor is None:
            issues.append("unrecognized_listing_item")
            continue
        url = public_link(page_url, anchor.attrs["href"])
        title = compact(anchor.attrs.get("title") or anchor.text())
        if not url or not title:
            issues.append("invalid_listing_link_or_title")
            continue
        month = item.first(lambda n: n.has_class("mon"))
        day = item.first(lambda n: n.has_class("day"))
        if month is not None and day is not None:
            date = parsed_date(compact(month.text()) + "-" + compact(day.text()))
        else:
            date_node = item.first(lambda n: n.has_class("date"))
            date = parsed_date(date_node.text()) if date_node else None
        intro = item.first(lambda n: n.has_class("abst"))
        records.append({
            "title": title, "url": url, "published_date": date,
            "list_excerpt": compact(intro.text()) if intro else "",
            "categories": [category], "listing_pages": [page_url],
            "detail_status": "not_requested",
        })
    if not records:
        raise ValueError("listing_contains_no_recognized_articles")
    next_node = root.first(lambda n: n.has_class("p_next"))
    next_anchor = next_node.first(lambda n: n.tag == "a") if next_node else None
    next_url = public_link(page_url, next_anchor.attrs.get("href")) if next_anchor else None
    if next_url and not valid_next(next_url, page_url, category):
        issues.append("unsupported_next_page_link")
        next_url = None
    return records, next_url, issues


def valid_next(url, page_url, category):
    parts = urllib.parse.urlsplit(url)
    return (allowed_url(url) and parts.netloc == urllib.parse.urlsplit(page_url).netloc
            and parts.path.startswith(SOURCES[category]["next_prefix"]))


def parse_article(html, url):
    root = Document(html).root
    article = root.first(lambda n: n.has_class("article-con"))
    if article is None:
        raise ValueError("article_template_not_recognized")
    title_node = article.first(lambda n: n.tag == "h1")
    info = article.first(lambda n: n.has_class("info"))
    body = article.first(lambda n: n.has_class("v_news_content"))
    if body is None:
        body = article.first(lambda n: n.attrs.get("id", "").startswith("vsb_content"))
    if body is None:
        raise ValueError("article_body_not_recognized")
    lines = [compact(line) for line in body.text(blocks=True).splitlines() if compact(line)]
    text = "\n".join(lines)
    images = list(body.find_all(lambda n: n.tag == "img"))
    links = []
    for anchor in body.find_all(lambda n: n.tag == "a" and n.attrs.get("href")):
        target = public_link(url, anchor.attrs["href"])
        if target and target not in {link["url"] for link in links}:
            links.append({"label": compact(anchor.text()), "url": target})
    # Some VSBSites put downloadable attachments beside, rather than inside, the body.
    for anchor in article.find_all(lambda n: n.tag == "a" and n.attrs.get("href")):
        href = anchor.attrs["href"]
        if "download" in href.lower() or re.search(r"\.(?:pdf|docx?|xlsx?|zip)(?:\?|$)", href, re.I):
            target = public_link(url, href)
            if target and target not in {link["url"] for link in links}:
                links.append({"label": compact(anchor.text()), "url": target})
    tables = []
    for table in body.find_all(lambda n: n.tag == "table"):
        rows = []
        for row in table.find_all(lambda n: n.tag == "tr"):
            rows.append([compact(cell.text()) for cell in row.find_all(
                lambda n: n.tag in {"td", "th"}
            )][:20])
        tables.append({"rows": rows[:60], "rows_truncated": len(rows) > 60})
    return {
        "detail_title": compact(title_node.text()) if title_node else None,
        "detail_published_date": parsed_date(info.text()) if info else None,
        "body_text": text[:12000], "body_truncated": len(text) > 12000,
        "text_status": ("image_only" if not text and images else
                        "text_with_images" if text and images else
                        "text" if text else "empty"),
        "body_excerpt": text[:180], "links": links[:30],
        "links_truncated": len(links) > 30,
        "images": [{"url": public_link(url, n.attrs.get("src")),
                    "alt": n.attrs.get("alt", "")} for n in images[:20]],
        "images_truncated": len(images) > 20,
        "tables": tables[:10], "tables_truncated": len(tables) > 10,
    }


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class Client:
    def __init__(self):
        self.requests = []
        self.local = threading.local()

    def thread_opener(self):
        if not hasattr(self.local, "opener"):
            self.local.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirects(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
        return self.local.opener

    def get(self, url):
        if not allowed_url(url):
            raise ValueError("unsupported_article_or_listing_url")
        for attempt in range(1, 4):
            try:
                return self.get_once(url, attempt)
            except urllib.error.HTTPError as error:
                if error.code not in (408, 425, 429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 3:
                    raise
            time.sleep(attempt)

    def get_once(self, url, attempt):
        request = urllib.request.Request(url, headers={
            "User-Agent": "CampusLifeNews/1.0", "Accept": "text/html",
            "Accept-Encoding": "identity",
        })
        headers = {name.lower() for name, _ in request.header_items()}
        event = {"url": url, "method": "GET", "http_status": None, "attempt": attempt,
                 "header_names": sorted(headers),
                 "cookie_header_present": "cookie" in headers,
                 "authorization_header_present": "authorization" in headers}
        self.requests.append(event)
        try:
            with self.thread_opener().open(request, timeout=12) as response:
                event["http_status"] = response.status
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("response_too_large")
                if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError("response_is_not_html")
                charset = response.headers.get_content_charset()
                if not charset:
                    meta = re.search(br'charset\s*=\s*["\']?([\w-]+)', raw[:4096], re.I)
                    charset = meta.group(1).decode("ascii") if meta else "utf-8"
                text = raw.decode(charset)
                event["response_bytes"] = len(raw)
                event["status"] = "pass"
                return text
        except Exception as error:
            event.update(status="fail", error_type=type(error).__name__)
            if isinstance(error, urllib.error.HTTPError):
                event["http_status"] = error.code
            raise


def date_value(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD") from error


def integer_range(low, high):
    def value(text):
        number = int(text)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Expected {low}..{high}")
        return number
    return value


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--category", choices=("all", "academic", *SOURCES), default="all",
                      help="academic reads main-site notices for the model to review.")
    period = root.add_mutually_exclusive_group()
    period.add_argument("--days", type=integer_range(1, 366))
    period.add_argument("--since", type=date_value)
    period.add_argument("--latest", type=integer_range(1, 40))
    root.add_argument("--until", type=date_value)
    root.add_argument("--keyword", default="", help="Literal title/list-excerpt filter, not full-text search")
    root.add_argument("--limit", type=integer_range(1, 40), default=12)
    root.add_argument("--max-pages", type=integer_range(1, 10), default=5)
    root.add_argument("--workers", type=integer_range(1, 8), default=4)
    root.add_argument("--output-dir")
    return root


def execute(args, client, now=None):
    now = now or datetime.datetime.now(BEIJING)
    today = now.astimezone(BEIJING).date()
    until = args.until or today
    since = None if args.latest else (args.since or until - datetime.timedelta(days=(args.days or 7) - 1))
    if until > today or (since is not None and since > until):
        raise ValueError("invalid_or_future_date_range")
    limit = args.latest or args.limit
    result = {
        "source": "live_public_university_website", "queried_at_beijing": now.isoformat(),
        "school": "深圳信息职业技术大学",
        "query": {"mode": "latest" if args.latest else "date_range",
                  "since": since.isoformat() if since else None,
                  "until": until.isoformat(), "keyword": args.keyword, "limit": limit,
                  "requested_category": args.category},
        "sources": {}, "items": [], "errors": [], "requests": client.requests,
        "scope": "Three public university columns: notices/news/updates. Academic queries use main-site notices for model review; internal department notices are not covered.",
        "academic_relevance_requires_review": args.category == "academic",
        "full_school_coverage_guaranteed": False,
    }
    candidates = {}
    categories = (list(SOURCES) if args.category == "all" else
                  ["notices"] if args.category == "academic"
                  else [args.category])
    for category in categories:
        source = SOURCES[category]
        coverage = {
            "label": source["label"], "url": source.get("origin", ORIGIN) + source["path"],
            "pages_fetched": 0, "listing_records": 0, "unknown_date_records": 0,
            "future_date_records": 0, "issues": [], "latest_observed_date": None,
            "stop_reason": "page_limit", "matched_count": 0,
        }
        result["sources"][category] = coverage
        url = coverage["url"]
        visited, seen_records, source_matches = set(), set(), set()
        for _ in range(args.max_pages):
            if url in visited:
                coverage["stop_reason"] = "pagination_cycle"
                break
            visited.add(url)
            try:
                records, next_url, issues = parse_listing(client.get(url), url, category)
            except Exception as error:
                coverage["stop_reason"] = "listing_failed"
                result["errors"].append({"stage": "listing", "category": category,
                                         "url": url, "error": type(error).__name__})
                break
            coverage["pages_fetched"] += 1
            coverage["issues"].extend(issues)
            coverage["listing_records"] += len(records)
            new_records = {row["url"] for row in records} - seen_records
            seen_records.update(row["url"] for row in records)
            for row in records:
                date = row["published_date"]
                if date is None:
                    coverage["unknown_date_records"] += 1
                    continue
                if date > today.isoformat():
                    coverage["future_date_records"] += 1
                    continue
                previous = coverage["latest_observed_date"]
                coverage["latest_observed_date"] = max(previous or date, date)
                if date > until.isoformat() or (since and date < since.isoformat()):
                    continue
                if args.keyword and args.keyword.casefold() not in (
                    row["title"] + " " + row["list_excerpt"]
                ).casefold():
                    continue
                source_matches.add(row["url"])
                if row["url"] in candidates:
                    existing = candidates[row["url"]]
                    if category not in existing["categories"]:
                        existing["categories"].append(category)
                    if url not in existing["listing_pages"]:
                        existing["listing_pages"].append(url)
                else:
                    candidates[row["url"]] = row
            if not new_records:
                coverage["stop_reason"] = "repeated_listing"
                break
            if args.latest and len(source_matches) >= limit:
                coverage["stop_reason"] = "latest_count_reached"
                break
            known_dates = [row["published_date"] for row in records if row["published_date"]]
            # A pinned old article must not stop scanning a page containing newer articles.
            if since and len(known_dates) == len(records) and max(known_dates) < since.isoformat():
                coverage["stop_reason"] = "page_entirely_before_window"
                break
            if not next_url:
                coverage["stop_reason"] = "pagination_gap" if issues else "end_of_listing"
                break
            url = next_url
        coverage["matched_count"] = len(source_matches)
        latest = coverage["latest_observed_date"]
        coverage["days_since_latest_observed"] = (
            (today - datetime.date.fromisoformat(latest)).days if latest else None)
        coverage["window_state"] = (
            "listing_unavailable" if coverage["pages_fetched"] == 0 else
            "no_recent_publications_observed" if since and latest
            and latest < since.isoformat() and not source_matches else
            "matches_found" if source_matches else "no_matches_in_scanned_pages")
    ordered = sorted(candidates.values(), key=lambda row: (row["published_date"], row["url"]), reverse=True)
    result["coverage"] = {
        "unique_matching_articles": len(ordered), "selected_articles": min(limit, len(ordered)),
        "articles_omitted_by_limit": max(0, len(ordered) - limit),
        "detail_success": 0, "detail_failed": 0, "detail_not_fetched": 0,
        "articles_excluded_after_detail": 0,
    }
    def fetch_detail(row):
        if not allowed_url(row["url"]):
            return row, None, None
        try:
            return row, parse_article(client.get(row["url"]), row["url"]), None
        except Exception as error:
            return row, None, type(error).__name__

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        details = list(pool.map(fetch_detail, ordered[:limit]))
    result["coverage"]["detail_workers"] = args.workers
    for row, detail, error_type in details:
        if not allowed_url(row["url"]):
            row["detail_status"] = "external_link_not_fetched"
            result["coverage"]["detail_not_fetched"] += 1
        else:
            if error_type is None:
                row.update(detail)
                row["detail_status"] = "pass"
                result["coverage"]["detail_success"] += 1
                detail_date = row["detail_published_date"]
                if detail_date and detail_date != row["published_date"]:
                    row["list_published_date"] = row["published_date"]
                    row["published_date"] = detail_date
                    row["date_discrepancy"] = True
                    if detail_date > until.isoformat() or (since and detail_date < since.isoformat()):
                        result["coverage"]["articles_excluded_after_detail"] += 1
                        continue
            else:
                row["detail_status"] = "failed"
                result["coverage"]["detail_failed"] += 1
                result["errors"].append({"stage": "detail", "url": row["url"],
                                         "error": error_type})
        result["items"].append(row)
    result["items"].sort(key=lambda row: (row["published_date"], row["url"]), reverse=True)
    gaps = {"page_limit", "listing_failed", "repeated_listing", "pagination_cycle", "pagination_gap"}
    incomplete = any(source["stop_reason"] in gaps or source["issues"]
                     or source["unknown_date_records"] for source in result["sources"].values())
    all_failed = all(source["stop_reason"] == "listing_failed" for source in result["sources"].values())
    result["status"] = "fail" if all_failed else "partial" if (
        incomplete or result["errors"] or result["coverage"]["detail_not_fetched"]
    ) else "pass"
    result["coverage"]["returned_articles"] = len(result["items"])
    result["retrieved_urls"] = list(dict.fromkeys(
        event["url"] for event in client.requests
        if event.get("status") == "pass" and event.get("http_status") == 200
    ))
    result["failed_urls"] = [
        {"url": event["url"], "http_status": event.get("http_status"),
         "error_type": event.get("error_type")}
        for event in client.requests if event.get("status") == "fail"
    ]
    return result


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    destination = Path(args.output_dir).resolve() if args.output_dir else None
    if destination is not None and not destination.is_dir():
        root.error("--output-dir must be an existing writable directory")
    try:
        result = execute(args, Client())
    except ValueError as error:
        root.error(str(error))
    if destination:
        filename = "campus-news-" + uuid.uuid4().hex + ".json"
        result["artifact"] = {"filename": filename, "status": "pass"}
        with (destination / filename).open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
