"""Read bounded anonymous official pages; never infer policy from HTTP success."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT / "references/official-sources.json").read_text("utf-8"))
BEIJING = dt.timezone(dt.timedelta(hours=8))
MAX_BYTES = 1000000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class PublicClient:
    def __init__(self, hosts, url_guard=None, timeout=20, max_attempts=3):
        self.hosts, self.url_guard, self.timeout = set(hosts), url_guard, timeout
        if not 1 <= max_attempts <= 3:
            raise ValueError("Allow one to three attempts.")
        self.max_attempts = max_attempts
        self.requests = []
        self.lock = threading.Lock()

    def valid(self, url):
        try:
            value = urllib.parse.urlsplit(url)
            return (value.scheme == "https" and value.hostname in self.hosts
                    and not value.username and not value.password
                    and value.port in (None, 443)
                    and not value.fragment
                    and (self.url_guard is None or self.url_guard(url)))
        except (ValueError, TypeError):
            return False

    def get_once(self, url, accept):
        request = urllib.request.Request(url, headers={
            "Accept": accept, "User-Agent": "exam-error-coach/2.0",
        }, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=self.timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Response exceeds the one-megabyte read limit.")
            return response.status, body, response.headers

    def get(self, url, accept="text/html,application/xhtml+xml,text/plain"):
        if not self.valid(url):
            return {"status": "blocked", "url": url, "error": "URL outside the configured read scope"}, None, None
        for attempt in range(1, self.max_attempts + 1):
            event = {
                "url": url, "method": "GET", "attempt": attempt,
                "cookie_header_present": False, "authorization_header_present": False,
            }
            retry = False
            try:
                status, body, headers = self.get_once(url, accept)
                event["http_status"] = status
                if status != 200:
                    retry = status in (408, 429) or 500 <= status <= 599
                    event.update(status="fail", error="Unexpected HTTP status")
                elif not body:
                    retry = True
                    event.update(status="fail", error="Empty response body")
                else:
                    event.update(status="pass", response_bytes=len(body))
            except urllib.error.HTTPError as error:
                event.update(status="fail", http_status=error.code, error_type="HTTPError")
                retry = error.code in (408, 429) or 500 <= error.code <= 599
                error.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                event.update(status="fail", http_status=None, error_type=type(error).__name__)
                retry = True
            except ValueError as error:
                event.update(status="fail", http_status=None, error=str(error))
            event["will_retry"] = retry and attempt < self.max_attempts
            with self.lock:
                self.requests.append(event)
            if event["status"] == "pass":
                return event, body, headers
            if not event["will_retry"]:
                return event, None, None
            time.sleep(attempt)
        raise AssertionError("Unreachable retry state")


class PageText(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links = base, [], []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "a" and not self.skip:
            values = dict(attrs)
            if values.get("href"):
                self.links.append(urllib.parse.urljoin(self.base, values["href"]))
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3") and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def decode(body, headers):
    encoding = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    if not encoding:
        found = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", body[:8000])
        encoding = found.group(1).decode("ascii") if found else "utf-8"
    for candidate in dict.fromkeys((encoding, "utf-8", "gb18030")):
        try:
            return body.decode(candidate)
        except (UnicodeError, LookupError):
            continue
    raise ValueError("Page encoding could not be decoded without replacement.")


def fetch_page(client, url, max_chars):
    event, body, headers = client.get(url)
    if body is None:
        return {"url": url, "status": "fail", "error": event.get("error", event.get("error_type")),
                "http_status": event.get("http_status"), "error_type": event.get("error_type")}
    try:
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise ValueError("Non-text resource needs a dedicated environment reader.")
        page = PageText(url)
        page.feed(decode(body, headers))
        text = "\n".join(" ".join(line.split()) for line in "".join(page.parts).splitlines() if line.strip())
        if not text:
            raise ValueError("No readable text was extracted.")
        links = [link for link in dict.fromkeys(page.links) if client.valid(link)]
        return {
            "url": url, "status": "pass", "body_text": text[:max_chars],
            "text_truncated": len(text) > max_chars, "available_links": links[:80],
            "links_truncated": len(links) > 80, "policy_applicability_verified": False,
        }
    except ValueError as error:
        return {"url": url, "status": "parse_failed", "error": str(error),
                "http_status": 200, "policy_applicability_verified": False}


def allows_entry_fallback(page):
    code = page.get("http_status")
    return (
        page["status"] == "parse_failed"
        or code in (404, 410, 408, 429)
        or (isinstance(code, int) and 500 <= code <= 599)
        or (code is None and page.get("error_type") in (
            "URLError", "TimeoutError", "ConnectionError",
        ))
        or page.get("error") == "Empty response body"
    )


def run(args, client=None):
    client = client or PublicClient(REGISTRY["allowed_hosts"], timeout=args.timeout)
    entry = REGISTRY["exams"][args.exam]
    urls = list(dict.fromkeys(args.url or entry["urls"]))
    if len(urls) > 8:
        raise ValueError("At most eight explicit pages per call; do not crawl whole sites.")
    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(urls)))) as pool:
        pages = list(pool.map(lambda url: fetch_page(client, url, args.max_chars), urls))
    fallback_pages = []
    if not args.url and pages and all(allows_entry_fallback(page) for page in pages):
        for url in entry.get("fallback_urls", []):
            if url not in urls:
                fallback_pages.append(fetch_page(client, url, args.max_chars))
            if fallback_pages and fallback_pages[-1]["status"] == "pass":
                break
    fallback_recovered = any(page["status"] == "pass" for page in fallback_pages)
    status = ("pass" if all(page["status"] == "pass" for page in pages)
              else "fallback_success" if fallback_recovered else "partial_or_failed")
    pages.extend(fallback_pages)
    return {
        "status": status, "preferred_urls": urls,
        "fallback_used": bool(fallback_pages), "fallback_recovered": fallback_recovered,
        "queried_at_beijing": dt.datetime.now(BEIJING).isoformat(),
        "pages": pages, "requests": client.requests,
        "retrieved_urls": list(dict.fromkeys(
            event["url"] for event in client.requests if event.get("http_status") == 200
        )),
        "failed_urls": [page["url"] for page in pages if page["status"] != "pass"],
        "note": "Links listed in a page were discovered, not fetched. The model must read applicable notices.",
    }


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--exam", choices=tuple(REGISTRY["exams"]), required=True)
    root.add_argument("--url", action="append")
    root.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    root.add_argument("--max-chars", type=int, default=12000)
    root.add_argument("--timeout", type=int, choices=range(5, 61), default=20,
                      help="Per-request timeout in seconds; at most three attempts.")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 100 <= args.max_chars <= 30000:
            raise ValueError("Text limit must be between 100 and 30000 characters.")
        result = run(args)
    except ValueError as error:
        result = {"status": "error", "error": str(error), "retrieved_urls": []}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return result


if __name__ == "__main__":
    main()
