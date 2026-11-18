"""Bounded, anonymous campus API diagnostics; Python standard library only."""

import argparse
import datetime
import gzip
import hashlib
import io
import json
import platform
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ORIGIN = "https://m.kplocker.com"
LIST_PATH = "/mm/wx/api/shop/gets"
MENU_PATH = "/mm/wx/api/shop/all/gets"
QUEUE_PATH = "/mm/wx/api/shop/order/accepted/blocked"
PAGE_PATH = "/mm/index.html"
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 12
TARGETS = {
    ("GET", ORIGIN + PAGE_PATH),
    ("POST", ORIGIN + LIST_PATH),
    ("POST", ORIGIN + MENU_PATH),
    ("POST", ORIGIN + QUEUE_PATH),
    ("POST", ORIGIN + "/mm/wx/api/shop/categories"),
}
SHOP_FIELDS = (
    "id", "shopName", "status", "openStatus", "mealTime",
    "bizZoneId", "bizZoneName", "placeName",
)
ITEM_FIELDS = (
    "id", "shopId", "itemName", "originPrice", "discountPrice", "boxFee",
)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pick_fields(value, names):
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in names if name in value}


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def first_shop_id(shops):
    if isinstance(shops, list):
        for shop in shops:
            value = shop.get("id") if isinstance(shop, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return str(value)
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                return value
    return None


def queue_summary(data):
    values = pick_fields(data, ("blockedOrders", "waitTime"))
    known = {key: is_number(values.get(key)) for key in ("blockedOrders", "waitTime")}
    return {
        "status": "pass" if all(known.values()) else "unavailable",
        "field_has_number": known,
        "values": values,
        "scope": "Platform queue only; other ordering channels are unverified.",
    }


def error_summary(error):
    underlying = getattr(error, "reason", error)
    if isinstance(underlying, ssl.SSLCertVerificationError):
        category = "tls_certificate"
    elif isinstance(underlying, ssl.SSLError):
        category = "tls"
    elif isinstance(underlying, socket.gaierror):
        category = "dns"
    elif isinstance(underlying, (TimeoutError, socket.timeout)):
        category = "timeout"
    elif isinstance(underlying, ConnectionRefusedError):
        category = "connection_refused"
    elif isinstance(underlying, json.JSONDecodeError):
        category = "invalid_json"
    elif isinstance(underlying, UnicodeError):
        category = "text_decode"
    elif isinstance(underlying, PermissionError):
        category = "permission"
    else:
        category = "request_or_processing_error"
    return {
        "category": category,
        "type": type(error).__name__,
        "underlying_type": type(underlying).__name__,
    }


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class AuditedHTTPS(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())
        self.requests = []

    def https_open(self, request):
        if (request.get_method(), request.full_url) not in TARGETS:
            raise ValueError("Request is outside the fixed diagnostic target list")
        headers = {name.lower() for name, _ in request.header_items()}
        allowed = {"accept", "content-type", "content-length", "host", "user-agent",
                   "connection"}
        if headers - allowed:
            raise ValueError("Unexpected request header")
        body = json.loads(request.data) if request.data is not None else None
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "header_names": sorted(headers),
            "body": body,
            "cookie_header_present": "cookie" in headers,
            "authorization_header_present": "authorization" in headers,
        })
        return super().https_open(request)


def bounded_response(response):
    raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Response exceeds byte limit")
    if "gzip" in response.headers.get("Content-Encoding", "").lower():
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Decoded response exceeds byte limit")
    return raw


def request_once(opener, method, path, body=None):
    started = time.monotonic()
    event = {
        "method": method,
        "path": path,
        "observed_at_utc": utc_now(),
        "http_status": None,
        "business_code": None,
        "data_present": None,
        "data_type": None,
        "response_set_cookie_present": None,
        "status": "fail",
    }
    payload = None
    headers = {"Accept": "application/json", "User-Agent": "CampusEnvCheck/1.0"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        ORIGIN + path, data=payload, headers=headers, method=method
    )
    data = None
    try:
        try:
            response = opener.open(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            event["http_status"] = response.status
            event["response_set_cookie_present"] = bool(
                response.headers.get("Set-Cookie")
            )
            raw = bounded_response(response)
        event["response_bytes"] = len(raw)
        if not 200 <= event["http_status"] < 300:
            event["error"] = {"category": "http_status"}
        elif method == "GET":
            event["status"] = "pass"
        else:
            decoded = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(decoded, dict):
                raise ValueError("JSON response is not an object")
            code = decoded.get("code")
            event.update(
                business_code=code,
                data_present="data" in decoded,
                data_type=type(decoded.get("data")).__name__,
            )
            if type(code) is int and code == 0:
                event["status"] = "pass"
                data = decoded.get("data")
            else:
                event["error"] = {"category": "business_code"}
    except Exception as error:
        event["error"] = error_summary(error)
    event["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return event, data


def filesystem_probe(output_dir, run_id):
    result = {"status": "fail", "temporary_file_cleaned": False}
    target = None
    created = False
    try:
        if not output_dir.is_dir():
            return {"status": "fail", "reason": "Output directory does not exist"}
        target = output_dir.resolve() / ("campus-check-" + run_id + ".tmp")
        with target.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(run_id)
        if target.read_text(encoding="utf-8") != run_id:
            raise ValueError("File roundtrip mismatch")
        result["status"] = "pass"
    except Exception as error:
        result["error"] = error_summary(error)
    finally:
        if created:
            try:
                target.unlink()
                result["temporary_file_cleaned"] = True
            except Exception as error:
                result["status"] = "fail"
                result["cleanup_error"] = error_summary(error)
    return result


def run_network(report, opener):
    steps = report["checks"]
    event, _ = request_once(opener, "GET", PAGE_PATH)
    steps["https_get"] = event
    body = {
        "statuses": ["Open", "Pause"],
        "startRow": 0,
        "limitRows": 3,
        "bizZoneId": 185,
        "sortField": "sort desc",
        "diningTypes": [],
        "couponUseChannel": "synjonesApp",
        "cateringChannel": "SynjonesApp",
    }
    event, shops = request_once(opener, "POST", LIST_PATH, body)
    steps["shop_list"] = event
    event["data_status"] = "pass" if isinstance(shops, list) else "unavailable"
    event["shop_count"] = len(shops) if isinstance(shops, list) else None
    event["shops"] = [pick_fields(shop, SHOP_FIELDS) for shop in shops[:3]] \
        if isinstance(shops, list) else []
    shop_id = first_shop_id(shops)
    if shop_id is None:
        for name in ("menu", "school_scope", "queue"):
            steps[name] = {"status": "skipped", "reason": "No returned valid shop ID"}
        return
    report["selected_shop_id"] = shop_id
    detail_body = {
        "status": "Open",
        "id": shop_id,
        "couponUseChannel": "synjonesApp",
        "cateringChannel": "SynjonesApp",
    }
    event, menu = request_once(opener, "POST", MENU_PATH, detail_body)
    steps["menu"] = event
    shop = menu.get("shop") if isinstance(menu, dict) else None
    items = menu.get("items") if isinstance(menu, dict) else None
    event["data_status"] = "pass" if isinstance(items, list) else "unavailable"
    event["shop"] = pick_fields(shop, SHOP_FIELDS)
    event["item_count"] = len(items) if isinstance(items, list) else None
    event["sample_items"] = [pick_fields(item, ITEM_FIELDS) for item in items[:2]] \
        if isinstance(items, list) else []
    zone_id = shop.get("bizZoneId") if isinstance(shop, dict) else None
    scope_ok = type(zone_id) is int and zone_id == 185
    steps["school_scope"] = {
        "status": "pass" if scope_ok else ("unknown" if zone_id is None else "fail"),
        "expected_bizZoneId": 185,
        "returned_bizZoneId": zone_id,
        "returned_bizZoneName": shop.get("bizZoneName") if isinstance(shop, dict) else None,
    }
    queue_body = {key: value for key, value in detail_body.items() if key != "status"}
    event, queue = request_once(opener, "POST", QUEUE_PATH, queue_body)
    event["queue_data"] = queue_summary(queue)
    steps["queue"] = event


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--offline", action="store_true",
                        help="Skip network; diagnostic development only")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    run_id = uuid.uuid4().hex
    checks = {
        "code_execution": {
            "status": "pass",
            "run_id_sha256": hashlib.sha256(run_id.encode("ascii")).hexdigest(),
        },
        "filesystem": filesystem_probe(output_dir, run_id),
    }
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at_utc": utc_now(),
        "runtime": {
            "python_version": platform.python_version(),
            "os_family": platform.system(),
            "isolated_mode": bool(sys.flags.isolated),
            "no_site": bool(sys.flags.no_site),
        },
        "privacy": {
            "browser_state_read": False,
            "capture_files_read": False,
            "prior_reports_read": False,
            "environment_variables_read_by_script": False,
            "cookies_loaded_or_stored": False,
            "environment_proxy_used": False,
            "redirects_followed": False,
            "tls_verification_enabled": True,
            "scope": "Client-side requests only; infrastructure is not audited.",
        },
        "checks": checks,
        "outgoing_requests": [],
    }
    if args.offline:
        for name in ("https_get", "shop_list", "menu", "school_scope", "queue"):
            checks[name] = {"status": "skipped", "reason": "Offline mode requested"}
    else:
        audit = None
        try:
            audit = AuditedHTTPS()
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirects(), audit
            )
            run_network(report, opener)
        except Exception as error:
            checks["network_setup"] = {"status": "fail", "error": error_summary(error)}
            for name in ("https_get", "shop_list", "menu", "school_scope", "queue"):
                checks.setdefault(name, {"status": "skipped",
                                         "reason": "Network setup or processing failed"})
        finally:
            if audit is not None:
                report["outgoing_requests"] = audit.requests
    report["finished_at_utc"] = utc_now()
    filename = "campus-env-report-" + run_id + ".json"
    checks["report_file"] = {"status": "pass", "filename": filename}
    try:
        serialized = json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False)
        with (output_dir / filename).open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except Exception as error:
        checks["report_file"] = {"status": "fail", "error": error_summary(error)}
    print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))
    return report


if __name__ == "__main__":
    main()
