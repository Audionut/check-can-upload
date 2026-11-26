import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests


RED = "https://redacted.sh"
OPS = "https://orpheus.network"
RED_SITES = {"redacted.sh", "redacted.ch", "passtheheadphones.me"}
RED_TRACKERS = RED_SITES | {"flacsfor.me"}
OPS_SITES = {"orpheus.network"}
OPS_TRACKERS = OPS_SITES | {"home.opsfet.ch"}
AUDIO_EXTENSIONS = {
    "aac",
    "aiff",
    "alac",
    "ape",
    "dff",
    "dsf",
    "flac",
    "m4a",
    "mp3",
    "ogg",
    "opus",
    "wav",
    "wv",
}
VARIOUS_ARTISTS = {"various artists", "v/a", "va"}
MIN_INTERVAL = 4.0
OPS_CACHE_TTL = 24 * 60 * 60
TIMEOUT = (10, 30)
EXACT_REPORT = "ops-exact-matches"
PARTIAL_REPORT = "ops-partial-matches"
MISSING_REPORT = "ops-not-found"
UNCHECKED_REPORT = "ops-unchecked"
REPORT_NAMES = (EXACT_REPORT, PARTIAL_REPORT, MISSING_REPORT, UNCHECKED_REPORT)
LEGACY_MATCH_REPORT = "ops-matches"
REPORT_TIMESTAMP = "%Y%m%dT%H%M%S.%fZ"
REPORT_DIRECTORY = re.compile(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z(?:-.+)?")
URL = re.compile(r"https?://[^\s<>\"'\[\]()]+", re.IGNORECASE)
INFO_HASH = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
EDITION_FIELDS = (
    "remasterYear",
    "remasterTitle",
    "remasterRecordLabel",
    "remasterCatalogueNumber",
)


class ScanError(Exception):
    pass


def request(session, method, url, service, **kwargs):
    try:
        return session.request(
            method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs
        )
    except requests.RequestException as exc:
        raise ScanError(f"{service} connection error ({type(exc).__name__})") from None


def json_body(response, service):
    try:
        return response.json()
    except ValueError:
        raise ScanError(f"{service} returned invalid JSON") from None


class QBit:
    def __init__(self, session, url, user, password, api_key):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.query
            or parts.fragment
        ):
            raise ScanError(
                "Specify a qBittorrent WebUI URL without credentials, a query or a fragment"
            )
        self.session = session
        self.base = f"{url.rstrip('/')}/api/v2"
        origin = f"{parts.scheme}://{parts.netloc}"
        session.headers.update({"Origin": origin, "Referer": f"{origin}/"})
        if api_key:
            session.headers["Authorization"] = f"Bearer {api_key}"
        elif user or password:
            response = request(
                session,
                "POST",
                f"{self.base}/auth/login",
                "qBittorrent login",
                data={"username": user, "password": password},
            )
            legacy_success = response.status_code == 200 and response.content == b"Ok."
            if response.status_code != 204 and not legacy_success:
                raise ScanError(
                    f"qBittorrent login rejected (HTTP {response.status_code})"
                )

    def get(self, endpoint, expected, **params):
        service = f"qBittorrent {endpoint}"
        response = request(
            self.session,
            "GET",
            f"{self.base}/torrents/{endpoint}",
            service,
            params=params,
        )
        if response.status_code != 200:
            raise ScanError(f"{service} returned HTTP {response.status_code}")
        data = json_body(response, service)
        if not isinstance(data, expected):
            raise ScanError(f"{service} returned an unexpected response")
        return data


def retry_delay(header):
    if header and header.isdecimal():
        return max(20, int(header))
    if header:
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(20, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, OverflowError):
            pass
    return 20


class Gazelle:
    def __init__(self, session, site, base_url, authorization, interval, debug=False):
        self.session = session
        self.site = site
        self.url = f"{base_url}/ajax.php"
        self.authorization = authorization
        self.interval = interval
        self.debug = debug
        self.next_request = 0.0
        self.blocked = ""
        self.recent_requests = deque(maxlen=14)

    def get(self, action, **params):
        if self.blocked:
            raise ScanError(self.blocked)
        service = f"{self.site} {action}"
        download = action == "download"
        for attempt in range(2):
            now = time.monotonic()
            start = max(now, self.next_request)
            # Browse has a lower limit on the shared OPS account counter. A
            # previous group request can leave that counter active for 60 seconds.
            if self.site == "OPS":
                limit = 5 if action == "browse" else self.recent_requests.maxlen
                if len(self.recent_requests) >= limit:
                    start = max(start, self.recent_requests[-limit] + 61)
            wait = start - now
            if wait > 0:
                time.sleep(wait)
            start = time.monotonic()
            self.recent_requests.append(start)
            self.next_request = start + self.interval
            response = request(
                self.session,
                "GET",
                self.url,
                service,
                headers={
                    "Authorization": self.authorization,
                    "Accept": (
                        "application/x-bittorrent, application/json, text/plain"
                        if download
                        else "application/json"
                    ),
                },
                params={"action": action, **params},
            )
            status = response.status_code
            content_type = response.headers.get("Content-Type", "").lower()
            if (
                download
                and status == 200
                and content_type.split(";", 1)[0]
                in {
                    "application/x-bittorrent",
                    "text/plain",
                }
            ):
                body = response.content
                if body.startswith(b"d") and body.endswith(b"e") and b"4:infod" in body:
                    return body
            if self.debug and not download:
                print(
                    f"[{service} HTTP {status}]\n{response.text}",
                    file=sys.stderr,
                    flush=True,
                )
            payload = None
            if status == 200 or "json" in content_type:
                try:
                    payload = json_body(response, service)
                except ScanError:
                    pass
            error = payload.get("error", "") if isinstance(payload, dict) else ""
            if (
                status == 429
                or "rate limit" in str(error).casefold()
                or (
                    (payload is None or download)
                    and "rate limit" in response.text[:4096].casefold()
                )
            ):
                if download:
                    self.blocked = f"{self.site} rate limited torrent downloads. Try again after the cooldown."
                    raise ScanError(self.blocked)
                delay = retry_delay(response.headers.get("Retry-After"))
                if attempt == 0 and delay <= 60:
                    print(
                        f"{self.site} throttled requests. Waiting {delay:.0f}s before retrying.",
                        file=sys.stderr,
                    )
                    self.next_request = max(self.next_request, time.monotonic() + delay)
                    continue
                self.blocked = (
                    f"{self.site} rate limited the scan. Try again after the cooldown."
                )
                raise ScanError(self.blocked)
            if status in {401, 403} or 300 <= status < 400:
                self.blocked = f"{self.site} rejected the API request (HTTP {status})"
                raise ScanError(self.blocked)
            if status in {502, 503, 504}:
                delay = retry_delay(response.headers.get("Retry-After"))
                if delay > 60:
                    self.blocked = f"{self.site} requested a cooldown of {delay:.0f}s (HTTP {status})"
                    raise ScanError(self.blocked)
                self.next_request = max(self.next_request, time.monotonic() + delay)
                if attempt == 0:
                    continue
            if status != 200:
                raise ScanError(f"{service} returned HTTP {status}")
            if download:
                raise ScanError(f"{service} did not return a torrent file")
            if (
                action == "artist"
                and isinstance(payload, dict)
                and payload.get("status") == "failure"
                and (
                    error == "bad artistname"
                    or payload.get("response") == "bad artistname"
                )
            ):
                return {"torrentgroup": []}
            if (
                isinstance(payload, dict)
                and payload.get("status") == "success"
                and isinstance(payload.get("response"), dict)
            ):
                return payload["response"]
            raise ScanError(f"{service} did not return a successful API response")


def hostname(url):
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def positive_int(value):
    if type(value) in {str, int} and str(value).isascii() and str(value).isdecimal():
        return int(value) or None
    return None


def comment_links(comment, sites):
    links = []
    for url in URL.findall(html.unescape(comment or "")):
        if hostname(url) not in sites:
            continue
        parts = urlsplit(url.rstrip(".,;"))
        query = parse_qs(parts.query)
        if parts.path != "/torrents.php" or "action" in query:
            continue
        group_id = positive_int(query.get("id", [None])[0])
        torrent_id = positive_int(query.get("torrentid", [None])[0])
        if torrent_id is None and parts.fragment.startswith("torrent"):
            torrent_id = positive_int(parts.fragment.removeprefix("torrent"))
        if group_id or torrent_id:
            links.append((group_id, torrent_id))
    return links


def site_link(site, group_id, torrent_id):
    if group_id and torrent_id:
        return f"{site}/torrents.php?id={group_id}&torrentid={torrent_id}#torrent{torrent_id}"
    if torrent_id:
        return f"{site}/torrents.php?torrentid={torrent_id}"
    return f"{site}/torrents.php?id={group_id}" if group_id else ""


def qbit_audio_layout(files):
    tracks = []
    for file in files:
        path = unicodedata.normalize("NFC", file["name"]).casefold()
        if path.rpartition(".")[2] in AUDIO_EXTENSIONS:
            size = positive_int(file.get("size"))
            if not size:
                return ()
            tracks.append((path, size))
    roots = {path.split("/", 1)[0] for path, _ in tracks}
    if len(roots) == 1 and all("/" in path for path, _ in tracks):
        tracks = [(path.split("/", 1)[1], size) for path, size in tracks]
    return tuple(sorted(tracks))


def display(value):
    return " ".join(html.unescape(str(value or "")).split())


def normalized(value):
    return unicodedata.normalize("NFC", display(value)).casefold()


def media_key(value):
    media = normalized(value)
    return "blu-ray" if media == "bd" else media


def title_key(value):
    text = normalized(value).translate(str.maketrans("", "", ",'\"‘’“”"))
    return " ".join(re.sub(r"\s*[-–—]\s*", "-", text).split())


def labels_match(left, right):
    if left == right:
        return True
    phrases = (" ".join(re.findall(r"[^\W_]+", label)) for label in (left, right))
    shorter, longer = sorted(phrases, key=len)
    return bool(shorter) and f" {shorter} " in f" {longer} "


def artist_names(rows):
    if not isinstance(rows, (list, dict)):
        return ()
    return tuple(
        name
        for row in (rows.values() if isinstance(rows, dict) else rows)
        if isinstance(row, dict) and (name := display(row.get("name")))
    )


def artist_key(names):
    return frozenset(
        "various artists" if name in VARIOUS_ARTISTS else name
        for name in map(normalized, names)
    )


def various_artists(credits):
    return len(credits) > 2 or "various artists" in credits


def artist_credits_match(left, right):
    return left == right or (various_artists(left) and various_artists(right))


def group_artists(group):
    return artist_names((group.get("musicInfo") or {}).get("artists"))


@dataclass(frozen=True)
class Edition:
    year: int | None
    label: str
    catalogue: str
    title: str
    media: str

    @classmethod
    def from_api(cls, group, torrent):
        year = positive_int(torrent.get("remasterYear"))
        label = display(torrent.get("remasterRecordLabel"))
        catalogue = display(torrent.get("remasterCatalogueNumber"))
        title = display(torrent.get("remasterTitle"))
        if not (year or label or catalogue or title or torrent.get("remastered")):
            year = positive_int(group.get("year"))
            label = display(group.get("recordLabel"))
            catalogue = display(group.get("catalogueNumber"))
        return cls(year, label, catalogue, title, display(torrent.get("media")))

    def catalogue_key(self):
        return re.sub(r"[\s\-‐‑–]+", "", normalized(self.catalogue))

    def compare(self, other):
        required = (
            (self.year, other.year),
            (media_key(self.media), media_key(other.media)),
        )
        if any(a and b and a != b for a, b in required):
            return False
        left_label, right_label = normalized(self.label), normalized(other.label)
        if left_label and right_label and not labels_match(left_label, right_label):
            return False
        return True if all(a and b for a, b in required) else None

    def missing_fields(self):
        return [
            field
            for field, value in (
                ("edition year", self.year),
                ("media", media_key(self.media)),
            )
            if not value
        ]


def result_rows(payload, key, service):
    rows = payload.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ScanError(f"{service} response is missing a valid {key} list")
    return rows


def ops_title_groups(ops, title):
    page = 1
    while True:
        data = ops.get("browse", groupname=title, filter_cat="Music", page=page)
        results = result_rows(data, "results", "OPS browse")
        if not results and page == 1 and data.get("pages") in (None, 0, "0"):
            return
        pages = positive_int(data.get("pages"))
        if (
            positive_int(data.get("currentPage")) != page
            or pages is None
            or pages < page
        ):
            raise ScanError("OPS browse returned inconsistent pagination")
        for group in results:
            if title_key(group.get("groupName")) != title_key(title):
                continue
            torrents = result_rows(group, "torrents", "OPS browse")
            artists = next(
                (torrent["artists"] for torrent in torrents if torrent.get("artists")),
                [],
            )
            if not artist_names(artists) and various_artists(
                artist_key((display(group.get("artist")),))
            ):
                artists = [{"name": "Various Artists"}]
            yield {
                **group,
                "artists": artists,
                "torrent": [
                    {**torrent, "id": torrent.get("torrentId")} for torrent in torrents
                ],
            }
        if page == pages:
            return
        page += 1


def ops_artist_groups(artist_releases, artists):
    # A group with these main artists is included in each of their catalogues.
    for artist in dict.fromkeys(artists):
        groups = artist_releases(artist)
        if groups:
            return groups
    return ()


@dataclass(frozen=True)
class OpsMatch:
    group_id: int
    format_pair: tuple[str, str]
    exceptions: tuple[str, ...]
    totals: tuple[int | None, int | None] = (None, None)
    edition_key: tuple = ()


def torrent_totals(torrent):
    return positive_int(torrent.get("fileCount")), positive_int(torrent.get("size"))


def file_manifest(torrent):
    count, size = torrent_totals(torrent)
    value = torrent.get("fileList")
    if not count or not size or not isinstance(value, str):
        return None
    files = {}
    for entry in value.split("|||"):
        path, separator, tail = entry.rpartition("{{{")
        number = tail.removesuffix("}}}")
        if (
            not (
                path
                and separator
                and tail.endswith("}}}")
                and number.isascii()
                and number.isdecimal()
            )
            or path in files
        ):
            return None
        files[path] = int(number)
    if len(files) != count or sum(files.values()) != size:
        return None
    # Retain a fingerprint of the sorted path/size pairs instead of the file names.
    return (
        count,
        size,
        hashlib.sha256(json.dumps(sorted(files.items())).encode()).hexdigest(),
    )


def edition_exceptions(red, ops):
    exceptions = []
    for field, left, right in (
        ("record label", normalized(red.label), normalized(ops.label)),
        ("catalogue number", red.catalogue_key(), ops.catalogue_key()),
        ("edition title", normalized(red.title), normalized(ops.title)),
    ):
        if left == right and (left or field == "edition title"):
            continue
        if not left and not right:
            exceptions.append(f"RED and OPS are missing {field}")
        elif not left or not right:
            site = "RED" if not left else "OPS"
            exceptions.append(f"{site} is missing {field}")
        else:
            exceptions.append(f"{field} differs")
    return exceptions


def audio_key(format_pair):
    return tuple(map(normalized, format_pair))


def compatible_audio(audio, expected):
    return all(not value or value == wanted for value, wanted in zip(audio, expected))


def ops_matches(groups, group_details, title, artists, edition, format_pair):
    wanted_title = title_key(title)
    wanted_artists = artist_key(artists)
    source_audio = audio_key(format_pair)
    matches = {}
    uncertain = None
    for result in groups:
        if title_key(result.get("groupName")) != wanted_title:
            continue
        if positive_int(result.get("groupCategoryID")) not in {None, 1}:
            continue
        group_id = positive_int(result.get("groupId"))
        if group_id is None:
            raise ScanError("OPS returned a release without a group ID")
        group_key = artist_key(artist_names(result.get("artists")))
        if group_key and not artist_credits_match(wanted_artists, group_key):
            continue
        group = {
            "year": result.get("groupYear"),
            "recordLabel": result.get("groupRecordLabel"),
            "catalogueNumber": result.get("groupCatalogueNumber"),
        }
        rows = [
            row
            for row in result_rows(result, "torrent", "OPS release")
            if "remasterYear" not in row
            or edition.compare(Edition.from_api(group, row)) is not False
        ]
        if not rows:
            continue
        if not group_key or any(
            Edition.from_api(group, row).missing_fields()
            or "remasterYear" not in row
            or (edition.title and "remasterTitle" not in row)
            or (edition.label and "remasterRecordLabel" not in row)
            or (
                edition.label
                and not any(row.get(key) for key in EDITION_FIELDS)
                and not group["recordLabel"]
                and not group["catalogueNumber"]
            )
            for row in rows
        ):
            details = group_details(group_id)
            detail_group = details.get("group")
            if not isinstance(detail_group, dict):
                raise ScanError("OPS torrentgroup returned no group metadata")
            group_key = artist_key(group_artists(detail_group)) or group_key
            if group_key and not artist_credits_match(wanted_artists, group_key):
                continue
            group.update(detail_group)
            rows = result_rows(details, "torrents", "OPS torrentgroup")
        for row in rows:
            candidate = Edition.from_api(group, row)
            equal = edition.compare(candidate)
            if equal is False:
                continue
            torrent_id = positive_int(row.get("id"))
            ops_format = (display(row.get("format")), display(row.get("encoding")))
            missing_compilation_evidence = (
                group_key
                and group_key != wanted_artists
                and not (
                    (normalized(edition.label) and normalized(candidate.label))
                    or (edition.catalogue_key() and candidate.catalogue_key())
                )
            )
            if (
                equal is None
                or not group_key
                or not torrent_id
                or missing_compilation_evidence
            ):
                if uncertain is None and compatible_audio(
                    audio_key(ops_format), source_audio
                ):
                    red_missing = edition.missing_fields() if equal is None else []
                    ops_missing = candidate.missing_fields() if equal is None else []
                    if not group_key:
                        ops_missing.append("main artists")
                    if not torrent_id:
                        ops_missing.append("torrent ID")
                    problems = [
                        f"{site} is missing {', '.join(fields)}"
                        for site, fields in (("RED", red_missing), ("OPS", ops_missing))
                        if fields
                    ]
                    if missing_compilation_evidence:
                        problems.append(
                            "different various-artist credits require labels or catalogue numbers from both sites"
                        )
                    uncertain = f"{site_link(OPS, group_id, torrent_id)} ({' | '.join(problems)})"
                continue
            matches[site_link(OPS, group_id, torrent_id)] = OpsMatch(
                group_id,
                ops_format,
                tuple(edition_exceptions(edition, candidate)),
                torrent_totals(row),
                (
                    candidate.year,
                    normalized(candidate.label),
                    candidate.catalogue_key(),
                    normalized(candidate.title),
                    media_key(candidate.media),
                ),
            )
    if uncertain and not any(
        audio_key(match.format_pair) == source_audio for match in matches.values()
    ):
        raise ScanError(
            f"OPS has a possible match with incomplete edition metadata | {uncertain}"
        )
    return matches


class JsonReport:
    def __init__(self, file):
        self.file = file
        contents = file.read()
        if contents:
            rows = report_rows(contents.decode("utf-8-sig"), file.name)
            self.has_rows = bool(rows)
            self.end = len(contents.rstrip()[:-1].rstrip())
        else:
            self.has_rows = False
            self.end = 1
            file.write(b"[\n]\n")

    def append(self, row):
        entry = json.dumps(row, ensure_ascii=False).encode(
            "utf-8", errors="backslashreplace"
        )
        prefix = b",\n  " if self.has_rows else b"\n  "
        # Replace the closing bracket so the report is readable after every row.
        self.file.seek(self.end)
        try:
            data = prefix + entry + b"\n]\n"
            if self.file.write(data) != len(data):
                raise OSError("Incomplete JSON report write")
            self.file.truncate()
        except (OSError, KeyboardInterrupt):
            self.restore(self.end, self.has_rows)
            raise
        self.end += len(prefix) + len(entry)
        self.has_rows = True

    def restore(self, end, has_rows):
        self.file.seek(end)
        self.file.write(b"\n]\n")
        self.file.truncate()
        self.end, self.has_rows = end, has_rows


def missing_ops_formats(format_pair, available):
    source = audio_key(format_pair)
    if source[0] != "flac":
        return []
    wanted = [("MP3", "320"), ("MP3", "V0 (VBR)")]
    if source[1] == "24bit lossless":
        wanted.append(("FLAC", "Lossless"))
    return [
        {"format": fmt, "encoding": encoding}
        for fmt, encoding in wanted
        if audio_key((fmt, encoding)) not in available
    ]


def partial_file_check(source, candidates, matches):
    totals = torrent_totals(source)
    eligible = {
        url: match for url, match in candidates.items() if match.totals == totals
    }
    if not eligible or (manifest := file_manifest(source)) is None:
        return None
    available = {}
    for match in matches.values():
        available.setdefault((match.group_id, match.edition_key), set()).add(
            audio_key(match.format_pair)
        )
    return {
        "file_count": manifest[0],
        "size": manifest[1],
        "manifest_sha256": manifest[2],
        "candidates": {
            url: sorted(available[(match.group_id, match.edition_key)])
            for url, match in eligible.items()
        },
    }


def show_missing_ops_formats(record):
    if missing := record.get("missing_ops_formats"):
        print(
            "  Missing OPS formats | "
            + ", ".join(f"{item['format']} / {item['encoding']}" for item in missing),
            flush=True,
        )


def save_torrent_file(directory, name, torrent_id, body):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if stem.lower().endswith(".torrent"):
        stem = stem[:-8].rstrip(" .")
    if not stem:
        stem = f"OPS {torrent_id}"
    if re.fullmatch(
        r"con|prn|aux|nul|(?:com|lpt)[1-9¹²³]", stem.split(".")[0].rstrip(), re.I
    ):
        stem = f"_{stem}"
    for suffix in (".torrent", f" [OPS {torrent_id}].torrent"):
        # Leave room for the suffix within the filesystem's filename limit.
        truncated = stem.encode("utf-8")[: 255 - len(suffix)].decode("utf-8", "ignore")
        path = directory / f"{truncated.rstrip(' .')}{suffix}"
        try:
            file = path.open("xb")
        except FileExistsError:
            if path.is_file() and path.read_bytes() == body:
                return path
            continue
        try:
            with file:
                file.write(body)
        except (OSError, KeyboardInterrupt):
            path.unlink(missing_ok=True)
            raise
        return path
    raise ScanError(f"Cannot save OPS torrent {torrent_id}: both filenames are in use")


class OpsTorrentDownloader:
    def __init__(self, ops, directory):
        self.ops = ops
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.failures = 0

    def save(self, record):
        files, errors = {}, {}
        for url in record["ops_urls"]:
            torrent_id = positive_int(
                parse_qs(urlsplit(url).query).get("torrentid", [None])[0]
            )
            try:
                if torrent_id is None:
                    raise ScanError("OPS match has no torrent ID")
                body = self.ops.get("download", id=torrent_id)
                path = save_torrent_file(
                    self.directory, record["torrent_name"], torrent_id, body
                )
            except (ScanError, OSError) as exc:
                errors[url] = str(exc)
                self.failures += 1
                print(
                    f"  Could not save OPS torrent {torrent_id} | {exc}",
                    file=sys.stderr,
                )
                if self.ops.blocked:
                    break
                continue
            files[url] = str(path)
            print(f"  OPS torrent file | {path}", flush=True)
        if files:
            record["ops_torrent_files"] = files
        if errors:
            record["ops_download_errors"] = errors


def write_search_result(
    reports, row, matches, format_pair, *, source=None, verify=None, download=None
):
    source_audio = audio_key(format_pair)
    same_audio = {
        url: match
        for url, match in matches.items()
        if audio_key(match.format_pair) == source_audio
    }
    exact = {url: match for url, match in same_audio.items() if not match.exceptions}
    if exact:
        name, candidates = EXACT_REPORT, exact
    elif same_audio:
        name, candidates = PARTIAL_REPORT, same_audio
    else:
        for url, match in matches.items():
            audio = audio_key(match.format_pair)
            if not all(audio) and compatible_audio(audio, source_audio):
                raise ScanError(f"OPS is missing audio format or encoding for {url}")
        name, candidates = MISSING_REPORT, {}

    record = {
        **row,
        "ops_urls": list(candidates),
        "ops_formats": [" / ".join(match.format_pair) for match in candidates.values()],
        "same_format_and_encoding": True if candidates else None,
    }
    if name == PARTIAL_REPORT:
        record["match_exceptions"] = {
            url: match.exceptions for url, match in candidates.items()
        }
        if source is not None and (
            check := partial_file_check(source, candidates, matches)
        ):
            record["file_check"] = check
            if verify is not None and (promoted := verify(record)) is not None:
                name, record = EXACT_REPORT, promoted
    if exact and source_audio[0] == "flac":
        exact_groups = {match.group_id for match in exact.values()}
        available = {
            audio_key(match.format_pair)
            for match in matches.values()
            if match.group_id in exact_groups and not match.exceptions
        }
        record["missing_ops_formats"] = missing_ops_formats(format_pair, available)
    show_missing_ops_formats(record)
    if name == EXACT_REPORT and download is not None:
        download(record)
    reports[name].append(record)
    return name


class PartialFileVerifier:
    def __init__(self, ops):
        self.ops = ops
        self.failures = 0
        self.details = lru_cache(maxsize=128)(
            lambda torrent_id: ops.get("torrent", id=torrent_id)
        )

    def verify(self, row):
        check = row["file_check"]
        candidates = check["candidates"]
        expected = check["file_count"], check["size"], check["manifest_sha256"]
        verified = {}
        for url, available in list(candidates.items()):
            if self.ops.blocked:
                break
            torrent_id = positive_int(
                parse_qs(urlsplit(url).query).get("torrentid", [None])[0]
            )
            try:
                if not torrent_id:
                    raise ScanError("Saved OPS link has no torrent ID")
                data = self.details(torrent_id)
                torrent = data.get("torrent")
                if (
                    not isinstance(torrent, dict)
                    or positive_int(torrent.get("id")) != torrent_id
                ):
                    raise ScanError("OPS returned unexpected torrent details")
                manifest = file_manifest(torrent)
                if manifest is None and torrent_totals(torrent) == expected[:2]:
                    raise ScanError("OPS returned a missing or invalid file list")
            except ScanError as exc:
                self.failures += 1
                print(
                    f"Could not verify OPS torrent {torrent_id} | {exc}",
                    file=sys.stderr,
                )
                continue
            del candidates[url]
            if (
                manifest == expected
                and media_key(torrent.get("media")) == media_key(row["media"])
                and audio_key((torrent.get("format"), torrent.get("encoding")))
                == audio_key((row["red_format"], row["red_encoding"]))
            ):
                verified[url] = available

        if not candidates:
            del row["file_check"]
        if not verified:
            return None
        exact = {
            key: value
            for key, value in row.items()
            if key not in ("file_check", "match_exceptions")
        }
        exact["ops_urls"] = list(verified)
        exact["ops_formats"] = [
            fmt
            for url, fmt in zip(row["ops_urls"], row["ops_formats"])
            if url in verified
        ]
        exact["match_basis"] = "file_list"
        if normalized(row["red_format"]) == "flac":
            exact["missing_ops_formats"] = missing_ops_formats(
                (row["red_format"], row["red_encoding"]),
                {tuple(pair) for pairs in verified.values() for pair in pairs},
            )
        print(
            f"  Verified file list | {row['release_name']} ({len(verified)} OPS match{'es' if len(verified) != 1 else ''})",
            flush=True,
        )
        return exact


def cached_ops_result(cache_dir, key, fetch, refresh):
    digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
    path = cache_dir / f"{digest}.json"
    if not refresh:
        try:
            if (
                datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
                < OPS_CACHE_TTL
            ):
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    result = fetch()
    try:
        cache_dir.mkdir(exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        print(f"Could not save OPS cache | {exc}", file=sys.stderr)
    return result


def report_rows(contents, path):
    try:
        rows = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ScanError(f"Invalid JSON in report {path}") from exc
    if not isinstance(rows, list):
        raise ScanError(f"Expected a JSON array in report {path}")
    return rows


def open_report(path):
    try:
        return path.open("r+b", buffering=0)
    except FileNotFoundError:
        return path.open("x+b", buffering=0)


@contextmanager
def lock_reports(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".scan.lock").open("a+b", buffering=0) as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ScanError(
                f"Could not lock {output_dir}. Another scan may be using it."
            ) from exc
        yield


def latest_report_directory(output_dir):
    return max(
        (
            report.parent
            for report in output_dir.glob("*/ops-*.json")
            if report.stem in (*REPORT_NAMES, LEGACY_MATCH_REPORT)
            and REPORT_DIRECTORY.fullmatch(report.parent.name)
        ),
        default=None,
    )


def select_report_directory(output_dir, new_reports):
    latest = latest_report_directory(output_dir)
    if latest is not None and not new_reports:
        return latest

    stamp = datetime.now(timezone.utc)
    if latest is not None:
        previous = datetime.strptime(
            latest.name.split("-", 1)[0], REPORT_TIMESTAMP
        ).replace(tzinfo=timezone.utc)
        stamp = max(stamp, previous + timedelta(microseconds=1))
    return Path(
        tempfile.mkdtemp(prefix=f"{stamp.strftime(REPORT_TIMESTAMP)}-", dir=output_dir)
    )


def processed_hashes(output_dir):
    hashes = set()
    for report in output_dir.glob("*/ops-*.*"):
        if report.stem not in (
            *REPORT_NAMES,
            LEGACY_MATCH_REPORT,
        ) or report.suffix not in (
            ".json",
            ".csv",
        ):
            continue
        with report.open(encoding="utf-8-sig", newline="") as file:
            if report.suffix == ".json":
                contents = file.read()
                rows = report_rows(contents, report) if contents else []
            else:
                rows = csv.DictReader(file)
            for row in rows:
                info_hash = row.get("torrent_hash") if isinstance(row, dict) else None
                if isinstance(info_hash, str) and INFO_HASH.fullmatch(
                    info_hash.strip().lower()
                ):
                    hashes.add(info_hash.strip().lower())
    return hashes


def verify_saved_partial_files(ops, output_dir, limit):
    report_dir = latest_report_directory(output_dir)
    if report_dir is None:
        print("No saved reports to verify.")
        return 0
    partial_path = report_dir / f"{PARTIAL_REPORT}.json"
    if not partial_path.is_file():
        print("No saved partial matches to verify.")
        return 0
    partial_rows = report_rows(
        partial_path.read_text(encoding="utf-8-sig"), partial_path
    )
    pending = [
        (index, row)
        for index, row in enumerate(partial_rows)
        if isinstance(row, dict) and row.get("file_check", {}).get("candidates")
    ]
    if not pending:
        print("No saved partial matches with eligible file checks.")
        return 0
    exact_path = report_dir / f"{EXACT_REPORT}.json"
    checked = promoted = 0
    removed = set()
    changed = False
    verifier = PartialFileVerifier(ops)
    with open_report(exact_path) as exact_file:
        contents = exact_file.read().decode("utf-8-sig")
        exact_hashes = (
            {
                row.get("torrent_hash")
                for row in report_rows(contents, exact_path)
                if isinstance(row, dict)
            }
            if contents
            else set()
        )
        exact_file.seek(0)
        exact_report = JsonReport(exact_file)
        try:
            for index, row in pending:
                if row["torrent_hash"] in exact_hashes:
                    removed.add(index)
                    continue
                if ops.blocked or (limit is not None and checked >= limit):
                    break
                check = row["file_check"]
                record = {
                    **row,
                    "file_check": {**check, "candidates": check["candidates"].copy()},
                }
                checked += 1
                if (exact := verifier.verify(record)) is None:
                    partial_rows[index] = record
                    changed = True
                    continue
                exact_report.append(exact)
                exact_hashes.add(row["torrent_hash"])
                removed.add(index)
                promoted += 1
                show_missing_ops_formats(exact)
        finally:
            if changed or removed:
                temporary = partial_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        [
                            row
                            for index, row in enumerate(partial_rows)
                            if index not in removed
                        ],
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                temporary.replace(partial_path)
    print(
        f"Saved partials checked {checked}, promoted {promoted}, verification errors {verifier.failures}."
    )
    return 1 if verifier.failures else 0


def scan(
    qbit,
    red,
    ops,
    output_dir,
    limit,
    rescan,
    new_reports=False,
    verify_partial_files=False,
    download_ops_torrents=None,
):
    print("Reading torrent list from qBittorrent...", flush=True)
    torrents = qbit.get("info", list)
    total_torrents = len(torrents)
    processed = set() if rescan else processed_hashes(output_dir)
    report_dir = select_report_directory(output_dir, new_reports)
    print(
        f"Scanning {total_torrents} qBittorrent torrents. Reports in {report_dir.resolve()}",
        flush=True,
    )

    # Preselect by name or size to avoid fetching file lists for the whole client.
    by_name, by_size = defaultdict(list), defaultdict(list)
    for torrent in torrents:
        if name_key := title_key(torrent.get("name")):
            by_name[name_key].append(torrent)
        if size := positive_int(torrent.get("total_size")):
            by_size[size].append(torrent)

    @lru_cache(maxsize=128)
    def tracker_hosts(torrent_hash):
        return {
            hostname(tracker.get("url") or "")
            for tracker in qbit.get("trackers", list, hash=torrent_hash)
        }

    @lru_cache(maxsize=128)
    def audio_layout(torrent_hash):
        return qbit_audio_layout(qbit.get("files", list, hash=torrent_hash))

    def local_ops_matches(source):
        source_hash = source["hash"]
        by_same_name = by_name.get(title_key(source.get("name")), [])
        by_same_size = by_size.get(positive_int(source.get("total_size")), [])
        candidates = {
            candidate["hash"]: candidate
            for candidate in (*by_same_name, *by_same_size)
            if candidate.get("hash")
            and candidate["hash"].lower() != source_hash.lower()
        }
        layout = None
        matches = []
        for candidate_hash, candidate in candidates.items():
            try:
                if hostname(
                    candidate.get("tracker") or ""
                ) not in OPS_TRACKERS and OPS_TRACKERS.isdisjoint(
                    tracker_hosts(candidate_hash)
                ):
                    continue
                if layout is None:
                    layout = audio_layout(source_hash)
                if not layout:
                    break
                if layout != audio_layout(candidate_hash):
                    continue
            except ScanError as exc:
                print(
                    f"  Could not compare local OPS candidate | {exc}", file=sys.stderr
                )
                continue
            match = {
                "torrent_hash": candidate_hash.lower(),
                "torrent_name": candidate.get("name", ""),
                "data_location": candidate.get("content_path", ""),
            }
            try:
                properties = qbit.get("properties", dict, hash=candidate_hash)
                links = set(comment_links(properties.get("comment"), OPS_SITES))
                if len(links) == 1:
                    match["ops_url"] = site_link(OPS, *links.pop())
            except ScanError:
                # The tracker and file layout already confirm the local match.
                pass
            matches.append(match)
        return matches

    @lru_cache(maxsize=512)
    def red_details(key, value):
        return red.get("torrent", **{key: value})

    cache_dir = output_dir / ".ops-cache"
    cache_scope = f"{ops.url}\0{ops.authorization}"

    @lru_cache(maxsize=32)
    def artist_releases(artist):
        def fetch():
            data = ops.get("artist", artistname=artist, artistreleases=1)
            return result_rows(data, "torrentgroup", "OPS artist")

        return cached_ops_result(
            cache_dir, (cache_scope, "artist", artist), fetch, rescan
        )

    @lru_cache(maxsize=32)
    def title_releases(title):
        return cached_ops_result(
            cache_dir,
            (cache_scope, "title", title),
            lambda: list(ops_title_groups(ops, title)),
            rescan,
        )

    @lru_cache(maxsize=128)
    def group_details(group_id):
        def fetch():
            data = ops.get("torrentgroup", id=group_id)
            if not isinstance(data.get("group"), dict):
                raise ScanError("OPS torrentgroup returned no group metadata")
            result_rows(data, "torrents", "OPS torrentgroup")
            return data

        return cached_ops_result(
            cache_dir, (cache_scope, "group", group_id), fetch, rescan
        )

    counts = dict.fromkeys(REPORT_NAMES, 0)
    verifier = PartialFileVerifier(ops) if verify_partial_files else None
    downloader = (
        OpsTorrentDownloader(ops, download_ops_torrents)
        if download_ops_torrents is not None
        else None
    )
    checked_red = skipped = 0
    with ExitStack() as stack:
        reports = {
            name: JsonReport(
                stack.enter_context(open_report(report_dir / f"{name}.json"))
            )
            for name in REPORT_NAMES
        }
        for index, torrent in enumerate(torrents, 1):
            if limit is not None and checked_red >= limit:
                print(f"Reached the limit of {limit} new RED torrents.")
                break
            if index % 100 == 0:
                print(
                    f"Inspecting qBittorrent torrent {index}/{total_torrents}.",
                    flush=True,
                )
            name = torrent.get("name", "")
            torrent_hash = torrent.get("hash", "")
            hash_key = torrent_hash.lower()
            if hash_key in processed:
                skipped += 1
                continue
            content_path = torrent.get("content_path", "")
            row = {
                "red_url": "",
                "release_name": name,
                "torrent_name": name,
                "data_location": content_path,
                "torrent_hash": hash_key,
            }
            is_red = hostname(torrent.get("tracker") or "") in RED_TRACKERS
            try:
                properties = qbit.get("properties", dict, hash=torrent_hash)
                links = comment_links(properties.get("comment"), RED_SITES)
                is_red = is_red or bool(links)
                if not is_red:
                    is_red = not RED_TRACKERS.isdisjoint(tracker_hosts(torrent_hash))
                if not is_red:
                    continue
                check_number = (
                    f"{checked_red + 1}/{limit}"
                    if limit is not None
                    else str(checked_red + 1)
                )
                print(
                    f"[qBittorrent {index}/{total_torrents}] Checking RED release {check_number} | {name or torrent_hash}",
                    flush=True,
                )
                ids = {torrent_id for _, torrent_id in links if torrent_id}
                if len(links) == 1:
                    row["red_url"] = site_link(RED, *links[0])
                if not content_path:
                    raise ScanError("qBittorrent did not report a content path")
                if local_matches := local_ops_matches(torrent):
                    row.update(
                        {
                            "match_basis": "qbit_client",
                            "ops_torrents": local_matches,
                            "ops_urls": list(
                                dict.fromkeys(
                                    match["ops_url"]
                                    for match in local_matches
                                    if "ops_url" in match
                                )
                            ),
                            "ops_formats": [],
                            "same_format_and_encoding": None,
                        }
                    )
                    reports[EXACT_REPORT].append(row)
                    counts[EXACT_REPORT] += 1
                    processed.add(hash_key)
                    print(
                        f"  Found {len(local_matches)} local OPS match(es)", flush=True
                    )
                    continue
                if len(ids) == 1:
                    data = red_details("id", next(iter(ids)))
                else:
                    data = red_details("hash", torrent_hash)
                group, source = data.get("group"), data.get("torrent")
                if not isinstance(group, dict) or not isinstance(source, dict):
                    raise ScanError("RED did not provide release metadata")
                group_id = positive_int(group.get("id"))
                source_id = positive_int(source.get("id"))
                if (
                    not group_id
                    or not source_id
                    or (len(ids) == 1 and source_id not in ids)
                    or (
                        source.get("infoHash")
                        and normalized(source["infoHash"]) != torrent_hash.lower()
                    )
                ):
                    raise ScanError("RED returned unexpected release IDs")
                row["red_url"] = site_link(RED, group_id, source_id)
                edition = Edition.from_api(group, source)
                artists = group_artists(group)
                title = display(group.get("name"))
                various_credit = various_artists(artist_key(artists))
                artist_credit = (
                    "Various Artists" if various_credit else ", ".join(artists)
                )
                release_name = (
                    f"{artist_credit} - {title}" if artists and title else name
                )
                format_pair = (
                    display(source.get("format")),
                    display(source.get("encoding")),
                )
                row.update(
                    {
                        "release_name": release_name,
                        "edition_year": edition.year,
                        "record_label": edition.label,
                        "catalogue_number": edition.catalogue,
                        "edition_title": edition.title,
                        "media": edition.media,
                        "red_format": format_pair[0],
                        "red_encoding": format_pair[1],
                    }
                )
                missing_fields = edition.missing_fields()
                if not artists:
                    missing_fields.append("main artists")
                if not title:
                    missing_fields.append("release title")
                if not format_pair[0]:
                    missing_fields.append("audio format")
                if not format_pair[1]:
                    missing_fields.append("audio encoding")
                if missing_fields:
                    raise ScanError(
                        f"RED release is missing {', '.join(missing_fields)}"
                    )
                edition_name = " / ".join(
                    str(value)
                    for value in (
                        edition.year,
                        edition.label,
                        edition.catalogue,
                        edition.title,
                        edition.media,
                    )
                    if value
                )
                print(
                    f"  Searching OPS for {release_name} ({edition_name})", flush=True
                )
                groups = (
                    title_releases(title)
                    if various_credit
                    else ops_artist_groups(artist_releases, artists)
                )
                matches = ops_matches(
                    groups, group_details, title, artists, edition, format_pair
                )
                name = write_search_result(
                    reports,
                    row,
                    matches,
                    format_pair,
                    source=source,
                    verify=verifier.verify if verifier else None,
                    download=downloader.save if downloader else None,
                )
                counts[name] += 1
                if hash_key:
                    processed.add(hash_key)
                if (verifier or downloader) and ops.blocked:
                    print(
                        f"Scan stopped with {total_torrents - index} qBittorrent torrents left unscanned.",
                        file=sys.stderr,
                    )
                    break
            except ScanError as exc:
                counts[UNCHECKED_REPORT] += 1
                if is_red:
                    row["reason"] = str(exc)
                    reports[UNCHECKED_REPORT].append(row)
                    if hash_key:
                        processed.add(hash_key)
                print(
                    f"Unchecked [{index}/{total_torrents}] {row['red_url'] or torrent_hash} | {name}: {exc}",
                    file=sys.stderr,
                )
                if red.blocked or ops.blocked:
                    print(
                        f"Scan stopped with {total_torrents - index} qBittorrent torrents left unscanned.",
                        file=sys.stderr,
                    )
                    break
            finally:
                checked_red += int(is_red)
    print(
        f"OPS exact matches {counts[EXACT_REPORT]}, partial matches {counts[PARTIAL_REPORT]}, "
        f"not found {counts[MISSING_REPORT]}, unchecked {counts[UNCHECKED_REPORT]}, already processed {skipped}."
    )
    failed = (
        counts[UNCHECKED_REPORT]
        or (verifier and verifier.failures)
        or (downloader and downloader.failures)
    )
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report which RED torrent editions already exist on OPS."
    )
    parser.add_argument(
        "--qbit-url",
        help="qBittorrent WebUI URL (or QBIT_URL)",
    )
    parser.add_argument(
        "--qbit-username", help="qBittorrent WebUI username (or QBIT_USERNAME)"
    )
    parser.add_argument(
        "--qbit-password", help="qBittorrent WebUI password (or QBIT_PASSWORD)"
    )
    parser.add_argument(
        "--qbit-api-key", help="qBittorrent WebUI API key (or QBIT_API_KEY)"
    )
    parser.add_argument(
        "--red-api-key",
        default=os.getenv("RED_API_KEY", ""),
        help="RED API key (or RED_API_KEY)",
    )
    parser.add_argument(
        "--ops-api-key",
        default=os.getenv("OPS_API_KEY", ""),
        help="OPS API key (or OPS_API_KEY)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports"),
        help="report parent directory (default: reports)",
    )
    parser.add_argument(
        "--api-interval",
        type=float,
        default=MIN_INTERVAL,
        help="minimum seconds between requests to each tracker (default: 4)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="maximum number of new RED torrents to check in this run",
    )
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="ignore previous reports and refresh OPS lookups",
    )
    parser.add_argument(
        "--new-reports",
        action="store_true",
        help="start a new set of reports for this and subsequent runs",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print RED API response bodies to the terminal",
    )
    parser.add_argument(
        "--verify-partial-files",
        action="store_true",
        help="verify eligible partials inline with --qbit-url, or verify saved partials on its own",
    )
    parser.add_argument(
        "--download-ops-torrents",
        metavar="DIRECTORY",
        type=Path,
        help="save exact OPS matches missing from qBittorrent to this directory",
    )
    args = parser.parse_args(argv)
    verify_saved = args.verify_partial_files and args.qbit_url is None
    if verify_saved and (args.rescan or args.new_reports):
        parser.error(
            "pass --qbit-url to use --rescan or --new-reports with --verify-partial-files"
        )
    if verify_saved and args.download_ops_torrents is not None:
        parser.error(
            "pass --qbit-url to verify and download OPS matches in the same scan"
        )
    qbit_url = args.qbit_url or os.getenv("QBIT_URL")
    if not verify_saved and not qbit_url:
        parser.error("set QBIT_URL or pass --qbit-url")
    if not MIN_INTERVAL <= args.api_interval < float("inf"):
        parser.error(
            f"--api-interval must be a finite number of at least {MIN_INTERVAL:g} seconds"
        )
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    red_key, ops_key = args.red_api_key.strip(), args.ops_api_key.strip()
    if not ops_key or (not verify_saved and not red_key):
        if verify_saved:
            parser.error("pass --ops-api-key or set OPS_API_KEY")
        parser.error(
            "pass --red-api-key and --ops-api-key or set RED_API_KEY and OPS_API_KEY"
        )
    user, password, qbit_key = args.qbit_username, args.qbit_password, args.qbit_api_key
    if user is None and password is None and qbit_key is None:
        qbit_key = os.getenv("QBIT_API_KEY", "")
    if user is None:
        user = os.getenv("QBIT_USERNAME", "")
    if password is None:
        password = os.getenv("QBIT_PASSWORD", "")
    try:
        if verify_saved:
            with requests.Session() as ops_http, lock_reports(args.output_dir):
                ops = Gazelle(
                    ops_http, "OPS", OPS, f"token {ops_key}", args.api_interval
                )
                return verify_saved_partial_files(ops, args.output_dir, args.limit)
        with (
            requests.Session() as qbit_http,
            requests.Session() as red_http,
            requests.Session() as ops_http,
        ):
            qbit = QBit(
                qbit_http,
                qbit_url,
                user,
                password,
                qbit_key,
            )
            red = Gazelle(
                red_http, "RED", RED, red_key, args.api_interval, debug=args.debug
            )
            ops = Gazelle(ops_http, "OPS", OPS, f"token {ops_key}", args.api_interval)
            with lock_reports(args.output_dir):
                return scan(
                    qbit,
                    red,
                    ops,
                    args.output_dir,
                    args.limit,
                    args.rescan,
                    args.new_reports,
                    args.verify_partial_files,
                    args.download_ops_torrents,
                )
    except (ScanError, OSError, ValueError) as exc:
        print(f"Scan failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Scan interrupted. The JSON reports contain results saved so far.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    # Redirected Windows output may use a code page that cannot encode a release name.
    sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
