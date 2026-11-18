"""Discover live school plans and read one major from a remote ZIP using ranges."""

import argparse
import datetime as dt
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import runpy
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
HELPERS = runpy.run_path(str(ROOT / "scripts/official_fetch.py"))
ENTRY = "https://www.suit-sz.edu.cn/xxgk1/jxzlxx.htm"
HOST = "www.suit-sz.edu.cn"
LEVELS = {"bachelor": "本科", "junior": "专科"}
MAX_MEMBER = 24000000
MAX_TRANSFER = 32000000
BEIJING = dt.timezone(dt.timedelta(hours=8))


class PlanLinks(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links, self.title = base, [], ""
        self.anchor, self.label, self.heading = None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchor, self.label = dict(attrs).get("href"), []
        if tag == "h1":
            self.heading = True

    def handle_data(self, data):
        if self.anchor is not None:
            self.label.append(data)
        if self.heading:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "h1":
            self.heading = False
        if tag == "a" and self.anchor is not None:
            self.links.append((urllib.parse.urljoin(self.base, self.anchor),
                               "".join(self.label).strip()))
            self.anchor, self.label = None, []


def html_page(client, url):
    event, body, headers = client.get(url)
    if body is None:
        raise ValueError("Official page unavailable: " + url)
    text = HELPERS["decode"](body, headers)
    page = PlanLinks(url)
    page.feed(text)
    return page


def discover(client, level, cohort, max_pages=5):
    pending, visited, plans = [ENTRY], set(), {}
    while pending and len(visited) < max_pages:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = html_page(client, url)
        for link, title in page.links:
            match = re.search(r"(?P<year>20\d{2})级" + LEVELS[level]
                              + r".*人才培养方案", title)
            if match and client.valid(link):
                plans[link] = {"url": link, "title": title,
                               "cohort": int(match["year"]), "level": level}
            parsed = urllib.parse.urlsplit(link)
            if (client.valid(link) and link not in visited and link not in pending
                    and title in ("下页", "下一页")
                    and re.fullmatch(r"/xxgk1/jxzlxx(?:/\d+)?\.htm", parsed.path)):
                pending.append(link)
        if cohort is not None and any(p["cohort"] == cohort for p in plans.values()):
            break
    matches = [p for p in plans.values() if cohort is None or p["cohort"] == cohort]
    if not matches:
        return None, {"list_pages": len(visited), "pending_pages": len(pending),
                      "complete": not pending}, list(plans.values())
    newest = max(p["cohort"] for p in matches)
    candidates = [p for p in matches if p["cohort"] == newest]
    # Multiple revised notices for one cohort need the model's applicability review.
    selected = candidates[0] if len(candidates) == 1 else None
    return selected, {"list_pages": len(visited), "pending_pages": len(pending),
                      "complete": not pending}, candidates


class RangeClient(HELPERS["PublicClient"]):
    def __init__(self):
        super().__init__([HOST], timeout=20)
        self.transfer = 0

    def range_get(self, url, byte_range, referer, validator=None):
        if not self.valid(url):
            raise ValueError("Attachment URL outside school scope.")
        for attempt in range(1, 4):
            event = {"url": url, "method": "GET", "range": byte_range,
                     "attempt": attempt, "cookie_header_present": False,
                     "authorization_header_present": False}
            retry = False
            try:
                headers = {"Range": byte_range, "Accept-Encoding": "identity",
                           "User-Agent": "campus-life/4.5", "Referer": referer}
                if validator:
                    headers["If-Range"] = validator
                request = urllib.request.Request(url, headers=headers)
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), HELPERS["NoRedirect"]())
                with opener.open(request, timeout=self.timeout) as response:
                    event["http_status"] = response.status
                    if response.status != 206:
                        raise ValueError("Server did not honor Range; full download stopped.")
                    found = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                         response.headers.get("Content-Range", ""))
                    if not found:
                        raise ValueError("Invalid Content-Range.")
                    start, end, total = map(int, found.groups())
                    if not 0 <= start <= end < total:
                        raise ValueError("Invalid range bounds.")
                    if byte_range.startswith("bytes=-"):
                        if end != total - 1 or start != max(0, total - int(byte_range[7:])):
                            raise ValueError("Unexpected suffix range.")
                    elif byte_range != f"bytes={start}-{end}":
                        raise ValueError("Server returned a different range.")
                    size = end - start + 1
                    if size > MAX_MEMBER or self.transfer + size > MAX_TRANSFER:
                        raise ValueError("Per-query download budget exceeded.")
                    data = response.read(size + 1)
                    self.transfer += len(data)
                    if len(data) != size:
                        raise ValueError("Incomplete range response.")
                    metadata = response.headers
                event.update(status="pass", response_bytes=len(data), will_retry=False)
                self.requests.append(event)
                return start, total, data, metadata
            except urllib.error.HTTPError as error:
                event.update(http_status=error.code, error_type="HTTPError")
                retry = error.code in (408, 429) or 500 <= error.code <= 599
                error.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
                event.update(error_type=type(error).__name__)
                retry = True
            except ValueError as error:
                event["error"] = str(error)
            event.update(status="fail", will_retry=retry and attempt < 3)
            self.requests.append(event)
            if not event["will_retry"]:
                raise ValueError(event.get("error", event.get("error_type", "Range failed")))
            time.sleep(attempt)
        raise AssertionError("Unreachable")


class RemoteZip(io.RawIOBase):
    def __init__(self, client, url, referer):
        self.client, self.url, self.referer = client, url, referer
        start, self.size, data, headers = client.range_get(url, "bytes=-65557", referer)
        self.validator = headers.get("ETag")
        if self.validator and self.validator.startswith("W/"):
            self.validator = None
        self.validator = self.validator or headers.get("Last-Modified")
        if not self.validator:
            raise ValueError("No attachment validator; cannot pin multiple ranges.")
        self.position, self.cache = 0, [(start, data)]

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if whence not in (0, 1, 2) or position < 0:
            raise ValueError("Invalid ZIP seek.")
        self.position = position
        return position

    def read(self, size=-1):
        size = self.size - self.position if size < 0 else min(size, self.size - self.position)
        if size <= 0:
            return b""
        begin, end = self.position, self.position + size
        if size > MAX_MEMBER:
            raise ValueError("ZIP read exceeds per-member budget.")
        for cached_start, data in self.cache:
            if cached_start <= begin and end <= cached_start + len(data):
                self.position = end
                return data[begin - cached_start:end - cached_start]
        start, total, data, headers = self.client.range_get(
            self.url, f"bytes={begin}-{end - 1}", self.referer, self.validator)
        current = headers.get("ETag") if self.validator.startswith('"') else headers.get("Last-Modified")
        if total != self.size or current != self.validator:
            raise ValueError("Attachment changed during reading; discard mixed content.")
        self.cache.append((start, data))
        self.position = end
        return data


def members(archive):
    result = []
    if len(archive.infolist()) > 3000:
        raise ValueError("ZIP directory exceeds entry budget.")
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if not info.flag_bits & 0x800:
            try:
                name = name.encode("cp437").decode("gb18030")
            except (UnicodeError, LookupError):
                pass
        result.append((name, info))
    return result


def read_document(data, name, args):
    if name.lower().endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_MEMBER:
                raise ValueError("DOCX XML exceeds size limit.")
            tree = ET.fromstring(archive.read(info))
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        def text(element):
            return "".join(t.text or "" for t in element.findall(".//w:t", namespace))

        blocks = []
        for child in tree.find("w:body", namespace):
            if child.tag.endswith("}p"):
                blocks.append({"kind": "paragraph", "text": text(child)})
            elif child.tag.endswith("}tbl"):
                for row in child.findall("w:tr", namespace):
                    cells = []
                    for cell in row.findall("w:tc", namespace):
                        span = cell.find("w:tcPr/w:gridSpan", namespace)
                        merge = cell.find("w:tcPr/w:vMerge", namespace)
                        cells.append({
                            "text": text(cell),
                            "column_span": span.get("{" + namespace["w"] + "}val", "1") if span is not None else "1",
                            "vertical_merge": (merge.get("{" + namespace["w"] + "}val", "continue")
                                               if merge is not None else None),
                        })
                    blocks.append({"kind": "table_row", "cells": cells})
        return slice_units(blocks, "stdlib-docx", "block", args)
    if name.lower().endswith(".pdf"):
        try:
            import pypdf
        except ImportError:
            return {"status": "reader_required", "reason": "Environment PDF reader or pypdf unavailable.",
                    "text_read": False}
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return {"status": "reader_required", "reason": "Encrypted PDF.", "text_read": False}
        end = len(reader.pages) if args.find else min(len(reader.pages), args.offset + min(args.units, 6))
        pages = []
        for n in range(0 if args.find else args.offset, end):
            text = reader.pages[n].extract_text() or ""
            pages.append({"index": n + 1, "text": text[:args.max_chars],
                          "truncated": len(text) > args.max_chars,
                          "ocr_needed": not bool(text.strip())})
        if args.find:
            return slice_units(pages, "pypdf", "page", args)
        return {"reader": "pypdf", "unit": "page", "total_units": len(reader.pages),
                "units": pages, "next_offset": end if end < len(reader.pages) else None,
                "table_layout_verified": False}
    return {"status": "reader_required", "reason": "Use an environment reader for this file type.",
            "text_read": False}


def slice_units(blocks, reader, unit, args):
    selected = list(range(len(blocks)))
    if args.find:
        hits = [n for n, block in enumerate(blocks)
                if any(term.casefold() in json.dumps(block, ensure_ascii=False).casefold()
                       for term in args.find)]
        selected = sorted({i for n in hits for i in range(max(0, n - 2), min(len(blocks), n + 3))})
    output, used, end = [], 0, args.offset
    for cursor in range(args.offset, min(len(selected), args.offset + args.units)):
        index = selected[cursor]
        block = dict(blocks[index], index=index + 1)
        size = len(json.dumps(block, ensure_ascii=False))
        if used + size > args.max_chars:
            if not output:
                block = {"index": index + 1, "kind": "oversized_unit",
                         "text": json.dumps(block, ensure_ascii=False)[:args.max_chars],
                         "truncated": True, "environment_reader_required": True}
            else:
                break
        output.append(block)
        used += size
        end = cursor + 1
    return {"reader": reader, "unit": unit, "total_units": len(blocks),
            "filtered_units": len(selected), "find": args.find or [],
            "units": output, "next_offset": end if end < len(selected) else None,
            "table_layout_verified": False}


def run(args, client=None):
    client = client or RangeClient()
    result = {"queried_at_beijing": dt.datetime.now(BEIJING).isoformat(),
              "status": "failed", "entry_url": ENTRY,
              "requests": client.requests, "retrieved_urls": [], "failed_urls": []}
    try:
        directory = None
        if args.export_dir:
            try:
                directory = Path(args.export_dir).resolve(strict=True)
            except FileNotFoundError as error:
                raise ValueError("Export directory does not exist. Create the permitted task directory "
                                 "first, then pass it with --export-dir.") from error
            if not directory.is_dir():
                raise ValueError("--export-dir must point to an existing directory, not a file.")
        plan, coverage, candidates = discover(client, args.level, args.cohort, args.max_pages)
        result.update(discovery_coverage=coverage, plan_candidates=candidates)
        if not plan:
            applicable = [p for p in candidates if args.cohort is None or p["cohort"] == args.cohort]
            result["status"] = "ambiguous_plan" if applicable else "plan_not_found"
            return result
        page = html_page(client, plan["url"])
        if (str(plan["cohort"]) + "级" + LEVELS[args.level]) not in page.title:
            raise ValueError("Detail heading does not match cohort and level.")
        attachments = [(url, title) for url, title in page.links
                       if client.valid(url)
                       and urllib.parse.urlsplit(url).path == "/virtual_attach_file.vsb"
                       and title.lower().endswith(".zip")]
        result.update(plan=plan, discovered_attachments=[u for u, _ in attachments])
        if len(attachments) != 1:
            result["status"] = "attachment_selection_required"
            return result
        attachment, _ = attachments[0]
        remote = RemoteZip(client, attachment, plan["url"])
        result.update(attachment_url=attachment, archive_bytes=remote.size)
        with zipfile.ZipFile(remote) as archive:
            entries = members(archive)
            result["members"] = [{"name": name, "bytes": info.file_size}
                                 for name, info in entries[:200]]
            result["members_truncated"] = len(entries) > 200
            matches = [(name, info) for name, info in entries
                       if (args.member and name == args.member)
                       or (not args.member and args.major
                           and args.major.casefold() in name.casefold())]
            if not args.major and not args.member:
                result["status"] = "catalog"
                return result
            result["matched_members"] = [name for name, _ in matches]
            if len(matches) != 1:
                result["status"] = "ambiguous_major" if matches else "major_not_found"
                return result
            name, info = matches[0]
            if (info.file_size > MAX_MEMBER or info.compress_size > MAX_MEMBER
                    or info.flag_bits & 1
                    or info.file_size > 500 * max(info.compress_size, 1)):
                raise ValueError("Selected member exceeds limits or is encrypted.")
            data = archive.read(info)
            digest = hashlib.sha256(data).hexdigest()
            if args.document_sha256 and digest != args.document_sha256:
                raise ValueError("Document changed between calls; do not combine old and new units.")
            del result["members"]
            result["document"] = {"member": name, "bytes": len(data), "sha256": digest,
                                  "source": attachment}
            if directory is not None:
                suffix = Path(name).suffix.lower()
                if suffix not in (".pdf", ".docx", ".doc", ".txt"):
                    raise ValueError("Unsupported export type.")
                destination = directory / ("curriculum-" + hashlib.sha256(data).hexdigest()[:16] + suffix)
                if destination.exists():
                    if destination.read_bytes() != data:
                        raise ValueError("Existing export has different content.")
                else:
                    with destination.open("xb") as handle:
                        handle.write(data)
                result["document"]["export_file"] = str(destination)
            result["document"].update(read_document(data, name, args))
            result["status"] = ("reader_required" if result["document"].get("status") == "reader_required"
                                else "document")
        return result
    except (ValueError, OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        result.update(status="failed", error=str(error))
        return result
    finally:
        result["retrieved_urls"] = list(dict.fromkeys(
            e["url"] for e in client.requests if e.get("status") == "pass"))
        result["failed_urls"] = list(dict.fromkeys(
            e["url"] for e in client.requests if e.get("status") == "fail"))
        result["range_transfer_bytes"] = getattr(client, "transfer", 0)
        result["note"] = ("A ZIP range is partial network retrieval, not reading every file. "
                          "Only document.units are extracted text; table columns need visual verification. "
                          "Repeated pages must use the same applicable document.")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--level", choices=tuple(LEVELS), required=True)
    root.add_argument("--cohort", type=int)
    root.add_argument("--major")
    root.add_argument("--member")
    root.add_argument("--max-pages", type=int, default=5)
    root.add_argument("--offset", type=int, default=0)
    root.add_argument("--units", type=int, default=40)
    root.add_argument("--max-chars", type=int, default=16000)
    root.add_argument("--find", action="append")
    root.add_argument("--export-dir", help="Existing permitted task directory; create it before exporting.")
    root.add_argument("--read-file")
    root.add_argument("--document-sha256")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if (args.offset < 0 or not 1 <= args.units <= 60 or not 1 <= args.max_pages <= 10
            or not 100 <= args.max_chars <= 30000
            or (args.cohort is not None and not 2000 <= args.cohort <= 2100)):
        raise SystemExit("Invalid paging or cohort arguments.")
    if args.read_file:
        path = Path(args.read_file)
        if path.stat().st_size > MAX_MEMBER:
            raise SystemExit("Local document exceeds size limit.")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if args.document_sha256 and digest != args.document_sha256:
            raise SystemExit("Local document hash mismatch.")
        result = {"status": "local_document", "source_status": "local_file_not_live",
                  "file": str(path), "sha256": digest,
                  "document": read_document(data, path.name, args),
                  "retrieved_urls": [], "failed_urls": []}
    else:
        result = run(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return result


if __name__ == "__main__":
    main()
